"""Google Sheets read/write tools.

Reads pull structured values from a range; writes (create, append, update)
land in the user's own My Drive spreadsheets — no sharing, no outward effect —
so they are MUTATING_LOCAL. The model only ever passes A1 ranges and row
values; nothing here reaches beyond the user's own account.
"""

from __future__ import annotations

from .auth import get_service

SHEET_URL = "https://docs.google.com/spreadsheets/d/{id}/edit"


def sheets_create(title: str) -> dict:
    """Create a new, empty Google Sheets spreadsheet in My Drive. Returns its
    id and edit url."""
    svc = get_service("sheets", "v4")
    sheet = (
        svc.spreadsheets()
        .create(body={"properties": {"title": title}}, fields="spreadsheetId")
        .execute()
    )
    sheet_id = sheet["spreadsheetId"]
    return {"id": sheet_id, "url": SHEET_URL.format(id=sheet_id)}


def sheets_read(spreadsheet_id: str, range: str = "") -> dict:  # noqa: A002
    """Read a range of cells from a spreadsheet by id. `range` is A1 notation
    (e.g. "Sheet1!A1:D20", or a bare tab name like "Sheet1"); leave empty to
    read the first tab. Returns the rows as a list of lists."""
    svc = get_service("sheets", "v4")
    rng = range or "Sheet1"
    resp = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=rng)
        .execute()
    )
    return {"id": spreadsheet_id, "range": rng, "values": resp.get("values", [])}


def sheets_append(
    spreadsheet_id: str, values: list[list], range: str = "Sheet1"  # noqa: A002
) -> dict:
    """Append rows to the end of a sheet's existing table. `values` is a list
    of rows, each a list of cell values; `range` selects the tab (default the
    first tab). Values are parsed like user entry (dates and formulas work).
    The safe, additive write — use this for logging/appending data."""
    svc = get_service("sheets", "v4")
    resp = (
        svc.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=range,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        )
        .execute()
    )
    updates = resp.get("updates", {})
    return {
        "id": spreadsheet_id,
        "updated_range": updates.get("updatedRange"),
        "rows_appended": updates.get("updatedRows", 0),
    }


def sheets_update(spreadsheet_id: str, range: str, values: list[list]) -> dict:  # noqa: A002
    """Overwrite a specific range of cells with `values` (a list of rows).
    This OVERWRITES whatever is currently in `range` in place (like
    edit_file / docs_edit_text) — prefer sheets_append when you only
    want to add rows. Values are parsed like user entry."""
    svc = get_service("sheets", "v4")
    resp = (
        svc.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=range,
            valueInputOption="USER_ENTERED",
            body={"values": values},
        )
        .execute()
    )
    return {
        "id": spreadsheet_id,
        "updated_range": resp.get("updatedRange"),
        "cells_updated": resp.get("updatedCells", 0),
    }
