---
name: 淘工厂导出
description: 从淘工厂导出17类账单/数据文件（消费者体验提升服务、商品贷款、交易补贴、交易佣金、先用后付、新疆物流集运、评价有礼、营销费用、商业化账户充值提现、投流推广佣金、淘客推广佣金、直播推广佣金、客服服务费、赔付追缴、处罚数据、出海增值服务费、所有商品效果数据导出）。公司名称、支付日期、结算日期全部由用户在消息里发送（不从登录态解析）；登录态默认通过「淘工厂登录」skill 从本机浏览器 cookie 库自动获取（含 HttpOnly cookie），也支持用户直接贴 queryBillDetail fetch（cookie 只在内存/临时文件中使用，绝不落盘、不回显）。默认一次执行全部17个流程，用户指定单一流程时（如「淘工厂导出罚款」）只执行该流程。所有导出文件统一放入一个文件夹，文件夹名为「公司名称_结算开始_结算结束」。当用户说「淘工厂导出」「导出淘工厂账单」「淘工厂导出罚款/处罚数据/商品贷款」等导出淘工厂文件的需求时使用；只导出文件、不写盘算工作簿（盘算请用 asset-inventory）。
---

# 淘工厂导出

## 用途

把资产盘算的 17 个账单下载流程抽出来做成**纯导出**：查询 → 创建明细任务 → 轮询 → 下载 → 安全解压，**不写盘算工作簿**（需要盘算汇总时用 asset-inventory skill）。所有导出文件统一放进一个文件夹：`<输出根>/<公司名称>_<结算开始>_<结算结束>/`。

## 输入（全部来自用户消息，缺了就先问，不要自行编造、不从登录态解析）

1. **公司名称**（`--company-name`）：取用户消息原文（如「埔鸣」或「义乌市埔鸣日用品有限公司」），只用于导出文件夹命名。
2. **支付日期**（`--order-pay-start/--order-pay-end`）与**结算日期**（`--write-off-start/--write-off-end`），格式 `YYYY-MM-DD`，开始不晚于结束；两类日期各自原样透传，不做推算。默认全部流程两类日期都要。
3. **流程范围**：默认全部 17 个流程；用户明确指定（如「淘工厂导出罚款」）时只传对应 `--module`。单跑处罚数据/所有商品效果数据导出/4个仅结算流程（评价有礼、商业化账户充值提现、客服服务费、赔付追缴）时只需要支付日期。
4. **登录态**：默认复用本机浏览器登录态（自动含 HttpOnly cookie）；用户主动贴了 `queryBillDetail` fetch 时存成临时文件传 `--fetch-file`（用后即删）。用户随消息发来的账号/密码只用于需要登录时的浏览器填表，不落盘。

## 登录态：一律通过「淘工厂登录」skill

- 判据只用脚本：`python3 /Users/rabbit/.agents/skills/淘工厂登录/scripts/ensure_login.py`（退出码 0=有效 / 3=需登录 / 1=脚本错）。
- 需要登录时按「淘工厂登录」skill 的流程做：**当前已打开的浏览器**（绝不新开浏览器/新 profile）里**新开一个 Magpie 标签页**走 https://tgcwork.tmall.com/login 登录，不复用任何已有标签页；凭据用用户本轮消息里给的账号密码；滑块/短信让用户自己过；登录后 `ensure_login.py --wait 30` 确认再重跑导出。

## 流程名映射（用户中文说法 → --module）

| 用户说法 | --module |
|---|---|
| 全部（默认） | `all` |
| 消费者体验提升服务 | `consumer-experience-service` |
| 商品贷款/商品货款 | `product-loan` |
| 交易补贴 | `trade-subsidy` |
| 交易佣金 | `trade-commission` |
| 先用后付 | `credit-buy` |
| 新疆物流集运 | `xinjiang-logistics-consolidation` |
| 评价有礼 | `review-gift` |
| 营销费用 | `channel-promotion` |
| 商业化账户充值提现 | `ad-agency` |
| 投流推广佣金 | `all-site-channel-promotion` |
| 淘客推广佣金 | `taoke-channel-promotion` |
| 直播推广佣金 | `live-channel-promotion` |
| 客服服务费 | `customer-service-fee` |
| 赔付追缴 | `compensate-recovery` |
| 罚款/处罚/处罚数据/违规 | `punishment-data` |
| 出海增值服务费 | `overseas-added-commission` |
| 所有商品效果数据导出 | `all-item-effects-export` |

`--module` 也直接接受这些中文名（脚本内建别名表）。

## 执行命令

```bash
# 默认全部17个流程（公司名称与日期都取用户消息）
python3 /Users/rabbit/.agents/skills/淘工厂导出/scripts/taogongchang_export.py \
  --company-name 埔鸣 \
  --write-off-start 2026-07-01 --write-off-end 2026-07-31 \
  --order-pay-start 2026-07-01 --order-pay-end 2026-07-31 \
  --output-root /path/to/outputs

# 指定单一流程（例如用户说「淘工厂导出罚款」，罚款只要支付日期）
python3 /Users/rabbit/.agents/skills/淘工厂导出/scripts/taogongchang_export.py \
  --module 罚款 --company-name 埔鸣 \
  --order-pay-start 2026-07-01 --order-pay-end 2026-07-31 \
  --output-root /path/to/outputs

# 用户贴了 fetch：存临时文件传入（用后即删，绝不回显内容）
python3 /Users/rabbit/.agents/skills/淘工厂导出/scripts/taogongchang_export.py \
  --company-name 埔鸣 --fetch-file /tmp/fetch.txt \
  --write-off-start 2026-07-01 --write-off-end 2026-07-31 \
  --order-pay-start 2026-07-01 --order-pay-end 2026-07-31 \
  --output-root /path/to/outputs

# 中断续跑
... --resume
```

脚本内部调用 asset-inventory 的 `tmall_bill_download.py --no-pan-suan` 完成实际下载；17 个流程的固定顺序、billType、日期规则、轮询与安全解压全部沿用 asset-inventory 的定义。

## 输出

- 唯一导出文件夹：`<输出根>/<公司名称>_<结算开始>_<结算结束>/`（公司名取用户消息原文，非法字符替换为 `-`；只给支付日期时后缀用支付区间）。
- **文件夹里只放最终 xlsx 文件，且以流程名称命名**（如 `商品贷款.xlsx`、`处罚数据.xlsx`、`所有商品效果数据导出.xlsx`）：全部流程成功后脚本自动把解压出的 xlsx 重命名并提到文件夹根目录，删除 zip、解压目录、流程子目录、状态文件等全部中间产物；0 条数据的流程没有文件。导出中断（未全部成功）时不整理，保留中间产物供 `--resume` 续跑。
- 完成后汇报：公司名称、执行的流程清单、每流程查询条数/taskId、最终 xlsx 清单、导出文件夹绝对路径；失败时列出已完成与失败流程及 `--resume` 续跑方式。

## 安全红线

- Cookie、XSRF Token、授权头、签名 URL 只在内存/临时文件中使用，用后即删；绝不写入技能文件、日志、状态文件或回复，绝不回显 fetch 内容。
- 不落盘密码；不绕过验证码（滑块/短信让用户手动过）。
- 浏览器动作只在当前已打开的浏览器、新开的 Magpie 标签页里做，绝不新开浏览器、不复用已有标签页。
- HTTP 401/未登录时立即停止剩余流程，已下载文件保留。
- 日期只认用户给的值，不做推算。

## 依赖

- Python 3.11+，`cryptography`、`openpyxl`（与 asset-inventory 相同；缺依赖时按其 SKILL.md 用 uv 建 venv）。
- 依赖两个同级 skill：「淘工厂登录」（登录态与公司名）与「asset-inventory」（`scripts/tmall_bill_download.py` 实际下载）。这两个 skill 被移动/重命名时本 skill 的脚本路径要同步更新。
