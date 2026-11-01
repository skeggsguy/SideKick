---
tools: current_time, run_stats
---

Weekly cost report. Tell me what running you cost this week and whether
anything looks off.

1. Call run_stats for the last 7 days, and again for 30 days for context.
2. Write the report:
   - Lead with the week's total cost and how it compares to the monthly
     run-rate (computed from the 30-day numbers).
   - The top 2–3 sources of spend, one line each.
   - Flag anomalies: error counts above a handful, a source that suddenly
     dominates, or a cache-read share below ~50% on a normal week (that
     usually means something is invalidating the prompt-cache prefix and
     inputs are costing full price).
3. If nothing is unusual, say so in one line — don't invent concerns.
4. Keep it under 120 words; it may be pushed as a notification.
