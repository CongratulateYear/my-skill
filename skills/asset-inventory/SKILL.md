---
name: asset-inventory
description: 资产盘算工作流：默认直接复用本机浏览器已登录的天猫会话（无需用户发 fetch），登录态失效时用 magpie-browser 在当前已打开的浏览器里自动登录（不开新浏览器、不落盘密码），只需给出结算时间和支付时间，即按固定顺序执行全部17个账单流程，创建下载任务、轮询、下载与安全解压，并把每类 Excel 写入盘算工作簿对应 Sheet、刷新公司汇总和费用归集；用户发起 fetch 时也可复用其中登录态与日期。支持用户明确限定单流程及中断后续跑；支持一次给出多个店铺，按用户给定顺序一个店铺接一个店铺串行执行（见“多店铺串行执行”）。公司汇总固定30列表头不得改变，店铺列取用户发送的店铺名称，公司汇总只按商品逐行列出并保留模板55行结构，所有商品效果数据导出仅由专属流程导入，其他流程不得覆盖。盘算文件名固定为 `店铺_结算开始_结算结束.xlsx`。适用于消费者体验提升服务、商品贷款/商品货款、交易补贴、交易佣金、先用后付、新疆物流集运、评价有礼、营销费用、商业化账户充值提现、投流/淘客/直播推广佣金、客服服务费、赔付追缴、处罚数据、出海增值服务费和所有商品效果数据导出。
---

# 资产盘算

## 默认行为

用户给出店铺名称、结算时间和支付时间后直接执行，**不需要用户复制 fetch**：

0. **店铺名称**取用户本条消息里发送的名称原文（例如「埔鸣--迎风部」或「埔鸣」），不做增删改；它同时用于公司汇总「店铺」列和盘算文件名前缀。用户没给名称时先问一句店铺名，不要自行编造。用户一次给了多个店铺时，按用户列出的先后顺序逐店执行，见“多店铺串行执行”。
1. 登录态由脚本自动从本机浏览器读取（`scripts/tmall_session.py`：解密 Chrome cookie 库，取出含 HttpOnly 的完整 cookie），并用 `https://scm.tmall.com/loginStatus` 校验有效后才会发请求。
2. 校验不通过时**不要停下去找用户**：按下一节「登录态自愈」用 magpie-browser 在当前已打开的浏览器里自动完成登录，登录成功后再继续原流程。
3. 用户未明确限定流程时，按下表固定顺序执行全部17个流程。
4. 每个流程使用技能配置的固定 `billType`（导出流程为 `bizType`+`bizScene`）和日期字段，依次查询、创建明细任务、轮询、获取签名地址、下载和安全解压。
5. 将下载的 `.xlsx` 强制写入该流程对应 Sheet，不依赖下载文件名猜测。
6. 在同一盘算文件上累积各流程结果；保存后验证目标 Sheet、写入行数、固定表头及 `#REF!`。
7. 仅更新现有工作簿中与本次数据相关的值和公式；不会创建、删除或重建任何 Sheet，也不会自动打开工作簿。公式缓存默认不通过 LibreOffice 重写；只有用户明确要求时才可进行无界面重算。

若用户主动贴了一条 `queryBillDetail` fetch，则改用它提供的登录态和日期（fetch body 里的日期优先于缺省值），但仍可用命令行日期覆盖。fetch body 原有 `billType` 不限制执行范围。

不要只解释参数，也不要再次询问是否执行。只有用户明确说“只跑某流程”时才传单一 `--module`。

## 登录态自愈（未登录时自动登录）

**判据永远来自脚本，不要靠肉眼或 cookie 猜测**：先跑

```bash
python3 /Users/rabbit/.agents/skills/asset-inventory/scripts/ensure_login.py
```

它只输出一个 JSON 对象并以退出码区分状态：`0`=登录态有效（直接继续原流程，**不要再打开登录页**）；`3`=需要登录（`reason` 为 `no-cookie` / `expired` / `wrong-account`）；`1`=脚本自身出错。**只有退出码为 3 时才执行下面的登录动作**，并且优先照抄 JSON 里的 `action` 文案（它已经写明该复用什么标签）。

多店换号时加 `--expect-account <账号提示>`：即使当前有有效登录态，只要不是这家店的账号也会返回 `reason: wrong-account`，避免把上一家店的会话误当成这家店。

0. **先确认有浏览器可接管**：调 `browser` 工具 `action: "browsers"`。
   - 返回已有会话（如 `b5 Chrome 156`）→ 直接用它，**不要碰其他浏览器**。
   - 返回为空（Chrome 没启动）→ 先尝试 `open -a "Google Chrome"` 启动用户本机那个 Chrome（同一个 profile，不是新装/新 profile 的实例），然后每 2 秒调一次 `browsers`，最多等 20 秒等 Magpie 扩展连上。
   - 20 秒后仍为空（扩展没装/没启用）→ 只提示用户检查 Magpie Browser 扩展，不要自己另起浏览器或另建 profile。
1. **复用当前浏览器，绝不新开浏览器**：只用 magpie-browser MCP 的 `browser` 工具。先 `tabs` 找有没有 `tgcwork.tmall.com` 标签，**有就 `tab_select` 复用它**；没有再 `tab_new`（带 url）一个标签页 —— `tab_new` 就是在“当前这个已打开的浏览器里新开窗口”，符合要求。不要用 chrome-devtools，不要 `open -a`（除非第 0 步的“浏览器没开”场景，且只开用户原本那个 Chrome），不要启动第二个浏览器实例。
2. **判断登录框状态**：`snapshot`（interactive）读登录页，普通登录 tab 的 iframe 里有「请输入账号名/手机号/邮箱」「请输入登录密码」「登录」三个元素。
   - 两个输入框都已有值（用户或密码管理器预填）→ 直接进入第 4 步，**不要改动内容**。
   - 输入框为空 → 进入第 3 步。
3. **凭据来源只有两个，都不落盘**：用户在本轮对话里已给出账号/密码就用它；否则用 `ask_user_question` 问一次（账号 + 密码），说明只用于本次填表。**绝不把密码写进技能文件、状态文件、日志或回复**；**绝不尝试从 macOS 钥匙串、Chrome `Login Data`、`security` 或其他脚本里挖密码**。
4. 用 `type`/`fill` 按 ref 填表，再用 `click` 点「登录」按钮（真实点击，不要用 `evaluate` 合成点击）。
5. **登录要验证，不能假定成功**：刚点完登录时 cookie 可能还没落库，用

   ```bash
   python3 /Users/rabbit/.agents/skills/asset-inventory/scripts/ensure_login.py --wait 30
   ```

   轮询到 `loggedIn: true`（或退出码 0）才算成功。仍为 3 时：`snapshot` 看页面是否有短信/滑块/二次验证，有就停下来让用户在浏览器里自己过（不绕过验证码）；无提示则检查账号密码是否输错，最多重试 2 次。
6. 登录成功后才回到原流程重跑原命令。多店场景下每换一家店走一遍本节，并把 `ensure_login.py` 输出的 `accountHint` 与该店配置的 `accountHint` 比对，确认确实换了账号。

若用户明确要求“不要自动登录”，则退回旧行为：只提示用户在浏览器里登录一次后重试。除此之外，只有遇到短信/滑块等验证码时才需要人工介入。

## 不可破坏的工作簿约束

- **样式和结构绝对保护：** 除用户明确要求外，写入只能修改当前目标 Sheet 的数据单元格值，以及既有公司汇总/既有辅助 Sheet 中必要的值或公式；不得修改任何既有单元格的填充色、字体、字号、边框、对齐、数字格式、保护、条件格式、数据验证、合并单元格、行高、列宽、隐藏状态、筛选、冻结窗格、打印设置、视图或工作簿主题。
- **回填数据边框：** 对下载明细写入目标 Sheet 的第2行及以下，每个回填行的单元格复制该列既有表头的边框；这是用户明确要求的唯一新增展示属性。不得借此覆盖字体、填充、对齐、数字格式或其他既有样式；写入后必须验证回填行的边框已存在且与对应表头一致。
- 禁止删除或插入整行、整列，禁止使用 `delete_rows`、`append`、重建 Sheet、删除 Sheet 或新建 Sheet 清除旧数据；必须仅清空当前目标数据区域中的**值**，再从第2行按单元格写入数据。
- 写入前后必须比较所有既有 Sheet 的样式、行列尺寸、冻结窗格、筛选、合并范围、可见性及视图；样式保护验证失败时不得替换输出文件。
- 完成后不得自动启动或打开 Excel、Numbers、LibreOffice 或任何工作簿 GUI；默认只保存、校验并报告输出路径。即使使用 LibreOffice 进行用户明确要求的公式缓存重算，也必须采用无界面模式且不得自动打开文件。
- 不得创建、删除、重命名、移动或重建 Sheet。模板已含 `费用归集辅助` 与 `未归属费用`，脚本仅清空并更新这两个 Sheet 的值；若目标工作簿没有它们则跳过归集，不改变工作簿结构。

- `公司汇总!A1:AD1` 是固定30列表头：禁止改名、重排、插列、删列或覆盖表头。
- `公司汇总` 的 `店铺` 列（A列）由 `--shop-name` 统一填入用户发送的店铺名称；不改表头、不改变模板55行结构与样式。
- 公司汇总在全部商品行（含模板预留的空白公式行）之后**固定追加一行「总计」**（`店铺` 列写「总计」，`商品ID` 留空），见下方「公司汇总总计行」；除该行外不加其他合计行。
- `所有商品效果数据导出` 仅由专属流程 `all-item-effects-export` 导入；其余16个流程不得清空、替换或写入该 Sheet。
- 商品贷款下载文件写入模板的 `商品货款` Sheet。
- 每次只替换当前流程目标 Sheet 的表头以下数据；同一日期输出已存在时，以该文件为模板继续累积其他流程。
- 仅删除完全相同的明细行。不能仅按 `资金流水号` 去重，因为同一资金流水号可合法对应多个商品或子订单。
- 公司汇总商品清单＝有账单数据的商品 ∪ `所有商品效果数据导出` 中“预算加码消耗金额/智能优惠券金额”非零的商品；效果导出中无金额的商品不进入汇总。
- 商品ID、订单号、流水号等标识按文本保存，避免科学计数法或丢位。
- 评价有礼通过交易子单号/主单号关联商品；赔付追缴仅在处罚业务ID能唯一匹配订单时归集。
- 客服服务费、商业化账户充值提现及无法唯一匹配商品的费用仅在用户原有 `未归属费用` Sheet 已存在时写入；不得平均分摊、臆造归属或为此创建新 Sheet。
- 账单费用通常为负值；公司汇总成本列按既有盘算口径取反为正数。交易补贴保持原始净方向。
- 下载后必须验证目标 Sheet 存在、表头与模板一致、实际非空明细行数等于写入统计、输出可重新打开且无 `#REF!`；任一项失败都不得宣称写入成功。

## 固定流程顺序与配置

| 顺序 | 流程 | `--module` | 目标Sheet | 固定 `billType` | 日期字段 |
|---:|---|---|---|---|---|
| 1 | 消费者体验提升服务 | `consumer-experience-service` | 消费者体验提升服务 | `FREIGHT_INSURANCE_BILL` | 结算+支付 |
| 2 | 商品贷款 | `product-loan` | 商品货款 | `NYPX_MODE_PAYMENT_BILL,NYPX_GOODS_FUND_PAYMENT_BILL` | 结算+支付 |
| 3 | 交易补贴 | `trade-subsidy` | 交易补贴 | `GOLD_STANDARD_INVOICE_BILL` | 结算+支付 |
| 4 | 交易佣金 | `trade-commission` | 交易佣金 | `NYPX_MODE_SETTLE_BILL` | 结算+支付 |
| 5 | 先用后付 | `credit-buy` | 先用后付 | `CREDIT_BUY_BILL` | 结算+支付 |
| 6 | 新疆物流集运 | `xinjiang-logistics-consolidation` | 新疆物流集运 | `COLLECTIVE_LOGISTICS_BILL` | 结算+支付 |
| 7 | 评价有礼 | `review-gift` | 评价有礼 | `REVIEW_GIFT_BILL` | 仅结算（区间取支付日期） |
| 8 | 营销费用 | `channel-promotion` | 营销费用 | `CHANNEL_PROMOTION_BILL` | 结算+支付 |
| 9 | 商业化账户充值提现 | `ad-agency` | 商业化账户充值提现 | `AD_AGENCY_BILL` | 仅结算（区间取支付日期） |
| 10 | 投流推广佣金 | `all-site-channel-promotion` | 投流推广佣金 | `ALL_SITE_CHANNEL_PROMOTION_BILL` | 结算+支付 |
| 11 | 淘客推广佣金 | `taoke-channel-promotion` | 淘客推广佣金 | `TAOKE_CHANNEL_PROMOTION_BILL` | 结算+支付 |
| 12 | 直播推广佣金 | `live-channel-promotion` | 直播推广佣金 | `LIVE_CHANNEL_PROMOTION_BILL` | 结算+支付 |
| 13 | 客服服务费 | `customer-service-fee` | 客服服务费 | `CUSTOMER_SERVICE_FEE_BILL` | 仅结算（区间取支付日期） |
| 14 | 赔付追缴 | `compensate-recovery` | 赔付追缴 | `COMPENSATE_RECOVERY_BILL` | 仅结算（区间取支付日期） |
| 15 | 处罚数据 | `punishment-data` | 处罚数据 | `rules-center` 双 tab（`punish` 违规记录 + `compensation` 赔付记录） | 仅支付（处罚时间） |
| 16 | 出海增值服务费 | `overseas-added-commission` | 出海增值服务费 | `OVERSEAS_ADDED_COMMISSION_BILL` | 结算+支付 |
| 17 | 所有商品效果数据导出 | `all-item-effects-export` | 所有商品效果数据导出 | `COMMISSION_RATE`（bizType）+`SUPPLIER_ALL_ITEM_DAILY_DETAIL`（bizScene） | 仅支付 |

前15个流程（1–14、16）下载类型固定为 `DETAIL_BILL`。11个“结算+支付”流程发送 `writeOffTime` 和 `orderPayTime`，两段区间都按用户给的结算日期、支付日期原样传入。

4个“仅结算”流程（评价有礼、商业化账户充值提现、客服服务费、赔付追缴）在 `MODULES` 里带 `"settle_from": "orderPayTime"`：它们只发送 `writeOffTime`（必须省略 `orderPayTime`，否则接口可能报参数错误），但**这个 `writeOffTime` 区间取用户给的支付日期**，不用用户给的结算日期。用户只给一套支付日期时，这4个流程不需要额外的结算日期。


第15个流程 `punishment-data`（处罚数据）不使用 `DETAIL_BILL`，也不查询 `queryBillDetail`：它直接调用 rules-center 两个接口 —— `tabType=punish`（违规记录）与 `tabType=compensation`（赔付记录），日期用**处罚时间**（对应 `orderPayTime` 的起止日期，转成北京时间毫秒时间戳 `startDateTime`/`endDateTime`，结束日取 `23:59:59.999`）。单个 tab 按 `pageSize=50` 翻页，直到已取行数不小于响应 `paginator.items`（接口用 `items` 而非 `total`；也可参考 `paginator.pages`），未取满即报错停止；两个 tab 的数据**合并**后才是完整的处罚数据。

分页策略固定为 **每页 50 条**，`punish` 与 `compensation` 分别从第 1 页取满后合并。接口虽然允许最大 100 条，但本次实测较大分页更容易出现系统异常或连接断开，因此保持 50；不要自动尝试 100，也不要在翻页途中切换分页大小，以免漏取或重复。最多 60 页及取不满报错、不写残缺文件的规则保持不变。

- 写入 `处罚数据` Sheet 的11列映射：`店铺` = `--shop-name`；`类型` = `punish`→`处罚数据`、`compensation`→`赔付追缴`（店铺右侧）；`处罚内容` = `punishValue`（缺失回退 `remark`/`reason`）；`主订单号` = `orderNo`；`处罚时间` = `punishTime`（缺失回退 `gmtFinish`/`gmtCreate`）按北京时间格式化为 `YYYY-MM-DD HH:MM:SS`；`违规类型` = `violationType`；`违规事件` = `violationEvent`；`商品id` 三级回退 `extend.itemId` → `extend.extendInfo.itemId` → `punishDimensionValue`；`商品标题` 留空（接口不返回标题，不臆造）；`运营` 留空；`处罚金额` = `punishAmount ÷ 100`（接口单位为分，写入按元）。
- 0 元记录（如「单一商品下架/滥发信息」只有 `punishDimensionValue` 携带商品id）同样写入，保证处罚明细完整；`罚款` 在公司汇总中按商品ID用 SUMIFS 逐商品归集，总计行对这一列求和（不再取 `处罚数据` 整表合计）。

第17个流程不使用 `DETAIL_BILL`：改用 hosting 下载任务 `bizType=COMMISSION_RATE`、`bizScene=SUPPLIER_ALL_ITEM_DAILY_DETAIL`，日期取支付时间并转为 `YYYYMMDD`（如 `20260601`）。创建任务后等待5秒，再查询下载列表，检查第一条记录的 `taskStatus` 是否为 `FINISHED`；未完成则每5秒重查，最多轮询10次，成功后用第一条记录的 `fileUrl` 下载。

## 公司汇总总计行

`rebuild_summary()` 在写完所有商品行之后**紧贴最后一条商品行**追加一行总计行（`SUMMARY_TOTAL_LABEL = "总计"`），位置为 `max(2, 商品数+1) + 1`；模板预留的空白公式行则排在总计行**之后**，最多保留到第55行。该行沿用既有单元格样式（仅当目标单元格没有样式时才从第2行复制，避免改动模板既有样式指纹），`商品ID` 必须留空，否则会被当成商品行。

| 取值方式 | 列 |
| --- | --- |
| 对上方商品明细行求和（`=SUM(X2:X<最后一个商品行>)`） | 数量、收入、交易补贴、交易佣金、先用后付、消费者体验提升服务、新疆物流集运、出海增值服务费、新品营销(营销费用)、充值加码消耗、智能优惠券金额、投流推广佣金、淘客推广佣金、直播推广佣金、刷单金额、**罚款** |
| 直接取来源 Sheet 整表金额合计（`=SUM(源!$X$2:$X$N)`） | 客服服务费、赔付追缴、评价有礼、商业化账户充值提现（`账单金额`，来源为负，取反 `-SUM`） |
| 保持空白 | 运营姓名、产品名称、直通车营销、收入金额、收款人、货款、利润、备注（查询不到的） |

金额列不硬编码列号：用 `target_amount_header(sheet, headers)` 按表头动态定位（其余 → `账单金额`）。常量见 `SUMMARY_TOTAL_SUM_HEADERS`（16 项，含罚款）与 `SUMMARY_TOTAL_SHEET_HEADERS`（4 项）。`罚款` 的逐商品公式与其余成本列一样，走 `formula_by_header["罚款"]` = `SUMIFS('处罚数据'!$K$2:$K$N,'处罚数据'!$H$2:$H$N,$C<行>)`（处罚金额在 K 列、商品id 在 H 列，因店铺右侧插入了类型列）。

## 输入、安全与失败处理

- 登录态优先级：显式 `--fetch-file` > 本机浏览器 cookie 库。fetch 若使用，必须来自 `https://tgc.tmall.com/ds/api/v1/fbi/bill/queryBillDetail` 且方法为 POST。
- 日期格式必须为 `YYYY-MM-DD`，开始不晚于结束；命令行日期优先于 fetch body。

- 日期只认用户给的值，不做任何推算：用户在请求里给了结算日期和支付日期，就分别用它们；只给了支付日期（单一账期），就把它同时用于“结算+支付”流程的 `orderPayTime` 和4个“仅结算”流程的 `writeOffTime`。
- 默认全流程需要结算和支付两类日期；单跑4个“仅结算”流程（评价有礼、商业化账户充值提现、客服服务费、赔付追缴）时只需要支付日期 `--order-pay-start/--order-pay-end`；单跑 `punishment-data` 时只要求支付日期。
- 处罚数据只使用支付日期（按处罚时间取数）；
- 所有商品效果数据导出只使用支付日期；调用 hosting 接口时自动转为 `YYYYMMDD` 紧凑格式，且不查询 `queryBillDetail`，直接创建下载任务。
- Cookie、XSRF Token、授权头和签名 URL 只在内存中使用，不写入技能文件、工作簿、状态文件、普通日志或回复。
- HTTP 401 或明确未登录/鉴权失败时立即停止全部剩余流程，不再创建后续任务。
- 任一流程查询、创建、轮询、下载或盘算验证失败时立即停止；此前已成功写入的流程和原始下载文件必须保留。

## 中断与续跑

- 每个流程成功后更新无凭据状态文件：`<店铺>_<结算开始>_<结算结束>.asset-inventory-state.json`（与盘算文件同名前缀）。
- 同一次运行失败后，用新 fetch 加 `--resume`，自动跳过状态文件中的已完成流程，从失败/未执行流程继续。
- 对升级前已完成但没有状态文件的流程，用可重复的 `--skip-module` 手工跳过。例如商品货款已成功：`--skip-module product-loan`。
- 续跑必须使用同一结算/支付日期和同一盘算输出；日期不一致时拒绝混合账期。
- `--module <单流程>` 仅用于用户明确要求单流程；`--module all` 与省略 `--module` 等价。

## 执行命令

解释器要求：Python 3.11+（已移除 PEP 701 多行 f-string，3.11 与 3.12 均已实测可解析运行）且已安装 `cryptography`、`openpyxl`。若当前 `python3` 缺依赖，可先 `uv venv /tmp/aivenv --python 3.12 && uv pip install --python /tmp/aivenv/bin/python cryptography openpyxl`，再把下文的 `python3` 换成 `/tmp/aivenv/bin/python`。

默认执行全部17个流程（自动复用本机浏览器登录态，只需店铺名和日期）：

```bash
python3 /Users/rabbit/.agents/skills/asset-inventory/scripts/tmall_bill_download.py \
  --shop-name "埔鸣--迎风部" \
  --write-off-start 2026-07-01 --write-off-end 2026-07-31 \
  --order-pay-start 2026-07-01 --order-pay-end 2026-07-31 \
  --output-dir /path/to/outputs
```

明确单跑一个流程。4个“仅结算”流程只给支付日期（它们的结算区间就取这里给的值）：

```bash
python3 /Users/rabbit/.agents/skills/asset-inventory/scripts/tmall_bill_download.py \
  --module customer-service-fee --shop-name "埔鸣" \
  --order-pay-start 2026-07-01 --order-pay-end 2026-07-31 \
  --output-dir /path/to/outputs
```

“结算+支付”流程单跑时仍按用户给的结算、支付两段日期传：

```bash
python3 /Users/rabbit/.agents/skills/asset-inventory/scripts/tmall_bill_download.py \
  --module consumer-experience-service --shop-name "埔鸣" \
  --write-off-start 2026-07-01 --write-off-end 2026-07-31 \
  --order-pay-start 2026-07-01 --order-pay-end 2026-07-31 \
  --output-dir /path/to/outputs
```

想先确认登录态是否可用（唯一可信判据，退出码 0=有效 / 3=需登录 / 1=脚本出错）：

```bash
python3 /Users/rabbit/.agents/skills/asset-inventory/scripts/ensure_login.py
```

只想要原始诊断时用 `scripts/tmall_session.py --json`（输出为纯 JSON，日志已移到 stderr）。

仍支持用户贴 fetch 的老用法，`--fetch-file` 与命令行日期可混用：

```bash
.../tmall_bill_download.py --fetch-file /path/to/latest-fetch.txt --output-dir /path/to/outputs
```

中断后续跑，或跳过升级前已完成的商品货款：

```bash
# 有状态文件
.../tmall_bill_download.py --write-off-start 2026-07-01 --write-off-end 2026-07-31 \
  --order-pay-start 2026-07-01 --order-pay-end 2026-07-31 --output-dir /path/to/outputs --resume

# 无状态文件但已确认商品货款完成
.../tmall_bill_download.py --write-off-start 2026-07-01 --write-off-end 2026-07-31 \
  --order-pay-start 2026-07-01 --order-pay-end 2026-07-31 --output-dir /path/to/outputs \
  --skip-module product-loan
```

默认模板为 `assets/盘算模板.xlsx`，默认盘算输出为 `<output-dir>/<店铺>_<结算开始>_<结算结束>.xlsx`（店铺名取自用户发送的名称，文件名中的 `/ : * ? " < > |` 等非法字符自动替换为 `-`）。同名文件存在时继续累积。仅下载不写盘算必须由用户明确要求并使用 `--no-pan-suan`。

使用 `--dry-run` 查看脱敏的17流程请求计划；它不调用接口、不创建任务，也不生成盘算文件。已有本地账单时可直接调用 `scripts/pan_suan_builder.py`，并优先传 `--expected-target <目标Sheet>`。

## 两阶段执行与汇总文件夹

数据量大时推荐两阶段执行：先只下载全部17个流程，全部成功后一次性导入模板，避免中途失败留下半成品盘算。

```bash
# 第一阶段：只下载并安全解压（不写盘算）
.../tmall_bill_download.py --write-off-start 2026-07-01 --write-off-end 2026-07-31 \
  --order-pay-start 2026-07-01 --order-pay-end 2026-07-31 \
  --output-dir /path/to/outputs --no-pan-suan

# 第二阶段：全部下载成功后一次性导入模板
.../pan_suan_builder.py --template /Users/rabbit/.agents/skills/asset-inventory/assets/盘算模板.xlsx \
  --output /path/to/outputs/<店铺>_<结算开始>_<结算结束>.xlsx \
  --shop-name "<店铺>" \
  --strict --json --no-recalculate --input /path/to/outputs
```

- `--input` 传整个输出目录时自动识别每个下载文件对应的目标 Sheet；`所有商品效果数据导出` 的 hosting 导出 xlsx 缺少工作表 dimension，构建脚本会自动回退全量读取。
- 0 条数据的流程没有下载文件，其目标 Sheet 保持模板原样。
- 完成后把每个有数据流程的明细 xlsx（以目标 Sheet 名称命名）和最终盘算文件复制到 `<output-dir>/<店铺>_<结算开始>_<结算结束>_输出/`，与原始下载目录分开交付。
- 合并导入已优化：明细读取用只读流式、回填边框直接写样式ID（避免 openpyxl 逐格样式查重）、保存后校验用只读流式计数并扫描 `#REF!`，不再逐格二次扫描，百万级明细行可在秒级完成。

## 多店铺串行执行

用户一次给出多个店铺（每个店铺是独立的天猫账号）时，用 `scripts/multi_shop_runner.py` 串行执行：**一个店铺完整跑完（下载 → 导入模板 → 交付 → 状态落盘）才进入下一个店铺**，不复用上一家店的登录态，也不并发。

### 执行顺序与输入

- 店铺顺序＝用户列出的顺序，不重排、不合并、不去重。
- 店铺名取用户原文，各自用于该店的 `公司汇总!A` 列与盘算文件名前缀。
- runner 本身只接收店铺名、账号和日期，**它自己不接收也不保存密码**：登录要么复用浏览器现有会话，要么由 agent 按「登录态自愈」在浏览器里填表完成（密码只在本轮内存/表单里，不写入任何文件、状态或日志）。
- 两种给店方式（可混用，`--shop` 追加在清单之后）：
  - 命令行：`--shop "店名,账号,支付开始,支付结束[,结算开始,结算结束[,账号提示]]"`，可重复；第5/6段省略时结算区间回落到支付区间。
  - JSON 清单（`--shops-file`）：`{"shops":[{"shop":"店名","account":"账号","orderPayStart":"YYYY-MM-DD","orderPayEnd":"YYYY-MM-DD","writeOffStart":"YYYY-MM-DD","writeOffEnd":"YYYY-MM-DD","accountHint":"可选"}]}`，也接受裸数组。

### 逐店登录闸门（关键）

每进入一个店铺前后，runner 先用 `scripts/ensure_login.py` 的逻辑读本机浏览器登录态并取账号提示（cookie 里的 `tracknick`/`lgc`/`cookie17`/`unb`，空值跳过），然后：

1. 未登录（`reason` 为 `no-cookie`/`expired`）→ runner 输出 `{"status":"需要登录","shop":...,"account":...,"action":...}` 并以退出码 3 结束。此时**不要直接把球踢回给用户**：按「登录态自愈」用 magpie-browser 在当前已打开的浏览器里登录该店账号（复用已有 `tgcwork.tmall.com` 标签，没有才 `tab_new`），然后用 `ensure_login.py --wait 30` 确认 `loggedIn: true`，再重跑同一条 runner 命令从该店继续。
2. 该店配置了 `accountHint` 但当前提示不一致 → 输出 `{"status":"登录态与店铺不匹配",...}` 并停下（说明浏览器里还是别的账号），走「登录态自愈」换成这家店的账号。
3. 与上一个已跑店铺的提示相同 → 输出 `{"status":"登录态没有切换",...}` 并停下（说明用户没换账号），同上处理。

自动登录完成后重跑同一条命令即可从该店继续（已完成的店铺不会重跑，见 `--skip-completed`）。只有登录页出现短信/滑块等需要人过的验证码，或用户明确说“不要自动登录”时，才停下让用户自己在浏览器里点。

### 每店产物与批次状态

- 输出根目录下每店一个子目录 `<output-root>/<店铺>/`，内含各流程下载目录、`logs/1-download.log`、`logs/2-pan-suan.log`。
- 盘算文件：`<output-root>/<店铺>/<店铺>_<结算开始>_<结算结束>.xlsx`（命名规则与单店一致）。
- 交付文件夹：`<output-root>/<店铺>/<店铺>_<结算开始>_<结算结束>_输出/`，放 15 个按目标 Sheet 命名的明细 xlsx 加盘算文件副本。
- 批次状态：`<output-root>/multi-shop-state.json`，记录每店日期、路径、三个阶段（`download`/`panSuan`/`deliver`）结果与 `ok`；**只记店铺名、账号、路径、日期和结果摘要，不写任何 cookie/token/密码**。

### 命令

```bash
python3 /Users/rabbit/.agents/skills/asset-inventory/scripts/multi_shop_runner.py \
  --shop "埔鸣--迎风部,13800000001,2026-09-01,2026-09-30,2026-09-01,2026-10-31" \
  --shop "均沐桉,13800000002,2026-09-01,2026-09-30,2026-09-01,2026-10-31" \
  --output-root /path/to/outputs

# 或从 JSON 清单读店铺（按数组顺序执行）
python3 /Users/rabbit/.agents/skills/asset-inventory/scripts/multi_shop_runner.py \
  --shops-file /path/to/shops.json --output-root /path/to/outputs

# 先确认当前浏览器登录的是哪个账号（与 ensure_login.py 同源判定）
python3 /Users/rabbit/.agents/skills/asset-inventory/scripts/multi_shop_runner.py --check-login
```

- `--check-login`：只输出当前登录态 `{"loggedIn":true,"currentHint":"...","cookieCount":N}` 并以退出码 3/0 结束，用来判断“现在该不该换账号”。需要下一步动作建议时用 `scripts/ensure_login.py`（多返回 `reason`/`action`/`hasXsrf`，并可 `--wait` 等 cookie 落库）。
- `--skip-completed`：跳过批次状态里已 `ok` 的店铺，用于登录闸门中断后的续跑。
- `--only 店名`（可重复）：只跑指定店铺，仍按清单顺序。
- `--continue-on-error`：某店失败后继续下一店；**默认不继续**——某店失败立即停止后续店铺，已完成店铺的产物和批次状态全部保留。
- `--reuse-downloads`：复用该店目录里已有的下载结果，只重跑导入与交付（调试或修模板时用）。
- `--module <单流程>`：仅在用户明确限定单流程时传，语义与单店脚本一致。

每个店铺内部仍是“先全部下载（`--no-pan-suan`）、再一次性导入模板、再复制交付文件夹”的两阶段流程；runner 只是把这三步按店铺串起来并加上登录闸门。**单店场景继续用 `tmall_bill_download.py`，不要为了一个店铺去用 runner。**

## 固定下载链路

前15个流程（1–14、16）按顺序调用：

```text
POST /bill/queryBillDetail
POST /download/download
POST /download/pageQueryDownloadTask
POST /download/getDownloadUrl
```

所有商品效果数据导出（唯一例外）按 hosting 链路调用：

```text
GET /ds/api/v1/hosting/task/createDownloadTask?bizType=COMMISSION_RATE&bizScene=SUPPLIER_ALL_ITEM_DAILY_DETAIL&startDate=YYYYMMDD&endDate=YYYYMMDD
等待5秒
POST /ds/api/v1/hosting/task/pageListDownloadTask   body: {"pageNo":1,"pageSize":10}
检查第一条记录 taskStatus == FINISHED；未完成每5秒重查，最多10次
成功后使用第一条记录的 fileUrl 下载
```

第15个流程（处罚数据）走独立链路：

```text
POST /ds/api/v1/rules-center/pageList
body: {"pageNo":N,"pageSize":50,"startDateTime":<毫秒>,"endDateTime":<毫秒>,"tabType":"punish"|"compensation"}
按 paginator.items 翻页取满，两个 tab 合并
直接生成以目标 Sheet 名称命名的明细 xlsx 并写入盘算，无下载任务、无轮询、无解压
```

前15个流程轮询默认每10秒一次，最长900秒；所有商品效果数据导出例外（创建后等待5秒，之后每5秒一次、最多10次）。只在实时状态明确成功后下载。ZIP、TAR、GZ 解压时检查路径穿越，拒绝 ZIP 符号链接及 TAR 链接、设备和 FIFO；损坏压缩包保留原始文件。

## 输出与汇报

完成时汇报本次成功流程、每流程查询条数、taskId、原始文件、解压数量、目标 Sheet、写入行数和验证结果，以及盘算文件、公司汇总商品数和未归属费用行数。明确说明：

- 公司汇总固定30列表头已校验且未变，`店铺` 列已按用户发送的名称填写；
- 公司汇总已在最后一条商品行下方紧贴追加「总计」行（商品ID 留空）；说明罚款按商品ID逐商品匹配、总计行对该列求和，客服服务费、赔付追缴、评价有礼、商业化账户充值提现取的是整表合计；
- 盘算文件名与交付文件夹名均为 `店铺_日期` 形式；
- 所有商品效果数据导出仅由专属流程写入并已校验，其余流程未触碰；
- 无商品归属的费用已单列，没有擅自分摊；
- 如中断，列出已完成流程、失败流程和续跑方式。
- 多店铺时按顺序逐店汇报：每店一行「✓/✗ 店铺名｜商品数 N｜盘算文件路径」，并给出批次状态文件路径；说明后续店铺是被哪一个登录闸门挡住的（需要登录 / 登录态与店铺不匹配 / 登录态没有切换）。

## 参考

- [天猫账单模块与盘算映射](references/tmall-bill-module.md)
- 登录态闸门（唯一入口，含 `reason`/`action`/`--wait`）：`scripts/ensure_login.py`
- 多店铺串行执行：`scripts/multi_shop_runner.py`
- 浏览器 cookie 解密与会话校验：`scripts/tmall_session.py`
