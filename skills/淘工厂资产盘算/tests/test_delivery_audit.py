"""Local synthetic tests; no real delivery is ever written.

Cache injection below is a test double, NOT an approved recalculation engine.
Real read-only smoke checks are separately performed against cached deliveries.
"""
import importlib.util
import tempfile
import unittest
from hashlib import sha256
from unittest.mock import patch
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

from openpyxl import Workbook, load_workbook

SPEC = importlib.util.spec_from_file_location("delivery_audit", Path(__file__).parents[1] / "scripts" / "delivery_audit.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)
HEADERS = ["店铺", "运营姓名", "商品ID", "产品名称", "数量", "收入", "交易补贴", "交易佣金", "先用后付", "消费者体验提升服务", "客服服务费", "新疆物流集运", "出海增值服务费", "赔付追缴", "新品营销(营销费用)", "评价有礼", "商业化账户充值提现", "充值加码消耗", "智能优惠券金额", "投流推广佣金", "淘客推广佣金", "直播推广佣金", "刷单金额", "罚款", "直通车营销", "收入金额", "收款人", "货款", "利润", "备注（查询不到的）"]
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def cache_values(path, values):
    """Patch numeric/string cache values keyed by (sheet index, coordinate)."""
    with ZipFile(path) as z:
        entries = {n: z.read(n) for n in z.namelist()}
    for index in {i for i, _ in values}:
        name = f"xl/worksheets/sheet{index}.xml"
        root = ET.fromstring(entries[name])
        for cell in root.iter(NS + "c"):
            key = (index, cell.get("r"))
            if key not in values: continue
            value = values[key]
            if not isinstance(value, (int, float)):
                cell.set("t", "str")
            else:
                cell.attrib.pop("t", None)
            old = cell.find(NS + "v")
            if old is None: old = ET.SubElement(cell, NS + "v")
            old.text = str(value)
        entries[name] = ET.tostring(root)
    with ZipFile(path, "w", ZIP_DEFLATED) as z:
        for n, data in entries.items(): z.writestr(n, data)


def fixture(path):
    w = Workbook()
    s = w.active; s.title = "公司汇总"; s.append(HEADERS)
    row = [None] * 30; row[0] = "测试"; row[2] = "p1"; s.append(row)
    row = [None] * 30; row[0] = "总计"; s.append(row)
    for category, name, amount, product, _ in audit.DIRECT:
        if name not in w.sheetnames:
            w.create_sheet(name).append([amount, product])
        else:
            ws = w[name]; heads = [c.value for c in ws[1]]
            if amount not in heads: ws.cell(1, len(heads) + 1, amount)
    goods = w["商品货款"]
    for h in ["交易子单号", "交易主单号", "单据方向", "购买件数"]:
        goods.cell(1, goods.max_column + 1, h)
    goods.append([100, "p1", "s1", "m1", "销售", 2])
    goods.append([-20, "p1", "s1", "m1", "退款", 1])
    w["交易佣金"].append([-3, "p1"])
    # A no-product commission belongs only to the independently disclosed
    # unassigned partition, not to the product summary comparison.
    w["交易佣金"].append([-2, None])
    for name, heads in {
        "赔付追缴": ["账单金额", "处罚业务ID"],
        "评价有礼": ["账单金额", "交易子单号", "交易主单号"],
        "客服服务费": ["账单金额", "账单编号"],
        "商业化账户充值提现": ["账单金额", "业务单号"],
    }.items(): w.create_sheet(name).append(heads)
    w["评价有礼"].append([-1, "s1", "m1"])
    w["赔付追缴"].append([-4, "x:missing"])
    w["客服服务费"].append([-5, "cs"])
    w["商业化账户充值提现"].append([10, "topup"])
    h = w.create_sheet("费用归集辅助"); h.append(["类别", "商品ID", "金额", "关联单号", "来源", "原始行号"])
    h.append(["评价有礼", "p1", 1, "s1", "评价有礼", 2])
    h = w.create_sheet("未归属费用"); h.append(["类别", "金额", "关联单号/业务单号", "原因", "来源", "原始行号"])
    h.append(["赔付追缴", -4, "x:missing", "未知订单", "赔付追缴", 2])
    h.append(["客服服务费", -5, "cs", "无商品", "客服服务费", 2])
    h.append(["商业化账户充值提现", 10, "topup", "无商品", "商业化账户充值提现", 2])
    values = {"数量": 2, "收入": 80, "交易佣金": 3, "评价有礼": 1}
    for category in [x[0] for x in audit.DIRECT] + ["数量", "赔付追缴", "评价有礼"]:
        col = HEADERS.index(category) + 1
        s.cell(2, col, values.get(category, 0))
        s.cell(3, col, values.get(category, 0))
    for category, value in {"赔付追缴": 4, "评价有礼": 1, "客服服务费": 5, "商业化账户充值提现": -10}.items():
        s.cell(3, HEADERS.index(category) + 1, value)
    w.save(path); w.close()


class DeliveryAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "merged.xlsx"
        fixture(self.path)

    def change(self, sheet, cell, value):
        w = load_workbook(self.path); w[sheet][cell] = value; w.save(self.path); w.close()

    def test_signed_revenue_and_partition_not_full_bill_vs_product(self):
        r = audit.audit_workbook(self.path)
        self.assertFalse(r["blocking"], r)
        c = next(x for x in r["checks"] if x["category"] == "交易佣金" and x["scope"] == "商品合计")
        self.assertEqual(c["expected"], "3")  # whole bill is 5, product is 3
        self.assertTrue(all(x["status"] == audit.NA for x in r["checks"] if x["category"] == "利润"))

    def test_offsetting_per_product_errors_do_not_pass(self):
        self.change("公司汇总", "F2", 81)
        self.assertTrue(audit.audit_workbook(self.path)["blocking"])

    def test_tolerance_half_cent(self):
        self.change("公司汇总", "F2", 80.005)
        self.assertFalse(audit.audit_workbook(self.path)["blocking"])
        self.change("公司汇总", "F2", 80.00501)
        self.assertTrue(audit.audit_workbook(self.path)["blocking"])

    def test_missing_formula_cache_is_unknown(self):
        self.change("公司汇总", "F2", "=100-20")
        r = audit.audit_workbook(self.path)
        self.assertTrue(r["blocking"])
        self.assertTrue(any(x["status"] == audit.UNKNOWN and x["target"].endswith("F2") for x in r["checks"]))

    def test_bad_source_is_not_zero(self):
        self.change("商品货款", "A2", "not money")
        self.assertTrue(audit.audit_workbook(self.path)["blocking"])

    def test_traceability_tamper_even_equal_net(self):
        self.change("费用归集辅助", "D2", "different-order")
        r = audit.audit_workbook(self.path)
        self.assertTrue(any(x["category"] == "费用归集辅助" and x["status"] == audit.FAIL for x in r["checks"]))

    def test_new_copy_only_and_pending_recalc(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError): audit.append_audit_sheet(self.path, self.path)
        out = Path(self.tmp.name) / "candidate.xlsx"
        r = audit.append_audit_sheet(self.path, out)
        self.assertTrue(r["needs_recalculation"])
        self.assertEqual(before, self.path.read_bytes())
        with self.assertRaises(FileExistsError): audit.append_audit_sheet(self.path, out)
        self.assertTrue(audit.verify_audit(out)["blocking"])

    def test_verify_recomputes_static_and_checks_cache(self):
        out = Path(self.tmp.name) / "candidate.xlsx"
        audit.append_audit_sheet(self.path, out)
        report = audit.audit_workbook(out)
        w = load_workbook(out); index = w.sheetnames.index(audit.AUDIT_SHEET) + 1; w.close()
        values = {}
        for n, c in enumerate(report["checks"], 2):
            if c["target"]:
                values[index, f"F{n}"] = float(c["actual"])
                values[index, f"G{n}"] = float(c["difference"])
                values[index, f"H{n}"] = c["status"]
        cache_values(out, values)
        self.assertFalse(audit.verify_audit(out)["blocking"])
        w = load_workbook(out); w[audit.AUDIT_SHEET]["E2"] = "999"; w.save(out); w.close()
        self.assertTrue(audit.verify_audit(out)["blocking"])

    def test_extra_audit_record_is_rejected(self):
        out = Path(self.tmp.name) / "candidate.xlsx"
        audit.append_audit_sheet(self.path, out)
        w = load_workbook(out); w[audit.AUDIT_SHEET].append(["额外记录", "伪通过"]); w.save(out); w.close()
        self.assertTrue(any(x["category"] == "行数" for x in audit.verify_audit(out)["checks"]))

    def test_long_contiguous_provenance_is_compacted_exactly(self):
        w = load_workbook(self.path)
        ws = w["所有商品效果数据导出"]
        # These items have zero amount and intentionally no summary product.
        heads = [c.value for c in ws[1]]
        for i in range(2000):
            ws.append([0 if h != "商品id" else "unused" for h in heads])
        w.save(self.path); w.close()
        r = audit.audit_workbook(self.path)
        self.assertFalse(r["blocking"])
        check = next(x for x in r["checks"] if x["scope"] == "分区说明" and x["category"] == "充值加码消耗")
        self.assertEqual(check["source_refs"], ["所有商品效果数据导出!row:2-2001"])

    def test_product_grouped_provenance_compacts_without_excel_truncation(self):
        w = load_workbook(self.path)
        heads = [c.value for c in w['商品货款'][1]]
        for i in range(2400):
            w['商品货款'].append([0 if h == '账单金额' else ('p1' if i % 2 else 'p2') if h == '商品ID' else None for h in heads])
        row = [None] * 30; row[0] = '测试'; row[2] = 'p2'
        w['公司汇总'].insert_rows(3)
        for col, value in enumerate(row, 1): w['公司汇总'].cell(3, col, value)
        w.save(self.path); w.close()
        report = audit.audit_workbook(self.path)
        check = next(x for x in report['checks'] if x['scope'] == '商品合计' and x['category'] == '收入')
        self.assertEqual(check['source_refs'], ['商品货款!row:2-2403'])
        self.assertLess(len(';'.join(check['source_refs'])), 32767)

    def recache(self, path):
        report = audit.audit_workbook(path)
        w = load_workbook(path)
        index = w.sheetnames.index(audit.AUDIT_SHEET) + 1
        w.close()
        values = {}
        for n, c in enumerate(report["checks"], 2):
            if c["target"]:
                values[index, f"F{n}"] = float(c["actual"])
                values[index, f"G{n}"] = float(c["difference"])
                values[index, f"H{n}"] = c["status"]
        cache_values(path, values)

    def long_candidate(self):
        w = load_workbook(self.path)
        ws = w["所有商品效果数据导出"]
        heads = [c.value for c in ws[1]]
        # Alternating selected/unassigned zero rows defeat range compaction.
        for i in range(4000):
            ws.append([("p1" if i % 2 else "unused") if h == "商品id" else 0 for h in heads])
        expected = audit._report(w, None)
        labels, rows = audit._source_layout(expected["checks"])
        self.assertTrue(rows)
        audit.populate_audit(w)
        out = Path(self.tmp.name) / "long-candidate.xlsx"
        w.save(out); w.close()
        self.recache(out)
        return out, expected, labels, rows

    def test_sparse_long_sources_roundtrip_and_exact_reassembly(self):
        out, report, labels, rows = self.long_candidate()
        self.assertTrue(audit.verify_audit(out)["verified"])
        w = load_workbook(out)
        self.addCleanup(w.close)
        self.assertEqual(list(w[audit.SOURCE_SHEET].values), [audit.SOURCE_HEADERS, *rows])
        for n, (check, label) in enumerate(zip(report["checks"], labels), 2):
            original = ";".join(check["source_refs"])
            self.assertEqual(w[audit.AUDIT_SHEET].cell(n, 4).value or "", label)
            if label != original:
                chunks = [text for owner, _, text in rows if owner == n]
                self.assertEqual("".join(chunks), original)
                self.assertIn(sha256(original.encode("utf-8")).hexdigest(), label)
        for ws in w:
            for row in ws:
                for cell in row:
                    if isinstance(cell.value, str):
                        self.assertLessEqual(len(cell.value.encode("utf-16-le")) // 2, 32767)

    def test_long_source_tampering_is_blocked_with_valid_caches(self):
        out, _, _, _ = self.long_candidate()
        original = out.read_bytes()
        def swap(ws):
            values = [tuple(c.value for c in ws[row]) for row in (2, 3)]
            for n, value in zip((2, 3), reversed(values)):
                for col, item in enumerate(value, 1): ws.cell(n, col, item)
        for operation in ("missing_sheet", "missing_row", "extra_row", "swap", "owner", "sequence", "text", "header", "reference"):
            with self.subTest(operation=operation):
                out.write_bytes(original)
                w = load_workbook(out); ws = w[audit.SOURCE_SHEET]
                if operation == "missing_sheet": del w[audit.SOURCE_SHEET]
                elif operation == "missing_row": ws.delete_rows(2)
                elif operation == "extra_row": ws.append([2, 999, "extra"])
                elif operation == "swap": swap(ws)
                elif operation == "owner": ws["A2"] = 999
                elif operation == "sequence": ws["B2"] = 999
                elif operation == "text": ws["C2"] = ws["C2"].value[:-1]
                elif operation == "header": ws["C1"] = "wrong"
                else:
                    main = w[audit.AUDIT_SHEET]
                    for row in main:
                        if isinstance(row[3].value, str) and "SHA256=" in row[3].value:
                            row[3].value += "tampered"; break
                w.save(out); w.close()
                self.recache(out)  # do not let cleared caches mask evidence failure
                result = audit.verify_audit(out)
                self.assertTrue(result["blocking"])
                self.assertTrue(any(c["scope"] == "审计页" and c["status"] == audit.FAIL for c in result["checks"]))

    def test_layout_preserves_duplicate_refs_order_and_unicode_boundaries(self):
        refs = ["来源!row:2", "来源!row:2", "来源!row:3"] * 1800
        labels, rows = audit._source_layout([{"source_refs": refs}])
        self.assertEqual("".join(row[2] for row in rows), ";".join(refs))
        self.assertEqual([row[1] for row in rows], list(range(1, len(rows) + 1)))
        self.assertEqual(audit._source_layout([{"source_refs": ["x" * 32767]}])[1], [])
        labels, rows = audit._source_layout([{"source_refs": ["😀" * 16384]}])
        self.assertEqual("".join(row[2] for row in rows), "😀" * 16384)
        self.assertTrue(all(len(row[2].encode("utf-16-le")) // 2 <= 30000 for row in rows))
        with patch.object(audit, "EXCEL_ROW_LIMIT", 2):
            with self.assertRaisesRegex(ValueError, "行数上限"):
                audit._source_layout([{"source_refs": refs}])

    def test_short_sources_stay_inline_and_orphan_detail_is_blocked(self):
        out = Path(self.tmp.name) / "short-candidate.xlsx"
        audit.append_audit_sheet(self.path, out)
        self.recache(out)
        self.assertTrue(audit.verify_audit(out)["verified"])
        w = load_workbook(out)
        self.assertNotIn(audit.SOURCE_SHEET, w.sheetnames)
        self.assertEqual(w[audit.AUDIT_SHEET]["D2"].value, "商品货款!row:2-3")
        w.create_sheet(audit.SOURCE_SHEET).append(audit.SOURCE_HEADERS)
        w.save(out); w.close(); self.recache(out)
        self.assertTrue(audit.verify_audit(out)["blocking"])
        w = load_workbook(self.path)
        w.create_sheet(audit.SOURCE_SHEET)
        try:
            with self.assertRaisesRegex(ValueError, "拒绝覆盖"): audit.populate_audit(w)
            self.assertNotIn(audit.AUDIT_SHEET, w.sheetnames)
        finally: w.close()

    def test_external_sources_explicit_mapping(self):
        external = Path(self.tmp.name) / "source.xlsx"
        w = Workbook(); s = w.active; s.append(["账单金额", "商品ID"]); s.append([90, "p1"]); w.save(external); w.close()
        self.assertTrue(audit.audit_workbook(self.path, {"商品货款": external})["blocking"])

    def test_zero_revenue_with_account_fees_warns(self):
        self.change("商品货款", "A2", 20)
        self.change("公司汇总", "F2", 0); self.change("公司汇总", "F3", 0)
        r = audit.audit_workbook(self.path)
        self.assertFalse(r["blocking"])
        self.assertTrue(r["warnings"])


if __name__ == "__main__": unittest.main()
