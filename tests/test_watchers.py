"""P5: triage classifier (local + cloud fallback), Gmail watcher, WatchHit runs.

Offline — Ollama is a MockTransport, Gmail is a fake API, the cloud
classifier is a scripted provider.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import httpx
import pytest

from harness.core.approvals import ApprovalWaiter
from harness.core.events import EventBus, WatchHit
from harness.core.orchestrator import Orchestrator
from harness.core.policy import Autonomy
from harness.core.types import LLMResponse, Message, Usage
from harness.providers.base import LLMProvider, OnTextDelta
from harness.providers.ollama_provider import Classifier, OllamaProvider
from harness.scheduler.watchers.gmail import (
    GmailWatcher,
    HistoryExpired,
    triage_text,
)
from harness.store.approvals import ApprovalStore
from harness.store.sessions import SessionStore
from harness.store.watch_state import WatchStateStore
from harness.tools.base import Tool

LABELS = ["urgent", "actionable", "newsletter", "ignore"]


from test_agent_loop import ScriptedProvider  # noqa: E402 - canonical test double


def ollama_with_reply(label: str) -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"message": {"content": json.dumps({"label": label})}}
        )

    return OllamaProvider(transport=httpx.MockTransport(handler))


def ollama_down() -> OllamaProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    return OllamaProvider(transport=httpx.MockTransport(handler))


# -- classifier ---------------------------------------------------------------


async def test_classify_local():
    classifier = Classifier(ollama_with_reply("newsletter"), cloud=None)
    label, source = await classifier.classify("x", LABELS, "triage")
    assert (label, source) == ("newsletter", "local")


async def test_classify_falls_back_to_cloud():
    cloud = ScriptedProvider([LLMResponse(text='{"label": "urgent"}')])
    classifier = Classifier(ollama_down(), cloud)
    label, source = await classifier.classify("x", LABELS, "triage")
    assert (label, source) == ("urgent", "cloud")
    assert cloud.calls == 1


async def test_classify_raises_when_both_unavailable():
    with pytest.raises(RuntimeError):
        await Classifier(ollama_down(), cloud=None).classify("x", LABELS, "triage")


# -- gmail watcher ------------------------------------------------------------


def test_triage_text_format_pinned():
    """The <email>-wrapped framing from quest fm-triage-refusals step 2: the
    From/Subject/snippet block inside <email>\\n…\\n</email>. Under guided
    generation the UNWRAPPED finance/insurance snippet reads as the user
    asking about their own affairs and the model refuses; the tag makes it
    data (scratch/fm_refusal_variants.py). If this fails, the text the
    classifier sees has drifted — refusals will follow."""
    meta = {
        "id": "m1",
        "from": "alice@example.com",
        "subject": "Friday digest",
        "snippet": "here is the week in review",
        "labels": ["INBOX"],
    }
    assert triage_text(meta) == (
        "<email>\n"
        "From: alice@example.com\nSubject: Friday digest\n\n"
        "here is the week in review\n"
        "</email>"
    )


class FakeGmailAPI:
    def __init__(self) -> None:
        self.profile_id = "100"
        self.history: tuple[list[str], str] = ([], "100")
        self.messages: dict[str, dict] = {}
        self.expire_next = False

    async def profile_history_id(self) -> str:
        return self.profile_id

    async def history_since(self, cursor: str) -> tuple[list[str], str]:
        if self.expire_next:
            self.expire_next = False
            raise HistoryExpired(cursor)
        return self.history

    async def message_meta(self, message_id: str) -> dict | None:
        return self.messages.get(message_id)


class FakeClassifier:
    """Labels by keyword in the subject line; records what it saw."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def classify(self, text, labels, instruction):
        self.seen.append(text)
        if "URGENT" in text:
            return "urgent", "local"
        if "digest" in text:
            return "newsletter", "local"
        return "ignore", "local"


def make_watcher(db, api=None, classifier=None):
    api = api or FakeGmailAPI()
    classifier = classifier or FakeClassifier()
    return GmailWatcher(api, classifier, WatchStateStore(db)), api, classifier


def add_message(api, msg_id, subject, labels=("INBOX",)):
    api.messages[msg_id] = {
        "id": msg_id,
        "from": "alice@example.com",
        "subject": subject,
        "snippet": f"snippet of {subject}",
        "labels": list(labels),
    }


async def test_first_poll_initializes_cursor_without_backlog(db):
    watcher, api, classifier = make_watcher(db)
    assert await watcher.poll() == []
    assert await WatchStateStore(db).get("gmail") == "100"
    assert classifier.seen == []  # nothing triaged on the first poll


async def test_urgent_raises_hit_newsletter_does_not(db, monkeypatch):
    watcher, api, classifier = make_watcher(db)
    # Wire pin: _triage must build its text through the module-level
    # triage_text — the eval script's fidelity to the watcher depends on it,
    # and step 2's fix lives ONLY in that function. The sentinel return makes
    # any inline re-construction (byte-identical to today) fail the assertion.
    seen: list[str] = []

    def fake_triage_text(meta: dict) -> str:
        seen.append(meta["id"])
        return f"<email> {meta['subject']} </email>"

    monkeypatch.setattr("harness.scheduler.watchers.gmail.triage_text", fake_triage_text)
    await watcher.poll()  # init cursor

    add_message(api, "m1", "URGENT: server down")
    add_message(api, "m2", "Friday digest")
    api.history = (["m1", "m2", "m1"], "205")  # m1 duplicated in history

    hits = await watcher.poll()
    assert [h["message_id"] for h in hits] == ["m1"]
    assert hits[0]["label"] == "urgent"
    assert hits[0]["triage_source"] == "local"
    assert len(classifier.seen) == 2  # deduped: m1 triaged once
    assert seen == ["m1", "m2"]  # every triage went through triage_text
    assert classifier.seen == [
        "<email> URGENT: server down </email>",
        "<email> Friday digest </email>",
    ]
    assert await WatchStateStore(db).get("gmail") == "205"


async def test_triage_sends_the_wrapped_text_to_the_classifier(db):
    """Step 2's end-to-end half: with the REAL triage_text (no monkeypatch —
    the wiring test above swaps it out, so it cannot show this), the text the
    classifier receives is the <email>-wrapped block."""
    watcher, api, classifier = make_watcher(db)
    await watcher.poll()
    add_message(api, "m5", "Friday digest")
    api.history = (["m5"], "500")
    await watcher.poll()
    assert classifier.seen == [
        "<email>\nFrom: alice@example.com\nSubject: Friday digest\n\n"
        "snippet of Friday digest\n</email>"
    ]


async def test_non_inbox_messages_skipped(db):
    watcher, api, classifier = make_watcher(db)
    await watcher.poll()
    add_message(api, "m3", "URGENT: but it's my own sent mail", labels=("SENT",))
    api.history = (["m3"], "300")
    assert await watcher.poll() == []
    assert classifier.seen == []


async def test_expired_history_resets_cursor(db):
    watcher, api, _ = make_watcher(db)
    await watcher.poll()
    api.expire_next = True
    api.profile_id = "999"
    assert await watcher.poll() == []
    assert await WatchStateStore(db).get("gmail") == "999"


async def test_triage_failure_defaults_to_actionable(db):
    class BrokenClassifier:
        async def classify(self, *a, **k):
            raise RuntimeError("no classifiers")

    watcher, api, _ = make_watcher(db, classifier=BrokenClassifier())
    await watcher.poll()
    add_message(api, "m4", "anything")
    api.history = (["m4"], "400")
    hits = await watcher.poll()
    assert hits[0]["label"] == "actionable"
    assert hits[0]["triage_source"] == "fallback"


# -- fm_triage_eval helper (scripts/fm_triage_eval.py) -----------------------


def load_fm_triage_eval():
    """Import scripts/fm_triage_eval.py as a plain module (it is not a
    package). Importing is side-effect free: it must never open a DB."""
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "fm_triage_eval.py"
    spec = importlib.util.spec_from_file_location("fm_triage_eval", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_fm_triage_eval_source_imports_no_store_or_db():
    """The plan forbids the eval from touching/importing the store — the
    gotcha is that any command opening harness.db applies migrations. Pin by
    source (a runtime check can't prove absence of imports): the script's own
    imports are provider + watcher only, and nothing in it names the DB."""
    script = (
        Path(__file__).resolve().parent.parent / "scripts" / "fm_triage_eval.py"
    ).read_text()
    code = script.split('"""', 2)[2] if script.startswith('"""') else script
    forbidden = ("harness.store", "sqlite3", "harness.db", "Database(")
    hits = [f for f in forbidden if f in code]
    assert not hits, f"fm_triage_eval.py must stay store/DB-free, found {hits}"


def test_fm_triage_eval_import_pulls_no_watch_state_store():
    """gmail.py's store import is TYPE_CHECKING-only, so importing the eval
    must not pull store.watch_state into the process. Runs in a clean
    interpreter because this test module imports the store itself (which would
    mask a regression in an in-process check)."""
    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "scripts" / "fm_triage_eval.py"
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('fm_triage_eval', {str(script)!r})\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "assert spec.loader is not None\n"
        "spec.loader.exec_module(m)\n"
        "store = sorted(k for k in sys.modules if k.startswith('harness.store'))\n"
        "print('WATCH_STATE_IMPORTED' if 'harness.store.watch_state' in store else 'CLEAN', '|', ','.join(store))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    print(out.stdout.strip())  # keep the module graph visible in -v runs
    assert "WATCH_STATE_IMPORTED" not in out.stdout, (
        "gmail.py's WatchStateStore import is no longer TYPE_CHECKING-only"
    )


def test_fm_triage_eval_compare_scores_only_ids_both_runs_labelled(capsys):
    """Step 2's gate (>=90% agreement) is scored by compare(); pin the math:
    ids where either run refused/vanished (label None) or that only one run
    saw do not count as disagreements."""
    ev = load_fm_triage_eval()
    ev.compare(
        # d: refused in baseline (None); e: vanished in baseline (absent)
        {"a": "newsletter", "b": "urgent", "c": "ignore", "d": None},
        {"a": "newsletter", "b": "ignore", "d": "actionable", "e": "urgent"},
    )
    out = capsys.readouterr().out
    assert "agreement vs baseline: 1/2 (50.0%)" in out
    assert "disagree b: baseline=urgent now=ignore" in out
    assert "disagree d:" not in out  # refused in baseline -> not both labelled


def test_fm_triage_eval_compare_no_shared_labelled_ids(capsys):
    ev = load_fm_triage_eval()
    ev.compare({"a": None}, {"a": "urgent"})
    out = capsys.readouterr().out
    assert "no ids both runs labelled" in out


# -- orchestrator -------------------------------------------------------------


class FakeAgent:
    def __init__(self, log: list) -> None:
        self.log = log

    async def run_turn(self, session, user_input, **kwargs):
        self.log.append({"input": user_input, "session": session, **kwargs})
        return "push text"


async def test_watch_hit_runs_email_triage_supervised(db):
    log, completed, factory_calls = [], [], []

    async def on_complete(title, text, channel, session_id):
        completed.append((title, text, channel, session_id))

    def factory(autonomy: Autonomy):
        factory_calls.append(autonomy)
        return FakeAgent(log)

    orchestrator = Orchestrator(
        bus=EventBus(),
        sessions=SessionStore(db),
        agent_factory=factory,
        waiter=ApprovalWaiter(ApprovalStore(db)),
        on_run_complete=on_complete,
    )
    await orchestrator.handle(
        WatchHit(
            watcher="gmail",
            payload={
                "message_id": "m1",
                "from": "alice@example.com",
                "subject": "server down",
                "label": "urgent",
            },
        )
    )
    await orchestrator.drain()

    assert factory_calls == [Autonomy.SUPERVISED]
    run = log[0]
    assert run["trigger"] == "watch:gmail"
    assert run["playbook"] == "email_triage"
    assert "<watch_event" in run["input"] and "m1" in run["input"]
    assert "gmail_read" in run["input"]  # playbook text resolved
    title, text, channel, session_id = completed[0]
    assert (title, text, channel) == ("Urgent email: server down", "push text", "web")
    assert session_id == run["session"].id  # notification deep-links here
