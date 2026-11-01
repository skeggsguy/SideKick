---
tools: current_time, convert_time, calendar_list_events, calendar_get_event, gmail_search, gmail_read, tasks_list, calendar_event_create, task_create, recall, remember, rss_fetch, reddit_comments, hn_comments
---

Produce my morning briefing. Gather, in parallel where you can:

1. **Calendar** — today's events (and tomorrow morning's, if today is nearly
   over), via calendar_list_events.
2. **Email** — unread mail from the last 2 days (gmail_search with
   `is:unread newer_than:2d`). Read anything that looks urgent or actionable;
   ignore newsletters and notifications.
3. **Tasks** — open tasks via tasks_list; flag anything due today or overdue.
4. **Feeds** — if you have RSS feeds configured (feed URLs given in these
   instructions), fetch them with rss_fetch and include 1–2 interesting
   headlines. For posts worth a deeper look, pull top comments with
   reddit_comments or hn_comments.

Before stating any meeting time, pass every calendar start/end through
convert_time (op='to_local') in one batch and quote the local labels it
returns — never read the raw RFC3339 offset yourself.

Then write the briefing:

- Lead with the single most important thing today.
- Then: schedule at a glance, mail needing a reply or action (one line each,
  with sender), tasks due.
- Keep it tight — under 200 words. Plain prose and short lists, no headers.
- If a section is empty, one line says so ("Inbox: nothing needs you.").
- If this run is marked as a late catch-up, open with what was missed while
  the machine was asleep.
