/* Simulator pass (sim.py): no Playwright to click Send inside Simulator Safari,
   so this classic script waits for the probe page to boot, injects
   instrument.js (the scenarios live there), arms ?scenario= (default smooth)
   and sends after ?delay= ms (default 4000: time to start recordVideo). Once
   the run closes it POSTs the instrument's raw numbers to sim.py's
   /sim-result, so the Simulator's own WebKit reports retypes/raw/collapse. */
(() => {
  const q = new URLSearchParams(location.search);
  const scenario = q.get("scenario") || "smooth", delay = Number(q.get("delay") || 4000);
  const go = () => {
    if (!(window.__probe && window.__probe.ready)) return setTimeout(go, 100);
    const s = document.createElement("script");
    s.src = "/instrument.js";
    s.onload = () => setTimeout(() => {
      window.__probe.arm(scenario, "sid-" + scenario);
      document.querySelector("#chat-input").value = "go";
      document.querySelector("#chat-send").click();
      const done = () => {
        if (!window.__mock.runClosed) return setTimeout(done, 250);
        setTimeout(() => { window.__S.on = false;
          fetch("/sim-result?scenario=" + scenario, { method: "POST", body: JSON.stringify(window.__S) }); }, 1500);
      };
      done();
    }, delay);
    document.head.appendChild(s);
  };
  go();
})();
