"""Fully synthetic fixtures, NOT captured/verified 聚水潭 production responses.

No network, cookie reads, package installation, or real credentials are used.
"""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jst_export as j


HTML = '''<html><input type="hidden" name="__VIEWSTATE" value="dynamic-state">
<input type="hidden" name="__VIEWSTATEGENERATOR" value="dynamic-generator">
<input type="hidden" name="other" value="a&amp;b"></html>'''


def xlsx():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>')
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets/></workbook>')
    return buffer.getvalue()


class Response:
    def __init__(self, obj=None, *, text=None, content=b"", status=200, headers=None):
        self.status_code = status
        self.text = json.dumps(obj) if text is None else text
        self.content = content
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


class Session:
    def __init__(self, gets=(), posts=()):
        self.gets, self.posts = list(gets), list(posts)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.gets.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.posts.pop(0)


def packet(value):
    return Response({"ReturnValue": json.dumps(value)})


class ExportTests(unittest.TestCase):
    def test_dynamic_hidden(self):
        session = Session(gets=[Response(text=HTML)])
        fields = j.get_hidden(session, {})
        self.assertEqual(fields["other"], "a&b")
        self.assertEqual(fields["__VIEWSTATE"], "dynamic-state")
        self.assertFalse(session.calls[0][2]["allow_redirects"])

    def test_login_and_http_rejected(self):
        for response in (Response(text='<html><input type="password"></html>'),
                         Response(text=HTML, status=302)):
            with self.assertRaises(j.ExportError):
                j.get_hidden(Session(gets=[response]), {})
        for response in (Response(text="<html>login</html>"), packet({"Error": "secret"}),
                         packet({"success": False}), Response(text="garbage")):
            with self.assertRaises(j.ExportError) as cm:
                j._unwrap(response)
            self.assertNotIn("secret", str(cm.exception))

    def test_nested_json_and_callback_framing(self):
        raw = json.dumps({"ReturnValue": json.dumps({"datas": []})})
        self.assertEqual(j._unwrap(Response(text=str(len(raw)) + "|" + raw)), {"datas": []})
        self.assertEqual(j._unwrap(Response(text="3|abc" + raw)), {"datas": []})
        self.assertEqual(j._unwrap(Response(text=json.dumps(json.dumps(json.loads(raw))))), {"datas": []})
        with self.assertRaises(j.ExportError):
            j._unwrap(Response(text="99999|" + raw))

    def test_full_pagination_dedup_and_string_ids(self):
        huge = "99887766554433221100998877665544332211"
        session = Session(posts=[packet({"datas": [{"so_id": huge, "o_id": "100"}]}),
                                 packet({"datas": [{"so_id": huge, "o_id": "100"}, {"so_id": "2", "o_id": "101"}]}),
                                 packet({"datas": []})])
        self.assertEqual(j.query_orders(session, {}, [huge, "2"], {}), ["100", "101"])
        payload = json.loads(session.calls[0][2]["data"]["__CALLBACKPARAM"])
        self.assertEqual(payload["Args"], ["1", j._filter("so_id", [huge, "2"]), "{}"])
        self.assertEqual(session.calls[1][2]["params"]["am___"], "LoadDataToJSON")
        self.assertEqual(session.calls[0][2]["data"]["__CALLBACKID"], "JTable1")

    def test_numeric_json_id_stays_exact(self):
        huge = "99887766554433221100998877665544332211"
        text = '{"ReturnValue":{"datas":[{"so_id":' + huge + ',"o_id":123}],"total":1}}'
        self.assertEqual(j.query_orders(Session(posts=[Response(text=text)]), {}, [huge], {}), ["123"])
        with self.assertRaises(j.ExportError):
            j.normalize_order_ids([1.0])
        response = j._unwrap(Response(text='{"datas":[{"so_id":1e25}]}'))
        with self.assertRaises(j.ExportError):
            j._identifier(response["datas"][0]["so_id"], "订单号")
        # Unrelated monetary decimals do not make otherwise valid rows fail.
        response = Response(text='{"datas":[{"so_id":"1","o_id":"2","amount":1.25}],"total":1}')
        self.assertEqual(j.query_orders(Session(posts=[response]), {}, ["1"], {}), ["2"])

    def test_missing_explicit(self):
        with self.assertRaises(j.MissingOrdersError) as cm:
            j.query_orders(Session(posts=[packet({"datas": []})]), {}, ["missing"], {})
        self.assertEqual(cm.exception.missing, ["missing"])

    def test_no_guess_coverage_or_out_of_scope(self):
        for row in ({"o_id": "1"}, {"o_id": "1", "so_id": "outside"}):
            with self.assertRaises(j.ExportError):
                j.query_orders(Session(posts=[packet({"datas": [row]})]), {}, ["1"], {})

    def test_loop_limit_and_total_consistency(self):
        row = {"so_id": "1", "o_id": "1"}
        for packets, limit in (([packet({"datas": [row]})] * 2, 10),
                               ([packet({"datas": [row]})], 1),
                               ([packet({"datas": [row], "total": 2}), packet({"datas": [], "total": 3})], 10),
                               ([packet({"datas": [row], "total": 2}), packet({"datas": [], "total": 2})], 10)):
            with self.assertRaises(j.ExportError):
                j.query_orders(Session(posts=packets), {}, ["1"], {}, max_pages=limit)

    def test_create_and_poll_payload(self):
        success = lambda value: Response({"IsSuccess": True, "ReturnValue": value, "RequestId": "not-a-task"})
        session = Session(gets=[Response(text=HTML)], posts=[success(None), success(None),
                             success(None), success("https://www.erp321.com/file.xlsx")])
        hidden = {"__VIEWSTATE": "start", "__VIEWSTATEGENERATOR": "gen", "extra": "keep"}
        saved = []
        self.assertIsNone(j.create_export(session, {}, hidden, ["12"], before_create=lambda: saved.append(True)))
        self.assertEqual(saved, [True])
        create = json.loads(session.calls[1][2]["data"]["__CALLBACKPARAM"])
        self.assertEqual(create["Method"], "ExportBySync")
        self.assertEqual(create["Args"], [j._filter("o_id", ["12"]), "1", "true", "false", "false", None])
        self.assertEqual(create["CallControl"], "{page}")
        url = j.poll_export(session, {}, "12", sleep=lambda _: None)
        self.assertTrue(url.endswith("file.xlsx"))
        polls = [c for c in session.calls if c[0] == "post"][2:]
        self.assertEqual(polls[0][2]["data"]["__VIEWSTATE"], "dynamic-state")
        self.assertNotIn("__VIEWSTATE_DEL", polls[0][2]["data"])
        self.assertEqual(polls[0][2]["data"]["__CALLBACKID"], "JTable1")
        self.assertEqual(json.loads(polls[0][2]["data"]["__CALLBACKPARAM"])["Method"], "CheckAndGetExportData")
        self.assertEqual(json.loads(polls[0][2]["data"]["__CALLBACKPARAM"])["Args"], ["12"])

    def test_task_status_not_guessed_and_configurable(self):
        for obj in ({"download_url": "https://www.erp321.com/a"}, {"status": "42"},
                    {"status": "failed"}, {"status": "success"}):
            with self.assertRaises(j.ExportError):
                j.poll_export(Session(gets=[Response(text=HTML)], posts=[packet(obj)]), {}, "12", sleep=lambda _: None)
        with self.assertRaises(j.ExportError):
            j.poll_export(Session(), {}, "callback-request-id")
        with self.assertRaises(j.ExportError):
            j.poll_export(Session(gets=[Response(text=HTML)], posts=[Response({"IsSuccess": True, "ReturnValue": None})]),
                          {}, "12", max_polls=1, sleep=lambda _: None)

    def test_url_policy(self):
        for url in ("http://www.erp321.com/a", "https://erp321.com.evil.test/a", "https://evil.test/a",
                    "https://user@erp321.com/a", "https://erp321.com:444/a", "https://erp321.com/a#fragment",
                    "https://erp321.com\\@evil.test/a", "https://erp321.com/a\n"):
            with self.assertRaises(j.ExportError):
                j.validate_download_url(url)
        j.validate_download_url("https://cdn.jushuitan.com/a")
        j.validate_download_url("https://files.example.test/a", ("example.test",))

    def test_external_and_cross_origin_redirect_strip_credentials(self):
        headers = {"Cookie": "synthetic-secret", "Authorization": "synthetic-token", "X-CSRF": "secret", "User-Agent": "test"}
        session = Session(gets=[Response(status=302, headers={"Location": "https://cdn.jushuitan.com/f?signature=not-real"})])
        calls = []
        def public(url, safe, timeout, maximum):
            calls.append((url, safe))
            return 200, {}, xlsx()
        self.assertEqual(j.download_export(session, headers, "https://www.erp321.com/start", public_download=public), xlsx())
        self.assertEqual(calls[0][1], {"User-Agent": "test"})
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.calls[0][2]["allow_redirects"])
        session = Session(gets=[Response(status=302, headers={"Location": "https://evil.test/a"})])
        with self.assertRaises(j.ExportError):
            j.download_export(session, headers, "https://www.erp321.com/a", public_download=public)
        self.assertEqual(len(calls), 1)

    def test_archives(self):
        self.assertEqual(j.validate_archive(xlsx()), ".xlsx")
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w") as z:
            z.writestr("folder/export.xlsx", xlsx())
        self.assertEqual(j.validate_archive(bundle.getvalue()), ".zip")
        for name, content in (("../evil.xlsx", xlsx()), ("/evil.xlsx", xlsx()),
                              ("a\\evil.xlsx", xlsx()), ("readme.txt", b"hello"),
                              ("fake.xlsx", b"not zip"), ("bomb.xlsx", b"0" * 100000)):
            b = io.BytesIO()
            with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
                info = zipfile.ZipInfo(name)
                info.filename = name  # Keep malicious backslash fixture on Windows.
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, content)
            with self.assertRaises(j.ExportError):
                j.validate_archive(b.getvalue())
        with self.assertRaises(j.ExportError):
            j.validate_archive(b"<html>login</html>")

    def test_archive_xml_macros_symlinks_and_limits(self):
        for extra_name, extra_content in (("xl/bad.xml", b"<!DOCTYPE x [<!ENTITY a 'bad'>]><x/>"),
                                          ("xl/vbaProject.bin", b"macro"),
                                          ("xl/_rels/workbook.xml.rels", b'<Relationships><Relationship TargetMode="External" Target="https://example.test"/></Relationships>')):
            buf = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(xlsx())) as original:
                with zipfile.ZipFile(buf, "w") as archive:
                    for item in original.infolist():
                        archive.writestr(item.filename, original.read(item))
                    archive.writestr(extra_name, extra_content)
            with self.assertRaises(j.ExportError):
                j.validate_archive(buf.getvalue())
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            symlink = zipfile.ZipInfo("link.xlsx")
            symlink.create_system = 3
            symlink.external_attr = 0o120777 << 16
            archive.writestr(symlink, "target")
        with self.assertRaises(j.ExportError):
            j.validate_archive(buf.getvalue())
        for kwargs in ({"max_members": 1}, {"max_unpacked": 1}, {"max_ratio": 0.5}):
            with self.assertRaises(j.ExportError):
                j.validate_archive(xlsx(), **kwargs)

    def test_unpack_unique_and_preserve_zip(self):
        bundle = io.BytesIO()
        expected = xlsx()  # ZIP member timestamps vary across separately built fixtures.
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("a/report.xlsx", expected)
            archive.writestr("b/report.xlsx", expected)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "original.zip"
            path.write_bytes(bundle.getvalue())
            first = j.unpack_export(path)
            second = j.unpack_export(path)
            self.assertEqual(len(first), 2)
            self.assertNotEqual(first[0].parent, second[0].parent)
            self.assertEqual(first[0].read_bytes(), expected)
            self.assertTrue(path.exists())
            self.assertEqual(j.unpack_export(first[0]), [first[0]])
            with self.assertRaises(j.ExportError):
                j.unpack_export(path, max_bytes=1)

    def test_suffixless_output_automatic_and_existing_target_safe(self):
        from test_live_export import LiveSession, IDS
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "orders"
            session = LiveSession()
            report = j.run_export(session, {}, IDS, output, max_polls=1)
            self.assertEqual(report["output"], str(output.with_suffix(".xlsx")))
            with self.assertRaises(j.ExportError):
                j.run_export(LiveSession(), {}, IDS, output, max_polls=1)
            self.assertEqual(output.with_suffix(".xlsx").read_bytes(), session.content)

    def test_end_to_end_safe_report_and_non_overwrite(self):
        from test_live_export import LiveSession, IDS
        session = LiveSession()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "orders.xlsx"
            report = j.run_export(session, {"Cookie": "synthetic-secret"}, IDS, output, max_polls=1)
            self.assertTrue(output.read_bytes().startswith(b"PK"))
            self.assertEqual(report["exported_internal_orders"], 2)
            text = json.dumps(report)
            self.assertNotIn("signature", text)
            self.assertNotIn("Cookie", text)
            self.assertNotIn("synthetic-secret", text)
            with self.assertRaises(j.ExportError):
                j.run_export(session, {}, IDS, output, max_polls=1)
            self.assertEqual({p.name for p in Path(directory).iterdir()},
                             {"orders.xlsx", "orders.xlsx.state.json", "orders.xlsx.state.json.lock"})


if __name__ == "__main__":
    unittest.main()
