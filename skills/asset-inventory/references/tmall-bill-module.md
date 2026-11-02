# 天猫账单模块、日期与续跑规则

## 登录态与日期来源

登录态默认由 `scripts/tmall_session.py` 从本机浏览器 cookie 库自动读取，取出含 HttpOnly 的完整 cookie，并用 `https://scm.tmall.com/loginStatus` 返回 `data: true` 校验后才发请求。用户不需要复制 fetch。

若用户主动提供一条有效 `queryBillDetail` fetch，则复用其中登录态（优先级更高），结算时间和支付时间可由命令行 `--write-off-*` / `--order-pay-*` 覆盖。默认执行范围是下表全部17个流程，而不是 fetch body 原有 `billType`。每个流程均覆盖为自己的固定 `billType`；所有商品效果数据导出改用 hosting 任务的 `bizType`/`bizScene`。只有用户明确限定单流程时才缩小范围。

cookie 解密原理（macOS Chrome）：钥匙串 `Chrome Safe Storage` → `PBKDF2-HMAC-SHA1(salt=b"saltysalt", 1003, 16)` → `AES-128-CBC`（IV 为 16 个空格）→ 去 `v10` 前缀与 PKCS7 填充 → 当 cookie 库 `meta.version >= 24` 时再剥掉前 32 字节 SHA256(host)。`v20` 为 App-Bound Encryption，无法解密，遇到会明确报错。

## 固定接口顺序

前15个流程（1–14、16）依次调用：

```text
POST https://tgc.tmall.com/ds/api/v1/fbi/bill/queryBillDetail
POST https://tgc.tmall.com/ds/api/v1/fbi/download/download
POST https://tgc.tmall.com/ds/api/v1/fbi/download/pageQueryDownloadTask
POST https://tgc.tmall.com/ds/api/v1/fbi/download/getDownloadUrl
```

下载类型固定为 `DETAIL_BILL`。

所有商品效果数据导出（唯一例外）不使用上述 FBI 链路，改用 hosting 下载任务：

```text
GET https://tgc.tmall.com/ds/api/v1/hosting/task/createDownloadTask?bizType=COMMISSION_RATE&bizScene=SUPPLIER_ALL_ITEM_DAILY_DETAIL&startDate=YYYYMMDD&endDate=YYYYMMDD
等待5秒
POST https://tgc.tmall.com/ds/api/v1/hosting/task/pageListDownloadTask   body: {"pageNo":1,"pageSize":10}
```

该流程只使用支付日期，并以 `YYYYMMDD` 紧凑格式传给 hosting 接口。创建后等待5秒，随后每5秒轮询一次，最多10次，检查第一条记录 `taskStatus == FINISHED`；成功后用第一条记录的 `fileUrl` 下载。

处罚数据（`punishment-data`，第15个流程）同样不使用 FBI 链路，改为直接调用 rules-center 明细接口：

```text
POST https://tgc.tmall.com/ds/api/v1/rules-center/pageList
body: {"pageNo":N,"pageSize":50,"startDateTime":<毫秒>,"endDateTime":<毫秒>,"tabType":"punish"}
body: {"pageNo":N,"pageSize":50,"startDateTime":<毫秒>,"endDateTime":<毫秒>,"tabType":"compensation"}
```

- 日期按**处罚时间**（即支付时间窗口），北京时间起止转毫秒时间戳，结束日取当天 `23:59:59.999`。
- 单 tab 固定 `pageSize=50` 翻页，直到已取行数不小于响应 `paginator.items`（该接口用 `items`，不是 `total`；`paginator.pages` 可作参考），未取满即报错停止，最多 60 页。
- `punish`（违规记录）与 `compensation`（赔付记录）两个 tab 的数据**合并**才是完整的处罚数据。
- 无需创建下载任务、无需轮询、无需解压：脚本直接把合并结果生成以目标 Sheet 命名的明细 xlsx，再走盘算构建。

## 固定执行顺序

| 顺序 | 流程 | module | billType | 日期 | 盘算目标Sheet |
|---:|---|---|---|---|---|
| 1 | 消费者体验提升服务 | `consumer-experience-service` | `FREIGHT_INSURANCE_BILL` | 结算+支付 | 消费者体验提升服务 |
| 2 | 商品贷款 | `product-loan` | `NYPX_MODE_PAYMENT_BILL,NYPX_GOODS_FUND_PAYMENT_BILL` | 结算+支付 | 商品货款 |
| 3 | 交易补贴 | `trade-subsidy` | `GOLD_STANDARD_INVOICE_BILL` | 结算+支付 | 交易补贴 |
| 4 | 交易佣金 | `trade-commission` | `NYPX_MODE_SETTLE_BILL` | 结算+支付 | 交易佣金 |
| 5 | 先用后付 | `credit-buy` | `CREDIT_BUY_BILL` | 结算+支付 | 先用后付 |
| 6 | 新疆物流集运 | `xinjiang-logistics-consolidation` | `COLLECTIVE_LOGISTICS_BILL` | 结算+支付 | 新疆物流集运 |
| 7 | 评价有礼 | `review-gift` | `REVIEW_GIFT_BILL` | 仅结算（区间取支付日期） | 评价有礼 |
| 8 | 营销费用 | `channel-promotion` | `CHANNEL_PROMOTION_BILL` | 结算+支付 | 营销费用 |
| 9 | 商业化账户充值提现 | `ad-agency` | `AD_AGENCY_BILL` | 仅结算（区间取支付日期） | 商业化账户充值提现 |
| 10 | 投流推广佣金 | `all-site-channel-promotion` | `ALL_SITE_CHANNEL_PROMOTION_BILL` | 结算+支付 | 投流推广佣金 |
| 11 | 淘客推广佣金 | `taoke-channel-promotion` | `TAOKE_CHANNEL_PROMOTION_BILL` | 结算+支付 | 淘客推广佣金 |
| 12 | 直播推广佣金 | `live-channel-promotion` | `LIVE_CHANNEL_PROMOTION_BILL` | 结算+支付 | 直播推广佣金 |
| 13 | 客服服务费 | `customer-service-fee` | `CUSTOMER_SERVICE_FEE_BILL` | 仅结算（区间取支付日期） | 客服服务费 |
| 14 | 赔付追缴 | `compensate-recovery` | `COMPENSATE_RECOVERY_BILL` | 仅结算（区间取支付日期） | 赔付追缴 |
| 15 | 处罚数据 | `punishment-data` | `punish` + `compensation` 双 tab（rules-center） | 仅支付（处罚时间） | 处罚数据 |
| 16 | 出海增值服务费 | `overseas-added-commission` | `OVERSEAS_ADDED_COMMISSION_BILL` | 结算+支付 | 出海增值服务费 |
| 17 | 所有商品效果数据导出 | `all-item-effects-export` | `COMMISSION_RATE` + `SUPPLIER_ALL_ITEM_DAILY_DETAIL`（hosting 任务） | 仅支付 | 所有商品效果数据导出 |

## 日期载荷

| 概念 | 请求字段 |
|---|---|
| 结算时间 | `param.writeOffTime.start/end` |
| 支付时间 | `param.orderPayTime.start/end` |

- 11个“结算+支付”流程发送两类日期，取值就是用户给的结算日期与支付日期，不做推算。
- `review-gift`、`ad-agency`、`customer-service-fee`、`compensate-recovery` 只发送 `writeOffTime`，必须省略 `orderPayTime`（否则接口可能报参数错误）；这4个模块在 `MODULES` 里带 `"settle_from": "orderPayTime"`，`module_date_filters()` 的 `date_source_key()` 会把 `writeOffTime` 的取值来源改到 `--order-pay-start/--order-pay-end`，所以它们的结算区间取用户给的**支付日期**，不需要用户额外提供结算日期。
- `queryBillDetail.param` 和创建任务的 `param` 使用相同模块日期载荷；创建任务的 `param` 另加固定 `billType`。
- `all-item-effects-export` 只发送 `orderPayTime`，且不调用 `queryBillDetail`；hosting 接口要求 `YYYYMMDD`。
- `punishment-data` 只使用支付日期（口径为处罚时间），传给 rules-center 的却是毫秒时间戳 `startDateTime`/`endDateTime`，不再使用 `param.orderPayTime`。

分页和轮询：

```json
{
  "queryPageParam": {"itemsPerPage": 10, "page": 1},
  "pollPageParam": {"page": 1, "size": 10},
  "pollIntervalSeconds": 10,
  "maxWaitSeconds": 900
}
```

所有商品效果数据导出的轮询固定为：创建后 `sleep(5)`，之后 `pollIntervalSeconds=5`、最多10次（`maxPolls=10`），判定第一条记录 `taskStatus`。

## 全流程、中止与续跑

- 省略 `--module` 或使用 `--module all`：按固定顺序执行全部17个流程。
- 显式 `--module <name>`：只执行该流程。
- 任一流程失败即停止后续流程；HTTP 401 或鉴权失败尤其必须立即停止。
- 每个流程成功后写入不含登录凭据和签名URL的 JSON 状态文件。
- `--resume` 读取状态文件，校验账期一致后跳过已完成流程。
- `--skip-module <name>` 可重复，用于没有历史状态文件但能确认某流程已完成的情况。
- 默认不调用 LibreOffice 或任何工作簿 GUI；完成后只保存和校验输出文件，不自动打开表格。用户明确要求公式缓存时才可无界面重算，且不得以重算改变样式或结构。

## 盘算构建规则

- 使用 `scripts/pan_suan_builder.py`，默认模板为 `assets/盘算模板.xlsx`。
- 下载器传入固定 `--expected-target`，不依赖下载文件名识别目标 Sheet。
- `公司汇总!A1:AD1` 必须与固定30列表头完全一致，否则拒绝写入。
- `所有商品效果数据导出` 仅由专属流程 `all-item-effects-export` 作为下载文件导入；其他账单流程不得写入或清空它。该 Sheet 的 `B` 列是 `商品id`，`H` 列 `预算加码消耗金额`、`K` 列 `智能优惠券金额` 分别供公司汇总的“充值加码消耗”和“智能优惠券金额”公式引用，导入后自动联动汇总。
- 目标 Sheet 仅清除第2行以下单元格的**值**；不得删除/插入行列、不得使用 append 写入、不得修改表头颜色、边框、字体、对齐、数字格式、列宽、行高、筛选、冻结窗格、合并单元格或任何 Sheet 视图。下载明细回填到第2行及以下后，回填行单元格仅复制对应表头的边框，使数据格可见；其余样式不复制、不重设。
- 写入前后必须验证所有既有 Sheet 的样式、行列尺寸、视图、合并区域、筛选和隐藏状态完全一致；验证失败不得覆盖输出文件。
- 不得创建、删除、重建、重命名或移动任何 Sheet。仅当工作簿原已包含 `费用归集辅助` 与 `未归属费用` 时，才可更新其值；否则跳过该归集步骤。
- 同一日期输出文件存在时继续累积。
- 仅对完全相同整行去重；长ID和订单号按文本写入。
- 公司汇总商品清单＝有账单数据的商品 ∪ `所有商品效果数据导出` 中“预算加码消耗金额/智能优惠券金额”非零的商品；效果导出中无金额的商品不进入汇总。
- 评价有礼通过交易子/主单号与商品货款关联；赔付追缴通过处罚业务ID尝试唯一关联。
- 处罚数据（`处罚数据` Sheet，10 列）字段映射：`店铺`=`--shop-name`；`处罚内容`=`punishValue`（回退 `remark`/`reason`）；`主订单号`=`orderNo`；`处罚时间`=`punishTime`（回退 `gmtFinish`/`gmtCreate`）按北京时间 `YYYY-MM-DD HH:MM:SS`；`违规类型`=`violationType`；`违规事件`=`violationEvent`；`商品id` 三级回退 `extend.itemId` → `extend.extendInfo.itemId` → `punishDimensionValue`；`商品标题` 留空（接口不返回标题）；`运营` 留空；`处罚金额`=`punishAmount ÷ 100`（接口单位为分，写入按元）。
- 0 元处罚记录（如「单一商品下架/滥发信息」，只有 `punishDimensionValue` 带商品id）也写入，保证明细完整。`公司汇总.罚款` 按商品ID用 `SUMIFS('处罚数据'!$J:$J,'处罚数据'!$G:$G,$C<行>)` 逐商品归集，总计行再对这一列求和。
- 公司汇总总计行：`rebuild_summary()` 在**最后一条商品行之后紧贴**追加一行（`店铺` 列写「总计」，`商品ID` 留空），位置 `max(2, 商品数+1)+1`；模板预留的空白公式行排到总计行之后（上限第55行）。样式沿用既有单元格，仅当目标单元格没有样式时才从第2行复制。16 个列（数量、收入、交易补贴、交易佣金、先用后付、消费者体验提升服务、新疆物流集运、出海增值服务费、新品营销(营销费用)、充值加码消耗、智能优惠券金额、投流推广佣金、淘客推广佣金、直播推广佣金、刷单金额、罚款）用 `=SUM(X2:X<最后商品行>)` 对上方明细求和；4 个列（客服服务费、赔付追缴、评价有礼、商业化账户充值提现）直接取来源 Sheet 整表金额合计，费用表为负值故用 `-SUM`；金额列按 `target_amount_header()` 动态定位列号，不硬编码。其余列保持空白。
- 模板含 `费用归集辅助`（类别/商品ID/金额/关联单号/来源/原始行号）与 `未归属费用`（类别/金额/关联单号/原因/来源/原始行号）：能唯一匹配商品的评价有礼、赔付追缴写入辅助表并汇总到公司汇总；匹配不了的以及客服服务费、商业化账户充值提现写入 `未归属费用`，不擅自分摊。若目标工作簿没有这两个 Sheet 则跳过归集。
- 保存前后校验固定表头、目标 Sheet 表头、非空明细行数，并扫描 `#REF!`。

## 隐私和失败

- Cookie、XSRF Token、授权头与签名URL不得写入脚本、状态文件、工作簿或普通日志。
- 创建任务无实时 taskId、轮询失败/取消/超时、成功后无下载地址：停止。
- 下载成功但盘算构建失败：保留原始和解压文件，保留之前已写入流程，并通过状态文件续跑。
