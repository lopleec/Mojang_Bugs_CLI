import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from com.lopleec.mojang_bugs_cli.cli import main
from com.lopleec.mojang_bugs_cli.client import MojangClient, MojangError, _retry_after
from com.lopleec.mojang_bugs_cli.exporter import write_export
from com.lopleec.mojang_bugs_cli.models import normalize, rich_text
from com.lopleec.mojang_bugs_cli.query import Query, id_range, issue_key


def bug(key="MC-1", *, status="Open", versions=None):
    return {
        "id": "123",
        "key": key,
        "fields": {
            "summary": "中文 Zombie bug",
            "description": {
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "你好"}]}],
            },
            "status": {"name": status},
            "resolution": None,
            "created": "2026-01-31T23:59:00.000+0000",
            "versions": [{"name": v} for v in (versions or [])],
        },
    }


def page(items, *, number=0, more=False):
    return {"issues": items, "pagination": {"currentPage": number, "hasNextPage": more}}


class QueryTests(unittest.TestCase):
    def test_id_forms_and_normalization(self):
        for value in ("100-200", "MC-100-MC-200", "MC-100-200", "mc-100..mc-200"):
            with self.subTest(value=value):
                self.assertEqual(id_range(value, "MC"), ("MC-100", "MC-200"))
        self.assertEqual(id_range("MC-123", "MC"), ("MC-123", "MC-123"))
        self.assertEqual(issue_key("12", "MCPE"), "MCPE-12")

    def test_invalid_ranges_and_injection(self):
        for value in ("20-10", "MCPE-1-MCPE-2", "MC-0", "MC-1 OR key=MC-2", "1-2-3", "1.5-10"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                id_range(value, "MC")

    def test_date_is_inclusive_across_leap_day(self):
        query = Query(created=("2024-02-28", "2024-02-29")).build()
        self.assertIn('created >= "2024-02-28"', query)
        self.assertIn('created < "2024-03-01"', query)

    def test_invalid_dates(self):
        for bounds in (
            ("2026-02-29", "2026-03-01"),
            ("2026-03-02", "2026-03-01"),
            ("20260101", "2026-01-31"),
            ("9999-12-31", "9999-12-31"),
        ):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                Query(created=bounds).build()

    def test_filters_quote_values_and_use_union(self):
        query = Query(
            text='a"b\\c',
            statuses=("Open", "Closed"),
            resolutions=("Fixed", "Unresolved"),
            labels=("标签",),
        ).build()
        self.assertIn('text ~ "a\\"b\\\\c"', query)
        self.assertIn('status IN ("Open", "Closed")', query)
        self.assertIn('(resolution IN ("Fixed") OR resolution IS EMPTY)', query)
        self.assertIn('labels IN ("标签")', query)

    def test_custom_jql_stays_scoped(self):
        query = Query(
            jql='labels = "one" OR labels = "two"', sort="updated", descending=True
        ).build()
        self.assertIn('AND (labels = "one" OR labels = "two")', query)
        self.assertTrue(query.endswith("ORDER BY updated DESC, key DESC"))
        with self.assertRaises(ValueError):
            Query(jql="key=MC-1 ORDER BY created").build()

    def test_custom_fields_use_public_adapter_names(self):
        query = Query(
            confirmations=("Unconfirmed",), categories=("Test",), priorities=("Normal",)
        ).build()
        self.assertIn('customfield_10054 IN ("Unconfirmed")', query)
        self.assertIn('customfield_10055 IN ("Test")', query)
        self.assertIn('customfield_10049 IN ("Normal")', query)

    def test_affected_version_uses_public_adapter_name(self):
        query = Query(versions=("Minecraft 1.4.1",)).build()
        self.assertIn('versions IN ("Minecraft 1.4.1")', query)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = MojangClient(delay=0, retries=0)

    def test_pagination_uses_server_flag_and_fixed_size(self):
        pages = [page([bug("MC-1"), bug("MC-2")], more=True), page([bug("MC-3")], number=1)]
        with patch.object(self.client, "search_page", side_effect=pages) as fetch:
            result = list(self.client.iter_issues("MC", "query", page_size=2))
        self.assertEqual([b["key"] for b in result], ["MC-1", "MC-2", "MC-3"])
        self.assertEqual([c.args[2:] for c in fetch.call_args_list], [(0, 2), (1, 2)])

    def test_short_page_does_not_end_early(self):
        with patch.object(
            self.client,
            "search_page",
            side_effect=[page([bug()], more=True), page([bug("MC-2")], number=1)],
        ):
            self.assertEqual(len(list(self.client.iter_issues("MC", "query"))), 2)

    def test_limit_does_not_fetch_extra_page(self):
        with patch.object(
            self.client, "search_page", return_value=page([bug(), bug("MC-2")], more=True)
        ) as fetch:
            self.assertEqual(len(list(self.client.iter_issues("MC", "query", limit=1))), 1)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args[-1], 1)

    def test_duplicate_pages_abort(self):
        pages = [page([bug()], more=True), page([bug()], number=1, more=True)]
        with (
            patch.object(self.client, "search_page", side_effect=pages),
            self.assertRaises(MojangError),
        ):
            list(self.client.iter_issues("MC", "query"))

    def test_unknown_pagination_and_wrong_page_abort(self):
        for response in ({"issues": []}, page([bug()], number=4)):
            with (
                self.subTest(response=response),
                patch.object(self.client, "search_page", return_value=response),
                self.assertRaises(MojangError),
            ):
                list(self.client.iter_issues("MC", "query"))

    def test_legacy_pagination_flags(self):
        for response in ({"issues": [bug()], "isLast": True}, {"issues": [bug()], "total": 1}):
            with patch.object(self.client, "search_page", return_value=response):
                self.assertEqual(len(list(self.client.iter_issues("MC", "query"))), 1)

    def test_exact_id_must_match(self):
        with patch.object(self.client, "search_page", return_value=page([bug("MC-2")])):
            with self.assertRaises(MojangError):
                self.client.get_issue("MC-1")

    def test_post_payload_and_real_json_decoding(self):
        with patch("com.lopleec.mojang_bugs_cli.client.urlopen") as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = json.dumps(
                page([bug()])
            ).encode()
            self.client.search_page("MC", "key=MC-1", 0, 1)
            request = open_url.call_args.args[0]
            payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://bugs.mojang.com/api/jql-search-post")
        self.assertEqual(payload["filter"], "all")
        self.assertEqual(payload["page"], 0)
        self.assertTrue(payload["advanced"])

    def test_retry_after_429(self):
        self.client.retries = 1
        error = HTTPError(
            "https://bugs.mojang.com",
            429,
            "limited",
            {"Retry-After": "0"},
            io.BytesIO(b'{"message":"slow down"}'),
        )
        with (
            patch(
                "com.lopleec.mojang_bugs_cli.client.urlopen",
                side_effect=[error, URLError("offline")],
            ) as fetch,
            patch("com.lopleec.mojang_bugs_cli.client.time.sleep"),
        ):
            with self.assertRaises(MojangError):
                self.client.search_page("MC", "query", 0, 1)
            self.assertEqual(fetch.call_count, 2)

    def test_non_json_and_malformed_issues_fail_cleanly(self):
        for data in (b"<html>offline</html>", b'{"issues": [{}]}', b'{"message": "bad query"}'):
            with (
                self.subTest(data=data),
                patch("com.lopleec.mojang_bugs_cli.client.urlopen") as fetch,
            ):
                fetch.return_value.__enter__.return_value.read.return_value = data
                with self.assertRaises(MojangError):
                    self.client.search_page("MC", "query", 0, 1)

    def test_retry_after_invalid_numbers(self):
        for value in (None, "invalid", "NaN", "inf"):
            self.assertIsNone(_retry_after(value))
        self.assertEqual(_retry_after("10"), 10)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "bugs.json"

    def test_streamed_export_and_unicode(self):
        self.assertEqual(write_export(self.path, iter([bug()]), project="MC", jql="query"), 1)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["issues"][0]["description"], "你好")
        self.assertIn("中文", self.path.read_text(encoding="utf-8"))

    def test_empty_export_is_valid(self):
        write_export(self.path, [], project="MC", jql="query")
        self.assertEqual(json.loads(self.path.read_text())["issues"], [])

    def test_failed_export_keeps_old_file_and_cleans_temporary(self):
        self.path.write_text("original", encoding="utf-8")

        def broken():
            yield bug()
            raise MojangError("network failed")

        with self.assertRaises(MojangError):
            write_export(self.path, broken(), project="MC", jql="query", force=True)
        self.assertEqual(self.path.read_text(), "original")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_no_overwrite_or_network_without_force(self):
        self.path.write_text("original")

        def unexpected():
            self.fail("must not start fetching")
            yield bug()

        with self.assertRaises(FileExistsError):
            write_export(self.path, unexpected(), project="MC", jql="query")

    def test_groups_include_multivalue_and_missing(self):
        items = [bug(versions=["A", "B"]), bug("MC-2")]
        write_export(self.path, items, project="MC", jql="query", group_by="version")
        payload = json.loads(self.path.read_text())
        self.assertEqual(payload["count"], 2)
        self.assertEqual(set(payload["groups"]), {"A", "B", "(none)"})
        self.assertEqual(payload["groups"]["B"][0]["key"], "MC-1")

    def test_raw_grouped_export_preserves_adf(self):
        write_export(
            self.path, [bug()], project="MC", jql="query", group_by="created-month", raw=True
        )
        payload = json.loads(self.path.read_text())
        self.assertEqual(payload["groups"]["2026-01"][0], bug())


class CliTests(unittest.TestCase):
    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def test_help_and_dry_run_need_no_network(self):
        self.assertEqual(self.invoke([])[0], 0)
        with patch("com.lopleec.mojang_bugs_cli.cli.MojangClient") as client:
            code, out, _ = self.invoke(["export", "--ids", "1-5", "--dry-run"])
            client.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn('key <= "MC-5"', out)

    def test_json_stdout_has_no_table_or_progress(self):
        with patch.object(MojangClient, "iter_issues", return_value=iter([bug()])):
            code, out, err = self.invoke(["search", "zombie", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["key"], "MC-1")
        self.assertEqual(err, "")

    def test_get_numeric_id_for_other_project(self):
        with patch.object(MojangClient, "get_issue", return_value=bug("MCPE-12")) as fetch:
            code, out, _ = self.invoke(["get", "12", "-p", "MCPE", "--json"])
            fetch.assert_called_once_with("MCPE-12")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["project"], "MCPE")

    def test_network_error_has_nonzero_exit_and_no_traceback(self):
        with patch.object(MojangClient, "get_issue", side_effect=MojangError("offline")):
            code, out, err = self.invoke(["get", "MC-1"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("offline", err)

    def test_invalid_options_exit_two(self):
        for args in (
            ["search", "--page-size", "101"],
            ["search", "--timeout", "0"],
            ["search", "--delay", "nan"],
            ["export", "--ids", "20-10", "--dry-run"],
        ):
            with (
                self.subTest(args=args),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                main(args)
            self.assertEqual(raised.exception.code, 2)


class TextTests(unittest.TestCase):
    def test_nested_list_and_break(self):
        adf = {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": "A"},
                                {"type": "hardBreak"},
                                {"type": "text", "text": "B"},
                            ],
                        }
                    ],
                }
            ],
        }
        self.assertEqual(rich_text(adf).strip(), "- A\nB")

    def test_attachment_link_uses_public_api(self):
        issue = bug()
        issue["fields"]["attachment"] = [{"id": "123", "filename": "test.png"}]
        data = normalize(issue)
        self.assertEqual(
            data["attachments"][0]["url"],
            "https://bugs.mojang.com/api/issue-attachment-get?attachmentId=123",
        )


if __name__ == "__main__":
    unittest.main()
