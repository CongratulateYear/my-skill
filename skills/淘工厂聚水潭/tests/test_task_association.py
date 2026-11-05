"""Synthetic offline regression: product counts are not order coverage proof."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_live_export import LiveSession, task, workbook, export


IDS21 = [str(123456789012345678 + n) for n in range(21)]
NOW = 1793867765.0  # 2026-11-05 16:36:05 Asia/Shanghai, independent of host TZ.


def dated_task(auto_id=11, count=25, typ="order", timestamp=NOW):
    row = task(auto_id, count, typ)
    row["created"] = export.datetime.fromtimestamp(timestamp, export.ERP_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    return row


class TaskAssociationTests(unittest.TestCase):
    def run_it(self, session, root, **kwargs):
        with patch.object(export.time, "time", return_value=NOW):
            return export.run_export(session, {"Cookie": "credential-do-not-store"}, IDS21,
                                     root / "orders", max_polls=1, poll_interval=0,
                                     sleep=lambda _: None, **kwargs)

    def session(self, **kwargs):
        return LiveSession(found=IDS21, tasks=[dated_task()],
                           content=workbook(IDS21 + IDS21[:4]), **kwargs)

    def state(self, root):
        return json.loads((root / "orders.state.json").read_text())

    def test_21_orders_25_product_lines_publish_only_after_exact_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = self.run_it(self.session(), root)
            self.assertTrue(report["ok"])
            self.assertEqual(report["exported_internal_orders"], 21)
            self.assertEqual(report["task_total_count"], 25)
            self.assertFalse(report["task_count_matches_internal_orders"])
            self.assertTrue(report["task_association_time_checked"])
            text = (root / "orders.state.json").read_text()
            self.assertNotIn("credential-do-not-store", text)
            self.assertNotIn("https://", text)
            self.assertEqual(self.state(root)["creation_window"], [NOW, NOW + 30])

    def test_different_counts_do_not_resolve_two_in_window_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = LiveSession(found=IDS21, tasks=[dated_task(11, 25), dated_task(12, 21, timestamp=NOW + 5)])
            with self.assertRaisesRegex(export.ExportError, "歧义"):
                self.run_it(session, root)
            self.assertFalse((root / "orders.xlsx").exists())
            self.assertFalse(any(p["Method"] == "GetExportData" for _, p, _ in session.calls))

    def test_wrong_coverage_never_published_even_if_log_count_matches(self):
        for ids in (IDS21[:-1], IDS21 + ["999999999999999999"], IDS21[:-1] + ["999999999999999999"]):
            with self.subTest(ids=ids), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                session = LiveSession(found=IDS21, tasks=[dated_task(count=21)], content=workbook(ids))
                with self.assertRaisesRegex(export.ExportError, "范围/覆盖"):
                    self.run_it(session, root)
                self.assertFalse((root / "orders.xlsx").exists())
                self.assertEqual(self.state(root)["phase"], "candidate")

    def test_interrupted_creating_keeps_window_and_never_recreates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(export.ExportError):
                self.run_it(self.session(interrupt=True), root)
            before = self.state(root)
            self.assertEqual(before["phase"], "creating")
            second = self.session()
            second.log_calls = 1
            with patch.object(export.time, "time", return_value=NOW + 86400):
                report = export.run_export(second, {}, IDS21, root / "orders", resume=True, max_polls=1)
            self.assertTrue(report["ok"])
            self.assertEqual(self.state(root)["creation_window"], before["creation_window"])
            self.assertFalse(any(p["Method"] in ("ExportBySync", "ExportCheckConfirm") for _, p, _ in second.calls))

    def test_legacy_creating_without_time_safe_unique_resume_and_no_fake_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(export.ExportError):
                self.run_it(self.session(interrupt=True), root)
            state = self.state(root)
            del state["creation_window"]
            export._save_state(root / "orders.state.json", state)
            session = self.session()
            session.log_calls = 1
            session.tasks[0].pop("created")
            report = self.run_it(session, root, resume=True)
            self.assertTrue(report["ok"])
            self.assertFalse(report["task_association_time_checked"])
            self.assertNotIn("creation_window", self.state(root))
            self.assertFalse(any(p["Method"] == "ExportBySync" for _, p, _ in session.calls))

    def test_time_window_excludes_stale_and_future_without_picking_latest(self):
        state = {"baseline_ids": ["10"], "internal_ids": IDS21,
                 "export_type": None, "creation_window": [NOW, NOW + 30]}
        rows = [dated_task(10), dated_task(12, timestamp=NOW - 121),
                dated_task(13, timestamp=NOW + 151), dated_task()]
        self.assertEqual(export._candidate_tasks(rows, state), [("11", "order")])
        self.assertEqual(export._created_timestamp("2026-11-05 16:36:05"), NOW)

    def test_bad_created_and_unknown_type_fail_closed_regardless_count(self):
        state = {"baseline_ids": [], "internal_ids": IDS21,
                 "export_type": None, "creation_window": [NOW, NOW + 30]}
        for created in (None, "synthetic", "2026-02-30 01:02:03", "2026-11-05T16:36:05Z"):
            row = dated_task()
            row["created"] = created
            with self.subTest(created=created), self.assertRaises(export.ExportError):
                export._candidate_tasks([row], state)
        for typ in (None, "", "   ", "unknown", False, [], {}):
            with self.subTest(typ=typ), self.assertRaises(export.ExportError):
                export._candidate_tasks([dated_task(typ=typ)], state)

    def test_legacy_without_time_still_blocks_multiple_candidates(self):
        state = {"baseline_ids": [], "internal_ids": IDS21, "export_type": None}
        with self.assertRaisesRegex(export.ExportError, "歧义"):
            export._candidate_tasks([dated_task(11, 25), dated_task(12, 21)], state)
        self.assertNotIn("creation_window", state)

    def test_resume_outside_original_window_never_recreates_or_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(export.ExportError):
                self.run_it(self.session(interrupt=True), root)
            session = self.session()
            session.log_calls = 1
            session.tasks = [dated_task(timestamp=NOW + 86400)]
            with self.assertRaisesRegex(export.ExportError, "绝不重建"):
                self.run_it(session, root, resume=True)
            self.assertEqual(self.state(root)["phase"], "creating")
            self.assertFalse(any(p["Method"] in ("ExportBySync", "GetExportData") for _, p, _ in session.calls))

    def test_candidate_coverage_failure_can_resume_without_reassociation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.session()
            first.content = workbook(IDS21[:-1])
            with self.assertRaisesRegex(export.ExportError, "范围/覆盖"):
                self.run_it(first, root)
            second = self.session()
            report = self.run_it(second, root, resume=True)
            self.assertTrue(report["ok"])
            self.assertEqual(report["task_total_count"], 25)
            self.assertEqual(second.log_calls, 0)
            self.assertFalse(any(p["Method"] in ("ExportBySync", "ExportCheckConfirm") for _, p, _ in second.calls))

    def test_missing_or_invalid_count_is_diagnostic_only(self):
        for count in (None, "unknown", "", -1, {"Cookie": "not-a-count"}):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                session = self.session()
                session.tasks[0]["total_count"] = count
                report = self.run_it(session, root)
                self.assertTrue(report["ok"])
                self.assertIsNone(report["task_total_count"])
                self.assertNotIn("not-a-count", (root / "orders.state.json").read_text())

    def test_old_and_invalid_windows_do_not_get_replaced_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(export.ExportError):
                self.run_it(self.session(interrupt=True), root)
            state = self.state(root)
            for window in ([NOW + 1, NOW], [True, NOW], [NOW, float("inf")], [NOW]):
                state["creation_window"] = window
                export._save_state(root / "orders.state.json", state)
                with self.subTest(window=window), self.assertRaisesRegex(export.ExportError, "状态文件无效"):
                    export._load_state(root / "orders.state.json")


if __name__ == "__main__":
    unittest.main()
