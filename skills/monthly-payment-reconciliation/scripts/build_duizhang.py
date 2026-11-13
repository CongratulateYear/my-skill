#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""月度货款对账表生成器

钉钉审批表（付款侧） × 退货登记表（扣款侧）→ 月度对账表 xlsx（8 个页签）

用法:
  python3 build_duizhang.py --dingding <钉钉审批表.xlsx> --deduct <退货登记表.xlsx> \
      --month 8月 --year 2026 [--out 输出文件.xlsx] [--threshold 10000]

扣款侧两种格式自动识别（按表头列名映射）:
  A 售后台账格式: 类型|登记日期|登记月份|运营|产品|群|时间区间|所属月份|抵扣方式|金额|备注|厂家|财务
  B 汇总格式:     类型|登记日期|登记月份|运营|产品|群|拦截时间区间|所属月份|金额|抵扣方式|是否抵扣/回款|收款人

运行前置检查（preflight）:
  逐 sheet 报告两侧「目标月行数/金额」与月份分布；目标月缺失直接报错；
  扣款簿多个 sheet 均含目标月数据时，列出各 sheet 金额并高亮被选中者，防止选错 sheet。
"""
import argparse
import difflib
import re
import sys
from collections import Counter, OrderedDict, defaultdict

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

TYPES = ["后台退货", "仓库退货", "罚款", "拦截"]
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HDR_FILL = PatternFill("solid", fgColor="1F4E79")
HDR_FONT = Font(bold=True, color="FFFFFF", size=10)
F10 = Font(size=10)
YELLOW = PatternFill("solid", fgColor="FFF2CC")


# ---------- 工具 ----------
def clean(s):
    return str(s).strip() if s is not None else ""


def nm(s):
    """'姓名 (支付宝账号 xxx)' -> '姓名'"""
    return str(s).split(" (")[0].strip() if s else ""


def canon(s):
    """'8.1' / '8.01-8.02' / '7.28-8.2拦截' -> ((7,28),(8,2))"""
    ds = re.findall(r"(\d{1,2})\.(\d{1,2})", str(s))
    if not ds:
        return None
    a = (int(ds[0][0]), int(ds[0][1]))
    b = (int(ds[1][0]), int(ds[1][1])) if len(ds) > 1 else a
    return (a, b) if a <= b else (b, a)


def period(s):
    m = re.search(r"抵扣在(.+?)货款", str(s))
    return canon(m.group(1)) if m else None


def uniq(lst):
    out = []
    for x in lst:
        if not x:
            continue
        s = str(x).strip()
        if s in out or any(s in o for o in out):
            continue
        out = [o for o in out if o not in s]
        out.append(s)
    return out


def norm_grp(s):
    """群名去噪：去括号内容、去群后缀、去符号与数字（用于相似度）"""
    s = clean(s).lower()
    s = re.sub(r"[（(].*?[）)]", "", s)
    s = re.sub(r"(对接群|对账群|代发群|对接发货群|发货群|群)", "", s)
    s = re.sub(r"[\s\-_/\\&、,，.。:：+*|{}]", "", s)
    s = re.sub(r"\d+", "", s)
    return s


def grp_score(a, b):
    na, nb = norm_grp(a), norm_grp(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.9
    return difflib.SequenceMatcher(None, na, nb).ratio()


def header_map(ws):
    """首行表头 -> {列名(去空白): 列号}"""
    m = {}
    for cell in ws[1]:
        if cell.value is not None:
            m[re.sub(r"\s+", "", str(cell.value))] = cell.column
    return m


def pick(hm, *names):
    """按候选列名取列号（支持模糊：包含匹配）"""
    for n in names:
        if n in hm:
            return hm[n]
    for n in names:
        for k, v in hm.items():
            if n in k:
                return v
    return None


def find_sheet(wb, must_have, fallback_name=None):
    """先按 sheet 名（含 fallback_name）找，再按表头关键词找，避免多个明细 sheet 误命中"""
    if fallback_name:
        for ws in wb.worksheets:
            if fallback_name in ws.title:
                hm = header_map(ws)
                if hm and all(any(k in col for col in hm) for k in must_have):
                    return ws, hm
    for ws in wb.worksheets:
        hm = header_map(ws)
        if hm and all(any(k in col for col in hm) for k in must_have):
            return ws, hm
    if fallback_name:
        for ws in wb.worksheets:
            if fallback_name in ws.title:
                return ws, header_map(ws)
    raise SystemExit("错误：在文件中找不到表头含关键词 %s 的 sheet（现有 sheet：%s）。"
                     "若目标数据在聚合格式 sheet（如「财务登记售后」：后台退货/仓库退货/罚款/拦截四列按行直接填、"
                     "无逐条「类型」列），本脚本暂不支持，需先拆成逐条格式" % ("/".join(must_have),
                                                                     "、".join(ws.title for ws in wb.worksheets)))


def header(ws, n):
    for c in range(1, n + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill, cell.font = HDR_FILL, HDR_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 30


def z(v):
    return None if v == 0 else v


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser(description="月度货款对账表生成器")
    ap.add_argument("--dingding", required=True, help="钉钉审批表 xlsx")
    ap.add_argument("--deduct", required=True, help="退货登记表 xlsx（拦截-罚款-退货 汇总 / 售后台账）")
    ap.add_argument("--month", required=True, help="对账月份，如 8月")
    ap.add_argument("--year", default="2026", help="年份（用于标题），默认 2026")
    ap.add_argument("--out", help="输出 xlsx 路径，默认 <月份>对账表_<年份>.xlsx")
    ap.add_argument("--threshold", type=float, default=10000, help="大额无售后阈值，默认 10000")
    args = ap.parse_args()
    MONTH, YEAR = args.month, str(args.year)
    OUT = args.out or f"{MONTH}对账表_{YEAR}.xlsx"

    # ===== 读钉钉审批表 =====
    wbp = openpyxl.load_workbook(args.dingding, data_only=True)
    wsp, hmp = find_sheet(wbp, ["费用类型", "货款所属月份"], "钉钉审批总表")
    col = lambda n: pick(hmp, n)
    need = ["费用类型", "结算时间", "运营", "产品名称", "店铺", "对接群", "收款账户", "报销金额", "审批结果", "货款所属月份"]
    missing = [n for n in need if col(n) is None]
    if missing:
        raise SystemExit("错误：钉钉审批表缺少列 %s" % missing)
    C = {n: col(n) for n in need}
    pay_rows = []
    for r in wsp.iter_rows(min_row=2, values_only=True):
        if clean(r[C["货款所属月份"] - 1]) == MONTH:
            pay_rows.append(r)

    # ===== 读退货登记表（自动识别格式） =====
    wbd = openpyxl.load_workbook(args.deduct, data_only=True)
    wsd, hmd = find_sheet(wbd, ["类型", "运营", "金额", "所属月份"], "汇总")
    fmt_a = pick(hmd, "厂家") is not None          # 售后台账格式
    payee_name = "厂家" if fmt_a else "收款人"
    note_name = "备注" if fmt_a else "是否抵扣/回款"
    D = {
        "类型": pick(hmd, "类型"), "运营": pick(hmd, "运营"), "产品": pick(hmd, "产品"),
        "群": pick(hmd, "群"), "所属月份": pick(hmd, "所属月份"), "金额": pick(hmd, "金额"),
        "抵扣方式": pick(hmd, "抵扣方式"), "payee": pick(hmd, payee_name),
        "note": pick(hmd, note_name), "财务": pick(hmd, "财务"),
        "区间": pick(hmd, "时间区间", "拦截时间区间", "退货时间区间"),
    }
    miss2 = [k for k, v in D.items() if v is None and k != "财务"]
    if miss2:
        raise SystemExit("错误：退货登记表缺少列 %s" % miss2)

    af = []
    for r in wsd.iter_rows(min_row=2, values_only=True):
        if not any(r):
            continue
        if clean(r[D["所属月份"] - 1]) != MONTH:
            continue
        af.append(dict(type=clean(r[D["类型"] - 1]), op=clean(r[D["运营"] - 1]),
                       prod=clean(r[D["产品"] - 1]), grp=clean(r[D["群"] - 1]),
                       span=clean(r[D["区间"] - 1]) if D["区间"] else "",
                       amt=r[D["金额"] - 1] or 0,
                       way=clean(r[D["抵扣方式"] - 1]) if D["抵扣方式"] else "",
                       note=clean(r[D["note"] - 1]) if D["note"] else "",
                       vendor=clean(r[D["payee"] - 1]),
                       fin=clean(r[D["财务"] - 1]) if D["财务"] else "",
                       per=period(r[D["抵扣方式"] - 1]) if D["抵扣方式"] else None))

    # ===== 预检：月份覆盖 / sheet 选择 / 噪声（防选错文件、选错 sheet、过期快照）=====
    def scan_all(wb, must, month_col, amt_col):
        """逐 sheet 统计月份分布；只认表头含 must 且含 month_col 的 sheet"""
        res = []
        for ws in wb.worksheets:
            hm = header_map(ws)
            if not hm or any(pick(hm, k) is None for k in must):
                continue
            mc = pick(hm, month_col)
            if mc is None:
                continue
            ac = pick(hm, amt_col)
            dist = OrderedDict()
            for r in ws.iter_rows(min_row=2, values_only=True):
                mv = clean(r[mc - 1]) if len(r) >= mc else ""
                if not mv:
                    continue
                d = dist.get(mv)
                if d is None:
                    d = dist[mv] = [0, 0.0]
                d[0] += 1
                if ac and len(r) >= ac and isinstance(r[ac - 1], (int, float)):
                    d[1] += r[ac - 1]
            res.append((ws.title, dist))
        return res

    PRE, WARN = [], []
    for title, dist in scan_all(wbp, ["费用类型"], "货款所属月份", "报销金额"):
        n, amt = dist.get(MONTH, [0, 0.0])
        tot = sum(v[1] for v in dist.values())
        share = (amt / tot) if tot else 0
        PRE.append(("付款表 sheet「{}」{}".format(title, " ← 本表选用" if title == wsp.title else ""),
                    "{} 行".format(n), "", "{}月金额 {:,.2f}，占全表 {:.0%}".format(MONTH, amt, share) if tot else ""))
        if title == wsp.title:
            if n == 0:
                raise SystemExit("错误：付款表 sheet「{}」中「货款所属月份={}」为 0 行。该 sheet 现有月份分布: {}".format(
                    title, MONTH, "；".join("{}({}行)".format(k, v[0]) for k, v in
                                            sorted(dist.items(), key=lambda kv: -kv[1][0])[:8])))
            if share < 0.5:
                WARN.append("付款表目标月金额仅占全表 {:.0%}（多月份混杂），确认不是别的月份导出或过期快照".format(share))
    dcan = []
    for title, dist in scan_all(wbd, ["运营"], "所属月份", "金额"):
        n, amt = dist.get(MONTH, [0, 0.0])
        if title == wsd.title and n == 0:
            raise SystemExit("错误：扣款表 sheet「{}」中「所属月份={}」为 0 行。该 sheet 现有月份分布: {}".format(
                title, MONTH, "；".join("{}({}条)".format(k, v[0]) for k, v in
                                        sorted(dist.items(), key=lambda kv: -kv[1][0])[:8])))
        if n or title == wsd.title:
            dcan.append((title, n, amt))
    for title, n, amt in dcan:
        PRE.append(("扣款表 sheet「{}」{}".format(title, " ← 本表选用" if title == wsd.title else ""),
                    "{} 条".format(n), "", "{:,.2f} 元".format(amt)))
    if len(dcan) > 1:
        mx = max(dcan, key=lambda t: t[2])
        if mx[0] != wsd.title:
            WARN.append("扣款簿多个 sheet 含 {} 数据，选中「{}」，但金额最大的是「{}」({:,.2f} 元)，确认选对".format(
                MONTH, wsd.title, mx[0], mx[2]))
    for wb2, tag in [(wbp, "付款"), (wbd, "扣")]:
        for ws2 in wb2.worksheets:
            hm2 = header_map(ws2)
            if hm2 and sum(1 for t in TYPES if pick(hm2, t) is not None) >= 3 and pick(hm2, "类型") is None:
                WARN.append("{}表簿中检测到疑似聚合格式 sheet「{}」（四列扣款直接按行填、无逐条类型），脚本暂不支持，如需对账先拆成逐条格式".format(tag, ws2.title))
    zero_cnt = sum(1 for x in af if not x["amt"])
    PRE.append(("扣款 0 元记录", "{} 条".format(zero_cnt), "", "只影响条数统计，不影响金额"))
    PRE.append(("扣款无群记录", "{} 条".format(sum(1 for x in af if not norm_grp(x["grp"]))), "", ""))
    for a, b, c, d in PRE:
        print("预检 |", a, "|", b, d)
    for w in WARN:
        print("预检 ⚠", w)

    # ===== 运营人员名单（刷单判定用） =====
    OPS = set()
    for r in pay_rows:
        for p in re.split(r"[+/]", clean(r[C["运营"] - 1])):
            if p.strip():
                OPS.add(p.strip())
    for x in af:
        for p in re.split(r"[+/]", x["op"]):
            if p.strip():
                OPS.add(p.strip())

    def is_brush(r):
        """刷单款判定：① 费用类型=破零自刷款；② 三多 且 收款人属于运营人员"""
        if clean(r[C["费用类型"] - 1]) == "破零自刷款":
            return True
        three_multi = (clean(r[C["产品名称"] - 1]) == "多产品"
                       and clean(r[C["店铺"] - 1]) == "多店铺"
                       and clean(r[C["对接群"] - 1]) == "多群")
        return three_multi and nm(r[C["收款账户"] - 1]) in OPS

    pay, brk_rows = [], []
    for r in pay_rows:
        if is_brush(r):
            brk_rows.append(r)
            continue
        if clean(r[C["费用类型"] - 1]) != "供应商货款":
            continue
        pay.append(dict(no=r[C["序号"] - 1] if C.get("序号") else "",
                        settle=clean(r[C["结算时间"] - 1]), op=clean(r[C["运营"] - 1]),
                        prod=clean(r[C["产品名称"] - 1]), grp=clean(r[C["对接群"] - 1]),
                        amt=r[C["报销金额"] - 1] or 0, acct=clean(r[C["收款账户"] - 1]),
                        payee=nm(r[C["收款账户"] - 1]), result=clean(r[C["审批结果"] - 1])))
    brk_from_sup = [r for r in brk_rows if clean(r[C["费用类型"] - 1]) == "供应商货款"]

    # ===== 财务映射（格式 A 自带；格式 B 留空待补） =====
    FM = defaultdict(Counter)
    for x in af:
        if x["op"] and x["fin"]:
            FM[x["op"]][x["fin"]] += 1
    FIN = {k: v.most_common(1)[0][0] for k, v in FM.items()}

    def fin_of(op):
        if op in FIN:
            return FIN[op]
        parts = [p.strip() for p in re.split(r"[+/]", op) if p.strip()]
        got = {FIN[p] for p in parts if p in FIN}
        return "/".join(sorted(got)) if got else ""

    # ===== 付款聚合 (运营, 群, 收款人) =====
    P = OrderedDict()
    for x in pay:
        k = (x["op"], x["grp"], x["payee"])
        d = P.setdefault(k, dict(op=k[0], grp=k[1], payee=k[2], prods=[], accts=[], amt=0.0, n=0, notes=[]))
        if x["prod"] not in d["prods"]:
            d["prods"].append(x["prod"])
        if x["acct"] not in d["accts"]:
            d["accts"].append(x["acct"])
        d["amt"] += x["amt"]
        d["n"] += 1
        if x["result"] != "审批通过":
            d["notes"].append(x["result"])

    # ===== 索引 =====
    K3 = set(P.keys())
    KOP, KGP, KPAY, KPER = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    for k in P:
        KOP[(k[0], k[2])].append(k)
        KGP[(k[1], k[2])].append(k)
        KPAY[k[2]].append(k)
    for x in pay:
        KPER[(x["op"], x["payee"])].append((canon(x["settle"]), (x["op"], x["grp"], x["payee"])))

    # ===== 逐条匹配（运营字段优先于群名精确） =====
    match = {}
    unmatched = []
    for x in af:
        op, grp, payee, pr = x["op"], x["grp"], x["vendor"], x["per"]
        if (op, grp, payee) in K3:
            match[id(x)] = ((op, grp, payee), "运营+群+厂家  精确", 1.0)
            continue
        hit = [kk for p2, kk in KPER.get((op, payee), []) if pr and p2 == pr]
        if hit:
            match[id(x)] = (hit[0], "运营+厂家+抵扣期间  精确", 1.0)
            continue
        c = KOP.get((op, payee), [])
        if len(c) == 1:
            match[id(x)] = (c[0], "运营+厂家（唯一群）", 1.0)
            continue
        if len(c) > 1:
            best = max(c, key=lambda kk: grp_score(grp, kk[1]))
            s = grp_score(grp, best[1])
            if s >= 0.55:
                match[id(x)] = (best, "运营+厂家（群名相似）", round(s, 2))
                continue
        g = KGP.get((grp, payee), [])
        if len(g) == 1:
            match[id(x)] = (g[0], "群+厂家（运营记为{}→{}）".format(op or "空", g[0][0]), 1.0)
            continue
        if len(g) > 1:
            same = [kk for kk in g if kk[0] == op]
            if same:
                match[id(x)] = (same[0], "群+厂家（同群多运营）", 1.0)
                continue
        p = KPAY.get(payee, [])
        if len(p) == 1:
            match[id(x)] = (p[0], "厂家唯一命中（运营{}→{}）".format(op or "空", p[0][0]), 1.0)
            continue
        if len(p) > 1:
            best = max(p, key=lambda kk: grp_score(grp, kk[1]))
            s = grp_score(grp, best[1])
            if s >= 0.6:
                match[id(x)] = (best, "厂家+群名相似（跨运营）", round(s, 2))
                continue
        unmatched.append(x)

    # ===== 挂账 =====
    assign = defaultdict(lambda: {t: 0.0 for t in TYPES})
    for x in af:
        mk = match.get(id(x))
        if mk:
            assign[mk[0]][x["type"]] += x["amt"]

    rows = []
    for k, d in P.items():
        a = assign.get(k, {t: 0.0 for t in TYPES})
        rows.append(dict(fin=fin_of(k[0]), op=k[0], prod="、".join(uniq(d["prods"])), grp=k[1],
                         acct="；".join(uniq(d["accts"])), payee=k[2], amt=d["amt"],
                         h=a["后台退货"], w=a["仓库退货"], f=a["罚款"], b=a["拦截"],
                         note="；".join(sorted(set(d["notes"]))), src="钉钉"))

    agg_u = OrderedDict()
    for x in unmatched:
        k = (x["op"], x["grp"], x["vendor"])
        d = agg_u.setdefault(k, dict(prods=[], amts={t: 0.0 for t in TYPES}, cnt=0))
        if x["prod"] not in d["prods"]:
            d["prods"].append(x["prod"])
        d["amts"][x["type"]] += x["amt"]
        d["cnt"] += 1
    for (op, grp, payee), d in agg_u.items():
        rows.append(dict(fin=fin_of(op), op=op, prod="、".join(uniq(d["prods"])), grp=grp, acct="—",
                         payee=payee, amt="未查到", h=d["amts"]["后台退货"], w=d["amts"]["仓库退货"],
                         f=d["amts"]["罚款"], b=d["amts"]["拦截"],
                         note=f"售后有登记{d['cnt']}条，{MONTH}钉钉无对应货款", src="仅售后"))
    rows.sort(key=lambda r: (r["fin"] == "", r["fin"], r["op"], r["grp"], r["payee"]))

    # ===== 汇总数字 =====
    all_amt = sum(r[C["报销金额"] - 1] or 0 for r in pay_rows if isinstance(r[C["报销金额"] - 1], (int, float)))
    brk_amt = sum(r[C["报销金额"] - 1] or 0 for r in brk_rows if isinstance(r[C["报销金额"] - 1], (int, float)))
    sup_amt = sum(x["amt"] for x in pay)
    h_amt = sum(x["amt"] for x in af)
    m_amt = sum(x["amt"] for x in af if id(x) in match)
    u_amt = h_amt - m_amt
    cash_back = sum(x["amt"] for x in af if "回款" in x["note"])
    nopass = [x for x in pay if x["result"] != "审批通过"]
    u_nz = [x for x in unmatched if x["amt"]]
    u_nz_amt = sum(x["amt"] for x in u_nz)
    if sup_amt and h_amt / sup_amt < 0.03:
        WARN.append("扣款合计仅占对账货款 {:.1%}（往期约 9%~10%），疑似扣款侧数据不全或选错 sheet，先核对再交付".format(
            h_amt / sup_amt))

    # ===== 大额无售后（报销>阈值 且 本行四列全空） =====
    person = defaultdict(lambda: [0.0, 0.0, set()])
    for r in rows:
        if not isinstance(r["amt"], (int, float)):
            continue
        d = person[(r["op"], r["payee"])]
        d[0] += r["amt"]
        dd = r["h"] + r["w"] + r["f"] + r["b"]
        d[1] += dd
        if dd:
            d[2].add(r["grp"])
    by_grp_af = defaultdict(list)
    for x in af:
        by_grp_af[norm_grp(x["grp"])].append(x)
    big = [r for r in rows if isinstance(r["amt"], (int, float)) and r["amt"] > args.threshold
           and (r["h"] + r["w"] + r["f"] + r["b"]) == 0]
    big.sort(key=lambda r: -r["amt"])

    # ---------- 输出 ----------
    wb = openpyxl.Workbook()

    # ===== 1 主表 =====
    ws = wb.active
    ws.title = f"{MONTH}对账表"
    HEAD = ["财务", "运营", "产品名称", "对接群", "收款账户", "收款人", "报销金额（元）",
            "后台退货", "仓库退货", "罚款", "拦截", "备注", "汇款比例"]
    ws.append(HEAD)
    header(ws, len(HEAD))
    for i, r in enumerate(rows, start=2):
        tot_ded = r["h"] + r["w"] + r["f"] + r["b"]
        ratio = tot_ded / r["amt"] if (isinstance(r["amt"], (int, float)) and r["amt"] and tot_ded) else None
        ws.append([r["fin"], r["op"], r["prod"], r["grp"], r["acct"], r["payee"], r["amt"],
                   z(r["h"]), z(r["w"]), z(r["f"]), z(r["b"]), r["note"], ratio])
        for c in range(1, len(HEAD) + 1):
            cell = ws.cell(row=i, column=c)
            cell.font, cell.border = F10, BORDER
            if c == 13:
                cell.number_format = "0.00%"
                cell.alignment = Alignment(horizontal="right")
            elif c >= 7:
                cell.number_format = "#,##0.00" if isinstance(cell.value, (int, float)) else "@"
                cell.alignment = Alignment(horizontal="right")
            if r["src"] == "仅售后":
                cell.fill = YELLOW
    last = len(rows) + 1
    tr = last + 2
    ws.cell(row=tr, column=1, value="合计").font = Font(bold=True, size=10)
    for c, cc in [(7, "G"), (8, "H"), (9, "I"), (10, "J"), (11, "K")]:
        cell = ws.cell(row=tr, column=c, value=f"=SUM({cc}2:{cc}{last})")
        cell.font = Font(bold=True, size=10)
        cell.number_format = "#,##0.00"
        cell.border = Border(top=Side(style="medium"), bottom=Side(style="double"))
    rc = ws.cell(row=tr, column=13, value=f'=IFERROR((H{tr}+I{tr}+J{tr}+K{tr})/G{tr},"")')
    rc.font = Font(bold=True, size=10)
    rc.number_format = "0.00%"
    rc.border = Border(top=Side(style="medium"), bottom=Side(style="double"))
    for i, w in enumerate([10, 11, 34, 30, 40, 10, 14, 11, 11, 9, 9, 30, 11], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:M{last}"

    # ===== 2 核对总览 =====
    ws2 = wb.create_sheet("核对总览")
    ws2["A1"] = f"{YEAR}年{MONTH} 货款对账核对总览"
    ws2["A1"].font = Font(bold=True, size=14, color="1F4E79")
    for cc, w in [("A", 30), ("B", 16), ("C", 8), ("D", 58)]:
        ws2.column_dimensions[cc].width = w
    blk = [("零、预检", None, None, None)] + PRE + \
        [("⚠ " + w, "", "", "") for w in WARN] + [
        ("一、付款侧（钉钉审批表）", None, None, None),
        ("货款所属月份", MONTH, "", f"筛选后 {len(pay_rows)} 行"),
        ("全表报销金额", round(all_amt, 2), "元", f"{len(pay_rows)} 笔"),
        ("　减：刷单款（破零自刷）", round(brk_amt, 2), "元",
         "{} 笔：破零自刷款 {} 笔 + 以货款名义提交的多产品/多店铺/多群 {} 笔 {:,} 元".format(
             len(brk_rows), len(brk_rows) - len(brk_from_sup), len(brk_from_sup),
             round(sum(r[C["报销金额"] - 1] or 0 for r in brk_from_sup), 2))),
        ("　＝ 对账货款", round(sup_amt, 2), "元", f"{len(pay)} 笔供应商货款，= 主表 G 列合计"),
        ("　其中：转账未通过钉钉", round(sum(x["amt"] for x in nopass), 2), "元", f"{len(nopass)} 笔实际付款"),
        ("二、扣款侧（{}）".format("售后台账格式" if fmt_a else "拦截-罚款-退货 汇总"), None, None, None),
        ("扣款记录数", len(af), "条", f"所属月份 = {MONTH}"),
        ("扣款金额合计", round(h_amt, 2), "元", ""),
    ]
    for t in TYPES:
        sub = [x for x in af if x["type"] == t]
        blk.append((f"　· {t}", round(sum(x["amt"] for x in sub), 2), "元", f"{len(sub)} 条"))
    blk += [
        ("　其中：标已回款", round(cash_back, 2), "元",
         f"{sum(1 for x in af if '回款' in x['note'])} 条，现金已回，需确认是否仍应冲减货款"),
        ("三、匹配结果", None, None, None),
        ("已匹配", len(match), "条", f"{m_amt:,.2f} 元"),
        ("未匹配", len(unmatched), "条",
         "共 {:,} 元 / {} 组；其中有金额 {} 条 {:,} 元，0 元噪声 {} 条（0 元不影响金额）".format(
             u_amt, len(agg_u), len(u_nz), u_nz_amt, len(unmatched) - len(u_nz))),
        ("匹配率（金额）", (m_amt / h_amt) if h_amt else 0, "", "已匹配扣款 ÷ 扣款总额"),
        ("匹配率（条数）", (len(match) / len(af)) if af else 0, "", "已匹配条数 ÷ 扣款记录数"),
        ("整体售后占货款比例", (h_amt / sup_amt) if sup_amt else 0, "", "扣款总额 ÷ 对账货款，即主表「汇款比例」合计行的值"),
        ("四、校验", None, None, None),
        ("主表报销金额合计", f"见 {MONTH}对账表 合计行", "", "应 = {:,} 元".format(round(sup_amt, 2))),
        ("主表四列扣款合计", f"见 {MONTH}对账表 合计行", "", "应 = 扣款总额 {:,} 元".format(round(h_amt, 2))),
        ("大额无售后", len(big), "笔", "报销>阈值且本行四列全空，见「大额无售后」页"),
    ]
    r = 3
    for a, b, c, d in blk:
        ws2.cell(row=r, column=1, value=a)
        if b is not None:
            cell = ws2.cell(row=r, column=2, value=b)
            if isinstance(b, float):
                cell.number_format = "#,##0.00"
            if a.startswith("匹配率") or a.startswith("整体售后"):
                cell.number_format = "0.0%"
        ws2.cell(row=r, column=3, value=c)
        ws2.cell(row=r, column=4, value=d)
        if b is None:
            ws2.cell(row=r, column=1).font = Font(bold=True, size=10, color="1F4E79")
        elif a.startswith("⚠"):
            ws2.cell(row=r, column=1).font = Font(bold=True, size=10, color="C00000")
        else:
            ws2.cell(row=r, column=1).font = F10
            ws2.cell(row=r, column=4).font = Font(size=9, color="808080")
        r += 1

    # ===== 3 匹配明细 =====
    ws3 = wb.create_sheet("匹配明细")
    H3 = ["类型", "运营", "售后-群", "售后-产品", payee_name, "抵扣期间", "金额", "登记时间区间",
          "匹配方式", "命中对接群", "相似度", "命中收款人", "备注"]
    ws3.append(H3)
    header(ws3, len(H3))
    i = 2
    for x in af:
        mk = match.get(id(x))
        k = mk[0] if mk else None
        per_txt = ""
        if x["per"]:
            (m1, d1), (m2, d2) = x["per"]
            per_txt = f"{m1}.{d1}-{m2}.{d2}"
        ws3.append([x["type"], x["op"], x["grp"], x["prod"], x["vendor"], per_txt, x["amt"], x["span"],
                    mk[1] if mk else "未匹配", k[1] if k else "—", mk[2] if mk else "",
                    k[2] if k else "—", x["note"]])
        for c in range(1, len(H3) + 1):
            cell = ws3.cell(row=i, column=c)
            cell.font, cell.border = F10, BORDER
            if c == 7:
                cell.number_format = "#,##0.00"
            if not mk:
                cell.fill = YELLOW
        i += 1
    for ci, w in zip(range(1, 14), [10, 10, 28, 24, 14, 14, 12, 16, 24, 28, 7, 10, 12]):
        ws3.column_dimensions[get_column_letter(ci)].width = w
    ws3.freeze_panes = "A2"
    ws3.auto_filter.ref = f"A1:M{i - 1}"

    # ===== 4 未匹配清单 =====
    ws4 = wb.create_sheet("未匹配清单")
    H4 = ["运营", "群", "产品", payee_name, "后台退货", "仓库退货", "罚款", "拦截", "合计", "条数",
          "最接近付款组（运营｜群）", "相似度", "建议"]
    ws4.append(H4)
    header(ws4, len(H4))
    i = 2
    for (op, grp, payee), d in agg_u.items():
        tot = sum(d["amts"].values())
        cands = KPAY.get(payee, [])
        if cands:
            bk = max(cands, key=lambda kk: grp_score(grp, kk[1]))
            bs = round(grp_score(grp, bk[1]), 2)
            cand, sim = "{}｜{}".format(bk[0], bk[1]), bs
            if tot == 0:
                advice = "全为 0 元记录，只影响条数统计，无金额影响"
            elif bs < 0.6:
                advice = ("该收款人本月有 {} 个付款组，最相似「{}」（{}）相似度 {} 低于阈值未自动挂账，人工确认是否同一业务"
                          ).format(len(cands), bk[1], bk[0], bs)
            else:
                advice = "最相似群相似度 {} 已达阈值仍未匹配（被更高优先级规则否决），请人工核对".format(bs)
        else:
            cand, sim, advice = "—", None, f"该{payee_name} {MONTH} 无任何钉钉付款，确认是否漏付或货款记在其他月份"
        ws4.append([op, grp, "、".join(uniq(d["prods"])), payee, d["amts"]["后台退货"], d["amts"]["仓库退货"],
                    d["amts"]["罚款"], d["amts"]["拦截"], tot, d["cnt"], cand, sim, advice])
        for c in range(1, len(H4) + 1):
            cell = ws4.cell(row=i, column=c)
            cell.font, cell.border = F10, BORDER
            if 5 <= c <= 9:
                cell.number_format = "#,##0.00"
            if tot == 0:
                cell.fill = YELLOW
        i += 1
    for ci, w in zip(range(1, 14), [10, 30, 24, 16, 11, 11, 9, 9, 12, 7, 34, 7, 46]):
        ws4.column_dimensions[get_column_letter(ci)].width = w
    ws4.freeze_panes = "A2"

    # ===== 5 大额无售后 =====
    ws5 = wb.create_sheet("大额无售后")
    H5 = ["财务", "运营", "产品名称", "对接群", "收款人", "报销金额", "本行售后扣款",
          "同人其他群已扣", "判定", "建议核查方向"]
    ws5.append(H5)
    header(ws5, len(H5))
    i = 2
    for r in big:
        d = person[(r["op"], r["payee"])]
        other = d[1]
        same_grp = [x for x in by_grp_af.get(norm_grp(r["grp"]), []) if x["vendor"] != r["payee"]]
        if other:
            verdict = "疑似已扣在兄弟行"
            advice = "售后已扣在「" + "、".join(sorted(d[2])) + "」行，核对是否同一业务，避免漏扣或重复抵扣"
        elif same_grp:
            verdict = "售后记在其他名下"
            advice = "同群有 {} 条 / {:,} 元售后，登记在「{}」名下，核对是否同一厂家".format(
                len(same_grp), round(sum(x["amt"] for x in same_grp), 2),
                "、".join(sorted({x["vendor"] for x in same_grp})))
        else:
            verdict = "本月零售后"
            advice = f"该运营+该收款人 {MONTH} 无任何售后登记，确认是否漏登或售后发生在其他月份"
        ws5.append([r["fin"], r["op"], r["prod"], r["grp"], r["payee"], round(r["amt"], 2), None,
                    round(other, 2) if other else None, verdict, advice])
        for c in range(1, len(H5) + 1):
            cell = ws5.cell(row=i, column=c)
            cell.font, cell.border = F10, BORDER
            if c in (6, 7, 8):
                cell.number_format = "#,##0.00"
            if other:
                cell.fill = YELLOW
        i += 1
    if big:
        out_tr = i
        ws5.cell(row=out_tr, column=1, value="合计").font = Font(bold=True, size=10)
        ws5.cell(row=out_tr, column=2, value=f"{len(big)} 笔").font = Font(bold=True, size=10)
        cc = ws5.cell(row=out_tr, column=6, value=f"=SUM(F2:F{i - 1})")
        cc.font = Font(bold=True, size=10)
        cc.number_format = "#,##0.00"
        cc.border = Border(top=Side(style="medium"), bottom=Side(style="double"))
    for ci, w in zip(range(1, 11), [9, 14, 30, 30, 9, 14, 13, 14, 18, 52]):
        ws5.column_dimensions[get_column_letter(ci)].width = w
    ws5.freeze_panes = "A2"
    ws5.auto_filter.ref = f"A1:J{max(i - 1, 1)}"

    # ===== 6 破零自刷款 =====
    ws6 = wb.create_sheet("破零自刷款")
    ws6.append(["财务", "运营", "产品名称", "店铺", "对接群", "收款账户", "原费用类型", "金额", "笔数"])
    header(ws6, 9)
    bmap = OrderedDict()
    for r in brk_rows:
        k = (clean(r[C["运营"] - 1]), clean(r[C["对接群"] - 1]), clean(r[C["费用类型"] - 1]))
        d = bmap.setdefault(k, dict(prod=[], acct=[], amt=0.0, n=0))
        if r[C["产品名称"] - 1] not in d["prod"]:
            d["prod"].append(r[C["产品名称"] - 1])
        d["acct"].append(clean(r[C["收款账户"] - 1]))
        d["amt"] += r[C["报销金额"] - 1] or 0
        d["n"] += 1
    i = 2
    for (op, grp, ftype), d in bmap.items():
        tp = ftype if ftype == "破零自刷款" else "供应商货款（实为刷单）"
        ws6.append([fin_of(op), op, "、".join(uniq(d["prod"])), "多店铺", grp,
                    "；".join(uniq(d["acct"])), tp, round(d["amt"], 2), d["n"]])
        for c in range(1, 10):
            cell = ws6.cell(row=i, column=c)
            cell.font, cell.border = F10, BORDER
            if c == 8:
                cell.number_format = "#,##0.00"
            if tp.startswith("供应商货款"):
                cell.fill = YELLOW
        i += 1
    for ci, w in zip(range(1, 10), [10, 10, 18, 10, 24, 34, 20, 13, 7]):
        ws6.column_dimensions[get_column_letter(ci)].width = w
    ws6.freeze_panes = "A2"

    # ===== 7 运营-财务映射 =====
    ws7 = wb.create_sheet("运营-财务映射")
    ws7.append(["运营", "财务", "售后条数", f"{MONTH}付款笔数", "付款金额", "说明"])
    header(ws7, 6)
    ops = OrderedDict()
    for x in pay:
        d = ops.setdefault(x["op"], [0, 0.0])
        d[0] += 1
        d[1] += x["amt"]
    i = 2
    for op in sorted(ops, key=lambda o: (fin_of(o) == "", fin_of(o), -ops[o][0])):
        ws7.append([op, fin_of(op), sum(FM.get(op, {}).values()), ops[op][0], round(ops[op][1], 2),
                    "" if fin_of(op) else "售后台账无此运营的财务归属，待补"])
        for c in range(1, 7):
            cell = ws7.cell(row=i, column=c)
            cell.font, cell.border = F10, BORDER
            if c == 5:
                cell.number_format = "#,##0.00"
            if c == 6 and not fin_of(op):
                cell.fill = YELLOW
        i += 1
    for ci, w in zip(range(1, 7), [22, 12, 10, 12, 14, 30]):
        ws7.column_dimensions[get_column_letter(ci)].width = w
    ws7.freeze_panes = "A2"

    # ===== 8 口径说明 =====
    ws8 = wb.create_sheet("口径说明")
    ws8.column_dimensions["A"].width = 3
    ws8.column_dimensions["B"].width = 118
    NOTES = [
        (f"{YEAR}年{MONTH} 货款对账表", "T"),
        ("", ""),
        ("1. 数据来源", "H"),
        ("· 付款侧：{}，货款所属月份={}；剔除刷单款后取 供应商货款 {:,} 笔，合计即主表 G 列。".format(
            args.dingding, MONTH, len(pay)), ""),
        ("· 刷单款判定：① 费用类型=破零自刷款；② 产品名称=多产品 且 店铺=多店铺 且 对接群=多群，且 收款人属于运营人员（不限本人，只要是运营团队的人）——这类常被误填成「供应商货款」，实质同为刷单。{} 共 {:,} 笔 / {:,} 元，其中以货款名义提交的 {:,} 笔 / {:,} 元。".format(
            MONTH, len(brk_rows), round(brk_amt, 2), len(brk_from_sup),
            round(sum(r[C["报销金额"] - 1] or 0 for r in brk_from_sup), 2)), ""),
        ("· 扣款侧：{}，所属月份={} 共 {:,} 条；{}。".format(
            args.deduct, MONTH, len(af),
            "该表自带 财务、厂家（=收款方）、抵扣方式列" if fmt_a else "收款人列即收款方；无财务列，财务列待补"), ""),
        ("", ""),
        ("2. 主表结构（对齐 11月(1).xlsx 的 Sheet2）", "H"),
        ("· 财务｜运营｜产品名称｜对接群｜收款账户｜收款人｜报销金额｜后台退货｜仓库退货｜罚款｜拦截｜备注｜汇款比例", ""),
        ("· 汇款比例 = （后台退货+仓库退货+罚款+拦截）÷ 报销金额，即该供应商本月售后扣款占其打款金额的比重；无售后或报销金额为「未查到」的行留空。", ""),
        ("· 一行 = 一个「运营+对接群+收款人」组合，同一组合多笔付款合并；产品名多个用「、」连接。", ""),
        ("· 黄色行 = 售后有登记、{} 钉钉查不到对应货款，报销金额显示「未查到」。".format(MONTH), ""),
        ("", ""),
        ("3. 匹配规则（按优先级，运营字段优先于群名精确）", "H"),
        ("① 运营+群+厂家 全等。", ""),
        ("② 运营+厂家+抵扣期间 全等：取抵扣方式里「抵扣在 X 货款中」的期间，与钉钉结算时间比对（8.01-8.02 与 8.1-8.2 视为同期）。", ""),
        ("③ 同运营+同厂家：唯一群直接认；多群按去噪后群名相似度取最高，≥0.55 才认。", ""),
        ("④ 群+厂家 全等（跨运营）：仅当该厂家在售后所记运营名下无任何付款时才跨运营（运营两边写法常不一致）。", ""),
        ("⑤ 厂家唯一命中；或 厂家+群名相似（跨运营，≥0.6）。", ""),
        ("⑥ 都不中 → 进「未匹配清单」，不强行挂账。", ""),
        ("· 原则：宁可同运营近似，不跨运营精确。", ""),
        ("", ""),
        ("4. 校验", "H"),
        ("· 主表 G 列合计 = 供应商货款合计；H~K 四列合计 = 售后扣款总额（未匹配金额单列成行，不遗漏、不重复）。", ""),
        ("· 标「已回款」的记录仍计入四列，但已在核对总览单列金额，便于剔除后重算（现金已回则不应再冲减货款）。", ""),
        ("", ""),
        ("5. 注意", "H"),
        ("· 抵扣期间可能指向其他月份的货款，本表按「所属月份={}」口径挂账，期间仅用于定位付款行。".format(MONTH), ""),
        ("· 汇款比例>100% 的行通常是跨月累计抵扣，结合「匹配明细」的抵扣期间核对。", ""),
        ("· 「大额无售后」页只列报销>阈值且本行四列全空的行；代发大账号（如陈寒、刘政类）的售后常按真实工厂名登记，按收款人匹配不上，属正常现象。", ""),
        ("", ""),
        ("6. 预检与噪声", "H"),
        ("· 运行时逐 sheet 核对两侧「{}」行数与金额、报月份分布；目标月为 0 行直接报错；扣款簿多个 sheet 均含目标月数据时列出各 sheet 金额，防止选错 sheet。".format(MONTH), ""),
        ("· 未匹配清单新增「最接近付款组/相似度」列：未自动挂账时给出该收款人名下最相似的钉钉群及相似度，便于人工快速判断是否同一业务（相似度低于阈值时给出差多少）。", ""),
        ("· 0 元扣款记录只影响条数不影响金额：核对总览把未匹配拆成「有金额 / 0 元噪声」，未匹配清单中全 0 元的组标黄。", ""),
        ("· 核对总览「零、预检」段含 ⚠ 警告（红字）时不建议直接交付，先向用户确认口径。", ""),
    ]
    i = 1
    for text, typ in NOTES:
        cell = ws8.cell(row=i, column=2, value=text)
        if typ == "T":
            cell.font = Font(bold=True, size=13, color="1F4E79")
        elif typ == "H":
            cell.font = Font(bold=True, size=10, color="1F4E79")
        else:
            cell.font = F10
        i += 1

    wb.save(OUT)

    # ---------- 校验 ----------
    g_sum = sum(r["amt"] for r in rows if isinstance(r["amt"], (int, float)))
    d_sum = sum(r["h"] + r["w"] + r["f"] + r["b"] for r in rows)
    print("saved", OUT)
    print("主表数据行", len(rows), "（另有合计行 1 行）", "| 付款组", len(P), "| 扣款记录", len(af))
    print("校验1 G列合计", round(g_sum, 2), "== 供应商货款", round(sup_amt, 2), "OK" if abs(g_sum - sup_amt) < 0.01 else "不一致!")
    print("校验2 四列扣款", round(d_sum, 2), "== 扣款总额", round(h_amt, 2), "OK" if abs(d_sum - h_amt) < 0.01 else "不一致!")
    print("匹配", len(match), "未匹配", len(unmatched),
          "（有金额 {} 条 {:,.2f} 元 + 0元噪声 {} 条，{} 组）".format(len(u_nz), u_nz_amt, len(unmatched) - len(u_nz), len(agg_u)),
          "| 匹配率 金额 {:.1%} / 条数 {:.1%}".format((m_amt / h_amt) if h_amt else 0, (len(match) / len(af)) if af else 0))
    print("整体售后占货款比例 {:.2%}".format((h_amt / sup_amt) if sup_amt else 0))
    print("财务空行", sum(1 for r in rows if not r["fin"]), "| 大额无售后", len(big), "笔")
    for w in WARN:
        print("⚠", w)


if __name__ == "__main__":
    main()
