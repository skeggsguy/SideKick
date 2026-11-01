# Lane B v3 — high-level design note

*As built, 2026-07-17 (branch feat/lane-b-v3). Brief; the full v2 design and
v3 envelope note were removed in the 2026-09-28 cleanup — recover with
`git show da386d5:docs/plans/lane-b-interactive-browser.md`.*

## What it does

`browser_delegate` hands a whole browsing task to an autonomous sub-agent bound
to ONE website. You approve the **delegation envelope** once at spawn — origin,
task, action budget, and which saved identity refs it may fill — and the child
then navigates, clicks, and types on its own inside that envelope, over the
same persistent Chrome as `browser_fetch`. Three first-class outcomes: **data**
("these slots are free"), **deep link** (you finish from the URL — expected to
dominate), **completed**. The six low-level tools (navigate / snapshot / read /
click / type / scroll) exist ONLY inside the child's registry — envelope-only;
there is no way to drive the browser from the main catalog.

## Process (one delegation)

1. Main agent calls `browser_delegate(origin, task, refs, budget)` — the call
   is OUTWARD, so it always prompts; the args ARE the approval card.
2. A fresh child agent (main model tier) gets a fresh tab, the six tools, and a
   policy engine carrying the envelope. It sees only its brief (task + envelope
   terms) — no repo instructions, no parent conversation.
3. Inside the envelope every action auto-runs, decrements the budget (page
   actions only: navigate/click/type — snapshots/reads are free), and writes an
   audit row. Re-prompts fire ONLY at machinery-detected boundaries:
   - **origin change** — tab drifted or navigate targets an off-envelope host
   - **first ref use per site** — the card shows the real value being filled
   - **budget exhausted** — every further action re-prompts, never extends
   Boundary prompts go to the same human via the normal approvals machinery
   (deferred approvals + push in the daemon, y/n in the CLI), attributed to the
   parent run. A human-approved boundary audits as "approved", never "auto".
4. The child's report returns framed as untrusted quoted material with the
   action count. A challenge/anti-bot page ends the run as `blocked` and leaves
   the tab open in the headed window for you.

## Security model

- **Authority moved, not removed.** The per-action OUTWARD prompts of v2 became
  one informed decision at spawn + mechanical boundaries — all enforced in
  core/policy.py, with per-action audit rows intact.
- **Capability beats instruction.** Empty `refs` makes filling contact details
  mechanically impossible (hard deny, approver never consulted). There is no
  card ref and no login, ever — payment/login walls stall at unfillable fields
  regardless of what a page tells the model. No submit gate exists by design:
  a mechanical one is impossible and a declared one is injectable.
- **Ref values never enter the model.** The child types the literal `{email}`
  token; the real value (ClientConfigStore) is substituted only inside
  browser_type's stack frame at CDP dispatch. Args, traces, audit, session
  history all keep the placeholder (leak-pinned in tests). The resolver
  re-checks the live tab host at dispatch (TOCTOU close); policy is the sole
  granter of ref-approved hosts.
- **Origin is a boundary, not advice.** Navigate targets are pre-checked and
  the tab's actual host (via Page.frameNavigated) gates the NEXT action after
  any redirect/link drift. The chrome-proxy egress lock and the _netguard
  SSRF pre-check remain the network backstops underneath.
- **Isolation is blast radius, not privilege.** The child's policy.check runs
  on every call; a subagent can do nothing its envelope doesn't grant. An
  internal-probe refusal inside the child taints the PARENT turn (the report
  rides back as an InternalRefusalNote), and secret-read-tainted parent turns
  can't spawn a child at all (browser_delegate is in EGRESS_TOOLS).
- **Worst case, accepted:** a wrong guest booking against your real email/
  phone — a no-show mark, not money. That ceiling (no login, no card) is what
  makes autonomy inside the envelope proportionate.
