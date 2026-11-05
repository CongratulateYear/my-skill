"""Offline company orchestration test: real XLSX matching/merge/validation.

Only run()'s network-facing export and ERP acquisition are synthetic. The final
merge uses the real strict builder and company_run loads real validation.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parent / 'skills'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


company = load('company_real_run', '淘工厂资产盘算/scripts/company_run.py')
builder = load('company_real_builder', '淘工厂合并/scripts/pan_suan_builder.py')
matcher = load('company_real_match', '淘工厂聚水潭/scripts/jst_match.py')
TEMPLATE = ROOT / '淘工厂合并/assets/盘算模板.xlsx'


class CompanyRealTest(unittest.TestCase):
    def test_full_chain_merges_once_after_authoritative_match_then_resume_reuses(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = root / '公司_2026-10-01_2026-10-31'
            main = root / '公司_2026-10-01_2026-10-31.xlsx'
            args = SimpleNamespace(company_name='公司', module='all', canonical_module='all',
                                   order_pay_start='2026-10-01', order_pay_end='2026-10-31',
                                   write_off_start='2026-10-01', write_off_end='2026-10-31',
                                   mode='full', resume=False, erp_transport='auto', erp_browser='auto',
                                   erp_bridge_url=None, settlement_date=None, output_dir=None)
            events = []
            fine = folder / '处罚数据.xlsx'
            expected = ['ERP authoritative title', 'ERP authoritative id']

            def resolve(chinese, english, script):
                return ROOT / chinese / 'scripts' / script

            def field(command, flag):
                return command[command.index(flag) + 1]

            def run(command):
                if command[0] == 'synthetic-export':
                    events.append('export')
                    wb = load_workbook(TEMPLATE)
                    headers = [c.value for c in wb['处罚数据'][1]]
                    wb.close()
                    wb = Workbook()
                    ws = wb.active
                    ws.title = '处罚数据'
                    ws.append(headers)
                    values = {'店铺': '公司', '商品标题': 'old title', '商品id': 'old id',
                              '主订单号': '123456', '主订单编号': '123456', '处罚金额': 7.25}
                    ws.append([values.get(h) for h in headers])
                    wb.save(fine)
                    wb.close()
                    receipt = {'run_id': field(command, '--run-id'), 'company': '公司',
                               'folder': str(folder.resolve()),
                               'dates': {k: getattr(args, k) for k in ('order_pay_start', 'order_pay_end', 'write_off_start', 'write_off_end')},
                               'files': {fine.name: company.digest(fine)},
                               'results': {'punishment-data': {'status': 'completed'}}}
                    Path(field(command, '--result-file')).write_text(json.dumps(receipt), encoding='utf-8')
                    return 0
                if 'jushuitan.py' in Path(command[1]).name:
                    events.append('erp')
                    erp_dir = folder / '聚水潭订单'
                    erp_dir.mkdir()
                    erp = erp_dir / '公司_聚水潭订单.xlsx'
                    wb = Workbook()
                    ws = wb.active
                    ws.append(['原始线上订单号', '商品名称', '店铺款式编码'])
                    ws.append(['123456', *expected])
                    wb.save(erp)
                    wb.close()
                    before_sha = company.digest(fine)
                    order_ids = matcher.extract_order_ids(fine)
                    report = matcher.match_fines(fine, [erp], '公司', 'fine-export')
                    erp_receipt = {'shop': '公司', 'fine': str(fine.resolve()),
                                   'order_ids': order_ids, 'fine_before_sha256': before_sha,
                                   'fine_after_sha256': company.digest(fine),
                                   'output_sha256': company.digest(erp)}
                    Path(field(command, '--result-file')).write_text(json.dumps({'ok': True, 'complete': True,
                        'output': str(erp.resolve()), 'receipt': erp_receipt,
                        'missing_ids': [], 'matching': report}), encoding='utf-8')
                    return 0
                if command[0] == 'real-merge':
                    events.append('merge')
                    self.assertEqual(events, ['export', 'erp', 'merge'])
                    self.assertIn('--strict', command)
                    builder.merge(TEMPLATE, [fine], main, strict=True, shop_name='公司')
                    return 0
                self.fail('Unexpected command: ' + repr(command))

            merge_cmd = ['real-merge', '--strict']
            rc = company.execute(args, folder, ['synthetic-export'], merge_cmd, run,
                                 lambda command: self.fail('Unexpected network gate'), None, resolve)
            self.assertEqual(rc, 0)
            self.assertEqual(events.count('merge'), 1)
            journal = json.loads((folder / '.company-run.json').read_text(encoding='utf-8'))
            self.assertEqual(journal['stage'], 'complete')
            self.assertTrue(journal['validation']['ok'])
            self.assertEqual(journal['fine_validation']['changed_cells'], 2)
            wb = load_workbook(main)
            ws = wb['处罚数据']
            positions = {c.value: c.column for c in ws[1]}
            self.assertEqual([ws.cell(2, positions[h]).value for h in ('商品标题', '商品id')], expected)
            self.assertEqual(ws.cell(2, positions['处罚金额']).value, 7.25)
            wb.close()
            source = load_workbook(fine)
            output = load_workbook(main)
            self.assertEqual(list(source['处罚数据'].values), list(output['处罚数据'].values)[:2])
            source.close()
            output.close()
            args.resume = True
            self.assertEqual(company.execute(args, folder, ['synthetic-export'], merge_cmd, run,
                             lambda command: self.fail('Unexpected gate on resume'), None, resolve), 0)
            self.assertEqual(events, ['export', 'erp', 'merge'])


if __name__ == '__main__':
    unittest.main()
