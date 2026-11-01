"""SSRF network guard for outbound fetches (web_fetch and future fetchers).

The daemon sits on a tailnet with a localhost web UI, so a prompt-injected
fetch of an internal address is a real exfiltration path. Three properties
here are load-bearing and must not be "simplified" away:

- Manual redirect following (the caller loops, re-invoking check_target on
  every hop) — httpx's own follow_redirects would skip the per-hop check.
- Per-hop re-resolve + IP-literal pinning: each hop is resolved and refused if
  any address is private, loopback, link-local, or otherwise non-global, and
  the connection is then made to the *validated* IP literal (hostname preserved
  via Host header + TLS SNI), so a rebinding DNS server can't serve a public
  address to the check and a private one to the connect.
- trust_env=False at the client (caller's concern): a proxy from the
  environment would carry the request past the IP validation.
- The confabulated-relay block (BK-037) runs before the resolver is awaited:
  a DNS query for a fabricated host is already egress. Additive to the three
  properties above; it never relaxes them.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

# Resolver returns the addresses a hostname points at; injectable so offline
# tests never touch DNS.
Resolver = Callable[[str], Awaitable[list[str]]]


async def default_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, None, type=socket.SOCK_STREAM
    )
    return [info[4][0] for info in infos]


class Refused(Exception):
    """Fetch refused before/while hitting the network; message is user-facing."""


class RefusedInternal(Refused):
    """The target resolved to a private/internal address — an attack-in-
    progress signal, not just a bad URL. Tools wrap it in InternalRefusalNote
    so the agent loop can taint the turn without string-matching output."""


class InternalRefusalNote(str):
    """Typed marker for a tool result reporting an internal-target refusal.

    Tool.run is contractually -> str, so the signal rides the return value:
    a str subclass keeps every existing string consumer working while
    agent._dispatch detects it by isinstance and taints the turn's egress.
    """


class RefusedConfabulated(Refused):
    """The target names a known model-confabulation artifact, not a fetch
    failure (BK-037 relay family). Deliberately NOT a RefusedInternal
    subclass: zero user data rode the 29 attested dials, and internal-refusal
    taint is the attack-in-progress signal — a confabulated dial must not
    mark the turn. Every surface's `except Refused` arm returns the message
    verbatim, so the directive lands in the tool result and the audit digest."""


# Loose sanity bound on outbound URLs (Control D of the egress-hardening
# plan): stops the laziest single-shot query-string dump, nothing more —
# chunking defeats any length cap, so the budget/taints do the real work.
# Well above legitimate signed URLs (~2 KB).
_MAX_URL_CHARS = 4096

# --- Confabulated relay-family block (BK-037, decisions D1/D2) ------------
# Provenance: the local Qwen checkpoint (a weights artifact — Vals AI
# 2026-10-27, wtogami/qwen-block-proxy-artifacts) confabulates signed Alibaba
# OSS "relay" URLs and dials them: 29 attempts across 6 sessions in Oct 2026,
# every executed one HTTP 403, compaction re-sending the URLs from the
# byte-frozen verbatim tail. This matcher is wtogami's rule 2 verbatim (the
# battle-tested generalization of the incident host; its rule 1, the exact
# host, is a strict subset and is not shipped).
#
# Deliberate under-inclusion trade: the predicate is ANCHORED TO THE HOSTNAME
# and to the routify+file family shape. A bare-token rule was tried upstream
# (wtogami) and REMOVED for over-firing on mere prose mentions, so the tokens
# in a path, query or prose still pass — by construction, since this never
# sees anything but a host — and BK-037's near-miss criterion pins that
# behavior out. Labels outside the family are knowingly under-included; the
# trade is measured by tests/fixtures/egress/relay_urls_oct2026.json and
# widened by attestation, never by guesswork.
#
# The trailing `[.](com|io)` is D1-verbatim and unanchored, so a hypothetical
# `…aliyuncs.computer` / `…aliyuncs.company` host also matches. Kept that way:
# it fails CLOSED on a name no Alibaba-controlled registrant serves, while an
# attested relay on aliyuncs.com/.io always matches; anchoring would deviate
# from the pinned rule for a case that cannot be a legitimate OSS endpoint.
# Both the bytes and that direction are pinned in test_relay_block.
_RELAY_FAMILY = re.compile(r"routify[\w.-]{0,12}file[\w.-]{0,56}aliyuncs[.](com|io)")

# The criterion-2 directive, verbatim from the board. Digest arithmetic
# (surface map §2): 126 chars + the longest tool prefix (24) < the 200-char
# result_digest, so it lands intact at every surface.
RELAY_DIRECTIVE = (
    "fabricated relay URL (known model artifact): re-derive the real URL "
    "from the previous web_search result; do not retry variants"
)


def is_confabulated_host(host: str) -> bool:
    """True when host is in the confabulated relay label family (BK-037).

    Normalizes its input on purpose: urlsplit(...).hostname lowercases but
    KEEPS a trailing dot (the FQDN-root trick), and the egress proxy's CONNECT
    path (step 4) hands this predicate a raw authority — neither lowercased
    nor stripped — so the hygiene lives here, not at the call sites."""
    return _RELAY_FAMILY.search(host.lower().rstrip(".")) is not None


async def check_target(url: str, resolver: Resolver) -> tuple[str, str | None]:
    """Validate the target and return (validated_ip, hostname).

    hostname is None when the URL already names an IP literal. The caller
    must connect to validated_ip, not the hostname — re-resolving after
    this check is the DNS-rebinding hole this closes.
    """
    if len(url) > _MAX_URL_CHARS:
        raise Refused(
            f"URL is {len(url)} characters — refusing URLs over "
            f"{_MAX_URL_CHARS} (sanity bound)."
        )
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise Refused(f"only http(s) URLs are supported, got {parts.scheme!r}.")
    host = parts.hostname
    if not host:
        raise Refused(f"no hostname in {url!r}.")
    if is_confabulated_host(host):
        # BEFORE the resolver is awaited — a DNS query is already a request
        # leaving the machine (D2). The message is the criterion's directive,
        # so the retry loop dies here with instructions, not just a refusal.
        raise RefusedConfabulated(RELAY_DIRECTIVE)
    try:
        addresses = [str(ipaddress.ip_address(host))]
        hostname = None
    except ValueError:
        hostname = host
        try:
            addresses = await resolver(host)
        except OSError:
            raise Refused(f"could not resolve {host!r}.") from None
    for addr in addresses:
        if not ipaddress.ip_address(addr).is_global:
            raise RefusedInternal(
                f"{host} resolves to a private or internal address — refused."
            )
    return addresses[0], hostname


def pin(
    url: str, ip: str, host: str | None
) -> tuple[str, dict[str, str] | None, dict[str, str] | None]:
    """Rewrite url's host to the validated IP literal so httpx connects to the
    address that passed the check (no second resolve). The original hostname
    rides along as the Host header and TLS SNI, so virtual hosting and cert
    verification still see the real name. host=None means the URL already
    named an IP literal — nothing to pin."""
    if host is None:
        return url, None, None
    parts = urlsplit(url)
    ip_literal = f"[{ip}]" if ":" in ip else ip
    netloc = ip_literal if parts.port is None else f"{ip_literal}:{parts.port}"
    pinned = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    host_header = host if parts.port is None else f"{host}:{parts.port}"
    extensions = {"sni_hostname": host} if parts.scheme == "https" else None
    return pinned, {"Host": host_header}, extensions
