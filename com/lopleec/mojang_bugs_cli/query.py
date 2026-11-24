"""Validated filters and JQL generation for the public tracker."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta


def project_key(value: str) -> str:
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
        raise ValueError("Use a project key such as MC, MCPE, or WEB")
    return value


def issue_key(value: str, project: str = "MC") -> str:
    value = value.strip().upper()
    if value.isascii() and value.isdigit():
        value = f"{project}-{value}"
    match = re.fullmatch(r"([A-Z][A-Z0-9_]*)-([1-9][0-9]*)", value)
    if not match:
        raise ValueError("Use a bug ID such as MC-123 or a positive number such as 123")
    return f"{match[1]}-{int(match[2])}"


def id_range(value: str, project: str) -> tuple[str, str]:
    value = value.strip().upper()
    # Accept 100-200, MC-100-MC-200, MC-100-200 and MC-100..MC-200.
    match = re.fullmatch(
        r"((?:[A-Z][A-Z0-9_]*-)?[1-9][0-9]*)(?:\.\.|:|-)((?:[A-Z][A-Z0-9_]*-)?[1-9][0-9]*)",
        value,
    )
    if not match:
        key = issue_key(value, project)
        bounds = key, key
    else:
        bounds = issue_key(match[1], project), issue_key(match[2], project)
    if any(key.rsplit("-", 1)[0] != project for key in bounds):
        raise ValueError(
            f"ID ranges must belong to {project}; select another project with --project"
        )
    if int(bounds[0].rsplit("-", 1)[1]) > int(bounds[1].rsplit("-", 1)[1]):
        raise ValueError("The start ID must not exceed the end ID")
    return bounds


def date_range(values: tuple[str, str] | list[str]) -> tuple[str, str]:
    try:
        if any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in values):
            raise ValueError
        start, end = (date.fromisoformat(v) for v in values)
        if start > end:
            raise ValueError("The start date must not be after the end date")
        return start.isoformat(), (end + timedelta(days=1)).isoformat()
    except OverflowError as exc:
        raise ValueError("The end date must be before 9999-12-31") from exc
    except ValueError as exc:
        raise ValueError("Use valid YYYY-MM-DD dates with the start on or before the end") from exc


def quote(value: str) -> str:
    if not value.strip():
        raise ValueError("Filter values must not be empty")
    if any(ord(c) < 32 for c in value):
        raise ValueError("Filter values must not contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True)
class Query:
    project: str = "MC"
    text: str | None = None
    ids: str | None = None
    created: tuple[str, str] | None = None
    updated: tuple[str, str] | None = None
    statuses: tuple[str, ...] = ()
    resolutions: tuple[str, ...] = ()
    versions: tuple[str, ...] = ()
    fix_versions: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    confirmations: tuple[str, ...] = ()
    priorities: tuple[str, ...] = ()
    jql: str | None = None
    sort: str = "key"
    descending: bool = False

    def build(self) -> str:
        project = project_key(self.project)
        clauses = [f"project = {quote(project)}"]
        if self.text is not None:
            clauses.append(f"text ~ {quote(self.text)}")
        if self.ids:
            start, end = id_range(self.ids, project)
            clauses.extend([f"key >= {quote(start)}", f"key <= {quote(end)}"])
        for field, bounds in (("created", self.created), ("updated", self.updated)):
            if bounds:
                start, exclusive_end = date_range(bounds)
                clauses.extend([f"{field} >= {quote(start)}", f"{field} < {quote(exclusive_end)}"])
        for field, values in (
            ("status", self.statuses),
            ("versions", self.versions),
            ("fixVersion", self.fix_versions),
            ("labels", self.labels),
            # This public adapter requires customfield_NNNNN, rather than the
            # cf[NNNNN] spelling accepted by a regular Jira REST endpoint.
            ("customfield_10055", self.categories),
            ("customfield_10054", self.confirmations),
            ("customfield_10049", self.priorities),
        ):
            if values:
                clauses.append(f"{field} IN ({', '.join(quote(v) for v in values)})")
        if self.resolutions:
            resolved = [v for v in self.resolutions if v.lower() != "unresolved"]
            parts = []
            if resolved:
                parts.append(f"resolution IN ({', '.join(quote(v) for v in resolved)})")
            if len(resolved) < len(self.resolutions):
                parts.append("resolution IS EMPTY")
            clauses.append("(" + " OR ".join(parts) + ")")
        if self.jql is not None:
            if not self.jql.strip() or re.search(r"\border\s+by\b", self.jql, re.I):
                raise ValueError(
                    "--jql accepts conditions without ORDER BY; use --sort and --asc/--desc"
                )
            clauses.append(f"({self.jql})")
        if self.sort not in {"key", "created", "updated", "status"}:
            raise ValueError("Unsupported sort field")
        direction = "DESC" if self.descending else "ASC"
        order = f"{self.sort} {direction}"
        if self.sort != "key":
            order += f", key {direction}"
        return " AND ".join(clauses) + f" ORDER BY {order}"
