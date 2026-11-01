# Chronicle

## Level 2 — Wanderers of the Azure Beacon — 2026-10-13

Four months of building, from the first commit (June 2026) to today. In that time
Sidekick went from a terminal assistant to a daemon that runs every day: it watches
Gmail, runs scheduled jobs, streams to an iPad and an Android phone, browses with a real
Chrome, and runs code in a locked-down sandbox. Along the way it moved from a Windows/WSL
box to a Mac mini. Its main brain is now a local Qwen model, with Apple's on-device model
handling the small jobs. The cloud fallback was deleted, and Sonnet survives only as an
opt-in per chat.

The bugs and traps conquered were mostly the quiet kind. Phone notifications died
silently three separate times: a push setting that dropped messages, a lost subscription,
and a placeholder email that Apple rejects but Android accepts. A web-page split shipped
with one missing import, which quietly moved every chat onto the backup stream. Chats
dropped when the PWA was backgrounded. The browser lane, it turned out, had never had
browser control at all. The Apple model refused to read some ordinary emails until they were
wrapped in a tag that said "email". The local model's cache was being thrown away every
turn because of a cloud-only optimisation. And one restart killed the very AI session
that issued it.

What the player learned: the security boundary is the policy engine, not the OAuth
scope. A background pipeline loses to the model just reading the source live (open-loops
was rolled back). Irreversible actions belong behind a plain confirm, never a clever
timer. A disconnected phone should detach a chat, never cancel it. "Feel" decisions get
settled with a live demo. And a check that can't fail loudly is worse than no check.

Notes: this was the first rest, so the whole inbox was processed (99 entries). Junk
flagged, since routine work is git history and not a learning: the Google Workspace write
phase (06-16), Gmail attachments (06-25), Web UI v2 (06-28), the v3 and post-review
session logs (07-03, 07-04), the Lane B v3 build summary (07-17) and the Mac mini cleanup
(09-28). Each carried one or two real lessons, now distilled, under build narration.
Obsolete (WSL/systemd, js_fetch, social cross-posting, Ollama-on-WSL) and distilled to
nothing. Pruned: three superseded decisions (social cross-post 08-03, Sheets scope,
gmail.compose scope) and the stale "smoke test calls the real Anthropic API" wording.
Amended: the 09-28 main-model decision (utilities now on Apple FM). Compacted: 94 gotchas,
31 decisions and 13 architecture notes, with their full text in
.claude/learnings-archive.md.

## Level 3 — Vanguards of the Gilded Vault — 2026-10-22

Nine days this level, and they were busy: chat learned to see and read (image
attachments with a lightbox, then documents read on demand), the phone PWA
learned to come back from the dead correctly (the pwa-accurate-resume quest:
every run event numbered, a per-run replay ring, one messages+live join),
and the context compactor was rebuilt from the ground up — a frozen opening,
a persisted byte-stable checkpoint, and a blue seam in the transcript.
Ollama came back for memory vectors; the system prompt got trimmed; the
Settings panes got a glitch sweep.

The biggest conquest was the compaction checkpoint quest: an over-budget
session had been re-summarizing and re-prefilling on EVERY turn — a "Nice
job!" reply costing 4m14s and a cache hit of 3 tokens. The party shipped a
persisted three-part context and measured the result live: one compaction
per budget crossing (was one per turn), post-compaction turns in 2.7–4.4
seconds, cache reads ≈ 22k — the whole kept prefix. Along the way the
reviews caught a first-turn crash, a compaction summary that could launder
an attacker's destination into the egress ledger, a summarizer that echoed
"ack" and got persisted as truth, and a divider line that existed only in
its own stylesheet.

What the player learned: a probe mock must model the store's TIMING, not
just its shape (the one design that passed every assertion would have wiped
the in-flight answer). A capped list is a data source, not a fact about
existence. Green is not honest — fixtures can sit on the failed path,
sync-pins prove agreement not application, and a CSS-only feature is
invisible to node assertions. Verify reports against the diff; the fiction
in the report becomes the fiction in the README. And on a shared one-slot
model server, a measurement is only trustworthy when the whole sequence
runs in one script with a warm-slot bookmark.

Notes: junk flagged — the image-attachments build narration (10-14) and the
"dirty decisions.md" bookkeeping (10-19); routine work is git history.
Pruned: one fully-superseded decision (10-16, `.subtab` overflow, superseded
the same day it landed). Compacted with full text kept in the archive: four
gotchas (card-swing, iOS-resume, resume-seq-dedupe, resume-run-identity) and
three decisions (iOS resume flash, server-contract-first, run-identity).
Distilled: ~18 new gotcha lines, one architecture clause (compaction
one-time cost). Archived: all 28 inbox entries, verbatim; the inbox is empty.

## Level 4 — Wardens of the Silent Summit — 2026-10-30

Eight days this level. The chat screen got a proper brain for its own life cycle:
one module (runstate.js) now decides what a running chat is doing, instead of three
files each keeping their own guess, and it has its own fast offline test tier that
runs the real web code in a headless browser. Scrolling learned to leave a reader
alone (BK-017): reading back up through a long answer no longer gets yanked to the
bottom. The iPad got a debug icon (BK-022) that switches on an on-device trace the
normal icon can't reach. One big experiment was built and then deliberately taken
back out: letting the agent look at web pages as pictures (BK-001) worked, but it
was too heavy for the local model, so it was reverted. The Gmail HTML-body
improvement from the same batch stayed.

What was conquered: a browser "clamp" that a first probe had declared impossible,
until a better-built probe caught it on both engines; a CSS line justified as
"Chrome-only" that turned out to matter on Safari too; a test harness that could go
green while checking nothing; a test that could be fooled by a commented-out tag;
and, after the revert, the trap that old saved chats still carried the removed
field and would have crashed on reopen.

What the player learned: "ruled out" only means "my test couldn't build that case".
Evidence is only good for the code it ran on. A safety control has to exercise
every check it vouches for. And a party member's report, or a decision's own
justification, is a claim to verify against the tree, not a fact.

Notes: this rest also moved the party's trap list into the code. gotchas.md went
from 46,457 to 3,907 characters: 197 entries left it (86 into test failure
messages, 93 into code comments, 18 general lessons to the archive only), leaving
14 tooling and host-ops traps no code owns. The full text of every moved entry is
kept in the archive with its new home. decisions.md was compacted from 30,738 to
about 16.9k characters (44 entries shortened or pruned, including the six reverted
BK-001 rulings, now one line, and the closed BK-019 "stays open" note);
architecture.md went from 9.3k to 7.5k. No junk flagged: the inbox was
general-lesson heavy (11 of 27 entries were archive-only) but every entry was a real
learning. Archived: all 27 inbox entries, verbatim; the inbox is empty.
