"""BK-037 step 2 — the confabulated relay-family block at the check_target choke.

Offline: injectable resolver only, no DNS, no network. The corpus is
tests/fixtures/egress/relay_urls_oct2026.json — 12 URL shapes attested in the
live audit_log (Oct 2026), the 5 D5 near-miss hosts, and 3 prose strings that
must never be treated as URLs. The corpus attests only TWO host spellings, so
criteria 1's other endpoint spellings (accelerate, *-internal, dual-stack and
.io variants) are tested here as CONSTRUCTED hosts — that test is the only
proof those spellings get (surface map §6: D1's 56-char second gap covers
8/8; the attested public spelling spends 29 of the 56).

The DIRECTIVE below is a hard copy of the board criterion, NOT an import of
the product constant — if RELAY_DIRECTIVE drifts, the pins here go red
instead of following it.

Every pin here was run RED on a planted break (red log in the step-2 build
report): matcher disabled; check moved after the resolve; taint-class swap to
RefusedInternal; directive edited; matcher aimed at the whole URL; bare-token
rule at entry (the rule wtogami removed); matcher end-anchored with the
dot-strip dropped (the FQDN-root evasion — note fullmatch alone is benign
while rstrip(".") stays); lowercase dropped (red at predicate level only —
urlsplit pre-lowercases URL inputs, so the raw-authority caller is where that
guarantee bites); matcher weakened to aliyuncs-only; fixture entry removed.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from harness.tools._netguard import (
    _RELAY_FAMILY,
    RELAY_DIRECTIVE,
    Refused,
    RefusedConfabulated,
    RefusedInternal,
    check_target,
    is_confabulated_host,
)

_CORPUS_PATH = Path(__file__).parent / "fixtures" / "egress" / "relay_urls_oct2026.json"
CORPUS = json.loads(_CORPUS_PATH.read_text())

# Verbatim from the BK-037 criterion 2 — hard copy on purpose (see docstring).
DIRECTIVE = (
    "fabricated relay URL (known model artifact): re-derive the real URL "
    "from the previous web_search result; do not retry variants"
)

_RELAY_HOST = "routify-file-proxy-sg.oss-ap-southeast-1.aliyuncs.com"


class SpyResolver:
    """Records every lookup it is asked for. The block's contract is that a
    relay refusal records NONE: the refusal precedes any resolve."""

    def __init__(self, addresses=("93.184.216.34",)):
        self.calls: list[str] = []
        self._addresses = list(addresses)

    async def __call__(self, host: str) -> list[str]:
        self.calls.append(host)
        return list(self._addresses)


def tripwire_resolver():
    """A resolver that fails loudly if it is ever awaited — the ordering pin:
    a DNS query for a blocked host is already egress (D2), so reaching this
    resolver mid-refusal is itself the regression."""

    async def resolver(host: str) -> list[str]:
        raise AssertionError(
            f"GOTCHA REGRESSION [bk-037-order] resolver awaited for {host!r} "
            "— the relay block must fire BEFORE any DNS lookup (a resolve is "
            "already a request leaving the machine)"
        )

    return resolver


def test_corpus_fixture_has_the_attested_shape_counts():
    # Fixture-integrity pin (surface map §6): 29 audit rows normalize to
    # 12 blocked shapes + the 5-host D5 near-miss matrix + 3 prose strings.
    # If this goes red the FIXTURE changed — re-derive against the live db
    # read-only; never "fix" the counts here.
    assert len(CORPUS["blocked"]) == 12
    assert len(CORPUS["allowed"]) == 5
    assert len(CORPUS["prose_never_url_checked"]) == 3


def test_directive_is_the_criterion_verbatim_and_fits_every_digest():
    # The criterion's wording is the contract; the length pins surface-map
    # §2's digest arithmetic: directive + longest tool prefix
    # ("browser_navigate error: ") stays inside DIGEST_CHARS = 200, so the
    # refusal lands verbatim in result_digest at every surface (criterion 2).
    assert DIRECTIVE == (
        "fabricated relay URL (known model artifact): re-derive the real URL "
        "from the previous web_search result; do not retry variants"
    )
    assert len(DIRECTIVE) == 126
    assert len("browser_navigate error: ") + len(DIRECTIVE) < 200
    assert RELAY_DIRECTIVE == DIRECTIVE  # product has not drifted from the board


def test_confabulated_class_shape():
    # D2: subclasses Refused (every surface's `except Refused` arm already
    # returns its message as the tool result — zero per-tool edits) and NOT
    # RefusedInternal (confabulation is not the attack-in-progress signal;
    # raising it internal would wrongly taint the turn's egress).
    assert issubclass(RefusedConfabulated, Refused)
    assert not issubclass(RefusedConfabulated, RefusedInternal)


_BLOCKED = CORPUS["blocked"]


@pytest.mark.parametrize(
    "url",
    [e["url"] for e in _BLOCKED],
    ids=[f"blocked{i}-{e['signature_form']}" for i, e in enumerate(_BLOCKED)],
)
async def test_every_corpus_url_refused_with_the_directive(url):
    # Criterion 4, corpus half: every URL shape in the Oct 2026 audit_log is
    # refused at the choke — both endpoint spellings, the Signature=xxx and
    # degenerate-signature variants — with the directive verbatim, no taint,
    # and no DNS lookup attempted.
    spy = SpyResolver()
    with pytest.raises(RefusedConfabulated) as ei:
        await check_target(url, spy)
    assert str(ei.value) == DIRECTIVE
    assert not isinstance(ei.value, RefusedInternal)
    assert spy.calls == []


async def test_block_fires_before_the_resolver_is_awaited():
    # The tripwire raises AssertionError, not Refused, if reached — so an
    # ordering regression (check moved past the resolve) fails HERE with the
    # gotcha message instead of being laundered into a normal refusal.
    for url in (_BLOCKED[0]["url"], _BLOCKED[-1]["url"]):
        with pytest.raises(RefusedConfabulated):
            await check_target(url, tripwire_resolver())


async def test_confabulation_wins_even_when_resolution_would_say_internal():
    # Arm order, other direction: a relay host whose resolver hands back a
    # CGNAT address must still be refused as confabulated (plain refusal, no
    # taint). If the relay check ever moved after the is_global loop this
    # becomes RefusedInternal — the wrong signal for a fabricated URL.
    spy = SpyResolver(addresses=("100.64.0.1",))
    with pytest.raises(RefusedConfabulated) as ei:
        await check_target(f"https://{_RELAY_HOST}/p", spy)
    assert not isinstance(ei.value, RefusedInternal)
    assert spy.calls == []


_ALLOWED = [(e["url"], e["why"]) for e in CORPUS["allowed"]]


@pytest.mark.parametrize(
    "url,why", _ALLOWED, ids=[f"allowed{i}" for i in range(len(_ALLOWED))]
)
async def test_near_misses_pass_through_to_the_resolver(url, why):
    # D5 near-miss matrix: legit OSS hosts and every token-outside-the-family
    # case keep working — check_target resolves them like any other target.
    spy = SpyResolver()
    ip, hostname = await check_target(url, spy)
    assert ip == "93.184.216.34"
    assert hostname == urlsplit(url).hostname
    assert spy.calls == [urlsplit(url).hostname]


@pytest.mark.parametrize(
    "url,why",
    [
        (
            "https://my-relay-file.oss-cn-hangzhou.aliyuncs.com/a.png",
            "relay+file tokens but no routify — outside the family",
        ),
        (
            "https://routify-file-proxy-sg.oss-ap-southeast-1.amazonaws.com/x",
            "family-shaped labels on the wrong vendor's domain",
        ),
    ],
    ids=["no-routify", "wrong-vendor"],
)
async def test_family_boundary_hosts_pass_through(url, why):
    # Matcher boundary (the wizard-checkpoint question: over-firing). The
    # family needs BOTH tokens in order on an aliyuncs TLD; near-family hosts
    # must still reach the resolver.
    spy = SpyResolver()
    await check_target(url, spy)
    assert spy.calls == [urlsplit(url).hostname]


def test_matcher_is_the_d1_family_verbatim():
    # D1 pins wtogami rule 2 by its BYTES. The behavioral matrix alone stays
    # green if a gap widens ({0,56} -> {0,60}) or the TLD alternation grows,
    # so the exact pattern needs its own pin. Case is handled by the
    # predicate's .lower(), not a regex flag (step 4 hands it a raw authority).
    assert _RELAY_FAMILY.pattern == (
        r"routify[\w.-]{0,12}file[\w.-]{0,56}aliyuncs[.](com|io)"
    )
    # The trailing (com|io) is deliberately UNANCHORED (D1 verbatim): a
    # hypothetical prefix-TLD host fails CLOSED. That is harmless and accepted
    # at the step-2 checkpoint — no legitimately registered OSS endpoint has
    # this shape, and an attested relay on aliyuncs.com/.io always matches —
    # so anchoring it is a DECISION (it changes the pinned bytes), never a
    # silent tightening to buy this line green.
    assert is_confabulated_host(
        "routify-file-proxy-sg.oss-ap-southeast-1.aliyuncs.computer"
    )


@pytest.mark.parametrize(
    "prose",
    [e["text"] for e in CORPUS["prose_never_url_checked"]],
    ids=["checkpoint-note", "user-turn", "assistant-turn"],
)
async def test_prose_mention_is_never_treated_as_a_url(prose):
    # D5: the token quoted in prose is never a URL check (fixture: real
    # strings from session_checkpoints and messages). The matcher only ever
    # sees a hostname, so prose cannot even reach it — what prose gets is the
    # generic scheme refusal, never the confabulation arm. This is exactly
    # the over-fire D1 rejected (wtogami's removed bare-token rule); planted
    # and pinned red — see the build report's break F.
    spy = SpyResolver()
    with pytest.raises(Refused) as ei:
        await check_target(prose, spy)
    assert not isinstance(ei.value, RefusedConfabulated)
    assert "only http(s)" in str(ei.value)
    assert spy.calls == []


@pytest.mark.parametrize(
    "url",
    [
        f"https://{_RELAY_HOST}/p",
        f"https://{_RELAY_HOST.upper()}/p",
        f"https://{_RELAY_HOST}./p",
        f"HTTP://{_RELAY_HOST.upper()}/P",
        f"https://{_RELAY_HOST}:443/p",
        f"https://user:pass@{_RELAY_HOST}/p",
    ],
    ids=["plain", "uppercase", "trailing-dot", "upper-scheme", "port", "userinfo"],
)
async def test_host_case_and_spelling_variants_still_refuse(url):
    # urlsplit(...).hostname lowercases but KEEPS a trailing dot (surface map
    # §2 trap), and userinfo/port never hide the host — the FQDN-root and
    # case variants of an incident host must not become an evasion.
    spy = SpyResolver()
    with pytest.raises(RefusedConfabulated) as ei:
        await check_target(url, spy)
    assert str(ei.value) == DIRECTIVE
    assert spy.calls == []


_CONSTRUCTED = [
    "routify-file-proxy-sg.oss-accelerate.aliyuncs.com",
    "routify-file-proxy-sg.oss-accelerate.aliyuncs.io",
    "routify-file-proxy-sg.oss-ap-southeast-1-internal.aliyuncs.com",
    "routify-file-proxy-sg.oss-ap-southeast-1-internal.aliyuncs.io",
    "routify-file-proxy-sg.oss-ap-southeast-1.aliyuncs.io",
    "routify-file-proxy-sg.ap-southeast-1.oss.aliyuncs.io",
    "routify-file-proxy-sg.ap-southeast-1.oss.aliyuncs.com",
    "routify-file-proxy-sg.oss-accelerate-oversea.aliyuncs.com",
]


@pytest.mark.parametrize(
    "host",
    _CONSTRUCTED,
    ids=[h.removeprefix("routify-file-proxy-sg.") for h in _CONSTRUCTED],
)
async def test_constructed_criterion1_spellings_refuse(host):
    # The corpus attests only two spellings; criteria 1 names the others
    # (accelerate, *-internal, dual-stack, .io, oversea). These constructed
    # hosts are the ONLY proof those spellings are blocked — D1's 56-char
    # gap is what covers them, and this test is where that claim is checked.
    spy = SpyResolver()
    with pytest.raises(RefusedConfabulated) as ei:
        await check_target(f"https://{host}/p", spy)
    assert str(ei.value) == DIRECTIVE
    assert spy.calls == []


def test_predicate_is_the_shared_rule_step4_reuses():
    # One rule, one module (D3): egress_proxy.target_addresses (step 4) calls
    # this predicate with the RAW CONNECT authority — neither lowercased nor
    # dot-stripped — so normalization must live inside it, not at call sites.
    assert is_confabulated_host(_RELAY_HOST.upper())
    assert is_confabulated_host(_RELAY_HOST + ".")
    assert is_confabulated_host(_RELAY_HOST)
    assert not is_confabulated_host("example.com")
    assert not is_confabulated_host("routify.example.com")
