"""Google Tasks read + write tools.

All writes are local to the user's own task lists — no outward effect — so
create/update/complete/tasklist_create are MUTATING_LOCAL.
"""

from __future__ import annotations

from .auth import get_service


def _default_tasklist(svc) -> str:
    """The id of the first task list — the implicit target when the caller
    doesn't name one, matching how tasks_list enumerates lists."""
    lists = svc.tasklists().list(maxResults=20).execute().get("items", [])
    if not lists:
        raise RuntimeError("No task lists found for this account.")
    return lists[0]["id"]


def tasklists_list() -> list[dict]:
    """List all task lists with their ids and titles."""
    svc = get_service("tasks", "v1")
    lists = svc.tasklists().list(maxResults=20).execute().get("items", [])
    return [{"id": tl["id"], "title": tl.get("title", "")} for tl in lists]


def tasks_list(show_completed: bool = False, max_results: int = 50) -> list[dict]:
    """List tasks across all task lists. By default only incomplete tasks.
    `due` is a DATE only (stored as UTC midnight): read its YYYY-MM-DD part
    as-is — do NOT pass it through convert_time, which would shift it a day
    in zones behind UTC."""
    svc = get_service("tasks", "v1")
    lists = svc.tasklists().list(maxResults=20).execute().get("items", [])
    out: list[dict] = []
    for tl in lists:
        resp = (
            svc.tasks()
            .list(
                tasklist=tl["id"],
                showCompleted=show_completed,
                maxResults=min(max_results, 100),
            )
            .execute()
        )
        for t in resp.get("items", []):
            out.append(
                {
                    "id": t["id"],
                    "list": tl.get("title", ""),
                    "title": t.get("title", ""),
                    "notes": t.get("notes", ""),
                    "due": t.get("due", ""),
                    "status": t.get("status", ""),
                }
            )
    return out


def _task_summary(t: dict, tasklist: str) -> dict:
    return {
        "id": t["id"],
        "tasklist": tasklist,
        "title": t.get("title", ""),
        "notes": t.get("notes", ""),
        "due": t.get("due", ""),
        "status": t.get("status", ""),
    }


def task_create(
    title: str, notes: str = "", due: str = "", tasklist: str | None = None
) -> dict:
    """Create a task. `due` is an RFC3339 date-time (the time is ignored by
    Google Tasks; only the date shows) — build it with convert_time
    (op='task_due') from the local date, don't hand-write it. Defaults to your
    first task list when `tasklist` (a task-list id) is omitted."""
    svc = get_service("tasks", "v1")
    tasklist = tasklist or _default_tasklist(svc)
    body: dict = {"title": title}
    if notes:
        body["notes"] = notes
    if due:
        body["due"] = due
    t = svc.tasks().insert(tasklist=tasklist, body=body).execute()
    return _task_summary(t, tasklist)


def task_update(
    task_id: str,
    title: str | None = None,
    notes: str | None = None,
    due: str | None = None,
    tasklist: str | None = None,
) -> dict:
    """Update fields of an existing task by id. Only the fields you pass are
    changed; build any `due` with convert_time (op='task_due'). Defaults to
    your first task list when `tasklist` is omitted."""
    svc = get_service("tasks", "v1")
    tasklist = tasklist or _default_tasklist(svc)
    body: dict = {}
    if title is not None:
        body["title"] = title
    if notes is not None:
        body["notes"] = notes
    if due is not None:
        body["due"] = due
    t = svc.tasks().patch(tasklist=tasklist, task=task_id, body=body).execute()
    return _task_summary(t, tasklist)


def task_complete(task_id: str, tasklist: str | None = None) -> dict:
    """Mark a task completed by id. Defaults to your first task list when
    `tasklist` is omitted."""
    svc = get_service("tasks", "v1")
    tasklist = tasklist or _default_tasklist(svc)
    t = (
        svc.tasks()
        .patch(tasklist=tasklist, task=task_id, body={"status": "completed"})
        .execute()
    )
    return _task_summary(t, tasklist)


def tasklist_create(title: str) -> dict:
    """Create a new task list."""
    svc = get_service("tasks", "v1")
    tl = svc.tasklists().insert(body={"title": title}).execute()
    return {"id": tl["id"], "title": tl.get("title", "")}
