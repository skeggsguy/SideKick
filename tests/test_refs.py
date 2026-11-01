"""RefResolver (tools/_refs.py): identity-ref substitution at dispatch time.

The real values are obviously-fake fixtures ("test@example.invalid") and the
tests assert the error strings NEVER carry a stored value — the whole point of
the resolver is that a value only ever reaches the browser field, never a
message the model or a log could see.
"""

from __future__ import annotations

from harness.core.envelope import DelegationEnvelope
from harness.store.client_config import REF_EMAIL_KEY, REF_PHONE_KEY, ClientConfigStore
from harness.tools._refs import RefResolver

EMAIL = "test@example.invalid"
PHONE = "+15550000000"


class FakeManager:
    """Stubs BrowserSessionManager.element_host() — the frame-aware host the
    resolver re-checks at dispatch (Phase C)."""

    def __init__(self, host: str | None) -> None:
        self._host = host

    def element_host(self, handle: str) -> str | None:
        return self._host


def _envelope(refs, ref_hosts=()):
    env = DelegationEnvelope(origin="example.com", task="book", refs=frozenset(refs))
    env.ref_resolved_hosts = set(ref_hosts)
    return env


async def test_no_refs_passes_through(db):
    cc = ClientConfigStore(db)
    r = RefResolver(cc, _envelope({"email"}, {"example.com"}), FakeManager("example.com"))
    out, err = await r.resolve("just plain text", "e1")
    assert out == "just plain text"
    assert err is None


async def test_ungranted_ref_errors_without_value(db):
    cc = ClientConfigStore(db)
    await cc.set(REF_EMAIL_KEY, EMAIL)
    # Envelope grants only phone; {email} is not a capability of this delegation.
    r = RefResolver(cc, _envelope({"phone"}, {"example.com"}), FakeManager("example.com"))
    out, err = await r.resolve("email is {email}", "e1")
    assert out == "email is {email}"  # no substitution
    assert err is not None
    assert "email" in err and "capability" in err
    assert EMAIL not in err


async def test_unapproved_host_errors(db):
    cc = ClientConfigStore(db)
    await cc.set(REF_EMAIL_KEY, EMAIL)
    # Ref granted, value present, but no site has been approved for ref use.
    r = RefResolver(cc, _envelope({"email"}, ()), FakeManager("example.com"))
    out, err = await r.resolve("{email}", "e1")
    assert out == "{email}"
    assert err == "ref use on this site has not been approved"
    assert EMAIL not in err


async def test_host_none_errors(db):
    cc = ClientConfigStore(db)
    await cc.set(REF_EMAIL_KEY, EMAIL)
    r = RefResolver(cc, _envelope({"email"}, {"example.com"}), FakeManager(None))
    out, err = await r.resolve("{email}", "e1")
    assert out == "{email}"
    assert err == "ref use on this site has not been approved"


async def test_missing_value_names_only_the_ref(db):
    cc = ClientConfigStore(db)
    # phone is stored but email is not — the error must name {email} and must
    # NOT echo the phone value that IS present.
    await cc.set(REF_PHONE_KEY, PHONE)
    r = RefResolver(
        cc, _envelope({"email", "phone"}, {"example.com"}), FakeManager("example.com")
    )
    out, err = await r.resolve("{email}", "e1")
    assert out == "{email}"
    assert err is not None
    assert "email" in err and "Contact details" in err
    assert PHONE not in err
    assert EMAIL not in err


async def test_successful_substitution_of_both_refs(db):
    cc = ClientConfigStore(db)
    await cc.set(REF_EMAIL_KEY, EMAIL)
    await cc.set(REF_PHONE_KEY, PHONE)
    r = RefResolver(
        cc, _envelope({"email", "phone"}, {"example.com"}), FakeManager("example.com")
    )
    out, err = await r.resolve("mail {email} tel {phone}", "e1")
    assert err is None
    assert out == f"mail {EMAIL} tel {PHONE}"


async def test_subdomain_host_is_within_approved(db):
    cc = ClientConfigStore(db)
    await cc.set(REF_EMAIL_KEY, EMAIL)
    # ref_resolved_hosts holds example.com; the tab is on a subdomain of it.
    r = RefResolver(
        cc, _envelope({"email"}, {"example.com"}), FakeManager("book.example.com")
    )
    out, err = await r.resolve("{email}", "e1")
    assert err is None
    assert out == EMAIL
