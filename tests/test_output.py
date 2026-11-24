import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_cli import bug

from com.lopleec.mojang_bugs_cli.cli import build_parser, main
from com.lopleec.mojang_bugs_cli.client import MojangClient, MojangError
from com.lopleec.mojang_bugs_cli.display import _fit, _width, clean_text
from com.lopleec.mojang_bugs_cli.exporter import write_export
from com.lopleec.mojang_bugs_cli.models import FIELDS, normalize, parse_fields


class OutputTests(unittest.TestCase):
    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_command_name_and_english_help(self):
        self.assertEqual(build_parser().prog, "mojangbugs")
        help_text = self.invoke([])[1]
        self.assertIn("Search, inspect, and export", help_text)
        for command in ("search", "get", "export"):
            with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit):
                main([command, "--help"])
            self.assertFalse(any("\u4e00" <= c <= "\u9fff" for c in output.getvalue()))

    def test_multi_id_lookup_preserves_order_and_deduplicates(self):
        with patch.object(MojangClient, "get_issue", side_effect=lambda key: bug(key)) as fetch:
            code, out, _ = self.invoke(["get", "2", "mc-1", "MC-2", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual([i["key"] for i in json.loads(out)], ["MC-2", "MC-1"])
        self.assertEqual(fetch.call_count, 2)

    def test_failed_multi_lookup_prints_no_partial_json(self):
        with patch.object(MojangClient, "get_issue", side_effect=[bug(), MojangError("missing")]):
            code, out, err = self.invoke(["get", "1", "2", "--json"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Error: missing", err)

    def test_get_field_selection_implies_json(self):
        with patch.object(MojangClient, "get_issue", return_value=bug()):
            code, out, _ = self.invoke(["get", "1", "--fields", "key,status"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"key": "MC-1", "status": "Open"})

    def test_search_field_selection(self):
        with patch.object(MojangClient, "iter_issues", return_value=iter([bug()])):
            code, out, _ = self.invoke(["search", "--fields", "key"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [{"key": "MC-1"}])

    def test_jsonl_is_emitted_before_next_report_is_fetched(self):
        output = io.StringIO()

        def records():
            yield bug()
            self.assertEqual(json.loads(output.getvalue()), {"key": "MC-1"})
            yield bug("MC-2")

        with (
            patch.object(MojangClient, "iter_issues", return_value=records()),
            redirect_stdout(output),
        ):
            self.assertEqual(main(["search", "--jsonl", "--fields", "key"]), 0)
        self.assertEqual(
            [json.loads(line)["key"] for line in output.getvalue().splitlines()], ["MC-1", "MC-2"]
        )

    def test_get_raw_jsonl_retains_original_fields(self):
        with patch.object(MojangClient, "get_issue", return_value=bug()):
            code, out, _ = self.invoke(["get", "1", "--raw", "--jsonl"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), bug())

    def test_invalid_output_options_fail_before_network(self):
        for args in (
            ["get", "1", "--raw", "--fields", "key"],
            ["search", "--fields", "key,unknown"],
            ["export", "--format", "jsonl", "--group-by", "status"],
        ):
            with (
                self.subTest(args=args),
                patch("com.lopleec.mojang_bugs_cli.cli.MojangClient") as client,
            ):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    main(args)
                self.assertEqual(error.exception.code, 2)
                client.assert_not_called()

    def test_export_jsonl_chooses_matching_default_extension(self):
        with (
            patch.object(MojangClient, "iter_issues", return_value=iter([])),
            patch("com.lopleec.mojang_bugs_cli.cli.write_export", return_value=0) as writer,
        ):
            self.assertEqual(self.invoke(["export", "--format", "jsonl", "--quiet"])[0], 0)
        self.assertEqual(writer.call_args.args[0], Path("exports/bugs.jsonl"))

    def test_english_summary_and_empty_state(self):
        with patch.object(MojangClient, "iter_issues", return_value=iter([bug()])):
            code, out, err = self.invoke(["search"])
        self.assertEqual(code, 0)
        self.assertIn("Summary", out)
        self.assertIn("Showing 1 bug(s)", err)
        with patch.object(MojangClient, "iter_issues", return_value=iter([])):
            self.assertEqual(self.invoke(["search"])[1], "No matching bugs found.\n")


class JsonLinesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "bugs.jsonl"

    def test_jsonl_export_handles_unicode_and_embedded_newlines(self):
        issue = bug()
        issue["fields"]["description"] = "first\nsecond 你好"
        count = write_export(
            self.path,
            [issue, bug("MC-2")],
            project="MC",
            jql="query",
            output_format="jsonl",
            fields=("key", "description"),
        )
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(count, 2)
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0]), {"key": "MC-1", "description": "first\nsecond 你好"})

    def test_empty_jsonl_export_is_empty_file(self):
        self.assertEqual(
            write_export(self.path, [], project="MC", jql="query", output_format="jsonl"), 0
        )
        self.assertEqual(self.path.read_bytes(), b"")

    def test_jsonl_failure_keeps_destination(self):
        self.path.write_text("original", encoding="utf-8")

        def interrupted():
            yield bug()
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            write_export(
                self.path,
                interrupted(),
                project="MC",
                jql="query",
                output_format="jsonl",
                force=True,
            )
        self.assertEqual(self.path.read_text(), "original")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_projected_grouping_uses_unprojected_group_field(self):
        write_export(
            self.path, [bug()], project="MC", jql="query", fields=("key",), group_by="status"
        )
        result = json.loads(self.path.read_text())
        self.assertEqual(result["groups"], {"Open": [{"key": "MC-1"}]})
        self.assertEqual(result["fields"], ["key"])

    def test_invalid_export_combination_does_not_create_file(self):
        with self.assertRaises(ValueError):
            write_export(
                self.path,
                [bug()],
                project="MC",
                jql="query",
                output_format="jsonl",
                group_by="status",
            )
        self.assertFalse(self.path.exists())

    def test_projection_retains_field_order_and_rejects_empty_fields(self):
        self.assertEqual(parse_fields("status, key, status"), ("status", "key"))
        for value in ("", "key,", "missing"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_fields(value)
        self.assertEqual(tuple(normalize(bug())), FIELDS)


class TerminalTests(unittest.TestCase):
    def test_terminal_width_for_wide_and_combining_characters(self):
        for value in ("中文abc", "abcdef", "e\u0301abc", "ab中"):
            with self.subTest(value=value):
                self.assertEqual(_width(_fit(value, 4)), 4)

    def test_summary_replaces_line_breaks_without_merging_words(self):
        self.assertEqual(clean_text("first\nsecond\tthird"), "first second third")

    def test_control_sequences_are_removed(self):
        self.assertNotIn("\x1b", clean_text("\x1b[31mred"))


if __name__ == "__main__":
    unittest.main()
