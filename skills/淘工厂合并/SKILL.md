---
name: 淘工厂合并
description: 把多个淘工厂 Excel 合并成一个盘算式工作簿（xlsx）。汇总逻辑复用「资产盘算」skill：以盘算模板为底，产出「公司汇总」Sheet（30 列固定表头、商品行公式、总计行）+ 各流程明细 Sheet + 费用归集等模板页。文件来源两种：①用户直接给文件路径；②用「淘工厂导出」skill 生成的导出文件夹（默认 ~/Downloads/淘工厂导出/<公司名称>_<结算开始>_<结算结束>/，按公司名称+结算日期自动定位）。合并后文件名为「公司名称_结算日期.xlsx」；公司名称和结算日期都由用户在消息里给出，结算日期没给就用当天日期。纯本地合并，不需要登录态。当用户说「淘工厂合并」「合并导出的文件」「把这几个 excel 合并成一个」等合并需求时使用。
---

# 淘工厂合并

## 用途

把淘工厂导出的流程 xlsx 合并成一个**盘算式工作簿**：复用「资产盘算」skill 的
`pan_suan_builder.py`（`/Users/rabbit/.agents/skills/asset-inventory/scripts/pan_suan_builder.py`），
以 `asset-inventory/assets/盘算模板.xlsx` 为底，把各流程明细写入对应 Sheet，并重建
**「公司汇总」Sheet**——30 列固定表头、按商品逐行的 SUMIFS 公式、紧贴数据的「总计」行，
保留模板 55 行结构。**纯本地操作**：不需要登录态、不联网；要重新导出文件用「淘工厂导出」skill。

## 输入（全部来自用户消息，缺了先问，不要自行编造）

1. **公司名称**（`--company-name`）：取用户消息原文（如「埔鸣」）；用于输出文件命名，并写入公司汇总「店铺」列。
2. **结算日期**（`--settlement-date`）：`YYYY-MM-DD` 单日或 `YYYY-MM-DD_YYYY-MM-DD` 区间（`~` 分隔也接受）；**用户没给就用当天日期**。
3. **文件来源（二选一）**：
   - 用户直接给文件：`--files 路径1 路径2 …`
   - 「淘工厂导出」生成的文件夹：默认 `--export-root ~/Downloads/淘工厂导出`，脚本按 `<公司名称>_<结算日期>` 自动定位文件夹；文件夹名对不上时用 `--export-folder` 直接指定路径。

用户随消息发来的账号/密码在本 skill 里用不上（不联网）；若发现导出文件夹缺失或数据要更新，提示用户改用「淘工厂导出」重新导出。

## 执行命令

```bash
# 方式1：用户直接给文件
python3 /Users/rabbit/.agents/skills/淘工厂合并/scripts/merge_excel.py \
  --company-name 埔鸣 \
  --settlement-date 2026-09-01_2026-10-31 \
  --files /path/交易佣金.xlsx /path/商品贷款.xlsx

# 方式2：合并「淘工厂导出」生成的文件夹（结算日期没给 → 当天）
python3 /Users/rabbit/.agents/skills/淘工厂合并/scripts/merge_excel.py \
  --company-name 埔鸣 \
  --settlement-date 2026-09-01_2026-10-31

# 导出文件夹名对不上时直接指定
... --export-folder ~/Downloads/淘工厂导出/埔鸣_2026-09-01_2026-10-31

# 分多批合并用户文件时：第二批起加 --accumulate 在已有输出上续并（保留公司汇总手工列）
... --files 批次2.xlsx --accumulate
```

## 输出

- 合并文件：`<输出目录>/<公司名称>_<结算日期>.xlsx`；输出目录默认=导出根目录（方式2）或第一个文件所在目录（方式1），`--output-dir` 可覆盖。
- 结构（= 盘算模板的 Sheet 结构）：**公司汇总**（店铺/运营姓名/商品ID/产品名称/数量/收入/交易补贴/交易佣金/先用后付/消费者体验提升服务/客服服务费/新疆物流集运/出海增值服务费/赔付追缴/新品营销(营销费用)/评价有礼/商业化账户充值提现/充值加码消耗/智能优惠券金额/投流推广佣金/淘客推广佣金/直播推广佣金/刷单金额/罚款/直通车营销/收入金额/收款人/货款/利润/备注（查询不到的） 30 列）+ 使用说明/数据检查/id汇总/中间表 + 各流程明细 Sheet + 费用归集辅助/未归属费用。
- 商品清单自动取自各流程明细 Sheet 的商品ID；公司汇总金额列是引用明细 Sheet 的公式；「总计」行=商品行合计+客服服务费/赔付追缴/评价有礼/商业化账户充值提现整表合计。
- 默认从干净模板**重建**（幂等：同一批源文件产出相同结果）；`--accumulate` 才在已有输出上续并，且公司汇总手工列（运营姓名等）按商品ID保留。
- 公式缓存：默认用 LibreOffice 无界面计算后写入（机器上没有 soffice 时自动跳过，Excel/WPS 打开时计算）；`--no-recalculate` 关闭。

## 注意

- 流程识别沿用 pan_suan_builder 规则：先按文件名前缀（商品贷款/商品货款→商品货款 Sheet），再按表头签名；无法识别的文件**默认跳过并在 stderr 提示**，`--strict` 改为直接失败。
- 含「公司汇总」Sheet 的文件（如上次合并的输出）不会被再次吞入。
- **所有商品效果数据导出.xlsx 缺工作表 dimension**：pan_suan_builder 已处理（read_only 误读时回退全量加载）。
- 运行脚本用 `python3` 即可，实际盘算构建在带 openpyxl 的 Python 里执行（`CODEX_SPREADSHEET_PYTHON` 可强制指定）。
- 依赖「资产盘算」skill 在 `/Users/rabbit/.agents/skills/asset-inventory/`（pan_suan_builder.py + 盘算模板.xlsx）。
