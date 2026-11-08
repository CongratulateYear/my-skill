"""聚水潭 orchestration: export normally; enrich only explicit fine-export triggers."""
from __future__ import annotations
import argparse,json,re
from pathlib import Path
from jst_export import run_export,unpack_export,ExportError,can_resume_offline,export_error_diagnostic
from jst_match import extract_order_ids,match_fines,MatchError
from jst_session import build_session,SessionError,recovery_action
from tgc_runtime import runtime_for

def publish_orders(sources, target, resume=False):
    """Publish every validated ERP shard; refuse changed existing business files."""
    from jst_export import _atomic_create
    import hashlib
    plans=[]
    for index, source in enumerate(sources, 1):
        output=target.with_name(target.name+('' if index == 1 else '_'+str(index))).with_suffix('.xlsx')
        content=Path(source).read_bytes()
        if output.exists() and (not resume or output.is_symlink() or output.read_bytes()!=content):
            raise ValueError('ERP交付文件已存在或被修改；不覆盖：'+str(output))
        plans.append((output,content))
    outputs=[]
    for output,content in plans:
        if not output.exists():
            _atomic_create(output,content)
        outputs.append(output)
    if not outputs:raise ValueError('ERP无有效Excel分片')
    return outputs


def save_result(report, destination):
    """Persist full safe results on success AND failure, never network payloads."""
    if not destination:
        return
    import os, uuid
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name+'.tmp-'+uuid.uuid4().hex)
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, destination)


def result_status(report, trigger):
    """Execution completion is independent of business coverage (legacy ok unchanged)."""
    report['execution_status'] = 'completed'
    report['missing_order_count'] = len(set(report.get('missing_ids', [])))
    matching = report.get('matching')
    if isinstance(matching, dict):
        counts = matching.get('counts', {})
        review = sum(counts.get(key, 0) for key in ('unmatched', 'ambiguous', 'invalid', 'conflict'))
        report['review_row_count'] = review
        for key in ('review_amount', 'review_amount_known_rows', 'review_amount_unknown_rows'):
            if key in matching:
                report[key] = matching[key]
        report['matching_status'] = 'partial_match' if review or report['missing_order_count'] else 'complete_match'
    else:
        report['matching_status'] = 'not_run' if trigger else 'not_requested'
    report['coverage_status'] = 'partial_match' if report['missing_order_count'] else 'complete'
    return report


def stdout_summary(report):
    """Small allowlisted view; full safe business details remain in result-file."""
    keys = ('ok', 'complete', 'requested_orders', 'matched_orders',
            'exported_internal_orders', 'missing_ids', 'execution_status', 'matching_status',
            'coverage_status', 'missing_order_count', 'review_row_count', 'review_amount',
            'review_amount_known_rows', 'review_amount_unknown_rows', 'recovery_action',
            'error_code', 'error_details', 'export_recovery', 'stage', 'preparation_status')
    summary = {key: report[key] for key in keys if key in report}
    if isinstance(report.get('matching'), dict):
        matching = report['matching']
        summary['matching'] = {key: matching[key] for key in ('counts', 'reason_counts')
                               if key in matching}
    return summary


def load_scope_approval(path):
    """Explicit local authorization only; reject credentials/unknown JSON fields."""
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > 1024 * 1024:
        raise ExportError('授权文件不是有界普通JSON文件', code='scope_approval_invalid')
    try:
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate key')
                result[key] = value
            return result
        value = json.loads(candidate.read_text(encoding='utf-8'), object_pairs_hook=unique_object)
        fields = {'approved', 'task_id', 'sha256', 'output', 'requested_ids', 'extra_ids'}
        if not isinstance(value, dict) or set(value) != fields or value['approved'] is not True:
            raise ValueError('schema')
        if (not isinstance(value['task_id'], str) or not re.fullmatch(r'[0-9]+', value['task_id']) or
                not isinstance(value['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', value['sha256']) or
                not isinstance(value['output'], str) or not Path(value['output']).is_absolute()):
            raise ValueError('binding')
        for key in ('requested_ids', 'extra_ids'):
            ids = value[key]
            if (not isinstance(ids, list) or not ids or
                    any(not isinstance(item, str) or not re.fullmatch(r'[0-9]+', item) for item in ids) or
                    len(ids) != len(set(ids))):
                raise ValueError('ids')
        if value['extra_ids'] != sorted(value['extra_ids']):
            raise ValueError('extra ordering')
        return value
    except (ValueError, TypeError, OSError, UnicodeError):
        raise ExportError('授权JSON格式或字段不合法', code='scope_approval_invalid') from None


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--order-ids', help='主订单号，逗号/换行分隔，作为文本')
    source.add_argument('--fine-file',help='从罚款Excel提取订单号；仅显式trigger时回填')
    p.add_argument('--shop-name',required=True)
    p.add_argument('--trigger',choices=['fine-export'])
    p.add_argument('--sheet');p.add_argument('--order-sheet')
    p.add_argument('--output-dir',help='ERP下载目录；罚款默认放原表下的聚水潭订单子目录')
    p.add_argument('--scope-approval-file', help='显式范围例外授权JSON；仅同一任务--resume，不能自动批准')
    p.add_argument('--resume',action='store_true',help='复用同一任务状态，绝不重复创建不确定任务')
    p.add_argument('--backup-dir',help='罚款原表备份目录，必须在合并输入目录之外')
    p.add_argument('--require-all-orders',action='store_true',help='有未查到订单则停止；默认导出已查到部分并明确报告')
    p.add_argument('--transport',choices=['auto','cookie','bridge'],default='auto')
    p.add_argument('--browser',default='auto')
    p.add_argument('--bridge-url');p.add_argument('--expect-company')
    p.add_argument('--allow-download-domain',action='append',default=[])
    p.add_argument('--dry-run',action='store_true',help='仅验证输入，不读cookie/网络/写文件')
    p.add_argument('--result-file',help='完整安全结果JSON（含逐行匹配报告），供编排和待核查页')
    p.add_argument('--verbose',action='store_true',help='stdout输出完整安全业务结果；默认仅统计、原因分类和缺单')
    p.add_argument('--prepare-only',action='store_true',help='离线验证本次罚款输入并返回agent预备计划；不启动桥/注入/网络')
    p.add_argument('--fine-export-succeeded',action='store_true',help='编排确认本次罚款导出成功，仅供prepare-only条件门')
    a=p.parse_args(argv)
    if not a.shop_name.strip() or any(x in a.shop_name for x in '/\\:*?"<>|') or a.shop_name in ('.','..') or a.shop_name.endswith(('.', ' ')):
        p.error('店铺名称不可含路径/非法字符')
    if a.trigger and not a.fine_file:p.error('fine-export触发必须指定--fine-file')
    report = {'ok': False, 'complete': False, 'execution_status': 'execution_failed',
              'matching_status': 'not_run' if a.trigger else 'not_requested'}
    safe_result_path = None
    stage = 'input_validation'
    try:
        scope_approval = None
        if a.scope_approval_file:
            if not a.resume or a.prepare_only or a.dry_run:
                raise ExportError('范围授权仅用于真实同任务续跑', code='scope_approval_invalid')
            scope_approval = load_scope_approval(a.scope_approval_file)
        if a.prepare_only and not (a.trigger == 'fine-export' and a.fine_file and a.fine_export_succeeded):
            report.update(execution_status='not_started', preparation_status='skipped',
                          error_code='current_fine_export_not_confirmed')
            print(json.dumps(stdout_summary(report), ensure_ascii=False)); return 0
        if a.fine_file:
            fine=Path(a.fine_file).resolve();ids=extract_order_ids(fine,a.sheet)
            directory=Path(a.output_dir).expanduser().resolve() if a.output_dir else fine.parent/'聚水潭订单'
            if directory==fine.parent:raise ValueError('ERP下载必须放独立子目录或合并输入目录之外')
        else:
            ids=list(dict.fromkeys(x for x in re.split(r'[,，;；\s]+',a.order_ids.strip()) if x))
            if not ids or any(not x.isdigit() for x in ids):raise ValueError('订单号必须是十进制文本')
            directory=Path(a.output_dir).expanduser().resolve() if a.output_dir else Path.home()/'Downloads'/'聚水潭'
        if not ids:
            raise ValueError('本次罚款没有有效订单；不预备ERP')
        target=directory/(a.shop_name+'_聚水潭订单')
        if a.result_file:
            result_path=Path(a.result_file).expanduser().resolve()
            forbidden=[directory.resolve()]+([fine.parent] if a.fine_file else [])
            if any(result_path==parent or parent in result_path.parents for parent in forbidden):
                raise ValueError('结果状态文件必须位于Excel交付目录之外')
            safe_result_path = result_path
        report['requested_orders'] = len(ids)
        if a.prepare_only:
            report.update(execution_status='not_started', preparation_status='agent_required',
                          recovery_action=recovery_action('preparation_required', a.expect_company, a.bridge_url))
            save_result(report, safe_result_path)
            print(json.dumps(stdout_summary(report), ensure_ascii=False)); return 0
        if not a.resume and (target.with_suffix('.xlsx').exists() or target.with_suffix('.zip').exists()):
            raise ValueError('订单下载输出已存在，用--resume核实原任务或指定新目录')
        if a.dry_run:
            print(json.dumps({'dryRun':True,'orderCount':len(ids),'outputBase':str(target),'matching':bool(a.trigger)},ensure_ascii=False));return 0
        import hashlib
        fine_before_sha=hashlib.sha256(fine.read_bytes()).hexdigest() if a.fine_file else None
        stage = 'export_state_validation'
        state_path = runtime_for(target)/'export.state.json'
        if a.resume and can_resume_offline(state_path, ids, runtime_for(target)/'orders'):
            built = {'session': None, 'headers': {}}
        else:
            stage = 'session_validation'
            built=build_session(a.transport,a.browser,a.bridge_url,a.expect_company)
        stage = 'export'
        export_options = {'scope_approval': scope_approval} if scope_approval is not None else {}
        report=run_export(built['session'],built['headers'],ids,runtime_for(target)/'orders',extra_domains=tuple(a.allow_download_domain),resume=a.resume,allow_missing=not a.require_all_orders,state_path=runtime_for(target)/'export.state.json',**export_options)
        if report.get('output'):
            stage = 'publish'
            orders=unpack_export(report['output'])
            outputs=publish_orders(orders,target,a.resume)
            report['output']=str(outputs[0])
            report['outputs']=[str(path) for path in outputs]
            if a.trigger:
                stage = 'matching'
                report['matching']=match_fines(fine,outputs,a.shop_name,a.trigger,sheet=a.sheet,order_sheet=a.order_sheet,backup_dir=a.backup_dir)
        result_status(report, a.trigger)
        if a.result_file:
            report['receipt']={'shop':a.shop_name,'fine':str(fine) if a.fine_file else None,
                               'order_ids':ids,'fine_before_sha256':fine_before_sha,
                               'fine_after_sha256':hashlib.sha256(fine.read_bytes()).hexdigest() if a.fine_file else None,
                               'output_sha256':hashlib.sha256(Path(report['output']).read_bytes()).hexdigest(),
                               'outputs_sha256':{path:hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in report['outputs']}}
            save_result(report, safe_result_path)
        print(json.dumps(report if a.verbose else stdout_summary(report),ensure_ascii=False,indent=2));return 0
    except (SessionError,ExportError,MatchError,ValueError,OSError) as exc:
        report.update(ok=False, complete=False, execution_status='execution_failed', stage=stage,
                      matching_status='execution_failed' if stage == 'matching' else 'not_run' if a.trigger else 'not_requested',
                      error_code=getattr(exc, 'code', type(exc).__name__))
        if isinstance(exc, ExportError):
            diagnostic = export_error_diagnostic(exc)
            report['error_code'] = diagnostic['code']
            report['error_details'] = diagnostic['details']
            report['export_recovery'] = diagnostic['recovery']
        # Do not serialize raw exception text: third-party errors may contain signed URLs.
        action = (recovery_action(getattr(exc, 'code', 'execution_failed'), a.expect_company, a.bridge_url)
                  if isinstance(exc, SessionError) else
                  {'type': 'resolve-error-before-resume', 'executor': 'agent',
                   'pythonControlsBrowser': False, 'resumePolicy': {
                       'sameInputs': True, 'sameOutputDirectory': True,
                       'preserveTaskState': True, 'neverRecreateUncertainTask': True}})
        action['resumePolicy']['exportAttempted'] = stage in ('export', 'publish', 'matching')
        action['resumePolicy']['requiredResume'] = a.resume or action['resumePolicy']['exportAttempted']
        report['recovery_action'] = action
        save_result(report, safe_result_path)
        print(json.dumps(report if a.verbose else stdout_summary(report), ensure_ascii=False, indent=2))
        return 3 if isinstance(exc, SessionError) else 2
if __name__=='__main__':raise SystemExit(main())
