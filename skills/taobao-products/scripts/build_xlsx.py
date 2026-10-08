#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""淘宝商品获取 · Excel 生成器

输入：OpenCLI `taobao top-sales` 导出的 JSON
输出：Excel（商品缩略图 + 独立链接列 + 说明页）

字段：排名(销量序)、商品标题、商品ID、价格(元)、销量(原文)、销量(约/笔)、
      店铺、发货地、商品链接、商品图片、图片链接、备注

两条硬规则（改动前先读，都是踩过的坑）：
  1. 推广位角标（「本月行业热销」/「行业销量TopN」）不得当成销量数字：
     角标里的数字是名次（Top3 的 3），不是成交笔数 → 销量(约/笔) 留空 + 备注标注。
     判定要求出现「N人收货 / N人付款」这类真实成交文案。
  2. 浮层缩略图必须与「查看图片」超链接分列：Excel 浮层图的绘制层级高于单元格文字，
     同一格既放图又放链接，链接会被图压住、看得见图却点不到链接。
     故「商品图片」列只放图（单元格文本留空），「图片链接」列单独放超链接。

"""
import argparse
import io
import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

try:
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit('缺少 openpyxl：请用本 skill 的 venv（~/.taobao-skill-venv/bin/python）运行，'
             '或 pip install openpyxl pillow')

try:
    from PIL import Image as PILImage
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')

HEADERS = ['排名(销量序)', '商品标题', '商品ID', '价格(元)', '销量(原文)',
           '销量(约/笔)', '店铺', '发货地', '商品链接', '商品图片', '图片链接', '备注']

COL = {name: i + 1 for i, name in enumerate(HEADERS)}
IMG_COL = COL['商品图片']          # 只放浮层缩略图，单元格文本留空
LINK_COL = COL['商品链接']         # 商品详情页链接
IMGLINK_COL = COL['图片链接']      # 「查看图片」超链接，独立成列，不会被浮层图遮挡
THUMB_PX = 64

# 列宽（字符单位）；商品图片列宽须 >= 缩略图宽度，否则图会溢到相邻列
WIDTHS = [11, 46, 17, 10, 15, 12, 20, 11, 44, 13, 11, 24]

PROMO_RE = re.compile(r'行业销量|行业热销|热销榜|广告|推广')


def parse_sales(raw):
    """'5万+人收货' → 50000；推广位角标（本月行业热销 / 行业销量TopN）→ None

    必须出现「N人收货/付款」这类真实成交文案才算销量；
    推广位角标里的数字是名次（如「行业销量Top3」的 3），不是成交笔数，一律留空。
    """
    if not raw:
        return None
    if PROMO_RE.search(raw):
        return None
    m = re.search(r'([0-9.]+)\s*(万|亿)?\s*\+?\s*人', raw)
    if not m:
        return None
    val = float(m.group(1))
    if m.group(2) == '万':
        val *= 10000
    elif m.group(2) == '亿':
        val *= 1e8
    return int(val)


def norm_price(raw):
    if not raw:
        return None
    m = re.search(r'([0-9]+(?:\.[0-9]+)?)', raw)
    return float(m.group(1)) if m else None


def fetch_thumb(url):
    """下载并压成缩略图 PNG 字节；失败返回 None。"""
    if not url or not HAVE_PIL:
        return None
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': 'https://s.taobao.com/'})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read()
        im = PILImage.open(io.BytesIO(data))
        im = im.convert('RGB')
        im.thumbnail((THUMB_PX, THUMB_PX))
        buf = io.BytesIO()
        im.save(buf, format='PNG')
        buf.seek(0)
        return buf, im.width, im.height
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description='淘宝商品获取 · 生成 Excel (v2)')
    ap.add_argument('--input', required=True, help='opencli taobao top-sales 输出的 JSON')
    ap.add_argument('--output', required=True, help='xlsx 输出路径')
    ap.add_argument('--keyword', default='淘宝', help='搜索关键词（用于表名与标题）')
    ap.add_argument('--pages', type=int, default=1, help='本次点了几次「下一页」（写进说明页）')
    ap.add_argument('--requested', type=int, default=0, help='用户要求的条数（写进说明页）')
    ap.add_argument('--no-images', action='store_true', help='跳过图片嵌入（仅保留图片链接）')
    args = ap.parse_args()

    with open(args.input, encoding='utf-8') as f:
        rows = json.load(f)
    if not isinstance(rows, list) or not rows:
        sys.exit('输入 JSON 为空或格式不对')

    embed = HAVE_PIL and not args.no_images
    thumbs = {}
    if embed:
        urls = [(i, r.get('image') or '') for i, r in enumerate(rows, start=2)]
        with ThreadPoolExecutor(max_workers=8) as ex:
            for (idx, _), res in zip(urls, ex.map(lambda t: fetch_thumb(t[1]), urls)):
                if res:
                    thumbs[idx] = res

    wb = Workbook()
    title = re.sub(r'[\[\]\*\?/\\:]', '', str(args.keyword))[:20]
    ws = wb.active
    ws.title = (title + '_销量TOP' + str(len(rows)))[:31]

    ws.append(HEADERS)
    fill = PatternFill('solid', fgColor='C0392B')
    hfont = Font(bold=True, color='FFFFFF', size=11)
    thin = Side(style='thin', color='D0D0D0')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for c in range(1, len(HEADERS) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = fill
        cell.font = hfont
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = border
    ws.row_dimensions[1].height = 26

    promo = 0
    img_ok = 0
    img_fail = 0
    for i, r in enumerate(rows, start=1):
        sales_raw = (r.get('sales') or '').strip()
        sales_num = parse_sales(sales_raw)
        url = (r.get('url') or '').strip()
        img_url = (r.get('image') or '').strip()
        note = ''
        if sales_num is None:
            note = '推广位·页面未显示成交数'
            promo += 1

        ws.append([
            i,
            r.get('title', ''),
            str(r.get('item_id') or ''),
            norm_price(r.get('price')),
            sales_raw or '(页面未显示)',
            sales_num,
            r.get('shop', ''),
            r.get('location', ''),
            url,                       # 商品链接：独立字段
            '',                        # 商品图片：只放浮层缩略图
            '',                        # 图片链接：下面单独填
            note,
        ])
        row = ws.max_row

        # 商品标题：纯文本，不再挂链接（链接已独立成列）
        h = ws.cell(row=row, column=COL['商品标题'])
        h.alignment = Alignment(vertical='center', wrap_text=True)
        h.font = Font(size=10, bold=(i <= 3))
        h.hyperlink = None

        # 商品链接：整格超链接，显示完整 URL，便于点击与复制
        lc = ws.cell(row=row, column=LINK_COL)
        if url:
            lc.hyperlink = url
            lc.font = Font(size=9, color='0563C1', underline='single')
        lc.alignment = Alignment(vertical='center')

        ws.cell(row=row, column=COL['排名(销量序)']).alignment = Alignment(horizontal='center')
        ws.cell(row=row, column=COL['商品ID']).alignment = Alignment(horizontal='center')
        pc = ws.cell(row=row, column=COL['价格(元)'])
        pc.number_format = '¥#,##0.00'
        pc.alignment = Alignment(horizontal='right')
        ws.cell(row=row, column=COL['销量(原文)']).alignment = Alignment(horizontal='center')
        sc = ws.cell(row=row, column=COL['销量(约/笔)'])
        sc.number_format = '#,##0'
        sc.alignment = Alignment(horizontal='right')
        ws.cell(row=row, column=COL['发货地']).alignment = Alignment(horizontal='center')
        ws.cell(row=row, column=COL['备注']).alignment = Alignment(horizontal='center')
        for c in range(1, len(HEADERS) + 1):
            ws.cell(row=row, column=c).border = border

        # 浮层缩略图只落在「商品图片」格；该格文本保持空，避免图压字
        if row in thumbs:
            buf, w, hgt = thumbs[row]
            xi = XLImage(buf)
            xi.width, xi.height = w, hgt
            ws.add_image(xi, f'{get_column_letter(IMG_COL)}{row}')
            ws.row_dimensions[row].height = max(46, hgt * 0.78)
            img_ok += 1
        else:
            ws.row_dimensions[row].height = 30
            if img_url:
                img_fail += 1

        # 图片链接：独立列，任何查看器下都可点（浮层图不会覆盖它）
        ic = ws.cell(row=row, column=IMGLINK_COL)
        if img_url:
            ic.value = '查看图片'
            ic.hyperlink = img_url
            ic.font = Font(size=9, color='0563C1', underline='single')
        else:
            ic.value = '(无)'
        ic.alignment = Alignment(horizontal='center', vertical='center')

    for idx, w in enumerate(WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = w
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = f'A1:{get_column_letter(len(HEADERS))}{ws.max_row}'

    # ---------- 说明页 ----------
    ws2 = wb.create_sheet('说明')
    ws2.column_dimensions['A'].width = 20
    ws2.column_dimensions['B'].width = 96
    nums = [parse_sales(r.get('sales')) for r in rows]
    known = [n for n in nums if n]
    _bad = [i for i in range(len(known) - 1) if known[i] < known[i + 1]]
    sort_check = ('销量(约/笔) 已剔除推广位空值后共 %d 个有效值，%s'
                  % (len(known), '严格降序，无逆序' if not _bad else '存在 %d 处逆序' % len(_bad)))
    prices = [p for p in (norm_price(r.get('price')) for r in rows) if p]
    info = [
        ('数据内容', '淘宝搜索「%s」按销量降序的前 %d 条商品（唯一商品 ID %d 个）'
                     % (args.keyword, len(rows), len({r.get('item_id') for r in rows if r.get('item_id')}))),
        ('用户要求条数', str(args.requested) if args.requested else '未指定'),
        ('字段顺序', ' → '.join(HEADERS)),
        ('字段说明', '「商品标题」为纯文本；「商品链接」独立成列（在「商品图片」之前），整格可点击并显示完整 URL；'
                     '「商品图片」列只放缩略图（单元格文本留空），「图片链接」独立成列，故浮层缩略图不会遮挡链接文字'),
        ('排序方式', '点击搜索结果页的「销量」标签排序（已验证：点击后页面「销量」标签状态变为选中）'),
        ('排序校验', sort_check),
        ('翻页方式', '点击页面「下一页」%d 次（每页约 48 条）；URL 的 &s= / &page= 参数实测无效' % args.pages),
        ('抓取时间', datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
        ('数据来源', 'OpenCLI 用户适配器 taobao/top-sales + 本机 Chrome 已登录的淘宝会话'),
        ('销量含义', '「销量(原文)」是页面原文（如 5万+人收货 / 4000+人付款）；「销量(约/笔)」为换算约数，仅供排序参考'),
        ('销量区间', ('约 %s ~ %s 笔' % (format(min(known), ','), format(max(known), ','))) if known else '—'),
        ('价格区间', ('¥%.2f ~ ¥%.2f' % (min(prices), max(prices))) if prices else '—'),
        ('商品图片', ('已嵌入缩略图 %d 张' % img_ok) + ('；%d 张因下载/库限制改为「查看图片」链接' % img_fail if img_fail else '')
                     + ('（未安装 Pillow，全部为链接）' if not HAVE_PIL else '')),
        ('推广位说明', '%d 条为列表中的推广位（角标「本月行业热销」/「行业销量TopN」），页面不显示成交笔数，'
                       '故「销量(约/笔)」留空并在备注列标注，不按角标名次臆造数字' % promo),
        ('排序参数提醒', 'URL 上的 &sort=sale-desc 对本页面无效：加载后页面状态仍为「综合」选中、销量乱序。必须点击页面控件才真正按销量排序'),
        ('翻页参数提醒', 'URL 上的 &s=44 / &page=2 同样无效：实测第二页与第一页重合 49/50。翻页只能点击页面「下一页」按钮'),
        ('风控说明', '脚本内置验证页检测：命中即中止、绝不自动重试。若曾在抓取中触发验证，请先在 Chrome 手动完成验证再重跑，并保持 90 秒以上的请求间隔'),
        ('数据口径', '价格与销量均为页面展示值，会随促销与时间波动；店铺名/发货地按页面原文录入；标题为原文截断（≤80 字）'),
        ('操作提示', '点击「商品标题」右侧的「商品链接」打开商品详情页；「图片链接」列可打开商品原图'),
    ]
    for k, v in info:
        ws2.append([k, v])
        rr = ws2.max_row
        ws2.cell(row=rr, column=1).font = Font(bold=True, size=10)
        ws2.cell(row=rr, column=1).alignment = Alignment(vertical='top')
        ws2.cell(row=rr, column=1).fill = PatternFill('solid', fgColor='F2F2F2')
        ws2.cell(row=rr, column=2).alignment = Alignment(vertical='top', wrap_text=True)
        ws2.row_dimensions[rr].height = 30

    outdir = os.path.dirname(os.path.abspath(args.output))
    if outdir and not os.path.isdir(outdir):
        os.makedirs(outdir, exist_ok=True)
    wb.save(args.output)

    print('已生成: %s' % args.output)
    print('数据行: %d | 推广位: %d | 嵌入图片: %d | 图片降级为链接: %d | Pillow: %s'
          % (len(rows), promo, img_ok, img_fail, HAVE_PIL))
    print('唯一商品ID: %d' % len({r.get('item_id') for r in rows if r.get('item_id')}))


if __name__ == '__main__':
    main()
