#!/opt/homebrew/bin/python3
"""Front-end smoothness probe (plan frontend-smoothness, Step 8; BK-033) — the
one entry point for every smoothness check, promoted from the step-by-step
scratch probe so a regression fails a run instead of a device.

    /opt/homebrew/bin/python3 tests/fixtures/smoothness/run_probe.py

Runs each SUITE as its own subprocess (one hung browser cannot take the rest
down; each has its own hang alarm and a wall-clock timeout here), with its
output written to WORK/logs/<suite>.log — redirected to a file, never piped
(a piped probe has hung). Then rolls the suites' rows up into one verdict.

Suites (README.md says what each row guards):
  stream        retypes 0, raw markers 0, think collapse 0 px, selection,
                tap highlight, code-block scrollLeft, long-answer churn —
                webkit/iphone, webkit/ipad, chromium/iphone-4xcpu  (BK-026/028/030)
  taps          any press unpins, "↓ Latest" resumes, D1 pauses (BK-029)
  toggles       toggle-anchor <= 1 px per frame, Show all, 0 inner
                scrollers, wheel chaining (BK-030/031)
  pills         pills open/close only on tap (BK-035)
  bgrun         a background-run chat heals on resume <= 2 s (BK-027)
  activity      the row's label + real elapsed time on a mid-run open, the
                approval-wait label (BK-041)
  reopen        reopening your own chat mid-run keeps it streaming; the
                whole answer lands once at the end (BK-024)
  stopanywhere  Stop on the observing device (BK-034)       — real gateway
  senderstop    the sender's Stop survives a quiet stream (BK-036) — real gateway
  orphan        <= 1 /api/events stream per page across sleep/wake (BK-032) — real gateway

Flags (all optional):
  --only=SUITE[,SUITE]     run a subset (names above)
  --engines=webkit[,chromium]  run only these engines
  --webui=DIR              serve another copy of the web UI (red runs on an
                           older tree: `git archive <rev> src/harness/gateway/webui`)
  --red=slack|keepat       toggles' own red modes (see toggles.py)

Exit codes (pwa_resume / scroll_follow precedent):
  0  every suite ran and every row PASSed
  1  the probe ran and some row FAILed
  2  harness broke: no Playwright here, a suite timed out or crashed (exit not
     0/1), a suite printed no PROBE-RESULT line or ZERO rows, or — on a run
     with both engines — a suite fell under its MIN_ROWS floor (a scenario
     family silently did not run; a results block that ran less than it
     claims must never read as green)

The real-gateway suites spawn `uv run python <suite>_server.py` (the REAL
create_app, a scratch DB, an ephemeral 127.0.0.1 port; never agentd's 8787).
Nothing ever POSTs to the live daemon.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent                  # tests/fixtures/smoothness
REPO = Path(__file__).resolve().parents[3]
WORK = REPO / "scratch" / "smoothness_fixture"          # gitignored; logs + the site mirror
LOGS = WORK / "logs"

# name -> (script, engines it runs on, wall-clock timeout in s)
SUITES = {
    "stream": ("stream.py", ("webkit", "chromium"), 1300),
    "taps": ("taps.py", ("webkit", "chromium"), 1600),
    "toggles": ("toggles.py", ("webkit", "chromium"), 1600),
    "pills": ("pills.py", ("webkit",), 700),
    "bgrun": ("bgrun.py", ("webkit",), 400),
    "activity": ("activity.py", ("webkit",), 300),
    "reopen": ("reopen.py", ("webkit",), 300),
    "stopanywhere": ("stopanywhere.py", ("webkit", "chromium"), 700),
    "senderstop": ("senderstop.py", ("webkit", "chromium"), 1000),
    "orphan": ("orphan.py", ("webkit", "chromium"), 700),
}

# Unique rows per suite on a BOTH-engine run, measured 2026-10-31 on the
# promoted fixture (every row PASS; stream after scrollLeft moved to the
# iPhone ports). May only RISE: a deliberate merge/removal edits the figure in
# the same change (pwa_resume's MIN_UNIQUE_ASSERTIONS rule). DERIVE it from a
# run's unique rows, never from a report or a raw line count (stopanywhere
# prints "A's own send shows Stop" once per scenario: 64 lines, 58 rows).
MIN_ROWS = {
    "stream": 26, "taps": 27, "toggles": 72, "pills": 17, "bgrun": 11,
    "stopanywhere": 58, "senderstop": 96, "orphan": 38,
    "activity": 36,  # BK-041, 2026-11-01: 18 rows x webkit iphone + ipad
    "reopen": 16,  # BK-024, 2026-11-01: 8 rows x webkit iphone + ipad
}

ROW_RE = re.compile(r"^(PASS|FAIL) (.*?)(?: \| .*)?$")
RESULT_RE = re.compile(r"^PROBE-RESULT: (PASS|FAIL)\b")


def usage(msg: str):
    print(f"usage error: {msg} (see module docstring)", file=sys.stderr)
    sys.exit(2)


def parse_args(argv: list[str]) -> dict:
    """--only / --engines / --webui / --red, `=`-joined (the sibling fixtures'
    flag shape). Unknown flags and empty or unknown values exit 2."""
    opts = {"only": list(SUITES), "engines": ["webkit", "chromium"], "webui": None, "red": None}
    for a in argv:
        k, eq, v = a.partition("=")
        if not eq or not v:
            usage(f"flag {a!r} wants =VALUE")
        if k == "--only":
            want = list(dict.fromkeys(x for x in v.split(",") if x))
            if not want or not set(want) <= set(SUITES):
                usage(f"--only wants a subset of {list(SUITES)}")
            opts["only"] = [s for s in SUITES if s in want]
        elif k == "--engines":
            want = list(dict.fromkeys(x for x in v.split(",") if x))
            if not want or not set(want) <= {"webkit", "chromium"}:
                usage("--engines wants webkit and/or chromium")
            opts["engines"] = want
        elif k == "--webui":
            if not (Path(v) / "index.html").is_file():
                usage(f"--webui {v}: no index.html there")
            opts["webui"] = str(Path(v).resolve())
        elif k == "--red":
            if v not in ("slack", "keepat"):
                usage("--red wants slack or keepat")
            opts["red"] = v
        else:
            usage(f"unknown flag {a!r}")
    if opts["red"] and "toggles" not in opts["only"]:
        usage("--red is a toggles mode: add toggles to --only")
    return opts


def selected(opts: dict) -> list[str]:
    """Suites to run: a webkit-only suite is dropped by --engines=chromium."""
    return [s for s in opts["only"] if set(SUITES[s][1]) & set(opts["engines"])]


def suite_argv(name: str, opts: dict) -> list[str]:
    script, engines, _ = SUITES[name]
    argv = [sys.executable, str(HERE / script),
            "--engines=" + ",".join(e for e in opts["engines"] if e in engines)]
    if opts["webui"]:
        argv.append("--webui=" + opts["webui"])
    if name == "toggles" and opts["red"]:
        argv.append("--red=" + opts["red"])
    return argv


def parse_log(text: str) -> dict:
    """Rows, MEASURE lines and the PROBE-RESULT verdict of one suite's log.
    Rows are keyed by name: a row printed twice counts once (the floor counts
    UNIQUE rows, so a doubled print cannot satisfy it)."""
    rows: dict[str, str] = {}
    measures: list[str] = []
    result = None
    for ln in text.splitlines():
        if (m := ROW_RE.match(ln)):
            # FAIL is sticky: a name that ever FAILed stays failed.
            if rows.get(m.group(2)) != "FAIL":
                rows[m.group(2)] = m.group(1)
        elif ln.startswith("MEASURE "):
            measures.append(ln[len("MEASURE "):])
        elif (m := RESULT_RE.match(ln)):
            result = m.group(1)
    return {"rows": rows, "measures": measures, "result": result}


def structural_problem(name: str, rc: int | None, parsed: dict, full_engines: bool) -> str | None:
    """Why this suite's output is not a measurement (exit 2), or None."""
    if rc is None:
        return f"{name}: timed out after {SUITES[name][2]} s"
    if rc not in (0, 1):
        return f"{name}: exited {rc} (harness break — see its log)"
    if parsed["result"] is None:
        return f"{name}: no PROBE-RESULT line — the suite did not reach its end"
    if not parsed["rows"]:
        return f"{name}: ZERO rows — a zero-row results block must never read as green"
    if full_engines and len(parsed["rows"]) < MIN_ROWS[name]:
        return (f"{name}: {len(parsed['rows'])} unique row(s), floor {MIN_ROWS[name]} — "
                "a scenario family did not run")
    return None


def run_suite(name: str, opts: dict) -> tuple[int | None, str, float]:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{name}.log"
    t0 = time.monotonic()
    with open(log, "w") as out:
        try:
            rc = subprocess.run(suite_argv(name, opts), cwd=REPO, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, timeout=SUITES[name][2]).returncode
        except subprocess.TimeoutExpired:
            rc = None
    return rc, log.read_text(), time.monotonic() - t0


def main() -> None:
    opts = parse_args(sys.argv[1:])
    try:
        import playwright  # noqa: F401 — the suites need it; the uv venv deliberately lacks it
    except ImportError:
        print("harness error: Playwright is not importable here — run with /opt/homebrew/bin/python3 "
              "(the uv venv and /usr/bin/python3 have no Playwright)", file=sys.stderr)
        sys.exit(2)
    suites = selected(opts)
    if not suites:
        usage("--only/--engines selected no suite (pills, bgrun, activity and reopen are webkit-only)")
    full_engines = set(opts["engines"]) == {"webkit", "chromium"}
    print(f"suites: {', '.join(suites)} | engines: {', '.join(opts['engines'])}"
          + (f" | webui: {opts['webui']}" if opts["webui"] else "")
          + (f" | red: {opts['red']}" if opts["red"] else ""), flush=True)
    problems, fails, measures = [], [], []
    for name in suites:
        print(f"\n===== {name} =====", flush=True)
        rc, text, secs = run_suite(name, opts)
        parsed = parse_log(text)
        bad = [r for r, v in parsed["rows"].items() if v == "FAIL"]
        print(f"{name}: exit {rc}, {len(parsed['rows'])} unique rows, {len(bad)} FAIL, "
              f"{secs:.0f} s — log {LOGS / (name + '.log')}", flush=True)
        for r in bad:
            print(f"  FAIL {r}", flush=True)
        fails += [f"{name}: {r}" for r in bad]
        measures += [f"{name}: {m}" for m in parsed["measures"]]
        if (rc == 1 or parsed["result"] == "FAIL") and not bad:  # it said failed but no FAIL row: never green
            fails.append(f"{name}: exit {rc} / PROBE-RESULT {parsed['result']} with no FAIL row")
        problem = structural_problem(name, rc, parsed, full_engines)
        if problem:
            print(f"harness error: {problem}", file=sys.stderr, flush=True)
            problems.append(problem)
    print("\n===== measures (reported, not asserted — the timing rule) =====")
    for m in measures:
        print(f"  {m}")
    print(f"\n{len(fails)} failing row(s), {len(problems)} structural problem(s)")
    print("PROBE-RESULT: " + ("PASS" if not fails and not problems else "FAIL"))
    sys.exit(2 if problems else 1 if fails else 0)


if __name__ == "__main__":
    main()
