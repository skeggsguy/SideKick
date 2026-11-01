---
tools: recall, remember, forget, schedule_list
---

Weekly memory hygiene. Review long-term memory and prune it so recall stays
sharp. Work only with the remember/recall/forget tools. This runs in two
phases: you PRESENT proposed changes and stop, then APPLY them only after the
user has reviewed and confirmed.

## Phase 1 — Review (this run)

1. Call recall with no query to list active facts. The listing is paged (100
   facts per call): if the output ends with "showing X–Y of N active facts —
   call recall again with offset=Y", keep calling recall with that offset
   until no such footer remains. You must see all N facts before proposing
   anything — the oldest facts, the likeliest prune candidates, are on the
   LAST pages.
2. Decide what to merge or drop, collecting the drops into a single list. Look
   for:
   - duplicates and near-duplicates → keep the best-phrased one, drop the rest
     (if two facts each hold part of the detail, plan to remember one merged
     fact and drop both originals);
   - contradictions → keep the newer fact, drop the older;
   - stale facts (past deadlines, finished projects, one-off context that
     never mattered) → drop;
   - aged facts, using the "saved" date in the recall output — propose for
     retirement anything older than roughly: project ~3 months, routine
     ~6 months, contact/preference/interest ~12 months, procedure/ops-lesson
     ~6–12 months — or immediately if contradicted by newer experience or the
     tool/file/list it references no longer exists (profile facts are exempt —
     who the user is doesn't age out). job-config facts don't age by date: call
     schedule_list and check whether the job they configure still exists,
     proposing retirement when it doesn't. Old is a reason to ASK, not proof
     it's wrong — propose, and let the user decide;
   - miscategorized facts: anything in profile/preference that is really a job
     rule, procedure, interest, or tool lesson → propose moving it to the right
     category (profile/preference are injected into every session, so strays
     there crowd out real identity/behavior facts);
   - the always-on cap: profile + preference together are injected into every
     session, but only the top 20 facts (~2400 chars) fit — anything over the
     cap is SILENTLY never injected. Count the two categories in the listing;
     if they exceed 20 facts, propose enough recategorizations, merges, and
     retirements to bring them under, keeping the facts that genuinely earn a
     place in every session;
   - as a secondary signal, the "last recalled" date: a fact recalled
     recently is earning its keep even if old; one saved months ago and
     never recalled since is a stronger retirement candidate. (This field
     starts as "never" for all facts — it only becomes meaningful once
     recall data has accumulated for a few weeks.)
3. Be conservative: when unsure whether a fact is stale, keep it.
4. PRESENT the full proposal and STOP. Do NOT call forget, and do NOT call
   remember for merges yet — nothing is changed in this run. Write out:
   - each drop, one per line, as `[id] "content" — reason` (e.g.
     `[7] "The Q2 review is on the 14th" — past deadline`);
   - each merge as the originals you'll drop plus the single merged fact you'll
     write in their place.
   End by asking the user to confirm (e.g. "Reply *go ahead* to apply, or tell
   me which to keep"). This presentation may be pushed as a notification, so
   keep it tight.
5. If there is nothing to merge or drop, just say so and stop — there is no
   Phase 2.

## Phase 2 — Apply (after the user confirms)

6. Only once the user has approved — and incorporating any amendments they made
   (facts to keep, ids to add) — apply the changes: perform merges first
   (remember the merged fact), then call forget ONCE with the full list of ids
   (e.g. forget(fact_ids=[3, 7, 12])) — never one id per call. The forget
   approval shows bare ids, but the content was already laid out in Phase 1, so
   the context is in the chat.
7. Finish with a 2–4 line summary: how many facts reviewed, merged, forgotten,
   and anything that looked odd. Keep it short.
