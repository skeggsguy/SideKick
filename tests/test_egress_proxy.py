"""harness.egress_proxy — the SSRF-filtering forward proxy the agent Chrome
is locked to (the chrome-proxy service).

Moved out of test_js_fetch.py when the proxy was promoted from the js_fetch
container into harness source. Two layers, both offline:
  1. The filter predicate (target_addresses) with injected resolvers.
  2. Real socket round-trips against a proxy on an ephemeral port (refusals +
     the CONNECT tunnel / absolute-form forward paths with an injected
     connector).
Plus module-contract pins (BK-037: stdlib-only import chain; the listen-port
default), and the BK-037 relay-family refusals on both request paths.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from harness import egress_proxy as ep


async def _pub(host: str) -> list[str]:
    return ["93.184.216.34"]


async def _priv(host: str) -> list[str]:
    return ["10.1.2.3"]


async def _tailnet(host: str) -> list[str]:
    return ["100.64.0.9"]


async def _mixed(host: str) -> list[str]:
    return ["93.184.216.34", "192.168.1.5"]


async def _unresolvable(host: str) -> list[str]:
    raise OSError("no dns")


# --- filter predicate ----------------------------------------------------------


async def test_proxy_filter_allows_public_only():
    assert await ep.target_addresses("example.com", 443, _pub) == ["93.184.216.34"]
    # IP literals validated directly, no resolver call.
    assert await ep.target_addresses("93.184.216.34", 80, _unresolvable) == [
        "93.184.216.34"
    ]

    for resolver, host in (
        (_priv, "internal.example"),
        (_tailnet, "tailnet.example"),
        (_mixed, "rebinder.example"),  # one private A record poisons the set
        (_unresolvable, "nx.example"),
    ):
        with pytest.raises(ep.Refused):
            await ep.target_addresses(host, 443, resolver)
    for literal in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "100.64.0.9", "169.254.1.1"):
        with pytest.raises(ep.Refused):
            await ep.target_addresses(literal, 443, _pub)
    # Non-http(s) ports are refused even for public hosts.
    with pytest.raises(ep.Refused):
        await ep.target_addresses("example.com", 25, _pub)


def test_proxy_rewrites_absolute_form_to_origin_form():
    head = (
        b"GET http://example.com/a?b=1 HTTP/1.1\r\n"
        b"Host: example.com\r\n"
        b"Proxy-Connection: keep-alive\r\n"
        b"Accept: */*\r\n\r\n"
    )
    new_head, host, port = ep.rewrite_absolute_form(head)
    assert host == "example.com" and port == 80
    assert new_head.startswith(b"GET /a?b=1 HTTP/1.1\r\n")
    assert b"Proxy-Connection" not in new_head
    assert b"Connection: close" in new_head
    assert b"Accept: */*" in new_head
    # https can't appear in absolute form (it rides CONNECT) — refused.
    with pytest.raises(ep.Refused):
        ep.rewrite_absolute_form(b"GET https://example.com/ HTTP/1.1\r\n\r\n")


# --- real socket round-trips on an ephemeral port ------------------------------


async def _start_proxy(**kwargs):
    server = await ep.start(port=0, **kwargs)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _request(port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return data


async def test_proxy_refuses_connect_to_private_targets():
    server, port = await _start_proxy(resolver=_pub)
    try:
        for target in (b"10.0.0.1:443", b"127.0.0.1:8443", b"100.64.0.9:443"):
            resp = await _request(port, b"CONNECT " + target + b" HTTP/1.1\r\n\r\n")
            assert resp.startswith(b"HTTP/1.1 403"), resp
            assert b"egress refused" in resp
    finally:
        server.close()
        await server.wait_closed()


async def test_proxy_refuses_connect_when_hostname_resolves_private():
    server, port = await _start_proxy(resolver=_priv)
    try:
        resp = await _request(port, b"CONNECT internal.example:443 HTTP/1.1\r\n\r\n")
        assert resp.startswith(b"HTTP/1.1 403"), resp
    finally:
        server.close()
        await server.wait_closed()


async def test_proxy_refuses_absolute_form_get_to_private():
    server, port = await _start_proxy(resolver=_pub)
    try:
        resp = await _request(
            port, b"GET http://192.168.1.1/admin HTTP/1.1\r\nHost: x\r\n\r\n"
        )
        assert resp.startswith(b"HTTP/1.1 403"), resp
    finally:
        server.close()
        await server.wait_closed()


async def test_proxy_connect_tunnels_to_the_validated_ip():
    """The success path: CONNECT hands the VALIDATED addresses (not the
    hostname) to the connector, replies 200, and pumps bytes both ways."""

    async def echo_once(reader, writer):
        data = await reader.read(100)
        writer.write(b"echo:" + data)
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo_once, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    handed: dict = {}

    async def connector(addresses, port):
        handed["addresses"], handed["port"] = addresses, port
        return await asyncio.open_connection("127.0.0.1", upstream_port)

    server, port = await _start_proxy(resolver=_pub, connector=connector)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        status = await reader.readline()
        assert b"200" in status
        assert await reader.readline() == b"\r\n"
        writer.write(b"hello")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 5) == b"echo:hello"
        writer.close()
        # The connector received the resolver's validated IP, never the name.
        assert handed == {"addresses": ["93.184.216.34"], "port": 443}
    finally:
        server.close()
        upstream.close()
        await server.wait_closed()
        await upstream.wait_closed()


async def test_proxy_forwards_absolute_form_http_rewritten():
    received: dict = {}

    async def upstream_handler(reader, writer):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await reader.read(4096)
            if not chunk:
                break
            head += chunk
        received["head"] = head
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nhi")
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]

    async def connector(addresses, port):
        return await asyncio.open_connection("127.0.0.1", upstream_port)

    server, port = await _start_proxy(resolver=_pub, connector=connector)
    try:
        resp = await _request(
            port,
            b"GET http://example.com/a?b=1 HTTP/1.1\r\n"
            b"Host: example.com\r\nProxy-Connection: keep-alive\r\n\r\n",
        )
        assert resp.startswith(b"HTTP/1.1 200 OK") and resp.endswith(b"hi")
        assert received["head"].startswith(b"GET /a?b=1 HTTP/1.1\r\n")
        assert b"Proxy-Connection" not in received["head"]
        assert b"Connection: close" in received["head"]
    finally:
        server.close()
        upstream.close()
        await server.wait_closed()
        await upstream.wait_closed()


# --- console entrypoint ---------------------------------------------------------


def test_main_parses_port(monkeypatch):
    captured = {}

    async def fake_serve(host, port):
        captured["host"], captured["port"] = host, port

    monkeypatch.setattr(ep, "_serve", fake_serve)
    monkeypatch.setattr(
        sys, "argv", ["egress-proxy", "--host", "127.0.0.1", "--port", "12345"]
    )
    ep.main()
    assert captured == {"host": "127.0.0.1", "port": 12345}


def test_listen_port_default():
    """The Chrome launch spec (docs/browser-fallback-setup.md) points Chrome at
    127.0.0.1:18080 — the default must match or the service config and the
    code drift apart."""
    assert ep.LISTEN_PORT == 18080


# --- BK-037: the confabulated relay family — the proxy is the browser's gate -----

_RELAY_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "egress" / "relay_urls_oct2026.json").read_text()
)

# Hard copy of the board's criterion-2 directive (the convention set by
# tests/test_relay_block.py:48): importing RELAY_DIRECTIVE would make the
# 403-body check tautological — if the constant drifts, these pins go RED.
_RELAY_DIRECTIVE = (
    "fabricated relay URL (known model artifact): re-derive the real URL "
    "from the previous web_search result; do not retry variants"
)

# The proxy validates (host, port), not URLs — the corpus supplies the relay
# hosts verbatim (both attested spellings, surface map §6). Selection counts
# pinned at collection: an empty fixture slice silently SKIPS and an empty
# loop asserts nothing (the step-3 lesson).
_RELAY_URLS = [e["url"] for e in _RELAY_CORPUS["blocked"]]
_RELAY_HOSTS = sorted({urlsplit(u).hostname for u in _RELAY_URLS})
_RELAY_ALLOWED = [e["url"] for e in _RELAY_CORPUS["allowed"]]
assert (len(_RELAY_URLS), len(_RELAY_HOSTS), len(_RELAY_ALLOWED)) == (12, 2, 5), (
    f"expected the frozen Oct corpus — 12 blocked shapes / 2 host spellings / "
    f"5 near-misses, got {len(_RELAY_URLS)}/{len(_RELAY_HOSTS)}/{len(_RELAY_ALLOWED)}; "
    "an empty selection would make the BK-037 proxy pins vacuous"
)


def _tripwire_connector():
    async def connector(addresses, port):
        raise AssertionError(
            f"GOTCHA REGRESSION [bk-037-proxy] connect opened to {addresses}:{port} "
            "— the relay block belongs in target_addresses BEFORE the handler "
            "calls the connector (a dialed relay host leaks its trace id)"
        )

    return connector


def _order_pin_resolver():
    """Records every lookup; a relay refusal must record NONE — the proxy's
    resolve is the first thing that puts bytes on the wire (D2's reasoning)."""
    lookups: list[str] = []

    async def resolver(host: str) -> list[str]:
        lookups.append(host)
        return ["93.184.216.34"]

    return resolver, lookups


def _tripwire_resolver():
    async def resolver(host: str) -> list[str]:
        raise AssertionError(
            f"GOTCHA REGRESSION [bk-037-order] resolver awaited for {host!r} — "
            "the proxy's relay block must fire BEFORE any DNS lookup"
        )

    return resolver


@pytest.mark.parametrize(
    "url", _RELAY_URLS, ids=[f"blocked{i}" for i in range(len(_RELAY_URLS))]
)
async def test_target_addresses_refuses_relay_hosts_before_dns(url):
    # Predicate layer: every corpus host, refused with the directive as the
    # message (that string is what the 403 body writer prefixes with
    # "egress refused: "), tripwire resolver never awaited.
    with pytest.raises(ep.Refused) as ei:
        await ep.target_addresses(urlsplit(url).hostname, 443, _tripwire_resolver())
    assert str(ei.value) == _RELAY_DIRECTIVE


_RAW_SHAPES = [
    (variant, f"spelling{i}-{tag}")
    for i, host in enumerate(_RELAY_HOSTS)
    for tag, variant in (
        ("plain", host),
        ("upper", host.upper()),
        ("dot", host + "."),
        ("upper-dot", host.upper() + "."),
    )
]


@pytest.mark.parametrize(
    "host", [v for v, _ in _RAW_SHAPES], ids=[i for _, i in _RAW_SHAPES]
)
async def test_predicate_refuses_raw_authority_shapes(host):
    # The CONNECT path hands target_addresses the RAW authority (_parse_connect_target
    # normalizes nothing); urlsplit pre-lowercases but keeps the trailing dot.
    resolver, lookups = _order_pin_resolver()
    with pytest.raises(ep.Refused) as ei:
        await ep.target_addresses(host, 443, resolver)
    assert str(ei.value) == _RELAY_DIRECTIVE
    assert lookups == []


@pytest.mark.parametrize(
    "url", _RELAY_ALLOWED, ids=[f"allowed{i}" for i in range(len(_RELAY_ALLOWED))]
)
async def test_near_miss_hosts_still_pass_the_proxy_filter(url):
    # The guard is not a general outage: every D5 near-miss host passes the
    # filter and resolves like any other target.
    assert await ep.target_addresses(urlsplit(url).hostname, 443, _pub) == [
        "93.184.216.34"
    ]


@pytest.mark.parametrize(
    "url", _RELAY_URLS, ids=[f"connect{i}" for i in range(len(_RELAY_URLS))]
)
async def test_proxy_refuses_relay_connect_with_directive_and_opens_no_connect(url):
    # CONNECT path over a real socket: 403 with the directive IN THE BODY
    # (substring — the writer prefixes "egress refused: ", surface map §7),
    # tripwire connector never called, resolver never consulted.
    resolver, lookups = _order_pin_resolver()
    server, port = await _start_proxy(resolver=resolver, connector=_tripwire_connector())
    try:
        target = f"{urlsplit(url).hostname}:443".encode()
        resp = await _request(port, b"CONNECT " + target + b" HTTP/1.1\r\n\r\n")
        assert resp.startswith(b"HTTP/1.1 403"), resp
        assert b"egress refused: " in resp
        assert _RELAY_DIRECTIVE.encode() in resp
        assert lookups == []
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "url", _RELAY_URLS, ids=[f"absform{i}" for i in range(len(_RELAY_URLS))]
)
async def test_proxy_refuses_relay_absolute_form_with_directive_and_opens_no_connect(url):
    # The plain-HTTP path: absolute-form requests arrive as full URLs (the
    # corpus URLs are https; the scheme swap keeps the host — the predicate's
    # only input — identical).
    http_target = urlsplit(url)._replace(scheme="http").geturl().encode()
    resolver, lookups = _order_pin_resolver()
    server, port = await _start_proxy(resolver=resolver, connector=_tripwire_connector())
    try:
        resp = await _request(
            port, b"GET " + http_target + b" HTTP/1.1\r\nHost: x\r\n\r\n"
        )
        assert resp.startswith(b"HTTP/1.1 403"), resp
        assert _RELAY_DIRECTIVE.encode() in resp
        assert lookups == []
    finally:
        server.close()
        await server.wait_closed()


async def test_proxy_refuses_raw_authority_variants_on_both_paths():
    # Step-4's named cases: `host:443` and `HOST.:443` must refuse on CONNECT
    # (raw authority, port stripped by _parse_connect_target) and the dotted/
    # cased form must refuse on absolute-form (urlsplit lowercases, keeps the
    # trailing dot; port stripped by rewrite_absolute_form).
    host = _RELAY_HOSTS[0]
    resolver, lookups = _order_pin_resolver()
    server, port = await _start_proxy(resolver=resolver, connector=_tripwire_connector())
    variants = [
        f"CONNECT {host}:443",
        f"CONNECT {host.upper()}:443",
        f"CONNECT {host}.:443",
        f"CONNECT {host.upper()}.:8443",
    ]
    try:
        for line in variants:
            resp = await _request(port, (line + " HTTP/1.1\r\n\r\n").encode())
            assert resp.startswith(b"HTTP/1.1 403"), (line, resp[:60])
            assert _RELAY_DIRECTIVE.encode() in resp, line
        for line in (
            f"GET http://{host.upper()}.:443/x",
            f"GET http://{host.upper()}/x",
        ):
            resp = await _request(port, (line + " HTTP/1.1\r\nHost: x\r\n\r\n").encode())
            assert resp.startswith(b"HTTP/1.1 403"), (line, resp[:60])
            assert _RELAY_DIRECTIVE.encode() in resp, line
        assert lookups == []  # every variant died before DNS
    finally:
        server.close()
        await server.wait_closed()


async def test_relay_guard_is_not_a_general_outage():
    """Same proxy instance: a clean CONNECT still tunnels (200 + echo bytes)
    while a relay CONNECT gets the directive 403 — and the connector spy shows
    exactly ONE connect opened, the clean one. Zero connects on refusals is
    measured, not inferred from the 403."""

    async def echo_once(reader, writer):
        data = await reader.read(100)
        writer.write(b"echo:" + data)
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo_once, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    connects: list[tuple[str, int]] = []

    async def counting_connector(addresses, port):
        connects.append((addresses[0], port))
        return await asyncio.open_connection("127.0.0.1", upstream_port)

    server, port = await _start_proxy(resolver=_pub, connector=counting_connector)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert b"200" in await reader.readline()
        await reader.readline()
        writer.write(b"hi")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 5) == b"echo:hi"
        writer.close()

        relay_line = f"CONNECT {_RELAY_HOSTS[0]}:443 HTTP/1.1\r\n\r\n".encode()
        # Bounded read + guaranteed close (NOT the _request helper's
        # read-to-EOF): if the guard ever regresses here the proxy replies 200
        # and TUNNELS — an EOF-read would hang the test, and a guard test must
        # fail, never hang the suite.
        reader2, writer2 = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer2.write(relay_line)
            await writer2.drain()
            try:
                resp = await asyncio.wait_for(reader2.read(4096), 5)
            except asyncio.TimeoutError:
                resp = b"<no response within 5s>"
        finally:
            writer2.close()
        assert resp.startswith(b"HTTP/1.1 403"), resp
        assert _RELAY_DIRECTIVE.encode() in resp
        assert connects == [("93.184.216.34", 443)]  # the clean one; refusals: 0
    finally:
        server.close()
        upstream.close()
        await server.wait_closed()
        await upstream.wait_closed()


def test_import_chain_stays_stdlib_only():
    """Module contract (docstring): stdlib asyncio only, importable with no
    browser installed. D3 added a cross-module import (the shared predicate) —
    this pins that the chain added NOTHING non-stdlib. Subprocess because the
    pytest process already has httpx/websockets loaded; baseline-diffed so venv
    bootstrap artifacts (_virtualenv, _distutils_hack, sitecustomize) don't
    need an allowlist."""
    code = (
        "import sys, json\n"
        "base = set(sys.modules)\n"
        "import harness.egress_proxy\n"
        "added = {m.split('.')[0] for m in set(sys.modules) - base}\n"
        "print(json.dumps(sorted(added - set(sys.stdlib_module_names) - {'harness'})))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip()) == [], (
        "egress_proxy's import chain grew a dependency — the chrome-proxy service "
        "must stay stdlib-only (module docstring); share egress rules via "
        "_netguard (stdlib), never by pulling a package into the proxy"
    )
