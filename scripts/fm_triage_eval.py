"""Manual live eval: Apple FM gmail-triage refusal rate on the real inbox.

Lists the last N inbox messages (read-only Gmail, query `in:inbox`) and
classifies each through the PRODUCTION path — the same `triage_text` the
watcher builds and the same `AppleFMProvider().classify(text, TRIAGE_LABELS,
TRIAGE_INSTRUCTION)` call, one at a time (the on-device model is shared with
nothing else, but serial runs keep the numbers comparable).

Prints the label distribution plus a refusal count (None with "refused" in
last_error — apple-fm-sdk 0.1.0 surfaces the macOS refusal as a generic
status-255 GenerationError, so the message is the only marker) and an
other-error count. `--out` writes {message_id: label|null}; `--compare`
scores a fresh run against an earlier snapshot. The model agrees with itself
only ~94–99% run-to-run: capture a same-code CONTROL run before judging an
agreement gate.

Never opens harness.db and imports no store modules of its own (gmail.py's
store import is TYPE_CHECKING-only; the store classes that still reach the
module graph arrive via harness.scheduler's package __init__, which eagerly
re-exports Scheduler — importing a store module never OPENS a database, and
nothing here calls store code). Needs the Mac: on-device FM + a live Gmail
token.

Run with: uv run python scripts/fm_triage_eval.py [--n 100]
          [--out FILE.json] [--compare FILE.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

from harness.providers.apple_fm import AppleFMProvider
from harness.scheduler.watchers.gmail import (
    GoogleGmailAPI,
    TRIAGE_INSTRUCTION,
    TRIAGE_LABELS,
    triage_text,
)


async def list_inbox_ids(api: GoogleGmailAPI, n: int) -> list[str]:
    """Newest-first ids for the query, paged until n are collected."""

    def call() -> list[str]:
        svc = api._service()  # the watcher's own auth path (cached MCP token)
        ids: list[str] = []
        page_token = None
        while len(ids) < n:
            resp = (
                svc.users()
                .messages()
                .list(
                    userId="me",
                    q="in:inbox",
                    maxResults=min(n, 100),
                    pageToken=page_token,
                )
                .execute()
            )
            ids.extend(m["id"] for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids[:n]

    return await asyncio.to_thread(call)


def load_snapshot(path: Path) -> dict[str, str | None]:
    return json.loads(path.read_text())


def compare(baseline: dict[str, str | None], current: dict[str, str | None]) -> None:
    both = [
        mid
        for mid, label in current.items()
        if mid in baseline and label is not None and baseline[mid] is not None
    ]
    if not both:
        print("\ncompare: no ids both runs labelled")
        return
    agreed = [mid for mid in both if current[mid] == baseline[mid]]
    print(
        f"\nagreement vs baseline: {len(agreed)}/{len(both)} "
        f"({100 * len(agreed) / len(both):.1f}%) on ids both runs labelled"
    )
    for mid in both:
        if current[mid] != baseline[mid]:
            print(f"  disagree {mid}: baseline={baseline[mid]} now={current[mid]}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--n", type=int, default=100, help="inbox messages to sample")
    parser.add_argument("--out", type=Path, help="write {message_id: label|null}")
    parser.add_argument("--compare", type=Path, help="baseline snapshot to score against")
    args = parser.parse_args()

    load_dotenv()
    provider = AppleFMProvider()
    reason = provider.unavailable_reason()
    if reason is not None:
        print(f"apple fm unavailable: {reason}", file=sys.stderr)
        return 2

    api = GoogleGmailAPI()
    ids = await list_inbox_ids(api, args.n)
    print(f"{len(ids)} inbox messages, classifying one at a time", flush=True)

    results: dict[str, str | None] = {}
    dist: Counter[str] = Counter()
    refused = other_errors = missing = 0
    for i, mid in enumerate(ids, 1):
        meta = await api.message_meta(mid)
        if meta is None:
            missing += 1
            results[mid] = None
            print(f"  [{i}/{len(ids)}] {mid} -> (message vanished)", flush=True)
            continue
        label = await provider.classify(triage_text(meta), TRIAGE_LABELS, TRIAGE_INSTRUCTION)
        results[mid] = label
        if label is not None:
            dist[label] += 1
        elif "refused" in (provider.last_error or "").lower():
            refused += 1
        else:
            other_errors += 1
        note = "" if label is not None else f"  ({(provider.last_error or '')[:100]})"
        print(f"  [{i}/{len(ids)}] {mid} -> {label or 'ERROR'}{note}", flush=True)

    labelled = sum(dist.values())
    print(f"\nlabelled: {labelled}/{len(ids)}  refused: {refused}  "
          f"other errors: {other_errors}  vanished: {missing}")
    print(f"distribution: {dict(dist.most_common())}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=1) + "\n")
        print(f"wrote {args.out}")
    if args.compare:
        compare(load_snapshot(args.compare), results)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
