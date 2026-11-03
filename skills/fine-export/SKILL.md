---
name: fine-export
description: 罚款导出工作流：从天猫 rules-center（商家处罚中心）按支付日期导出罚款明细（违规记录+赔付记录），默认直接复用本机浏览器已登录的天猫会话（无需用户发 fetch），自动校验登录态并获取 XSRF token；登录态失效时用 magpie-browser 在当前已打开的浏览器里自动登录（不开新浏览器、不落盘密码）；支持多账户串行导出（一个账户一个账户，切换登录后重跑同一命令续跑）；输出 11 列「处罚数据」格式 Excel（`店铺_罚款_支付开始_支付结束.xlsx`，店铺右侧带「类型」列：处罚数据/赔付追缴），表头与资产盘算的处罚数据 Sheet 一致，可直接导入盘算工作簿。当用户要求导出罚款、处罚数据、违规罚款、赔付记录时使用。
---

# 罚款导出（fine-export）

从天猫商家处罚中心（rules-center）导出罚款明细。数据 = 两个 tab 合并：`punish`（违规记录）+ `compensation`（赔付记录），按支付日期（即处罚时间范围）取数，翻页取满后写出 Excel。

## 目录结构

```
fine-export/
├── SKILL.md                     # 本文件
├── agents/openai.yaml           # 技能卡片（display_name 等）
└── scripts/
    ├── tmall_session.py         # 浏览器 cookie 解密 + 登录态校验（与 asset-inventory 同一份）
    ├── ensure_login.py          # 登录态闸门（唯一入口，0/3/1 退出码 + action 人话动作）
    ├── fine_export.py           # 单账户导出主脚本
    └── multi_account_runner.py  # 多账户串行执行器
```

## Python 环境

脚本依赖 `cryptography`（解密浏览器 cookie 库）与 `openpyxl`（写 Excel）。若系统 python3 缺库，先建运行环境再执行：

```bash
uv venv /tmp/aivenv --python 3.12
uv pip install --python /tmp/aivenv/bin/python cryptography openpyxl
CODEX_SPREADSHEET_PYTHON=/tmp/aivenv/bin/python3 /tmp/aivenv/bin/python3 scripts/fine_export.py …
```

写 Excel 的子进程解释器探测顺序：`CODEX_SPREADSHEET_PYTHON` → codex 主运行时 → `sys.executable`，以 `import openpyxl` 探活。

## 默认行为（单账户导出）

用户给出店铺名 + 支付日期后直接执行，**不需要用户发 fetch**：

```bash
python3 scripts/fine_export.py \
  --shop-name "店铺名称原文" \
  --pay-start 2026-10-01 --pay-end 2026-10-31 \
  --output-dir <输出目录>
```

流程（fine_export.py）：

1. `resolve_headers`：从本机 Chromium 系浏览器 cookie 库解密登录态（默认 `--browser chrome`，可选 chrome-beta/chrome-canary/edge/brave/chromium）。凭据只进内存，不写入技能文件、状态文件或日志。
2. `check_session`：调 `https://scm.tmall.com/loginStatus` 校验登录态；失败提示用户打开 `https://tgcwork.tmall.com/login` 登录后重试，退出码 2。
3. **获取 token**：从 cookie 中的 `XSRF-TOKEN`（部分环境为 `X-XSRF-TOKEN`，两个名字都接受）提取为 `x-xsrf-token` 请求头（`require_token`）。缺失则直接报 AuthenticationError（POST 会被 XSRF 校验拒绝，不让请求白打）。只打印「token 已获取」和脱敏诊断（账号、cookie 项数），**绝不打印 token 值**。
4. 两个 tab 各自按 `pageNo` 翻页（`POST https://tgc.tmall.com/ds/api/v1/rules-center/pageList`，Referer 为 rules-center 页面，pageSize 50，最多 60 页），按 `paginator.items`（回退 `total`）取满；取不满报错停止，**不写残缺文件**。
5. 行映射 → 写出 `<safe_prefix(店铺)>_罚款_<pay_start>_<pay_end>.xlsx`，Sheet 名「处罚数据」。

支付日期 = 处罚时间范围：开始日转北京时间毫秒 `00:00:00.000`，结束日 `23:59:59.999`（CST = UTC+8）。

## 罚款数据口径（11 列）

| 列 | 来源 |
|---|---|
| 店铺 | 用户发送的店铺名称原文 |
| 类型 | `punish`→`处罚数据`、`compensation`→`赔付追缴`（按数据来源 tab 标注） |
| 处罚内容 | `punishValue` → `remark` → `reason` 回退 |
| 主订单号 | `orderNo`（float 整数转 str，去空格） |
| 处罚时间 | `punishTime` → `gmtFinish` → `gmtCreate`，毫秒转 `%Y-%m-%d %H:%M:%S`（北京时间） |
| 违规类型 | `violationType` |
| 违规事件 | `violationEvent` |
| 商品id | `extend.itemId` → `extend.extendInfo.itemId` → `item.itemId` → `punishDimensionValue`（extend 可为 dict 或 JSON 字符串） |
| 商品标题 / 运营 | 接口不返回，留空不臆造 |
| 处罚金额 | `punishAmount` → `extend.amount`，接口单位为分，÷100 保留 2 位；0 元记录也写入 |

表头与资产盘算「处罚数据」Sheet 完全一致，因此导出文件可直接作为该 Sheet 导入盘算工作簿（参见 asset-inventory 的 pan_suan_builder.py，目标 Sheet「处罚数据」）。

## 备用登录态（--fetch-file）

浏览器 cookie 解不开（如 Chrome v20 App-Bound）时，让用户从 DevTools 复制一条对 `https://tgc.tmall.com` 的 **POST** fetch 代码存入文件：

```bash
python3 scripts/fine_export.py --fetch-file fetch.txt --shop-name … --pay-start … --pay-end …
```

只取其登录请求头（剔除 host/content-length/connection/accept-encoding/priority；cookie 经 `*tb\_token*`→`_tb_token_` 等修复）；日期一律以命令行为准。非 tgc.tmall.com 或非 POST 的 fetch 直接拒绝。

## 多账户串行导出

一次给出多个账户时，按用户给定顺序**一个账户一个账户串行**执行（multi_account_runner.py）。每次只处理当前登录的账户：

```bash
# 方式一：命令行直接给账户（可重复）
python3 scripts/multi_account_runner.py \
  --output-root <输出根目录> \
  --account "店铺A,账号A,2026-10-01,2026-10-31" \
  --account "店铺B,账号B,2026-10-01,2026-10-31"

# 方式二：账户清单 JSON（{"accounts":[{shop,account,payStart,payEnd,accountHint?}]}，也接受裸数组）
python3 scripts/multi_account_runner.py --accounts-file accounts.json --output-root <输出根目录>
```

闸门语义（与 asset-inventory multi_shop_runner 一致）：

- 每个账户执行前校验登录态：未登录 → 输出 `{"status":"需要登录",...}` 退出码 3；提供了 `accountHint` 且与当前登录昵称不符 → 「登录态与店铺不匹配」退 3；与上一账户昵称相同 → 「登录态没有切换」退 3。
- 退 3 时**不要直接把球踢回给用户**：先跑 `scripts/ensure_login.py`（与 asset-inventory 同一份、同一个天猫登录态，退出码 0=有效 / 3=需登录 / 1=出错），只有 3 才用 magpie-browser 在**当前已打开的浏览器**里复用（没有才 `tab_new`）一个 `tgcwork.tmall.com` 标签登录该账户——不开新浏览器实例、不落盘密码、不挖钥匙串；登录后用 `ensure_login.py --wait 30` 确认 `loggedIn: true`，再重跑同一条命令续跑。仅当出现短信/滑块验证码或用户明确拒绝自动登录时才交回人工。
- 退 3 后**重跑同一条命令**即可续跑：已完成账户凭产物文件判定，配 `--skip-completed` 跳过。
- 每账户输出目录 `<root>/<safe_prefix(店)>/`，日志 `logs/export.log`，罚款文件必须实际存在才算完成。
- 批次状态 `<root>/fine-export-state.json`（原子写：.tmp → replace），按 shop 名去重更新。
- 默认某账户失败立即停（已完成的产物全部保留）；`--continue-on-error` 继续后续账户。`--only 店名` 只跑指定店铺；`--check-login` 只输出脱敏 JSON 不执行；`--skip-login-check` 仅调试用。
- 退出码：0 全部成功；3 需要登录/登录态不匹配；4 某账户导出失败；1 参数或环境错误。

## 快速自检

```bash
python3 scripts/fine_export.py --check-login          # 登录态 + token 脱敏诊断，就绪退 0，否则退 2
python3 scripts/multi_account_runner.py --check-login # 当前登录昵称（用于多账户核对）
python3 scripts/fine_export.py --shop-name X --pay-start 2026-10-01 --pay-end 2026-10-31 --dry-run
```

`--dry-run` 只打印请求计划（含两 tab 的毫秒时间戳与输出文件名），不读钥匙串、不联网、不写文件。

## 失败处理

- 退出码 2（单账户）/ 3（多账户）= 登录问题：先跑 `ensure_login.py` 判定，为 3 时用 magpie-browser 在当前已打开的浏览器里（复用已有 `tgcwork.tmall.com` 标签，没有才 `tab_new`）登录对应账号，`ensure_login.py --wait 30` 确认后重跑同一命令；不要新开浏览器、不要落盘密码。
- 登录 cookie 缺 XSRF token（`XSRF-TOKEN` 与 `X-XSRF-TOKEN` 都没有）：提示重新登录 tgc.tmall.com 或改用 `--fetch-file`。
- 数据取不满（翻页 60 页仍未达总数）：报错停止，不写残缺文件。
- 所有错误信息经脱敏（cookie / authorization / x-xsrf-token / URL 一律 redact）。

## 与资产盘算的关系

罚款导出是 asset-inventory 中「处罚数据」流程的独立、可单跑版本：数据口径、11 列表头（含「类型」列）、Sheet 名完全一致；导出的罚款 Excel 可直接导入盘算工作簿的「处罚数据」Sheet（公司汇总按商品 id SUMIFS 处罚金额列）。盘算整体流程仍以 asset-inventory 为准，本 skill 只负责罚款单独导出。
