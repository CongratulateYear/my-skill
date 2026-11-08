"""Synthetic offline live-protocol and durable-resume tests (no network/cookies)."""
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jst_export as export


IDS = ["123456789012345678", "223456789012345678"]


def workbook(ids=IDS):
    wb = Workbook()
    ws = wb.active
    ws.append(["原始线上订单号", "颜色及规格"])
    for value in ids:
        ws.append([value, "synthetic product"])
    stream = io.BytesIO()
    wb.save(stream)
    wb.close()
    return stream.getvalue()


class Response:
    status_code = 200
    headers = {}

    def __init__(self, value=None, *, html=False, content=b""):
        self.text = value if html else json.dumps(value, ensure_ascii=False)
        self.content = content


def envelope(value):
    return Response({"IsSuccess": True, "ReturnValue": value, "RequestId": "999999"})


def task(auto_id, count=2, typ="order"):
    return dict(auto_id=str(auto_id), total_count=str(count), export_type=typ,
                filter_data="0", file_name="synthetic.xlsx",
                created=export.datetime.now(export.ERP_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"), status="0")


class LiveSession:
    def __init__(self, *, found=IDS, tasks=None, interrupt=False, content=None, missing_log=False):
        self.found = found
        self.tasks = tasks or [task(11, len(found))]
        self.interrupt = interrupt
        self.content = workbook(found) if content is None else content
        self.missing_log = missing_log
        self.log_calls = 0
        self.calls = []

    def get(self, url, **kwargs):
        if url == "https://www.erp321.com/synthetic.xlsx":
            return Response(content=self.content)
        return Response('<input type="hidden" name="__VIEWSTATE" value="secret-state">'
                        '<input type="hidden" name="__VIEWSTATEGENERATOR" value="secret-generator">', html=True)

    def post(self, url, **kwargs):
        payload = json.loads(kwargs["data"]["__CALLBACKPARAM"])
        self.calls.append((url, payload, kwargs["data"]["__CALLBACKID"]))
        method = payload["Method"]
        if method == "LoadDataToJSON":
            if url == export.LOG_URL:
                self.log_calls += 1
                rows = [task(10, len(self.found))]
                if self.log_calls > 1 and not self.missing_log:
                    rows += self.tasks
                return envelope(dict(datas=rows, dp=dict(DataCount=len(rows), PageIndex=1, IsLast=True)))
            rows = [dict(so_id=x, o_id=str(index + 100)) for index, x in enumerate(self.found)]
            return envelope(dict(datas=rows, dp=dict(DataCount=len(rows), PageIndex=1, IsLast=True)))
        if method == "ExportCheckConfirm":
            return envelope(None)
        if method == "ExportBySync":
            if self.interrupt:
                raise TimeoutError("sensitive transport URL cookie")
            return envelope(None)
        if method == "CheckAndGetExportData":
            if payload["Args"] != ["11"]:
                raise AssertionError("RequestId or wrong task selected")
            return envelope("https://www.erp321.com/synthetic.xlsx")
        raise AssertionError(method)


class LiveExportTests(unittest.TestCase):
    def run_it(self, session, root, **kwargs):
        return export.run_export(session, {"Cookie": "never-persist-cookie"}, IDS, root / "orders",
                                 max_polls=1, poll_interval=0, sleep=lambda _: None, **kwargs)

    def test_live_null_check_create_and_log_id_not_requestid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = LiveSession()
            report = self.run_it(session, root)
            self.assertTrue(report["complete"])
            self.assertEqual(report["task_id"], "11")
            create = [p for _, p, _ in session.calls if p["Method"] == "ExportBySync"][0]
            self.assertEqual(create["Args"], [export._filter("o_id", ["100", "101"]), "2",
                                               "true", "false", "false", None])
            self.assertTrue((root / "orders.xlsx").is_file())
            state_text = (root / "orders.state.json").read_text()
            self.assertNotIn("never-persist-cookie", state_text)
            self.assertNotIn("secret-state", state_text)
            self.assertNotIn("https://", state_text)
            self.assertNotIn("RequestId", state_text)
            self.assertEqual(json.loads(state_text)["phase"], "complete")

    def test_uncertain_create_resume_does_not_recreate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = LiveSession(interrupt=True)
            with self.assertRaises(export.ExportError):
                self.run_it(first, root)
            state = json.loads((root / "orders.state.json").read_text())
            self.assertEqual(state["phase"], "creating")
            second = LiveSession()
            second.log_calls = 1
            report = self.run_it(second, root, resume=True)
            self.assertTrue(report["complete"])
            self.assertFalse(any(p["Method"] in ("ExportBySync", "ExportCheckConfirm") for _, p, _ in second.calls))

    def test_uncertain_without_new_log_never_recreates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(export.ExportError):
                self.run_it(LiveSession(interrupt=True), root)
            session = LiveSession(missing_log=True)
            with self.assertRaises(export.ExportError):
                self.run_it(session, root, resume=True)
            self.assertFalse(any(p["Method"] == "ExportBySync" for _, p, _ in session.calls))

    def test_existing_journal_requires_explicit_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(export.ExportError):
                self.run_it(LiveSession(interrupt=True), root)
            session = LiveSession()
            with self.assertRaises(export.ExportError):
                self.run_it(session, root)
            self.assertFalse(session.calls)

    def test_ambiguity_fails_without_downloading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = LiveSession(tasks=[task(11), task(12)])
            with self.assertRaisesRegex(export.ExportError, "歧义"):
                self.run_it(session, root)
            self.assertFalse((root / "orders.xlsx").exists())
            self.assertFalse(any(p["Method"] == "CheckAndGetExportData" for _, p, _ in session.calls))

    def test_type_filter_but_count_is_not_a_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = LiveSession(tasks=[task(11), task(12, typ="other"), task(13, count=9)])
            with self.assertRaisesRegex(export.ExportError, "歧义"):
                self.run_it(session, root, export_type="order")
            self.assertFalse(any(p["Method"] == "CheckAndGetExportData" for _, p, _ in session.calls))

    def test_foreign_or_incomplete_workbook_rejected(self):
        for ids in (IDS[:1], IDS + ["323456789012345678"]):
            with self.subTest(ids=ids), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaisesRegex(export.ExportError, "范围/覆盖"):
                    self.run_it(LiveSession(content=workbook(ids)), root)
                self.assertFalse((root / "orders.xlsx").exists())
                self.assertEqual(json.loads((root / "orders.state.json").read_text())["phase"], "candidate")

    def test_partial_requires_optin_and_reports_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(export.MissingOrdersError):
                self.run_it(LiveSession(found=IDS[:1]), root)
            self.assertFalse((root / "orders.state.json").exists())
            report = self.run_it(LiveSession(found=IDS[:1]), root, allow_missing=True)
            self.assertFalse(report["complete"])
            self.assertFalse(report["ok"])
            self.assertEqual(report["found_ids"], IDS[:1])
            self.assertEqual(report["missing_ids"], IDS[1:])
            self.assertTrue(Path(report["output"]).is_file())

    def test_resume_completed_local_file_no_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_it(LiveSession(), root)
            session = LiveSession()
            self.assertTrue(self.run_it(session, root, resume=True)["complete"])
            self.assertFalse(session.calls)

    def test_resume_changed_order_scope_fails_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(export.ExportError):
                self.run_it(LiveSession(interrupt=True), root)
            session = LiveSession()
            with self.assertRaises(export.ExportError):
                export.run_export(session, {}, IDS[:1], root / "orders", resume=True)
            self.assertFalse(session.calls)

    def test_missing_xlsx_header_and_unsafe_long_numeric_ids(self):
        for value in (123456789012345678, '="123456789012345678"'):
            with self.subTest(value=value), self.assertRaises(export.ExportError):
                export.validate_order_coverage(workbook([value]), IDS[:1])
        wb = Workbook()
        wb.active.append(["wrong header"])
        stream = io.BytesIO()
        wb.save(stream)
        wb.close()
        with self.assertRaises(export.ExportError):
            export.validate_order_coverage(stream.getvalue(), IDS)

    def test_zip_coverage_is_union_and_traversal_stays_rejected(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("one.xlsx", workbook(IDS[:1]))
            archive.writestr("two.xlsx", workbook(IDS[1:]))
        self.assertEqual(export.validate_order_coverage(stream.getvalue(), IDS), ".zip")
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("../unsafe.xlsx", workbook())
        with self.assertRaises(export.ExportError):
            export.validate_order_coverage(stream.getvalue(), IDS)

    def test_pagination_uses_live_dp(self):
        class Paged(LiveSession):
            def post(self, url, **kwargs):
                payload = json.loads(kwargs["data"]["__CALLBACKPARAM"])
                page = int(payload["Args"][0])
                return envelope(dict(datas=[dict(so_id=IDS[page - 1], o_id=str(100 + page))],
                                     dp=dict(DataCount=2, PageIndex=page, IsLast=page == 2)))
        self.assertEqual(export.query_orders(Paged(), {}, IDS, {}), ["101", "102"])

    def test_resume_rechecks_internal_order_identity(self):
        class DifferentAccount(LiveSession):
            def post(self, url, **kwargs):
                response = super().post(url, **kwargs)
                payload = json.loads(kwargs["data"]["__CALLBACKPARAM"])
                if url == export.EXPORT_URL and payload["Method"] == "LoadDataToJSON":
                    body = json.loads(response.text)
                    body["ReturnValue"]["datas"][0]["o_id"] = "777"
                    return Response(body)
                return response
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(export.ExportError):
                self.run_it(LiveSession(interrupt=True), root)
            session = DifferentAccount()
            with self.assertRaisesRegex(export.ExportError, "账号已切换"):
                self.run_it(session, root, resume=True)
            self.assertFalse(any(p["Method"] in ("ExportBySync", "CheckAndGetExportData") for _, p, _ in session.calls))

    def test_same_journal_concurrent_run_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with export._journal_lock(root / "orders.state.json"):
                with self.assertRaisesRegex(export.ExportError, "其他进程"):
                    self.run_it(LiveSession(), root)

    def test_forged_sheet_dimensions_cannot_hide_foreign_orders(self):
        raw = workbook(IDS + ["323456789012345678"])
        stream = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as original, zipfile.ZipFile(stream, "w") as changed:
            for info in original.infolist():
                data = original.read(info)
                if info.filename == "xl/worksheets/sheet1.xml":
                    data = data.replace(b'ref="A1:B4"', b'ref="A1:B3"')
                changed.writestr(info, data)
        with self.assertRaises(export.ExportError):
            export.validate_order_coverage(stream.getvalue(), IDS)

    def test_callback_null_requires_explicit_success(self):
        with self.assertRaises(export.ExportError):
            export._unwrap(Response({"ReturnValue": None, "RequestId": "99"}))
        self.assertIsNone(export._unwrap(envelope(None)))


if __name__ == "__main__":
    unittest.main()
