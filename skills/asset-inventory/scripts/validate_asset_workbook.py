"""Independent read-only asset workbook validator. Never saves an input workbook.
Run with the fixed DSH Python; --help lists reusable arguments.
Source rows are matched by ALL mapped columns, using an in-memory SQLite set.
Only identifier normalization and numeric representation normalization are allowed.
"""
from __future__ import annotations
import argparse, contextlib, datetime, hashlib, json, re, sqlite3, uuid, zipfile
from pathlib import Path
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / 'assets' / '盘算模板.xlsx'
HEADERS = ['店铺','运营姓名','商品ID','产品名称','数量','收入','交易补贴','交易佣金','先用后付','消费者体验提升服务','客服服务费','新疆物流集运','出海增值服务费','赔付追缴','新品营销(营销费用)','评价有礼','商业化账户充值提现','充值加码消耗','智能优惠券金额','投流推广佣金','淘客推广佣金','直播推广佣金','刷单金额','罚款','直通车营销','收入金额','收款人','货款','利润','备注（查询不到的）']
TARGETS = ['消费者体验提升服务','商品货款','交易补贴','交易佣金','先用后付','新疆物流集运','评价有礼','营销费用','商业化账户充值提现','投流推广佣金','淘客推广佣金','直播推广佣金','客服服务费','赔付追缴','处罚数据','出海增值服务费','所有商品效果数据导出']
SUM_HEADERS = ['数量','收入','交易补贴','交易佣金','先用后付','消费者体验提升服务','新疆物流集运','出海增值服务费','新品营销(营销费用)','充值加码消耗','智能优惠券金额','投流推广佣金','淘客推广佣金','直播推广佣金','刷单金额','罚款']
WHOLE = ['客服服务费','赔付追缴','评价有礼','商业化账户充值提现']
NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
IDENT = ('ID','Id','id','订单号','单据号','流水号','业务单号','账单编号','运单号','处罚业务')
def cellstyle(wb, cell):
    return wb._cell_styles[getattr(cell,'_style_id',0)]
def nonempty(v): return v is not None and str(v).strip() != ''
def norm(v): return re.sub(r'\s+', '', str(v or '').strip())
def ident(v):
    if not nonempty(v): return None
    s = str(v).strip().lstrip("'")
    if re.fullmatch(r'[+-]?\d+(?:\.0+)?[Ee][+-]?\d+|\d+\.0+', s):
        try: return format(Decimal(s), 'f').split('.')[0]
        except InvalidOperation: pass
    return s

def keyrow(row, headers):
    out=[]
    for v,h in zip(row,headers):
        if any(w in h for w in IDENT): v=ident(v)
        if v is None or v == '': s=''
        elif isinstance(v,float): s=format(v,'.15g')  # XLSX serializer uses 15 significant digits.
        elif isinstance(v,(int,Decimal)) and not isinstance(v,bool): s=format(Decimal(str(v)).normalize(),'f')
        elif isinstance(v,(datetime.date,datetime.datetime)): s=v.isoformat(sep=' ') if isinstance(v,datetime.datetime) else v.isoformat()
        else: s=str(v)
        out.append(s)
    return json.dumps(out,ensure_ascii=False,separators=(',',':'))
def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()
def canonical(e):
    return (e.tag,tuple(sorted(e.attrib.items())),(e.text or '').strip(),tuple(canonical(c) for c in e))
def xml_meta(z,path):
    """Stream metadata, discarding cell contents; row dimensions checked separately."""
    meta={}; rows={}
    with z.open(path) as f:
        for event,e in ET.iterparse(f,events=('end',)):
            tag=e.tag.split('}')[-1]
            if tag=='row':
                attrs={k:v for k,v in e.attrib.items() if k not in ('r','spans')}
                if attrs: rows[e.attrib['r']]=attrs
                e.clear()
            elif tag in ('sheetPr','sheetViews','sheetFormatPr','cols','sheetProtection','protectedRanges','autoFilter','sortState','dataConsolidate','customSheetViews','mergeCells','phoneticPr','conditionalFormatting','dataValidations','hyperlinks','printOptions','pageMargins','pageSetup','headerFooter','rowBreaks','colBreaks','drawing','legacyDrawing','extLst'):
                meta.setdefault(tag,[]).append(canonical(e)); e.clear()
    return meta,rows

def run(args):
    path=args.workbook.resolve(); template=args.template.resolve()
    delivery=args.delivery_dir or path.parent/(path.stem+'_输出')
    state=args.state or path.with_suffix('.asset-inventory-state.json')
    state_data=json.loads(state.read_text(encoding='utf-8-sig')) if state.exists() else {}
    sources={}
    for result in state_data.get('results',{}).values():
        target=result.get('targetSheet')
        if not target: continue
        sources.setdefault(target,[])
        root=Path(result['outputDir']) if result.get('outputDir') else None
        candidates=list(root.rglob('*.xlsx')) if root and root.is_dir() else []
        candidates += [Path(f) for f in result.get('savedFiles',[]) if Path(f).suffix.lower()=='.xlsx']
        sources[target]=list(dict.fromkeys(f.resolve() for f in candidates if f.exists() and f.resolve()!=path))
    protected=[path,template,state]+list(delivery.glob('*.xlsx'))+[f for files in sources.values() for f in files]
    protected=list(dict.fromkeys(p for p in protected if p.exists()))
    before={str(p):digest(p) for p in protected}
    report={'workbook':str(path),'template':str(template),'deliveryDirectory':str(delivery),'readOnly':True,'checks':{},'details':{},'failures':[],'limitations':['Formula structure and source totals are independently checked; Excel formula caches are not recalculated.','This validates the delivered source files and recorded provenance; it does not re-query remote services.']}
    def check(name,ok,detail=None):
        report['checks'][name]=bool(ok)
        if detail is not None: report['details'][name]=detail
        if not ok: report['failures'].append(name)
    t=load_workbook(template,read_only=False,data_only=False)
    ts=load_workbook(template,read_only=True,data_only=False)
    template_styles={ws.title:{(r,c):tuple(cellstyle(ts,cell)) for r,row in enumerate(ws.iter_rows(),1) for c,cell in enumerate(row,1)} for ws in ts}
    ts.close()
    w=load_workbook(path,read_only=True,data_only=False)
    check('24_sheet_structure',len(w.sheetnames)==24 and w.sheetnames==t.sheetnames,w.sheetnames)
    check('fixed_30_headers',[c.value for c in next(w['公司汇总'].iter_rows(max_row=1,max_col=30))]==HEADERS==[t['公司汇总'].cell(1,c).value for c in range(1,31)])
    provenance=[]
    for module in state_data.get('completedModules',[]):
        r=state_data.get('results',{}).get(module,{})
        provenance.append({k:r.get(k) for k in ('module','name','targetSheet','queryRows','queryCount','taskId','savedFiles','extractedFiles','dates') if k in r})
    report['provenance']={'dates':state_data.get('dates'),'modules':provenance,'source':str(state)}
    expected_modules=getattr(args,'expected_modules',None)
    completed=state_data.get('completedModules',[])
    check('module_state',set(completed)==set(expected_modules) if expected_modules else len(provenance)==17,
          {'completed':completed,'expected':expected_modules or 'all 17 modules'})
    ref_errors=[]; border_errors=[]; style_errors=[]; summary=[]; stats={}; amounts={}; source_report={}; actual_headers={}
    # An in-memory index avoids filesystem lifecycle operations entirely.
    report['verificationIndex']='sqlite :memory: (complete row keys)'
    with contextlib.nullcontext():
        db=sqlite3.connect(':memory:')
        db.execute('PRAGMA journal_mode=OFF'); db.execute('PRAGMA synchronous=OFF')
        for ws in w:
            title=ws.title; tw=t[title]
            it=ws.iter_rows(); header_cells=next(it,())
            headers=[str(c.value or '') for c in header_cells]; actual_headers[title]=headers
            if title!='id汇总':
                check('headers:'+title,[c.value for c in header_cells[:tw.max_column]]==[tw.cell(1,c).value for c in range(1,tw.max_column+1)])
            detail=title in TARGETS
            for ci,c in enumerate(header_cells,1):
                tc=tw._cells.get((1,ci))
                if tc is not None and list(template_styles[title].get((1,ci),tuple(tc._style or [0]*9)))!=list(cellstyle(w,c)) and len(style_errors)<30:
                    style_errors.append(f'{title}!{get_column_letter(ci)}1')
            fallback=delivery/(title+'.xlsx')
            source_files=sources.get(title, [fallback] if fallback.exists() else []) if detail else []
            source_n=unique_n=0
            db.execute('DROP TABLE IF EXISTS expected'); db.execute('CREATE TABLE expected (v TEXT PRIMARY KEY) WITHOUT ROWID')
            for source in source_files:
                sw=load_workbook(source,read_only=True,data_only=True); ss=sw.worksheets[0]; ss.reset_dimensions()
                sit=ss.iter_rows(values_only=True); mapping=None
                for _ in range(10):
                    raw=next(sit,()); candidate=[norm(v) for v in raw]
                    if sum(h in candidate for h in map(norm,headers))>=max(3,min(len(headers),len(raw))//2):
                        index={h:i for i,h in enumerate(candidate) if h}; mapping=[index.get(norm(h)) for h in headers]; break
                if mapping is None or None in mapping: check('source_header:'+title,False)
                else:
                    for raw in sit:
                        if not any(nonempty(v) for v in raw): continue
                        projected=[raw[i] if i<len(raw) else None for i in mapping]
                        source_n+=1
                        db.execute('INSERT OR IGNORE INTO expected VALUES (?)',(keyrow(projected,headers),))
                    unique_n=db.execute('SELECT COUNT(*) FROM expected').fetchone()[0]
                sw.close(); db.commit()
            border_ids=[cellstyle(w,c).borderId for c in header_cells]
            n=0; mismatches=0; last=1; amount=Decimal(0)
            ai=headers.index('账单金额') if '账单金额' in headers else None
            for rn,row in enumerate(it,2):
                vals=[c.value for c in row]
                active=any(nonempty(v) for v in vals)
                if active:
                    n+=1; last=rn
                    if title=='公司汇总': summary.append((rn,vals))
                    if detail and source_files:
                        cur=db.execute('DELETE FROM expected WHERE v=?',(keyrow(vals[:len(headers)],headers),)); mismatches+=cur.rowcount!=1
                    if ai is not None and ai<len(vals):
                        try: amount+=Decimal(str(vals[ai] or 0).replace(',',''))
                        except InvalidOperation: pass
                for ci,c in enumerate(row):
                    v=c.value
                    if isinstance(v,str) and '#REF!' in v and len(ref_errors)<30: ref_errors.append(f'{title}!{get_column_letter(ci+1)}{rn}')
                    if detail and active:
                        if (cellstyle(w,c).borderId)!=border_ids[ci] and len(border_errors)<30: border_errors.append(f'{title}!{get_column_letter(ci+1)}{rn}')
                    tc=tw._cells.get((rn,ci+1))
                    if tc is not None:
                        expected=list(template_styles[title].get((rn,ci+1),tuple(tc._style or [0]*9))); current=list(cellstyle(w,c))
                        if detail: expected[2]=current[2]  # Sole permitted exception: imported data border.
                        if expected!=current and len(style_errors)<30: style_errors.append(f'{title}!{get_column_letter(ci+1)}{rn}')
            stats[title]={'actualNonemptyRows':n,'lastNonemptyRow':last,'maxRow':ws.max_row}; amounts[title]=str(amount)
            if detail:
                remaining=db.execute('SELECT COUNT(*) FROM expected').fetchone()[0]
                source_report[title]={'files':[str(f) for f in source_files],'sourceNonemptyRows':source_n,'wholeRowUniqueRows':unique_n,'exactDuplicatesRemoved':source_n-unique_n,'actualNonemptyRows':n,'unmatchedActualRows':mismatches,'missingSourceRows':remaining}
                check('source_rows:'+title,(n==unique_n and mismatches==0 and remaining==0) if source_files else n==0)
        db.close()
    check('no_REF_errors',not ref_errors,ref_errors)
    check('imported_borders_match_headers',not border_errors,border_errors)
    check('existing_template_cell_styles',not style_errors,style_errors)
    report['sourceComparisons']=source_report; report['sheetRows']=stats
    # Compare sheet metadata independently using XML, without loading large sheets.
    metadata_errors=[]
    with zipfile.ZipFile(path) as wz,zipfile.ZipFile(template) as tz:
        for i,title in enumerate(w.sheetnames,1):
            member=f'xl/worksheets/sheet{i}.xml'
            a,ar=xml_meta(tz,member); b,br=xml_meta(wz,member)
            if a!=b: metadata_errors.append({'sheet':title,'metadataKeys':[k for k in a.keys()|b.keys() if a.get(k)!=b.get(k)]})
            for r,attrs in ar.items():
                if br.get(r)!=attrs: metadata_errors.append({'sheet':title,'rowDimension':r})
        check('theme_unchanged',tz.read('xl/theme/theme1.xml')==wz.read('xl/theme/theme1.xml'))
        # Workbook views, properties and protection; ignore defined-name formulas / calc cache.
        ta=ET.fromstring(tz.read('xl/workbook.xml')); wa=ET.fromstring(wz.read('xl/workbook.xml'))
        for tag in ('bookViews','workbookPr','workbookProtection','sheets'):
            aa=ta.find(NS+tag); bb=wa.find(NS+tag)
            check('workbook_'+tag,(canonical(aa) if aa is not None else None)==(canonical(bb) if bb is not None else None))
    check('template_sheet_presentation',not metadata_errors,metadata_errors)
    products=[(r,v) for r,v in summary if len(v)>2 and nonempty(v[2])]
    totals=[(r,v) for r,v in summary if v and v[0]=='总计']
    check('summary_product_count',args.expected_products is None or len(products)==args.expected_products,{'actual':len(products),'expected':args.expected_products,'templateRows':t['公司汇总'].max_row,'expandedRowsAllowed':True})
    check('summary_shop',all(v[0]==args.shop for r,v in products))
    totalrow,totalvals=totals[0] if len(totals)==1 else (0,[])
    lastprod=products[-1][0] if products else 2
    check('total_immediately_after_products_C_empty',len(totals)==1 and totalrow==lastprod+1 and not nonempty(totalvals[2]))
    fine_last=max(2,stats['处罚数据']['maxRow']); fine_col=HEADERS.index('罚款')
    check('fine_product_SUMIFS_K_H',all(v[fine_col] in (f"=SUMIFS('处罚数据'!$K$2:$K${fine_last},'处罚数据'!$H$2:$H${fine_last},$C{r})",f'=IF($C{r}="","",SUMIFS(\'处罚数据\'!$K$2:$K${fine_last},\'处罚数据\'!$H$2:$H${fine_last},$C{r}))') for r,v in products))
    for h in SUM_HEADERS:
        i=HEADERS.index(h); col=get_column_letter(i+1)
        check('total_SUM:'+h,bool(totalvals) and totalvals[i]==f'=SUM({col}2:{col}{lastprod})')
    whole={}
    for h in WHOLE:
        col=get_column_letter(actual_headers[h].index('账单金额')+1); end=max(2,stats[h]['maxRow'])
        expected=f"=-SUM('{h}'!${col}$2:${col}${end})"
        actual=totalvals[HEADERS.index(h)] if totalvals else None
        check('whole_sheet_total:'+h,actual==expected)
        whole[h]={'formula':actual,'expectedFormula':expected,'independentSourceAmount':amounts[h],'expectedSignedTotal':str(-Decimal(amounts[h]))}
    report['wholeSheetTotals']=whole
    report['summary']={'products':len(products),'totalRow':totalrow,'unattributedFeeRows':stats.get('未归属费用',{}).get('actualNonemptyRows')}
    report['productCount']=len(products)
    report['unassignedRows']=stats.get('未归属费用',{}).get('actualNonemptyRows')
    report['counts']={name:item['actualNonemptyRows'] for name,item in stats.items()}
    w.close(); t.close()
    after={str(p):digest(p) for p in protected}
    check('input_files_unchanged',before==after)
    report['inputSHA256']=before
    report['ok']=not report['failures']
    try:
        from .asset_report import sanitize
    except ImportError:
        from asset_report import sanitize
    return sanitize(report)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workbook',required=True,type=Path); p.add_argument('--shop',required=True)
    p.add_argument('--template',type=Path,default=DEFAULT_TEMPLATE)
    p.add_argument('--delivery-dir',type=Path); p.add_argument('--state',type=Path)
    p.add_argument('--expected-modules',action='append',help='Expected module key; repeat for an explicit subset')
    p.add_argument('--expected-products',type=int); p.add_argument('--report',required=True,type=Path)
    a=p.parse_args()
    inputs=[a.workbook.resolve(),a.template.resolve(),(a.state or a.workbook.with_suffix('.asset-inventory-state.json')).resolve()]
    if a.report.resolve() in inputs or a.report.suffix.lower()!='.json': p.error('Report must be a separate JSON file')
    try: report=run(a)
    except Exception as e:
        # Avoid dumping state or credential-bearing request data in exception output.
        report={'ok':False,'errorType':type(e).__name__,'readOnly':True}
    a.report.parent.mkdir(parents=True,exist_ok=True)
    a.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'ok':report.get('ok'),'report':str(a.report),'failures':report.get('failures'),'summary':report.get('summary'),'error':report.get('error')},ensure_ascii=True))
    return 0 if report.get('ok') else 1
if __name__=='__main__': raise SystemExit(main())
