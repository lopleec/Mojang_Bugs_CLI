"""Atomic UTF-8 exports with streaming JSON and JSON Lines writers."""

from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, TextIO

from .client import BASE_URL
from .models import format_issue, normalize, parse_fields

GROUPS = (
    "status",
    "resolution",
    "version",
    "fix-version",
    "label",
    "category",
    "confirmation",
    "created-month",
)


def group_keys(issue: dict, group_by: str) -> list[str]:
    field = {
        "version": "affected_versions",
        "fix-version": "fix_versions",
        "label": "labels",
        "confirmation": "confirmation_status",
    }.get(group_by, group_by)
    value = issue.get(field)
    if group_by == "created-month":
        value = str(issue.get("created") or "")[:7]
    values = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(str(v) for v in values if v is not None and v != "")) or ["(none)"]


@contextmanager
def _atomic_output(path: Path, *, force: bool) -> Iterator[TextIO]:
    path = path.expanduser()
    if path.exists() and not force:
        raise FileExistsError(f"File already exists: {path}; use --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = stream.name
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            os.replace(temporary, path)
        else:
            # Create-if-absent protects against another writer creating the file.
            os.link(temporary, path)
            os.unlink(temporary)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def write_export(
    path: Path,
    issues: Iterable[dict],
    *,
    project: str,
    jql: str,
    limit: int | None = None,
    raw: bool = False,
    fields: tuple[str, ...] | None = None,
    group_by: str | None = None,
    force: bool = False,
    output_format: str = "json",
) -> int:
    if output_format not in {"json", "jsonl"}:
        raise ValueError("Unsupported export format")
    if group_by is not None and group_by not in GROUPS:
        raise ValueError("Unsupported grouping field")
    if group_by and output_format == "jsonl":
        raise ValueError("Grouping requires JSON format")
    if raw and fields:
        raise ValueError("Field selection cannot be combined with raw output")
    if fields is not None:
        fields = parse_fields(",".join(fields))

    count = 0
    with _atomic_output(path, force=force) as stream:
        if output_format == "jsonl":
            for issue in issues:
                json.dump(
                    format_issue(issue, raw=raw, fields=fields),
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                stream.write("\n")
                count += 1
            return count

        metadata = {
            "schema_version": 1,
            "source": BASE_URL,
            "project": project,
            "query": jql,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "limit": limit,
            "format": "raw" if raw else "normalized",
            "fields": fields,
            "group_by": group_by,
        }
        stream.write("{\n")
        for key, value in metadata.items():
            stream.write(f"  {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)},\n")
        if group_by:
            groups: dict[str, list] = defaultdict(list)
            for issue in issues:
                normalized = normalize(issue)
                record = issue if raw else normalized
                if fields:
                    record = {key: normalized[key] for key in fields}
                for key in group_keys(normalized, group_by):
                    groups[key].append(record)
                count += 1
            stream.write('  "groups": ')
            json.dump(groups, stream, ensure_ascii=False, indent=2)
        else:
            stream.write('  "issues": [')
            for issue in issues:
                stream.write(",\n" if count else "\n")
                json.dump(
                    format_issue(issue, raw=raw, fields=fields),
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
                count += 1
            stream.write("\n  ]")
        stream.write(f',\n  "count": {count}\n}}\n')
    return count
