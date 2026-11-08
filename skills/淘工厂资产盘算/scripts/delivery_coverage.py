"""Export evidence displayed inside delivery, without inventing query counts."""
import json
from pathlib import Path
from openpyxl import load_workbook
from openpyxl.styles import Font

MODULES = dict(zip(
    ('consumer-experience-service','product-loan','trade-subsidy','trade-commission','credit-buy','xinjiang-logistics-consolidation','review-gift','channel-promotion','ad-agency','all-site-channel-promotion','taoke-channel-promotion','live-channel-promotion','customer-service-fee','compensate-recovery','punishment-data','overseas-added-commission','all-item-effects-export'),
    ('消费者体验提升服务','商品贷款','交易补贴','交易佣金','先用后付','新疆物流集运','评价有礼','营销费用','商业化账户充值提现','投流推广佣金','淘客推广佣金','直播推广佣金','客服服务费','赔付追缴','处罚数据','出海增值服务费','所有商品效果数据导出')))


def add_coverage(source, destination, receipt):
    if Path(source).resolve() == Path(destination).resolve() or Path(destination).exists():
        raise ValueError('Coverage requires a separate new candidate')
    wb = load_workbook(source)
    try:
        result = _append_coverage(wb, receipt)
        wb.save(destination)
        return result
    finally:
        wb.close()


def _append_coverage(wb, receipt):
    """Append evidence to an owned candidate without loading or saving it."""
    if '执行说明' in wb.sheetnames:
        raise ValueError('执行说明 already exists')
    ws = wb.create_sheet('执行说明')
    ws.append(['公司', (receipt or {}).get('company', '未提供本次发布凭证')])
    dates = (receipt or {}).get('dates') or {}
    ws.append(['支付区间', dates.get('order_pay_start'), dates.get('order_pay_end')])
    ws.append(['结算区间', dates.get('write_off_start'), dates.get('write_off_end')])
    ws.append(['说明', '零数据来自本次查询凭证；未记录查询行数不代表零；未执行不等于成功。处罚和部分费用日期口径由导出模块决定。'])
    ws.append(['流程', '状态', '查询行数', '实际查询条件（非文件名日期）'])
    results = (receipt or {}).get('results') or {}
    counts = {}
    for key, name in MODULES.items():
        result = results.get(key, {})
        status = result.get('status', 'skipped' if key in ((receipt or {}).get('skipped') or []) else 'not_executed')
        if status not in ('completed','zero','not_executed','skipped','failed'):
            raise ValueError('Unknown export coverage status: '+str(status))
        counts[status] = counts.get(status,0)+1
        query = result.get('queryRows')
        if query is not None and (type(query) is not int or query < 0):
            raise ValueError('Invalid export query count')
        if status == 'zero':
            if query not in (None,0): raise ValueError('Zero export with nonzero query count')
            query = 0
        filters = result.get('dates')
        ws.append([name, {'completed':'成功','zero':'确认零数据','not_executed':'未执行/凭证未提供','skipped':'跳过','failed':'失败'}[status],
                   query if query is not None else '未记录', json.dumps(filters,ensure_ascii=False,sort_keys=True) if filters else '旧凭证未记录模块实际条件；勿推断'])
    for row in ws:
        for cell in row:
            if isinstance(cell.value,str): cell.data_type='s'
    for cell in ws[5]: cell.font=Font(bold=True)
    ws.freeze_panes='A6'; ws.auto_filter.ref='A5:D22'
    for col,width in {'A':30,'B':28,'C':20,'D':100}.items(): ws.column_dimensions[col].width=width
    return {'modules':17,'counts':counts}
