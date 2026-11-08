import sys
from pathlib import Path
import tempfile
import unittest
from openpyxl import Workbook,load_workbook
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import delivery_enrichment as d

class DeliveryTests(unittest.TestCase):
    def test_review_literal_ids_amount_and_original_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); main=root/'main.xlsx';fine=root/'fine.xlsx';out=root/'review.xlsx'
            w=Workbook();w.active.title='公司汇总';w.active['A1']='=1+2';w.save(main);w.close()
            w=Workbook();s=w.active;s.title='处罚数据';s.append(['主订单编号','处罚金额']);s.append(['123',8.5]);w.save(fine);w.close()
            original=main.read_bytes();fbytes=fine.read_bytes()
            erp={'matching':{'sheet':'处罚数据','reason_counts':{'erp_missing':1},'report':[{'row':2,'order_ids':['=123'],'reason_code':'erp_missing'}]}}
            result=d.add_review(main,out,erp,fine)
            self.assertEqual(result['rows'],1)
            w=load_workbook(out);self.assertEqual(w['公司汇总']['A1'].value,'=1+2')
            self.assertEqual(w['待核查']['C2'].data_type,'s');self.assertEqual(w['待核查']['F2'].value,8.5);w.close()
            self.assertEqual(original,main.read_bytes());self.assertEqual(fbytes,fine.read_bytes())
    def test_lean_delivery_skips_audit_and_keeps_review_coverage(self):
        from unittest.mock import patch
        import shutil
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);main=root/'main.xlsx';Workbook().save(main)
            events=[]
            def copy(source,destination,*a,**kw):shutil.copyfile(source,destination);return {}
            with patch.object(d,'recalculate',side_effect=copy):
                result = d.enrich(main,{},None,root,progress=events.append)
            self.assertEqual(events,['delivery_review','delivery_coverage'])
            self.assertTrue((root/'delivery-candidate.xlsx').exists())
            self.assertEqual(result['validation_profile'],'lean')
            self.assertEqual(result['audit']['status'],'not_performed')
            self.assertIsNone(result['audit']['verified'])

    def test_lean_cache_failure_never_publishes(self):
        from unittest.mock import patch
        import shutil
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); main=root/'main.xlsx'; Workbook().save(main)
            def copy(source,destination,*a,**kw):
                shutil.copyfile(source,destination); return {}
            with patch.object(d,'recalculate',side_effect=ValueError('cache invalid')):
                with self.assertRaisesRegex(ValueError,'cache invalid'):
                    d.enrich(main,{},None,root)
            self.assertFalse((root/'delivery-candidate.xlsx').exists())

    def test_single_pass_matches_two_step_preparation(self):
        from unittest.mock import patch
        import shutil
        import zipfile
        import delivery_coverage as coverage
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);main=root/'main.xlsx';fine=root/'fine.xlsx'
            w=Workbook();s=w.active;s.title='公司汇总'
            s.append(['商品ID','公式','空串']);s.append(['001','=1+2',''])
            s['A2'].number_format='@';s.merge_cells('D1:E1');s['D1']='合并'
            s.freeze_panes='A2';s.row_dimensions[2].height=24
            w.create_sheet('原明细').append(['原始数据',42,True]);w.save(main);w.close()
            w=Workbook();w.active.title='处罚数据'
            w.active.append(['主订单编号','处罚金额']);w.active.append(['123',8.5]);w.save(fine);w.close()
            erp={'matching':{'sheet':'处罚数据','reason_counts':{'erp_missing':1},'report':[{'row':2,'order_ids':['=123'],'reason_code':'erp_missing'}]}}
            receipt={'company':'=literal','results':{next(iter(coverage.MODULES)):{'status':'zero','queryRows':0}}}
            before=main.read_bytes();fine_before=fine.read_bytes()
            review=d.add_review(main,root/'review.xlsx',erp,fine)
            expected=coverage.add_coverage(root/'review.xlsx',root/'expected.xlsx',receipt)
            def copy(source,destination,**kw):
                shutil.copyfile(source,destination);return {'ok':True}
            original_save=Workbook.save
            with patch.object(d,'load_workbook',wraps=d.load_workbook) as loads, \
                    patch.object(Workbook,'save',autospec=True,side_effect=original_save) as saves, \
                    patch.object(coverage,'load_workbook',side_effect=AssertionError('coverage reload')), \
                    patch.object(d,'recalculate',side_effect=copy):
                result=d.enrich(main,erp,fine,root,receipt)
            self.assertEqual(saves.call_count,1)
            # Main parsed once, fine parsed once; coverage never reparses main.
            self.assertEqual([Path(call.args[0]) for call in loads.call_args_list],[main,fine])
            self.assertEqual(result['review'],review);self.assertEqual(result['coverage'],expected)
            self.assertEqual(main.read_bytes(),before);self.assertEqual(fine.read_bytes(),fine_before)
            with zipfile.ZipFile(root/'expected.xlsx') as old, zipfile.ZipFile(result['candidate']) as new:
                self.assertEqual(old.namelist(),new.namelist())
                for name in old.namelist():
                    old_content, new_content = old.read(name), new.read(name)
                    if name == 'docProps/core.xml':
                        # Separate saves can cross a wall-clock second. Compare
                        # all metadata except openpyxl's generated modified time.
                        import xml.etree.ElementTree as ET
                        left, right = ET.fromstring(old_content), ET.fromstring(new_content)
                        modified = '{http://purl.org/dc/terms/}modified'
                        left.find(modified).text = right.find(modified).text = 'fixed'
                        old_content, new_content = ET.tostring(left), ET.tostring(right)
                    self.assertEqual(old_content,new_content,name)
            w=load_workbook(result['candidate']);self.assertEqual(w.sheetnames,['公司汇总','原明细','待核查','执行说明']);w.close()

    def test_preparation_failures_preserve_inputs_and_existing_candidate(self):
        from unittest.mock import patch
        import delivery_coverage as coverage
        for failure in ('review','coverage','duplicate_review','duplicate_coverage'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as td:
                root=Path(td);main=root/'main.xlsx';w=Workbook()
                if failure.startswith('duplicate_'):
                    w.create_sheet('待核查' if failure=='duplicate_review' else '执行说明')
                w.save(main);w.close();before=main.read_bytes()
                candidate=root/'delivery-candidate.xlsx';candidate.write_bytes(b'previous candidate')
                erp={'matching':{'reason_counts':{'erp_missing':1},'report':[]}} if failure=='review' else {}
                receipt={'results':{next(iter(coverage.MODULES)):{'status':'zero','queryRows':2}}} if failure=='coverage' else None
                with patch.object(d,'recalculate') as recalc:
                    with self.assertRaises(ValueError):d.enrich(main,erp,None,root,receipt)
                recalc.assert_not_called()
                self.assertEqual(main.read_bytes(),before)
                self.assertEqual(candidate.read_bytes(),b'previous candidate')
                self.assertEqual(list(root.glob('delivery-*')), [candidate])

    def test_recalculation_substeps_and_engine_only_timeout(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        import importlib.util
        events = []
        helper = SimpleNamespace(transplant_caches=lambda *a: events.append('transfer') or {'formula_caches':1})
        spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _:None))
        with patch.object(d.Path,'is_file',return_value=True), \
                patch.object(importlib.util,'spec_from_file_location',return_value=spec), \
                patch.object(importlib.util,'module_from_spec',return_value=helper), \
                patch.object(d.subprocess,'run',side_effect=lambda *a,**kw: events.append(('engine',kw))) as engine, \
                patch.object(d,'verify_calculated',side_effect=AssertionError('duplicate scan')):
            with tempfile.TemporaryDirectory() as td:
                result = d.recalculate(Path(td)/'input.xlsx',Path(td)/'out.xlsx',progress=events.append)
        self.assertEqual(events[0],'delivery_engine')
        self.assertEqual(events[1],('engine',{'check':True,'timeout':600}))
        self.assertEqual(events[2:],['delivery_cache_transfer','transfer'])
        self.assertEqual(result['cache_transfer'],{'formula_caches':1})
        self.assertTrue(result['ok'])
        engine.assert_called_once()

    def test_missing_cache_and_engine_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'x.xlsx';w=Workbook();w.active['A1']='=1+2';w.save(p);w.close()
            with self.assertRaisesRegex(ValueError,'cache'):d.verify_calculated(p,p)
            with self.assertRaisesRegex(ValueError,'unavailable'):d.recalculate(p,Path(td)/'out.xlsx',node=Path(td)/'missing',cli=Path(td)/'missing')
    def test_explicit_empty_string_cache_accepted(self):
        import zipfile
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/'a.xlsx';b=Path(td)/'b.xlsx';w=Workbook();w.active['A1']='=IFERROR(1/0,"")';w.save(a);w.close()
            with zipfile.ZipFile(a) as src,zipfile.ZipFile(b,'w') as dst:
                for item in src.infolist():
                    data=src.read(item.filename)
                    if item.filename=='xl/worksheets/sheet1.xml':
                        data=data.replace(b'<c r="A1">',b'<c r="A1" t="str">')
                    dst.writestr(item,data)
            self.assertEqual(d.verify_calculated(a,b)['formula_count'],1)

    def test_review_report_truncation_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'x.xlsx';w=Workbook();w.save(p);w.close()
            with self.assertRaisesRegex(ValueError,'row count'):
                d.add_review(p,Path(td)/'out.xlsx',{'matching':{'reason_counts':{'erp_missing':2},'report':[]}},None)

    def test_existing_error_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'error.xlsx';w=Workbook();w.active['A1']='#VALUE!';w.save(p);w.close()
            with self.assertRaisesRegex(ValueError,'workbook error'):d.verify_calculated(p,p)

    def test_changed_array_formula_rejected(self):
        from openpyxl.worksheet.formula import ArrayFormula
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/'a.xlsx';b=Path(td)/'b.xlsx';w=Workbook()
            w.active['A1']=ArrayFormula(ref='A1:A2',text='=ROW(A1:A2)');w.save(a)
            w.active['A1']=ArrayFormula(ref='A1:A2',text='=ROW(A1:A2)+1');w.save(b);w.close()
            with self.assertRaisesRegex(ValueError,'Formula changed'):d.verify_calculated(a,b)

    def test_changed_formula_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/'a.xlsx';b=Path(td)/'b.xlsx';w=Workbook();w.active['A1']='=1+2';w.save(a);w.active['A1']='=999';w.save(b);w.close()
            with self.assertRaisesRegex(ValueError,'Formula changed'):d.verify_calculated(a,b)

    def test_dropped_formula_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/'a.xlsx';b=Path(td)/'b.xlsx';w=Workbook();w.active['A1']='=1+2';w.save(a);w.active['A1']=3;w.save(b);w.close()
            with self.assertRaisesRegex(ValueError,'Formula lost'):d.verify_calculated(a,b)

if __name__=='__main__':unittest.main()
