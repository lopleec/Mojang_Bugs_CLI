"""Convert Jira's rich text and field objects into useful JSON."""

from __future__ import annotations

from .client import BASE_URL

FIELDS = (
    "key",
    "jira_id",
    "project",
    "url",
    "summary",
    "description",
    "status",
    "resolution",
    "confirmation_status",
    "category",
    "mojang_priority",
    "created",
    "updated",
    "resolved",
    "affected_versions",
    "fix_versions",
    "labels",
    "votes",
    "attachments",
    "links",
)


def parse_fields(value: str) -> tuple[str, ...]:
    fields = tuple(dict.fromkeys(part.strip() for part in value.split(",")))
    if not all(fields):
        raise ValueError("Use comma-separated field names without empty entries, e.g. key,summary")
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise ValueError(
            "Unknown fields: " + ", ".join(sorted(unknown)) + ". Available: " + ", ".join(FIELDS)
        )
    return fields


def format_issue(issue: dict, *, raw: bool = False, fields: tuple[str, ...] | None = None) -> dict:
    if raw:
        return issue
    data = normalize(issue)
    return {name: data[name] for name in fields} if fields else data


def rich_text(node: object) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(rich_text(item) for item in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    if kind == "text":
        return str(node.get("text", ""))
    if kind == "hardBreak":
        return "\n"
    if kind in {"inlineCard", "blockCard"}:
        return str(attrs.get("url", "")) + ("\n" if kind == "blockCard" else "")
    if kind == "mention":
        return str(attrs.get("text", ""))
    if kind == "emoji":
        return str(attrs.get("text") or attrs.get("shortName") or "")
    if kind == "media":
        return str(attrs.get("url") or attrs.get("alt") or "[attachment]")
    if kind == "rule":
        return "\n---\n"
    text = rich_text(node.get("content", []))
    if kind in {"paragraph", "heading", "codeBlock", "blockquote", "tableRow", "mediaSingle"}:
        return text.rstrip() + "\n"
    if kind == "listItem":
        return "- " + text.strip() + "\n"
    if kind in {"tableCell", "tableHeader"}:
        return text.strip() + "\t"
    return text


def field_value(value: object) -> object:
    if isinstance(value, dict):
        for key in ("name", "value", "displayName"):
            if key in value:
                return value[key]
        return value
    if isinstance(value, list):
        return [field_value(v) for v in value]
    return value


def normalize(issue: dict) -> dict:
    fields = issue["fields"]
    key = issue["key"]
    project = key.rsplit("-", 1)[0]
    links = []
    for link in fields.get("issuelinks") or []:
        for direction, relation in (("inwardIssue", "inward"), ("outwardIssue", "outward")):
            target = link.get(direction)
            if target:
                links.append(
                    {
                        "key": target.get("key"),
                        "relation": (link.get("type") or {}).get(relation),
                        "summary": (target.get("fields") or {}).get("summary"),
                    }
                )
    attachments = [
        {
            "id": a.get("id"),
            "filename": a.get("filename"),
            "size": a.get("size"),
            "mime_type": a.get("mimeType"),
            "url": f"{BASE_URL}/api/issue-attachment-get?attachmentId={a['id']}",
        }
        for a in fields.get("attachment") or []
        if a.get("id")
    ]
    return {
        "key": key,
        "jira_id": issue.get("id"),
        "project": project,
        "url": f"{BASE_URL}/browse/{project}/issues/{key}",
        "summary": fields.get("summary", ""),
        "description": rich_text(fields.get("description")).strip(),
        "status": field_value(fields.get("status")),
        "resolution": field_value(fields.get("resolution")),
        "confirmation_status": field_value(fields.get("customfield_10054")),
        "category": field_value(fields.get("customfield_10055")),
        "mojang_priority": field_value(fields.get("customfield_10049")),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "resolved": fields.get("resolutiondate"),
        "affected_versions": field_value(fields.get("versions") or []),
        "fix_versions": field_value(fields.get("fixVersions") or []),
        "labels": fields.get("labels") or [],
        "votes": fields.get("customfield_10070"),
        "attachments": attachments,
        "links": links,
    }
