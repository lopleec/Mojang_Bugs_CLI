"""Readable terminal output with bounded-width columns."""

from __future__ import annotations

import re
import shutil
import sys
import unicodedata

from .models import normalize


def clean_text(value: object, *, multiline: bool = False) -> str:
    text = str(value if value is not None else "—")
    text = "".join(
        c if not c.isspace() or multiline else " "
        for c in text
        if not unicodedata.category(c).startswith("C") or c in "\n\t"
    )
    return text if multiline else re.sub(r"\s+", " ", text)


def _cell_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in "WF" else 1


def _width(text: str) -> int:
    return sum(_cell_width(char) for char in text)


def _fit(text: str, width: int) -> str:
    used = 0
    for index, char in enumerate(text):
        used += _cell_width(char)
        if used > width:
            prefix = text[:index]
            prefix_width = used - _cell_width(char)
            while prefix and prefix_width > width - 1:
                prefix_width -= _cell_width(prefix[-1])
                prefix = prefix[:-1]
            return prefix + "…" + " " * (width - prefix_width - 1)
    return text + " " * (width - used)


def show_search(issues: list[dict], limit: int | None) -> None:
    if not issues:
        print("No matching bugs found.")
        return
    # Avoid parsing descriptions and links for a four-column summary.
    rows = [
        (
            i["key"],
            i["fields"].get("status"),
            i["fields"].get("updated"),
            i["fields"].get("summary"),
        )
        for i in issues
    ]
    key_width = min(20, max(8, max(_width(clean_text(row[0])) for row in rows)))
    terminal = shutil.get_terminal_size((110, 24)).columns
    summary_width = max(20, terminal - key_width - 16 - 10 - 6)
    widths = (key_width, 16, 10, summary_width)
    print(
        "  ".join(
            _fit(s, w) for s, w in zip(("ID", "Status", "Updated", "Summary"), widths)
        ).rstrip()
    )
    for key, status, updated, summary in rows:
        status = status.get("name") if isinstance(status, dict) else status
        cells = (key, status, (updated or "")[:10], summary)
        print("  ".join(_fit(clean_text(s), w) for s, w in zip(cells, widths)).rstrip())
    message = f"Showing {len(rows)} bug(s)"
    if limit is not None and len(rows) == limit:
        message += f" (limit: {limit}; use --limit 0 for all matches)"
    print(message, file=sys.stderr)


def show_issue(issue: dict) -> None:
    data = normalize(issue)
    print(f"{data['key']}  {clean_text(data['summary'])}")
    for label, field in (
        ("Status", "status"),
        ("Resolution", "resolution"),
        ("Confirmation", "confirmation_status"),
        ("Category", "category"),
        ("Created", "created"),
        ("Updated", "updated"),
        ("Affected versions", "affected_versions"),
        ("Fix versions", "fix_versions"),
        ("Labels", "labels"),
    ):
        value = data[field]
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value) or None
        print(f"{label}: {clean_text(value)}")
    print(data["url"])
    if data["description"]:
        print("\n" + clean_text(data["description"], multiline=True))
    if data["attachments"]:
        print(f"\nAttachments: {len(data['attachments'])} (use --json for links)")
