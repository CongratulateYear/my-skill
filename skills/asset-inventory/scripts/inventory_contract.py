"""Fail-closed identity, date and artifact contracts (no credentials)."""
from __future__ import annotations
import hashlib
import json
from datetime import date
from pathlib import Path

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)

def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()

def identity_of(shop):
    """店铺的可核对身份：平台 supplierIdSec / 公司名优先，老清单退回 cookie 提示。"""
    supplier_id_sec = str(shop.get('supplierIdSec') or '').strip()
    company_name = str(shop.get('companyName') or '').strip()
    hint = str(shop.get('accountHint') or '').strip()
    if supplier_id_sec: return {'supplierIdSec': supplier_id_sec, 'companyName': company_name}
    if company_name: return {'supplierIdSec': '', 'companyName': company_name}
    if hint: return {'supplierIdSec': '', 'companyName': '', 'accountHint': hint}
    return {}

def context(shop, module='all'):
    for start, end in [('orderPayStart','orderPayEnd'), ('writeOffStart','writeOffEnd')]:
        if date.fromisoformat(shop[start]) > date.fromisoformat(shop[end]):
            raise ValueError('日期开始不得晚于结束')
    identity = identity_of(shop)
    if not identity:
        raise ValueError('身份未绑定：先登录指定账号，用 supplier_identity.py 读出 supplierIdSec/companyName 再传入；禁止接受任意已登录账号')
    if not shop.get('account'): raise ValueError('身份绑定缺少 account')
    keys = ('shop','account','orderPayStart','orderPayEnd','writeOffStart','writeOffEnd')
    return {k: shop[k] for k in keys} | identity | {'module':module}

def manifest_path(root): return Path(root)/'inventory-manifest.json'

def verify_files(root, files):
    root = Path(root).resolve()
    for relative, expected in files.items():
        path = (root/relative).resolve()
        if not path.is_relative_to(root) or not path.is_file() or digest(path) != expected:
            raise ValueError('产物缺失或摘要不一致：'+relative)

def bind(root, ctx):
    root = Path(root)
    path = manifest_path(root)
    if path.exists():
        saved = json.loads(path.read_text(encoding='utf-8'))
        if saved.get('context') != ctx: raise ValueError('目录身份/账期/模块与本次请求不同；请使用独立输出目录')
        verify_files(root, saved.get('files', {}))
        return saved
    if any(root.glob('asset-inventory-*')) or any(root.glob('*.asset-inventory-state.json')):
        raise ValueError('已有下载缺少身份清单，拒绝复用；请使用新的输出目录')
    saved = {'schemaVersion':1,'context':ctx,'files':{},'complete':False}
    atomic_json(path,saved)
    return saved

def checkpoint(root, ctx, complete=False, deliverables=(), external=()):
    root = Path(root).resolve()
    paths = [p for d in root.glob('asset-inventory-*') if d.is_dir() for p in d.rglob('*') if p.is_file()]
    paths.extend(root.glob('*.asset-inventory-state.json'))
    files = {str(p.relative_to(root)):digest(p) for p in paths}
    delivered = {}
    for p in deliverables:
        p=Path(p).resolve()
        if not p.is_relative_to(root): raise ValueError('交付文件越界')
        delivered[str(p.relative_to(root))]=digest(p)
    outside = {}
    for p in external:
        p=Path(p).resolve()
        if p.is_relative_to(root): raise ValueError('外部交付文件不应位于店铺目录内')
        if not p.is_file(): raise ValueError('外部交付文件缺失')
        outside[str(p)]=digest(p)
    saved={'schemaVersion':1,'context':ctx,'files':files,'complete':complete,'deliverables':delivered,'externalDeliverables':outside}
    atomic_json(manifest_path(root),saved)
    return saved

def verify_external(files):
    for path, expected in files.items():
        p=Path(path)
        if not p.is_file() or digest(p) != expected: raise ValueError('外部交付文件缺失或摘要不一致：'+path)

def validate_download_state(root, shop, modules):
    root=Path(root).resolve()
    prefix=shop['shop']
    for c in '/\\:*?"<>|\n\r\t': prefix=prefix.replace(c,'-')
    prefix=prefix.strip(' .-')
    path=root/f"{prefix}_{shop['writeOffStart']}_{shop['writeOffEnd']}.asset-inventory-state.json"
    s=json.loads(path.read_text(encoding='utf-8'))
    expected={'writeOffTime':{'start':shop['writeOffStart'],'end':shop['writeOffEnd']},'orderPayTime':{'start':shop['orderPayStart'],'end':shop['orderPayEnd']}}
    if s.get('shopName')!=shop['shop'] or s.get('dates')!=expected: raise ValueError('下载状态店铺或账期不符')
    if s.get('failedModule') or not set(modules).issubset(s.get('completedModules',[])): raise ValueError('下载模块未全部完成')
    directories=[]
    for module in modules:
        r=s.get('results',{}).get(module)
        if not isinstance(r,dict): raise ValueError('缺少模块结果：'+module)
        saved=r.get('savedFiles',[])
        if r.get('queryRows') == 0 and not saved: continue
        d=Path(r.get('outputDir') or '').resolve()
        if not d.is_relative_to(root): raise ValueError('下载目录越界')
        if r.get('queryRows') != 0 and not saved: raise ValueError('非零记录模块没有来源文件：'+module)
        for p in saved:
            p=Path(p).resolve()
            if not p.is_relative_to(root) or not p.is_file(): raise ValueError('来源文件缺失或越界')
        if d.is_dir(): directories.append(d)
    return s, directories

def validate_builder(result):
    if not isinstance(result,dict) or result.get('schemaVersion')!=1: raise ValueError('构建结果协议缺失或版本不符')
    for field in ('product_count','allocated_rows','unassigned_rows'):
        if type(result.get(field)) is not int or result[field]<0: raise ValueError('构建结果字段无效：'+field)
    for field in ('sheet_write_verified','presentation_verified'):
        if result.get(field) is not True: raise ValueError('构建验证未通过：'+field)
    if result.get('skipped_files'): raise ValueError('存在未导入来源文件')
    return result
