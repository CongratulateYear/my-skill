"""Credential-free company journal and ordered export → ERP → strict merge.

Single writer advisory lock; local artifacts are verified before reusing stages.
Never retries a successful export merely because downstream login failed.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid
from contextlib import contextmanager


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    temp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    with temp.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


@contextmanager
def locked(path):
    with path.open('a+b') as stream:
        stream.seek(0); stream.write(b'0'); stream.flush(); stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('公司目录正在执行；禁止并发') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def execute(args, folder, export_cmd, merge_cmd, run, gate, gate_cmd, resolve):
    identity = {'company': args.company_name, 'module': args.module,
                'erp_expect_company': getattr(args, 'erp_expect_company', None),
                'dates': {key: getattr(args, key) for key in
                          ('order_pay_start','order_pay_end','write_off_start','write_off_end')},
                'mode': args.mode, 'merge_command': merge_cmd}
    journal_path = folder / '.company-run.json'
    folder.mkdir(parents=True, exist_ok=True)
    with locked(folder / '.company-run.lock'):
        if journal_path.exists():
            if not args.resume:
                raise ValueError('已有公司运行记录；请用 --resume 或新批次目录，不覆盖旧成果')
            journal = json.loads(journal_path.read_text(encoding='utf-8'))
            if journal.get('identity') != identity or journal.get('version') != 1:
                raise ValueError('续跑公司/日期/模式/输出位置不符')
        else:
            if any(folder.glob('*.xlsx')):
                raise ValueError('目录含旧账单但无公司运行记录；请使用新批次，不推断本次完成')
            journal = {'version': 1, 'identity': identity, 'run_id': uuid.uuid4().hex,
                       'stage': 'new'}
            save(journal_path, journal)
        receipt_path = folder / '.export-result.json'
        erp_result = folder / '.erp-result.json'
        # Recover a publication that succeeded just before journal checkpoint.
        if journal['stage'] in ('new', 'export_running'):
            receipt = None
            if receipt_path.exists():
                candidate = json.loads(receipt_path.read_text(encoding='utf-8'))
                if candidate.get('run_id') == journal['run_id']:
                    receipt = candidate
            if receipt is None:
                if gate_cmd:
                    rc, _ = gate(gate_cmd)
                    if rc:
                        return rc
                journal['stage'] = 'export_running'; save(journal_path, journal)
                command = [c for c in export_cmd if c != '--resume']
                if args.resume and (folder / ('.work-' + args.canonical_module)).exists():
                    command.append('--resume')
                rc = run(command + ['--result-file', str(receipt_path), '--run-id', journal['run_id']])
                if rc:
                    return rc
                receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
            if (receipt.get('run_id') != journal['run_id'] or receipt.get('company') != args.company_name
                    or receipt.get('folder') != str(folder.resolve())
                    or receipt.get('dates') != identity['dates']):
                raise ValueError('本次发布凭证身份不符')
            journal['receipt'] = receipt
            journal['hashes'] = receipt['files']
            journal['stage'] = 'exported'; save(journal_path, journal)
        receipt = journal['receipt']
        if {path.name for path in folder.glob('*.xlsx') if not path.name.startswith(('.', '~$'))} != set(journal['hashes']):
            raise ValueError('公司输入目录出现非本次账单或缺少账单；不混入合并')
        fine_names = [name for name in receipt['files'] if name == '处罚数据.xlsx' or
                      name.startswith('处罚数据_') and name.endswith('.xlsx')]
        for name, sha in journal['hashes'].items():
            if Path(name).name != name:
                raise ValueError('运行记录含不安全文件名')
            # Only an interrupted ERP may legitimately have changed product cells.
            if journal['stage'] == 'erp_running' and name in fine_names:
                continue
            if not (folder/name).is_file() or digest(folder/name) != sha:
                raise ValueError('已完成阶段文件已被外部更改：' + name)
        validation = load_module(Path(__file__).with_name('inventory_validation.py'), 'inventory_validation')
        if journal['stage'] in ('exported', 'erp_running'):
            result = receipt.get('results', {}).get('punishment-data', {})
            # Missing/skip is not evidence of successful export.
            if result.get('status') == 'completed':
                if len(fine_names) != 1:
                    raise ValueError('本次罚款必须有唯一可核实Excel')
                fine = folder/fine_names[0]
                jst_script = resolve('淘工厂聚水潭','taogongchang-jushuitan','jushuitan.py')
                matcher = load_module(jst_script.with_name('jst_match.py'), 'chain_match')
                ids = matcher.extract_order_ids(fine)
                if ids:
                    if 'fine_snapshot' not in journal:
                        journal['fine_snapshot'] = validation.snapshot_fine(fine)
                    validation.validate_fine(fine, journal['fine_snapshot'])
                    journal['stage'] = 'erp_running'; save(journal_path, journal)
                    command = [sys.executable, str(jst_script), '--shop-name', args.company_name,
                               '--fine-file', str(fine), '--trigger', 'fine-export',
                               '--result-file', str(erp_result), '--transport', args.erp_transport,
                               '--browser', args.erp_browser]
                    if getattr(args, 'erp_expect_company', None):
                        command += ['--expect-company', args.erp_expect_company]
                    if args.erp_bridge_url:
                        command += ['--bridge-url', args.erp_bridge_url]
                    if (fine.parent/'聚水潭订单'/(args.company_name+'_聚水潭订单.state.json')).exists():
                        command.append('--resume')
                    report = None
                    if erp_result.exists():
                        candidate = json.loads(erp_result.read_text(encoding='utf-8'))
                        proof = candidate.get('receipt', {})
                        output = Path(candidate.get('output', ''))
                        if (proof.get('shop') == args.company_name and proof.get('fine') == str(fine.resolve())
                                and proof.get('order_ids') == ids
                                and proof.get('fine_before_sha256') == receipt['files'][fine.name]
                                and proof.get('fine_after_sha256') == digest(fine)
                                and output.is_file() and proof.get('output_sha256') == digest(output)):
                            report = candidate
                    if report is None:
                        if digest(fine) != receipt['files'][fine.name]:
                            raise ValueError('罚款商品字段已变化但无有效ERP完成凭证；请核实外部备份后恢复原罚款再 --resume，不猜测中断改动')
                        rc = run(command)
                        if rc:
                            print('账单完成，ERP匹配未完成；同一命令 --resume，不重导账单', file=sys.stderr)
                            return rc
                        report = json.loads(erp_result.read_text(encoding='utf-8'))
                    proof = report.get('receipt', {})
                    exported = Path(report.get('output', ''))
                    if (proof.get('shop') != args.company_name or proof.get('fine') != str(fine.resolve())
                            or proof.get('order_ids') != ids
                            or proof.get('fine_after_sha256') != digest(fine)
                            or proof.get('fine_before_sha256') != receipt['files'][fine.name]
                            or not exported.is_file() or proof.get('output_sha256') != digest(exported)):
                        raise ValueError('ERP结果凭证与本次文件不符；不进入合并')
                    journal['fine_validation'] = validation.validate_fine(fine, journal['fine_snapshot'])
                    # Whitelist report fields; never persist network envelopes/URLs.
                    journal['erp'] = {key: report[key] for key in
                                      ('ok','complete','missing_ids','matching') if key in report}
                    journal['hashes'][fine.name] = digest(fine)
                else:
                    journal['erp'] = {'skipped': 'no_order_id'}
            elif result.get('status') == 'zero':
                journal['erp'] = {'skipped': 'confirmed_zero'}
            else:
                journal['erp'] = {'skipped': 'not_exported_this_run'}
            journal['stage'] = 'erp_done'; save(journal_path, journal)
        if merge_cmd and journal['stage'] == 'erp_done':
            rc = run(merge_cmd)
            if rc:
                return rc
            journal['stage'] = 'merged'; save(journal_path, journal)
        if merge_cmd:
            settlement = args.settlement_date or f'{args.write_off_start}_{args.write_off_end}'
            output = (args.output_dir.expanduser().resolve() if args.output_dir else folder.parent) / (args.company_name+'_'+settlement+'.xlsx')
            merge_script = resolve('淘工厂合并','taogongchang-merge','merge_excel.py')
            template = merge_script.parent.parent/'assets'/'盘算模板.xlsx'
            if not template.exists():
                candidates = list((merge_script.parent.parent).rglob('*.xlsx'))
                if len(candidates) != 1:
                    raise ValueError('无法唯一定位校验模板')
                template = candidates[0]
            if journal['stage'] == 'complete' and digest(output) != journal.get('main_sha256'):
                raise ValueError('已交付主表已被外部更改；不自动覆盖')
            journal['validation'] = validation.validate_inventory(output,args.company_name,folder,template)
            journal['main_sha256'] = digest(output)
        journal['stage'] = 'complete'; save(journal_path,journal)
        print(json.dumps({'company':args.company_name,'stage':'complete','erp':journal.get('erp'),
                          'validation':journal.get('validation')},ensure_ascii=False))
        return 0
