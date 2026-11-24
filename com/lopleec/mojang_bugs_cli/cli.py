"""Command-line interface for searching and exporting public bug reports."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Iterable

from . import __version__
from .client import MojangClient, MojangError
from .display import clean_text, show_issue, show_search
from .exporter import GROUPS, write_export
from .models import format_issue, parse_fields
from .query import Query, issue_key, project_key


def nonnegative_int(value: str) -> int:
    try:
        number = int(value)
        if number < 0:
            raise ValueError
        return number
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a nonnegative integer") from exc


def positive_int(value: str) -> int:
    number = nonnegative_int(value)
    if number == 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def nonnegative_float(value: str) -> float:
    try:
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError
        return number
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite, nonnegative number") from exc


def field_list(value: str) -> tuple[str, ...]:
    try:
        return parse_fields(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-p",
        "--project",
        default="MC",
        metavar="KEY",
        help="project key (default: MC; e.g. MCPE or WEB)",
    )
    parser.add_argument(
        "--timeout",
        type=nonnegative_float,
        default=30,
        metavar="SECONDS",
        help="request timeout (default: 30 seconds)",
    )
    parser.add_argument(
        "--delay",
        type=nonnegative_float,
        default=0.3,
        metavar="SECONDS",
        help="minimum request interval (default: 0.3 seconds)",
    )
    parser.add_argument(
        "--retries",
        type=nonnegative_int,
        default=2,
        metavar="N",
        help="retries after temporary failures (default: 2; maximum: 10)",
    )


def output_options(parser: argparse.ArgumentParser, *, export: bool = False) -> None:
    if not export:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--json", action="store_true", help="output JSON")
        mode.add_argument("--jsonl", action="store_true", help="output one JSON object per line")
    parser.add_argument(
        "--raw", action="store_true", help="preserve original API fields and rich text"
    )
    parser.add_argument(
        "--fields",
        type=field_list,
        metavar="FIELD,...",
        help="select normalized fields, e.g. key,summary,status; implies JSON",
    )


def filter_options(parser: argparse.ArgumentParser, *, export: bool) -> None:
    connection_options(parser)
    parser.add_argument(
        "--ids",
        metavar="START-END",
        help="inclusive ID range or a single ID, e.g. 100-200 or MC-100",
    )
    parser.add_argument(
        "--created",
        nargs=2,
        metavar=("START", "END"),
        help="inclusive creation dates in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--updated",
        nargs=2,
        metavar=("START", "END"),
        help="inclusive update dates in YYYY-MM-DD format",
    )
    filters = parser.add_argument_group(
        "filters", "Different filters use AND; repeated values of one filter use OR."
    )
    for flag, description in (
        ("status", "status, e.g. Open, Resolved, or Closed"),
        ("resolution", "resolution, e.g. Fixed, Duplicate, or Unresolved"),
        ("affects-version", "affected version using its exact tracker name"),
        ("fix-version", "fix version using its exact tracker name"),
        ("label", "label"),
        ("category", "category"),
        ("confirmation", "confirmation status, e.g. Confirmed or Unconfirmed"),
        ("priority", "Mojang priority using its exact tracker value"),
    ):
        filters.add_argument(
            f"--{flag}",
            action="append",
            default=[],
            metavar="VALUE",
            help=description + " (repeatable)",
        )
    parser.add_argument(
        "--jql", metavar="CONDITION", help="additional JQL conditions without ORDER BY"
    )
    parser.add_argument(
        "--sort",
        choices=("key", "created", "updated", "status"),
        default="key" if export else "updated",
        help=f"sort field (default: {'key' if export else 'updated'})",
    )
    direction = parser.add_mutually_exclusive_group()
    direction.add_argument(
        "--desc",
        dest="descending",
        action="store_true",
        help="sort descending" + ("" if export else " (default)"),
    )
    direction.add_argument(
        "--asc",
        dest="descending",
        action="store_false",
        help="sort ascending" + (" (default)" if export else ""),
    )
    parser.set_defaults(descending=not export)
    parser.add_argument(
        "--page-size",
        type=positive_int,
        default=25,
        metavar="N",
        help="reports per page, 1–100 (default: 25)",
    )
    parser.add_argument(
        "--limit",
        type=nonnegative_int,
        default=0 if export else 20,
        metavar="N",
        help=f"maximum reports; 0 means all (default: {0 if export else 20})",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the query without fetching or writing files"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mojangbugs",
        description="Search, inspect, and export public Mojang bug reports.",
        epilog="Run mojangbugs COMMAND --help for command options.",
    )
    parser.add_argument("--version", action="version", version=f"Mojang_Bugs_CLI {__version__}")
    commands = parser.add_subparsers(dest="command", title="commands")
    search = commands.add_parser("search", help="search by keywords and filters")
    search.add_argument(
        "keywords", nargs="*", metavar="KEYWORD", help="search report titles and descriptions"
    )
    filter_options(search, export=False)
    output_options(search)

    get = commands.add_parser("get", help="look up one or more bug IDs")
    get.add_argument(
        "ids", nargs="+", metavar="BUG_ID", help="e.g. MC-123; numeric IDs use --project"
    )
    connection_options(get)
    output_options(get)

    export = commands.add_parser("export", help="export matching reports to JSON or JSON Lines")
    export.add_argument("keywords", nargs="*", metavar="KEYWORD", help="optional search keywords")
    filter_options(export, export=True)
    output_options(export, export=True)
    export.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="FILE",
        help="output path (default: exports/bugs.json or exports/bugs.jsonl)",
    )
    export.add_argument(
        "--format", choices=("json", "jsonl"), default="json", help="export format (default: json)"
    )
    export.add_argument("--group-by", choices=GROUPS, help="group reports in a JSON export")
    export.add_argument(
        "--force", action="store_true", help="replace an existing file after a successful export"
    )
    export.add_argument(
        "-q", "--quiet", action="store_true", help="suppress progress and completion messages"
    )
    return parser


def make_query(args: argparse.Namespace) -> str:
    return Query(
        project=args.project,
        text=" ".join(args.keywords) if args.keywords else None,
        ids=args.ids,
        created=args.created,
        updated=args.updated,
        statuses=tuple(args.status),
        resolutions=tuple(args.resolution),
        versions=tuple(args.affects_version),
        fix_versions=tuple(args.fix_version),
        labels=tuple(args.label),
        categories=tuple(args.category),
        confirmations=tuple(args.confirmation),
        priorities=tuple(args.priority),
        jql=args.jql,
        sort=args.sort,
        descending=args.descending,
    ).build()


def print_json(issues: Iterable[dict], args: argparse.Namespace, *, single: bool = False) -> None:
    records = (format_issue(i, raw=args.raw, fields=args.fields) for i in issues)
    if args.jsonl:
        for record in records:
            print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    else:
        data = next(records) if single else list(records)
        print(json.dumps(data, ensure_ascii=False, indent=2))


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    args.project = project_key(args.project)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    if args.retries > 10:
        parser.error("--retries must not exceed 10")
    if args.raw and args.fields:
        parser.error("--fields cannot be combined with --raw")
    if args.command == "get":
        keys = tuple(dict.fromkeys(issue_key(value, args.project) for value in args.ids))
    else:
        if args.page_size > 100:
            parser.error("--page-size must be between 1 and 100")
        if args.command == "export" and args.format == "jsonl" and args.group_by:
            parser.error("--group-by requires --format json")
        jql = make_query(args)
        if args.dry_run:
            print(jql)
            return 0
    client = MojangClient(timeout=args.timeout, delay=args.delay, retries=args.retries)
    if args.command == "get":
        # Resolve all requested reports before printing to avoid partial JSON.
        issues = [client.get_issue(key) for key in keys]
        if args.json or args.jsonl or args.raw or args.fields:
            print_json(issues, args, single=len(keys) == 1)
        else:
            for index, issue in enumerate(issues):
                if index:
                    print()
                show_issue(issue)
        return 0

    limit = args.limit or None
    progress = None
    if args.command == "export" and not args.quiet:

        def progress(page: int, count: int) -> None:
            print(f"Fetched {count} bug(s) (page {page})", file=sys.stderr)

    issues = client.iter_issues(
        args.project, jql, page_size=args.page_size, limit=limit, progress=progress
    )
    if args.command == "search":
        if args.json or args.jsonl or args.raw or args.fields:
            print_json(issues, args)
        else:
            show_search(list(issues), limit)
    else:
        path = args.output or Path(f"exports/bugs.{args.format}")
        count = write_export(
            path,
            issues,
            project=args.project,
            jql=jql,
            limit=limit,
            raw=args.raw,
            fields=args.fields,
            group_by=args.group_by,
            force=args.force,
            output_format=args.format,
        )
        if not args.quiet:
            print(f"Exported {count} bug(s) to {path.expanduser().resolve()}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        return run(args, parser)
    except ValueError as exc:
        parser.error(str(exc))
    except BrokenPipeError:
        sys.stdout = open(os.devnull, "w")
        return 0
    except (MojangError, OSError) as exc:
        print(f"Error: {clean_text(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "\nCancelled. Incomplete exports do not replace the destination file.", file=sys.stderr
        )
        return 130
    return 2
