"""SSRF-filtering forward proxy for the agent Chrome (browser_fetch, Lane A).

This is the LOAD-BEARING egress control for the persistent agent Chrome (see
docs/browser-fallback-setup.md). It runs as the `chrome-proxy` service (via
the `egress-proxy` console script) and Chrome is locked to it
(--proxy-server, with the implicit loopback bypass disabled via `<-loopback>`).
A browser re-resolves hostnames itself — a DNS-rebinding TOCTOU an in-page or
tool-side check cannot close — so egress is enforced here, in userspace, at
connect time: the proxy resolves each target itself, refuses anything that is
not globally routable, and then connects to the exact IP it validated — never
re-resolving. Since BK-037 it also refuses, before any resolve or connect,
hosts in the confabulated routify/OSS relay family (D3): Chrome follows
redirects internally, so this is the per-hop gate for the browser half.

Stdlib asyncio only — no dependencies, importable (and unit-testable) with no
browser installed. No top-level side effects; main() is the console script.

Supports the two things a browser needs from an HTTP/1.1 forward proxy:
- CONNECT host:port  → validate, open a raw tunnel to the validated IP.
- absolute-form requests (GET http://host/path ...) → validate, rewrite to
  origin-form, forward with Connection: close, pump bytes both ways.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Awaitable, Callable
from urllib.parse import urlsplit

# One rule, two call sites (BK-037 D3): the confabulated-relay family check is
# shared with tools/_netguard.check_target. _netguard is stdlib-only and both
# package __init__ files are empty, so this import keeps the no-dependency
# contract above intact — pinned by test_import_chain_stays_stdlib_only.
from harness.tools._netguard import RELAY_DIRECTIVE, is_confabulated_host

LISTEN_HOST = "127.0.0.1"
# 18080, not 8080: avoids the classic dev-server collision on the host. This is
# the LISTEN port; target ports are validated against ALLOWED_PORTS below.
LISTEN_PORT = 18080
# http(s) on standard + common alternates; anything else is refused.
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})

_HEAD_LIMIT = 64 * 1024
_HEAD_TIMEOUT = 30.0
_CONNECT_TIMEOUT = 15.0
_PUMP_CHUNK = 65_536

Resolver = Callable[[str], Awaitable[list[str]]]
Connector = Callable[[list[str], int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class Refused(Exception):
    """Target refused by the egress policy; message goes into the 403 body."""


async def default_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, None, type=socket.SOCK_STREAM
    )
    return [info[4][0] for info in infos]


async def target_addresses(host: str, port: int, resolver: Resolver) -> list[str]:
    """Validate (host, port) against the egress policy and return the addresses
    the caller must connect to. Refuses non-http(s) ports, hosts in the
    confabulated relay family (BK-037), unresolvable hosts, and any resolution
    containing a non-global address — one private A record poisons the whole
    set (rebinding servers interleave answers)."""
    if port not in ALLOWED_PORTS:
        raise Refused(f"port {port} is not an allowed http(s) port")
    if is_confabulated_host(host):
        # The browser's per-hop relay gate (D3): Chrome follows redirects and
        # re-resolves on its own, so a relay URL reached as a redirect hop is
        # gated ONLY here. Fires before the resolve (a DNS query is already
        # egress) and before the handler's connect; the message is the
        # criterion-2 directive, which the 403 writer prefixes with "egress
        # refused: ". The CONNECT path hands this a RAW authority (case and
        # trailing dot kept) — the predicate normalizes, not the call site.
        raise Refused(RELAY_DIRECTIVE)
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = await resolver(host)
        except OSError:
            raise Refused(f"could not resolve {host!r}") from None
    if not addresses:
        raise Refused(f"{host!r} resolved to no addresses")
    for addr in addresses:
        try:
            if not ipaddress.ip_address(addr).is_global:
                raise Refused(
                    f"{host!r} resolves to a private or internal address"
                )
        except ValueError:
            raise Refused(f"{host!r} resolved to an unparsable address") from None
    return addresses


async def default_connector(
    addresses: list[str], port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to the first reachable VALIDATED address (IP literal — no DNS)."""
    last: OSError | None = None
    for addr in addresses:
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(addr, port), _CONNECT_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError) as exc:
            last = exc if isinstance(exc, OSError) else OSError("connect timed out")
    raise last or OSError("no addresses to connect to")


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    """Read up to and including the blank line ending the request head."""
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = await reader.read(4096)
        if not chunk:
            break
        head += chunk
        if len(head) > _HEAD_LIMIT:
            raise Refused("request head too large")
    return head


def _parse_request_line(head: bytes) -> tuple[str, str, str]:
    """(method, target, version) from the first line; Refused on garbage."""
    line = head.split(b"\r\n", 1)[0]
    parts = line.split()
    if len(parts) != 3:
        raise Refused("malformed request line")
    try:
        method, target, version = (p.decode("ascii") for p in parts)
    except UnicodeDecodeError:
        raise Refused("malformed request line") from None
    return method, target, version


def _parse_connect_target(target: str) -> tuple[str, int]:
    """host, port from a CONNECT authority-form target ('example.com:443')."""
    host, sep, port = target.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise Refused(f"malformed CONNECT target {target!r}")
    if host.startswith("[") and host.endswith("]"):  # bracketed IPv6 literal
        host = host[1:-1]
    return host, int(port)


def rewrite_absolute_form(head: bytes) -> tuple[bytes, str, int]:
    """Rewrite an absolute-form http request head to origin-form for the
    upstream server. Returns (new_head, host, port). Forces Connection: close
    (one request per proxied connection — Chromium copes fine) and drops
    proxy-specific headers."""
    method, target, version = _parse_request_line(head)
    parts = urlsplit(target)
    if parts.scheme != "http" or not parts.hostname:
        raise Refused(f"only absolute-form http:// requests are proxied, got {target!r}")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    request_line = f"{method} {path} {version}".encode("ascii")

    headers: list[bytes] = []
    rest = head.partition(b"\r\n")[2]
    header_blob, sep, body = rest.partition(b"\r\n\r\n")
    for raw in header_blob.split(b"\r\n"):
        name = raw.split(b":", 1)[0].strip().lower()
        if name in (b"proxy-connection", b"connection", b"keep-alive"):
            continue
        headers.append(raw)
    headers.append(b"Connection: close")
    new_head = request_line + b"\r\n" + b"\r\n".join(headers) + b"\r\n\r\n" + body
    return new_head, parts.hostname, parts.port or 80


async def _pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(_PUMP_CHUNK)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            if dst.can_write_eof():
                dst.write_eof()
        except OSError:
            pass


async def _tunnel(
    client_r: asyncio.StreamReader,
    client_w: asyncio.StreamWriter,
    upstream_r: asyncio.StreamReader,
    upstream_w: asyncio.StreamWriter,
) -> None:
    """Pump bytes both ways until both directions hit EOF/error."""
    await asyncio.gather(
        _pump(client_r, upstream_w), _pump(upstream_r, client_w)
    )


async def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except OSError:
        pass


def make_handler(
    resolver: Resolver = default_resolver,
    connector: Connector = default_connector,
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
    """Build the per-connection handler; resolver/connector injectable for
    offline tests."""

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        upstream_w: asyncio.StreamWriter | None = None
        try:
            head = await asyncio.wait_for(_read_head(reader), _HEAD_TIMEOUT)
            if not head:
                return
            method, target, _version = _parse_request_line(head)

            if method.upper() == "CONNECT":
                host, port = _parse_connect_target(target)
                addresses = await target_addresses(host, port, resolver)
                upstream_r, upstream_w = await connector(addresses, port)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                # Any bytes the client sent past the head (unusual but legal)
                # belong to the tunnel.
                surplus = head.partition(b"\r\n\r\n")[2]
                if surplus:
                    upstream_w.write(surplus)
                    await upstream_w.drain()
                await _tunnel(reader, writer, upstream_r, upstream_w)
            else:
                new_head, host, port = rewrite_absolute_form(head)
                addresses = await target_addresses(host, port, resolver)
                upstream_r, upstream_w = await connector(addresses, port)
                upstream_w.write(new_head)
                await upstream_w.drain()
                await _tunnel(reader, writer, upstream_r, upstream_w)
        except Refused as exc:
            body = f"egress refused: {exc}".encode()
            try:
                writer.write(
                    b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n"
                    b"Content-Type: text/plain\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
            except OSError:
                pass
        except (OSError, asyncio.TimeoutError):
            pass  # connection-level noise; the browser sees a failed request
        finally:
            if upstream_w is not None:
                await _close(upstream_w)
            await _close(writer)

    return handle


async def start(
    host: str = LISTEN_HOST,
    port: int = LISTEN_PORT,
    resolver: Resolver = default_resolver,
    connector: Connector = default_connector,
) -> asyncio.AbstractServer:
    """Start the proxy; it is accepting connections when this returns."""
    return await asyncio.start_server(make_handler(resolver, connector), host, port)


async def _serve(host: str, port: int) -> None:
    server = await start(host, port)
    async with server:
        await server.serve_forever()


def main() -> None:
    """Console entrypoint (`egress-proxy`) — run by the chrome-proxy unit."""
    import argparse

    parser = argparse.ArgumentParser(
        description="SSRF-filtering egress proxy for the agent Chrome (Lane A)."
    )
    parser.add_argument("--host", default=LISTEN_HOST)
    parser.add_argument("--port", type=int, default=LISTEN_PORT)
    args = parser.parse_args()
    asyncio.run(_serve(args.host, args.port))


if __name__ == "__main__":
    main()
