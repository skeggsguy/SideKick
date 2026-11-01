#!/opt/homebrew/bin/python3
"""Serve the probe page for an iOS Simulator video pass (README "Simulator pass").

Builds the same static site the browser suites use (the real webui + the mock
SSE), adds sim.html = probe.html + sim_autorun.js (arms the scenario and sends
by itself — there is no Playwright inside Simulator Safari), and serves it on
127.0.0.1 until killed. Never the live agentd: a mock-only static server. When
the run closes the page POSTs the instrument's numbers to /sim-result: the
stream verdict rows (retypes, raw markers, collapse) print here as measured by
the Simulator's own WebKit, and the raw sample lands in WORK/sim-result-*.json.

  /opt/homebrew/bin/python3 tests/fixtures/smoothness/sim.py [--port=8799] [--webui=DIR]
  then: xcrun simctl openurl <udid> "http://127.0.0.1:8799/sim.html?scenario=smooth&delay=4000"
"""
from __future__ import annotations

import json
import os
import sys
from functools import partial
from http.server import ThreadingHTTPServer

import common
import metrics


class SimHandler(common.Quiet):
    """The static site, plus one POST: /sim-result writes the instrument's raw
    window.__S to WORK/sim-result-<scenario>.json and prints its verdict rows."""

    def do_POST(self) -> None:  # noqa: N802 — http.server's naming
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        scenario = self.path.partition("scenario=")[2] or "smooth"
        (common.WORK / f"sim-result-{scenario}.json").write_bytes(body)
        s = metrics.summarize(json.loads(body))
        for name, ok, detail in metrics.stream_checks(s):
            print(f"{'PASS' if ok else 'FAIL'} [sim] {name} | {json.dumps(detail)}", flush=True)
        print(f"MEASURE [sim] {scenario} = {json.dumps({k: s[k] for k in metrics.TIMING_KEYS})}", flush=True)
        self.send_response(204)
        self.end_headers()


def main(argv: list[str]) -> int:
    args = common.suite_args(argv, lambda ap: ap.add_argument("--port", type=int, default=8799))
    if args.port == 8787:
        sys.exit("never 8787: that is the live agentd")
    common.build_site(args.webui)
    for f in ("instrument.js", "sim_autorun.js"):
        os.symlink(common.HERE / f, common.SITE / f)
    probe = (common.SITE / "probe.html").read_text()
    (common.SITE / "sim.html").write_text(
        probe.replace("</body>", '  <script src="/sim_autorun.js"></script>\n</body>'))
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), partial(SimHandler, directory=str(common.SITE)))
    print(f"serving http://127.0.0.1:{args.port}/sim.html?scenario=smooth", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
