# Smoothness device checklist (BK-033)

One pass on the **iPad** and one on the **iPhone**, both as the home-screen PWA. Each item is one
action and one expected result. Tick it in BK-033's Notes, or say what you saw instead.

**First, reload the PWA.** agentd serves the web UI straight from the working tree, but a
home-screen app keeps the page it last loaded until it is relaunched. Swipe the app away in the
app switcher and open it again, on each device. If an item still looks like the old behaviour,
relaunch once more before you report it.

Use a chat where the model writes a long answer with a heading, a list, a code block and a
table. For example: "Explain SQLite WAL mode with a heading, a bulleted list, a Python code block
and a 10-row comparison table." Ask for thinking, or use a model that thinks.

| # | BK | Do this | Expect |
|---|---|---|---|
| 1 | BK-026 | Tap a tool pill, a link in an answer, and the thinking header ("Thought for …"). | No dark grey square flashes under your finger. A light pressed tint is fine. |
| 2 | BK-027 | Schedules → **Fire now** on a short job (or wait for a cron run), open its chat, then lock the iPad before the run finishes. Unlock after it has finished. | The finished answer is on screen within about 2 s of unlocking, and you never had to switch chats. |
| 3 | BK-028 | Send the long markdown prompt above and watch the answer stream. | Nothing flips. A table never shows as raw `\| a \| b \|` text first, `**bold**` never shows its stars, and the code block never turns into plain text and back. |
| 4 | BK-028 | While it streams, swipe the code block sideways, or select a word in an earlier paragraph. | The code block stays where you swiped it, and the selection stays put until the end. |
| 5 | BK-029 | While an answer streams and follows the bottom, tap once anywhere on the text. | Following stops at once and "↓ Latest" appears. One tap on "↓ Latest" follows again. |
| 6 | BK-029 | While an answer streams, open a tool pill or the thinking header. | Following pauses with "↓ Latest", so what you opened stays in view. |
| 7 | BK-030 | Watch a reply that thinks first, until the answer starts. | The thinking strip stays the same height (shimmer lines) and doesn't jump when thinking ends. |
| 8 | BK-030 | Tap the thinking header open, then closed. Do the same on a tool pill. | Each one slides open and closed (about 0.2 s), and the header you tapped stays under your finger. |
| 9 | BK-031 | Open a tool pill with a long result, put your finger on the result and swipe up. | The whole chat scrolls. The result box never scrolls by itself. |
| 10 | BK-031 | Tap "Show all" under a long result or long thinking, then "Show less". | Show all shows the whole text in place. Show less shrinks it back, and the button stays under your finger. |
| 11 | BK-034 | Start a long reply on the iPad. On the iPhone, open the same chat while it streams. | The iPhone shows **Stop**. Tap it: both devices go back to Send and show "Stopped". |
| 12 | BK-035 | Get a tool call to fail (e.g. ask it to fetch `https://example.invalid`). | The failed pill is red, "✗ failed", and **closed**. It opens only when you tap it, closes when you tap again, and stays closed after you switch chats and back. |
| 13 | BK-036 | Send a task that runs `browser_delegate` for several minutes, and approve its prompts from the notifications. | **Stop** stays on the sending device the whole time, until the run ends. |
| 14 | BK-032 | After a normal day of iPad use with sleeps and wakes, run `grep '\[events\] page' ~/Library/Logs/harness/agentd.log` on the Mac mini. | Each page holds at most 1 live stream. Any `while N live` line with N > 1 is followed by `closed n=<old> after 0.0s`. |

When every row passes on both devices, BK-033's "Goal 8: real iOS" criterion can be ticked, and
BK-032 can close on row 14.
