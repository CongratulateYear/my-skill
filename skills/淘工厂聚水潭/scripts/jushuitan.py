"""聚水潭 orchestration: export normally; enrich only explicit fine-export triggers."""
from __future__ import annotations
import argparse,json,re
from pathlib import Path
from jst_export import run_export,unpack_export,ExportError
from jst_match import extract_order_ids,match_fines,MatchError
from jst_session import build_session,SessionError

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--order-ids', help='主订单号，逗号/换行分隔，作为文本')
    source.add_argument('--fine-file',help='从罚款Excel提取订单号；仅显式trigger时回填')
    p.add_argument('--shop-name',required=True)
    p.add_argument('--trigger',choices=['fine-export'])
    p.add_argument('--sheet');p.add_argument('--order-sheet')
    p.add_argument('--output-dir',help='ERP下载目录；罚款默认放原表下的聚水潭订单子目录')
    p.add_argument('--resume',action='store_true',help='复用同一任务状态，绝不重复创建不确定任务')
    p.add_argument('--backup-dir',help='罚款原表备份目录，必须在合并输入目录之外')
    p.add_argument('--require-all-orders',action='store_true',help='有未查到订单则停止；默认导出已查到部分并明确报告')
    p.add_argument('--transport',choices=['auto','cookie','bridge'],default='auto')
    p.add_argument('--browser',default='auto')
    p.add_argument('--bridge-url');p.add_argument('--expect-company')
    p.add_argument('--allow-download-domain',action='append',default=[])
    p.add_argument('--dry-run',action='store_true',help='仅验证输入，不读cookie/网络/写文件')
    p.add_argument('--result-file',help='安全结果摘要JSON，供编排续跑')
    a=p.parse_args(argv)
    if not a.shop_name.strip() or any(x in a.shop_name for x in '/\\:*?"<>|') or a.shop_name in ('.','..') or a.shop_name.endswith(('.', ' ')):
        p.error('店铺名称不可含路径/非法字符')
    if a.trigger and not a.fine_file:p.error('fine-export触发必须指定--fine-file')
    try:
        if a.fine_file:
            fine=Path(a.fine_file).resolve();ids=extract_order_ids(fine,a.sheet)
            directory=Path(a.output_dir).expanduser().resolve() if a.output_dir else fine.parent/'聚水潭订单'
            if directory==fine.parent:raise ValueError('ERP下载必须放独立子目录或合并输入目录之外')
        else:
            ids=list(dict.fromkeys(x for x in re.split(r'[,，;；\s]+',a.order_ids.strip()) if x))
            if not ids or any(not x.isdigit() for x in ids):raise ValueError('订单号必须是十进制文本')
            directory=Path(a.output_dir).expanduser().resolve() if a.output_dir else Path.home()/'Downloads'/'聚水潭'
        target=directory/(a.shop_name+'_聚水潭订单')
        if not a.resume and (target.with_suffix('.xlsx').exists() or target.with_suffix('.zip').exists()):
            raise ValueError('订单下载输出已存在，用--resume核实原任务或指定新目录')
        if a.dry_run:
            print(json.dumps({'dryRun':True,'orderCount':len(ids),'outputBase':str(target),'matching':bool(a.trigger)},ensure_ascii=False));return 0
        import hashlib
        fine_before_sha=hashlib.sha256(fine.read_bytes()).hexdigest() if a.fine_file else None
        built=build_session(a.transport,a.browser,a.bridge_url,a.expect_company)
        report=run_export(built['session'],built['headers'],ids,target,extra_domains=tuple(a.allow_download_domain),resume=a.resume,allow_missing=not a.require_all_orders)
        if a.trigger and report.get('output'):
            orders=unpack_export(report['output'])
            report['matching']=match_fines(fine,orders,a.shop_name,a.trigger,sheet=a.sheet,order_sheet=a.order_sheet,backup_dir=a.backup_dir)
        if a.result_file:
            import os,uuid
            report['receipt']={'shop':a.shop_name,'fine':str(fine) if a.fine_file else None,
                               'order_ids':ids,'fine_before_sha256':fine_before_sha,
                               'fine_after_sha256':hashlib.sha256(fine.read_bytes()).hexdigest() if a.fine_file else None,
                               'output_sha256':hashlib.sha256(Path(report['output']).read_bytes()).hexdigest()}
            destination=Path(a.result_file)
            destination.parent.mkdir(parents=True,exist_ok=True)
            temporary=destination.with_name(destination.name+'.tmp-'+uuid.uuid4().hex)
            with temporary.open('x',encoding='utf-8') as stream:
                json.dump(report,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,destination)
        print(json.dumps(report,ensure_ascii=False,indent=2));return 0
    except (SessionError,ExportError,MatchError,ValueError,OSError) as exc:
        p.exit(3 if isinstance(exc,SessionError) else 2,str(exc)+'\n')
if __name__=='__main__':raise SystemExit(main())
