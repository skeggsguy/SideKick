"""Session-scoped ledger of trusted outbound destinations.

The destination gate (core/policy.py) strips auto-run from web_fetch and
browser_fetch when the target's host was never legitimized this session. A
host becomes trusted only through channels the user (or their own query)
controls:

- the user's own messages, including playbook prompts (seed_text),
- URLs in web_search results for the model's query (seed_search_results),
- a fetch that actually executed — auto or human-approved (add_url).

Tool results and fetched page content are NEVER seeded: an injected page
naming its exfil endpoint must not be able to legitimize it. seed_text also
strips the <memory> and <watch_event> regions of a user turn — both embed
content from outside the user's hands (recalled facts, attacker-authored
email bodies) inside the user message.

Matching is exact-normalized-host or subdomain-of-seed. Seeds are never
collapsed to a two-label "registrable domain": without a public-suffix list,
collapsing example.co.uk would trust all of *.co.uk. The cost (seeding
portal.example.com does not admit example.com) is absorbed by one approval.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# Regions of a user turn whose content is not the user's own words. The
# compaction checkpoint block is MODEL-GENERATED (its summary can repeat a
# domain that arrived inside a tool result), so it must never legitimize a
# destination — same rule as memory/watch_event. Any NEW synthesized user-role
# message is an egress-seed surface: its tag must join this regex.
_UNTRUSTED_REGIONS = re.compile(
    r"<memory>.*?</memory>|<watch_event\b.*?</watch_event>"
    r"|<context_checkpoint>.*?</context_checkpoint>",
    re.DOTALL,
)
_URL = re.compile(r"https?://[^\s<>\"'`)\]]+", re.IGNORECASE)
# Bare-domain tokens the user typed ("check rightmove.co.uk"). Requires an
# alphabetic TLD so version numbers ("3.12") never match; filename-like
# tokens on real TLDs (setup.py) can over-seed — accepted, the attacker
# would have to own that exact domain.
_BARE_DOMAIN = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.IGNORECASE)
# Bare IPv4 literals ("fetch 93.184.216.34") — the alpha-TLD rule above
# skips them. Internal ranges seeding is harmless: check_target refuses
# them in-tool regardless of the ledger.
_BARE_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# A web_search result URL is emitted on its own 3-space-indented line
# (web_search._format_results) — snippet lines carry prose and never match.
# Coupling pinned by tests/test_web_search.py round-trip.
_RESULT_URL_LINE = re.compile(r"^\s{3}(https?://\S+)\s*$", re.MULTILINE)


def _normalize_host(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    if host.startswith("www.") and host.count(".") >= 2:
        host = host[4:]
    return host


def host_of(url: str) -> str | None:
    """The normalized host of a URL, or None when it has none / won't parse."""
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return None
    return _normalize_host(host) if host else None


class EgressLedger:
    """Grows for the life of one Agent (CLI session / daemon run)."""

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def add_host(self, host: str) -> None:
        normalized = _normalize_host(host)
        if normalized:
            self.seen.add(normalized)

    def add_url(self, url: str) -> None:
        host = host_of(url)
        if host:
            self.seen.add(host)

    def seed_text(self, text: str) -> None:
        """Seed from a user-authored message (untrusted regions stripped)."""
        trusted = _UNTRUSTED_REGIONS.sub(" ", text)
        for url in _URL.findall(trusted):
            self.add_url(url)
        for token in _BARE_DOMAIN.findall(trusted):
            self.add_host(token)
        for token in _BARE_IPV4.findall(trusted):
            self.add_host(token)

    def seed_search_results(self, text: str) -> None:
        """Seed the result URLs (only) from formatted web_search output."""
        for url in _RESULT_URL_LINE.findall(text):
            self.add_url(url)

    def allows_host(self, host: str) -> bool:
        normalized = _normalize_host(host)
        if not normalized:
            return False
        return normalized in self.seen or any(
            normalized.endswith("." + seed) for seed in self.seen
        )

    def allows(self, url: str) -> bool:
        host = host_of(url)
        return host is not None and self.allows_host(host)
