/* suite.js — the jsunit tier's entry module: imports every test module (each
   self-registers via runner.js) and runs them ONCE. Adding a test FILE is:
   create tests/fixtures/jsunit/<module>.test.js, import it here, add it to
   jsc_check.js's list. (run_jsunit.py symlinks *.test.js automatically; the
   import list stays explicit on purpose — a dropped import reads as a
   missing suite, not a silently skipped one.) */
import { runAll } from "./runner.js";
import "./replay.test.js";
import "./runstate.test.js"; // BK-019 Step 3: the run-lifecycle transition table
import "./trace.test.js"; // BK-023 Step 2: the ?recoverdebug capture formatter
import "./observe.test.js"; // BK-034: Stop for a run another device started
import "./senderstop.test.js"; // BK-036: the sender's own Stop survives a silent stream end
import "./labels.test.js"; // BK-041: the activity row's words
import "./stream_md.test.js"; // BK-028: the streaming markdown renderer (needs real marked + DOMPurify)
runAll();
