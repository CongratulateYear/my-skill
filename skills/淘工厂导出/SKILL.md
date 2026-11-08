---
name: taogongchang-export
description: 从淘工厂导出17类账单/数据文件（消费者体验提升服务、商品贷款、交易补贴、交易佣金、先用后付、新疆物流集运、评价有礼、营销费用、商业化账户充值提现、投流推广佣金、淘客推广佣金、直播推广佣金、客服服务费、赔付追缴、处罚数据、出海增值服务费、所有商品效果数据导出）。公司名称、支付日期、结算日期全部由用户在消息里发送（不从登录态解析）；登录态默认通过「淘工厂登录」skill 自动获取（本机 cookie 库读不到时自动回退浏览器桥，含 HttpOnly cookie），也支持用户直接贴 queryBillDetail fetch（cookie 只在内存/临时文件中使用，绝不落盘、不回显）。默认一次执行全部17个流程，用户指定单一流程时（如「淘工厂导出罚款」）只执行该流程。所有导出文件统一放入一个文件夹，文件夹名为「公司名称_结算开始_结算结束」。当用户说「淘工厂导出」「导出淘工厂账单」「淘工厂导出罚款/处罚数据/商品贷款」等导出淘工厂文件的需求时使用；只导出文件、不写盘算工作簿（盘算请用「淘工厂合并」或「淘工厂资产盘算」）。
---

# 淘工厂导出

## 用途

把资产盘算的 17 个账单下载流程抽出来做成**纯导出**：查询 → 创建明细任务 → 轮询 → 下载 → 安全解压，**不写盘算工作簿**（需要盘算汇总时用「淘工厂合并」或「淘工厂资产盘算」skill）。所有导出文件统一放进一个文件夹：`<输出根>/<公司名称>_<结算开始>_<结算结束>/`。

## 输入（全部来自用户消息，缺了就先问，不要自行编造、不从登录态解析）

1. **公司名称**（`--company-name`）：取用户消息原文（如「埔鸣」或「义乌市埔鸣日用品有限公司」），用于导出文件夹命名；导出前还须将此原文传给登录闸门校验目标公司。
2. **支付日期**（`--order-pay-start/--order-pay-end`）与**结算日期**（`--write-off-start/--write-off-end`），格式 `YYYY-MM-DD`，开始不晚于结束；两类日期各自原样透传，不做推算。默认全部流程两类日期都要。
3. **流程范围**：默认全部 17 个流程；用户明确指定（如「淘工厂导出罚款」）时只传对应 `--module`。单跑处罚数据/所有商品效果数据导出/4个仅结算流程（评价有礼、商业化账户充值提现、客服服务费、赔付追缴）时只需要支付日期。
4. **登录态**：默认复用本机浏览器登录态（自动含 HttpOnly cookie）；用户主动贴了 `queryBillDetail` fetch 时存成临时文件传 `--fetch-file`（用后即删）。用户随消息发来的账号/密码只用于需要登录时的浏览器填表，不落盘。

## 登录态：一律通过「淘工厂登录」skill

- 判据只用脚本：`python "<skills根>/淘工厂登录/scripts/ensure_login.py" --expect-company "<用户公司名称>"`（退出码 0=有效 / 3=需登录 / 1=脚本错）。
- 需要登录时按「淘工厂登录」skill 的流程做：**当前已打开的浏览器**（绝不新开浏览器/新 profile）里**新开一个 Magpie 标签页**走 https://tgcwork.tmall.com/login 登录，不复用任何已有标签页；凭据用用户本轮消息里给的账号密码；滑块/短信让用户自己过；登录后 `ensure_login.py --wait 30 --expect-company "<用户公司名称>"` 确认再重跑导出。

- **身份以公司为准**：门面固定将 `--company-name` 原文传入下载器 `--expect-company`；下载器在实际账单使用的同一 headers/session 上执行 `fetch_company` + `company_matches`（cookie、bridge、用户 fetch 均如此），未知/失败 `company-unknown`、不匹配 `wrong-company` 一律停止，不查询或创建账单任务。此前独立登录闸门通过不能替代此执行期校验。多个手机号可登录同一公司；公司匹配时不因 `erpMobile` 与用户登录手机号不同而阻断。不自动传 `--expect-account`；只有用户明确要求严格账号核对才在登录闸门加 `--expect-account <指定账号> --strict-account`。不能用手机号代替公司验证。
- 独立调用内置下载器时优先 `--expect-company`、否则以 `--shop-name` 作为目标；两者均未提供的旧调用保留兼容，但**不提供目标公司保证**，不应用于本 skill 的公司导出。`--dry-run` 在任何 session/cookie/bridge/公司请求之前返回，无网络，不验证在线身份。门面日期必须由用户消息转成 CLI 参数（即便提供 fetch，也不再由 fetch 补缺日期），且在创建/隔离目录前本地校验。

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

## 路径与参数（Windows / macOS）

- `<本技能目录>` 从实际加载的 skill 路径取得，`<skills根>` 为其父目录；不得硬编码用户名、盘符或安装位置。Python 使用本机可用解释器（macOS 通常 `python3`，Windows 通常 `python`）。
- `--output-root Path`：默认 `Path.home()/Downloads/淘工厂导出`，自动展开 `~` 并解析为绝对路径。
- `--batch-folder Path`：可选批次**总目录**，优先于 `--output-root`；内部仍创建 `<公司名称>_<开始>_<结束>` 公司目录，不把批次路径当作公司目录。
- 多公司由上层严格按用户顺序逐家调用，每次复用同一个 `--batch-folder`；本脚本每次只处理一个公司。总编排可继续调用 `build_folder(output_root, company, args)`；公开 `folder_name(company, start, end)` 返回公司日期目录名。
- `--browser` 原样透传：`auto/chrome/chrome-beta/chrome-canary/edge/brave/chromium`。
- `--transport` 原样透传：`auto/cookie/bridge`；`--bridge-url` 原样透传浏览器桥地址。三者省略时由底层使用默认值（`auto`、`auto`、自动桥地址）。这些枚举已核对内置下载器参数，非自行猜测。
- `--resume` 保留：只续跑同公司日期输出路径对应的内部运行目录、同 `--module` 的未完成运行；使用相同日期和模块参数。成功运行已发布最终文件，不再需要 `--resume`。
- `--state-dir Path` 可显式指定内部运行目录，必须在公司导出目录之外；续跑保持原路径。默认由输出目录的规范绝对路径计算稳定摘要，存入 Windows `%LOCALAPPDATA%/DeepSeekHarness/taogongchang-runtime/<摘要>`，其他平台 `~/.local/state/DeepSeekHarness/taogongchang-runtime/<摘要>`；不含认证信息。

## 执行命令

```bash
# 默认全部17个流程（公司名称与日期都取用户消息）
python "<本技能目录>/scripts/taogongchang_export.py" \
  --company-name 埔鸣 \
  --write-off-start 2026-07-01 --write-off-end 2026-07-31 \
  --order-pay-start 2026-07-01 --order-pay-end 2026-07-31 \
  --output-root /path/to/outputs

# 指定单一流程（例如用户说「淘工厂导出罚款」，罚款只要支付日期）
python "<本技能目录>/scripts/taogongchang_export.py" \
  --module 罚款 --company-name 埔鸣 \
  --order-pay-start 2026-07-01 --order-pay-end 2026-07-31 \
  --output-root /path/to/outputs

# 用户贴了 fetch：存临时文件传入（用后即删，绝不回显内容）
python "<本技能目录>/scripts/taogongchang_export.py" \
  --company-name 埔鸣 --fetch-file /tmp/fetch.txt \
  --write-off-start 2026-07-01 --write-off-end 2026-07-31 \
  --order-pay-start 2026-07-01 --order-pay-end 2026-07-31 \
  --output-root /path/to/outputs

# 中断续跑
... --resume
```

脚本内部调用**本 skill 自带**的 `scripts/tmall_bill_download.py --no-pan-suan` 完成实际下载（源自 asset-inventory，已内置，无需再装）；17 个流程的固定顺序、billType、日期规则、轮询与安全解压全部沿用原定义。下载器的 `--transport auto` 与「淘工厂登录」一致：cookie 库读不到时自动回退浏览器桥。

## 输出

- 仅导出时目录规则不变，不生成主表。后续运行「淘工厂合并」或全流程盘算时，主表默认与此公司导出文件夹并列：`<输出根>/<公司名称>_<结算开始>_<结算结束>.xlsx`；有 `--batch-folder` 时两者均直接放在批次总目录，不增加公司包装目录。原始导出 xlsx 留在公司导出文件夹内，状态与下载工作目录存入独立内部运行目录，不移动既有结果。
- 唯一导出文件夹：`<输出根>/<公司名称>_<结算开始>_<结算结束>/`（公司名取用户消息原文，非法字符替换为 `-`；只给支付日期时后缀用支付区间）。
- 成功结果以流程名放在公司目录根部（如 `商品贷款.xlsx`、`处罚数据.xlsx`），多个分片用 `_2`、`_3` 后缀。每次下载在内部运行目录的 `.work-<module>` 隔离，成功后只清理本次工作目录，绝不删除其他模块、未知文件或其他失败运行。发布暂存目录短暂放在公司目录的父目录（同文件系统保证原子替换），正常成功和捕获异常后清理；不在公司交付目录内生成辅助文件。
- 单模块整理先识别文件所属模块，不会把其他模块强行改名。重跑仅用本次分片替换该模块整个旧分片集合（不会把旧 `_2` 重复计入）。下载器状态明确记录 `completedModules`、持久 `manualSkippedModules`，每个导出结果明确 `status=completed/zero` 与本地 `publishFiles` 清单（不含签名 URL）；门面以持久 skip 与本次 skip 的并集为排除集合发布。`zero` 仅由流程显式 `noData=true` 转换，不由缺文件推断；缺失分片/未知状态拒绝发布并保留旧结果。零数据清除该模块旧结果，skipped 保留旧结果。旧 v1 状态缺少清单的完成流程不可直接发布，请不带 `--resume` 重新导出（旧工作会隔离保留），不得人工猜零。
- 发布前完成所有复制，普通发布异常恢复旧结果并保留所有新源文件。隔离工作内的源不在 finalize 中逐个删除：发布完成后先同文件系统原子改名工作目录为 `.published-<module>-<唯一值>`，再清理。改名前中断仍有完整源可续跑；改名后清理失败只留下不可续跑的已发布目录，最终分片不受影响。警告提示后可人工清理该目录，不得改回 `.work-*`。保证范围是捕获的复制/发布异常与发布后的源清理中断；**不是多文件目录级原子切换或断电持久事务**，进程在逐个替换最终文件时被强杀仍需保留现场人工检查，勿并发写同公司目录。
- 失败时旧最终结果不变，工作目录及状态保留供 `--resume`。非续跑重新执行时，将旧未完成工作目录保留为 `.previous-<module>-<唯一值>`；续跑时也隔离未完成模块的旧下载，防止混入重试结果。所有这些保留物均在内部运行目录中，明确确认无需恢复后再人工清理。发现旧版公司目录内的 `.work-*`、`.previous-*`、`.published-*` 或公司日志时阻断，不自动忽略状态并创建新任务；先核实旧任务后迁移或使用新批次。不要对公司总目录递归清理。
- 完成后汇报：公司名称、执行的流程清单、每流程查询条数/taskId、最终 xlsx 清单、导出文件夹绝对路径；失败时列出已完成与失败流程及 `--resume` 续跑方式。

## 远端任务持久化与安全恢复

- FBI账单及 hosting「所有商品效果数据导出」在发起创建请求**之前**将 `remoteTasks[模块].phase=creating` 写入原状态文件（临时文件写入、flush/fsync 后原子替换）；收到唯一合法 `taskId` 后，立即写为 `phase=created`，再开始轮询。仅保存日期、查询条数、任务编号，不保存认证信息或下载 URL。
- 原参数 `--resume` 校验公司原文、模块范围及日期；已记录 `created` 的未完成模块直接恢复该 taskId 的 poll/download，不再查询账单或创建任务。超时、远端 FAILED、下载/解压失败均保留编号；不会因失败自动重建。hosting 仅接受列表中该编号的记录，绝不取“最新第一条”代替；若任务不在当前前10条返回中则保守超时，不扩大查询或猜测。
- `creating` 表示可能尚未发送、已发送无响应、响应缺编号/非法编号，或编号检查点落盘失败，均**阻断自动恢复与重建**。需要人工在远端核实任务、保留现场；当前不提供绕过开关。不要删状态/改 phase/去掉 `--resume` 来规避阻断，新的非续跑本来就代表新的导出请求，不享有旧任务去重保证。
- 旧状态的已完成模块与 `results.status/publishFiles`、手工 skip 及编排发布凭证保持兼容，不升级/猜测历史结果。旧未完成远端任务若缺少可靠跟踪条目，保守停止，不能承诺去重；仅查询明细不创建远端任务的处罚数据不受此限制。
- taskId 统一要求为**字符串**，1–128位，首位字母或数字，其余仅字母、数字、下划线、连字符；响应、续跑、轮询匹配及下载路径共用校验，拒绝整数、空白、路径片段、冲突编号及从响应文本猜编号。服务端若变更格式需人工核对再调整，不能静默放宽。
- 边界：这是单进程同状态文件的保守恢复协议，不是服务器幂等键、并发锁或断电事务。创建前意图写入到编号检查点之间的进程中断可能留下已创建但未知的远端任务，不能自动消除该不确定窗口；状态文件丢失/被改写、并发运行和显式新任务也不保证全局 exactly-once。不要并发写同一公司/运行目录。
- 离线回归：在本 skill 的 `scripts/` 执行 `python -B -m unittest -v test_task_recovery test_taogongchang_export`。新增恢复测试仅用合成 fixture 和 mock，禁用网络打开；覆盖写前意图、编号立即落盘、响应丢失/缺ID/非法ID、落盘失败、同任务恢复、FAILED/超时保留、hosting精确匹配、旧状态阻断及 taskId 边界。不访问cookie/凭据或历史业务目录。

## 安全红线

- Cookie、XSRF Token、授权头、签名 URL 只在内存/临时文件中使用，用后即删；绝不写入技能文件、日志、状态文件或回复，绝不回显 fetch 内容。
- 不落盘密码；不绕过验证码（滑块/短信让用户手动过）。
- 浏览器动作只在当前已打开的浏览器、新开的 Magpie 标签页里做，绝不新开浏览器、不复用已有标签页。
- HTTP 401/未登录时立即停止剩余流程，已下载文件保留。
- 日期只认用户给的值，不做推算。

## 依赖

- Python 3.11+，`cryptography`、`openpyxl`（缺依赖时用 uv 建 venv：`uv venv /tmp/tgexport-venv --python 3.12 && uv pip install --python /tmp/tgexport-venv/bin/python cryptography openpyxl`）。
- 依赖同级 skill：「淘工厂登录」（登录态闸门与公司名校验）。下载器 `tmall_bill_download.py`（及其依赖 `tmall_session.py`、`_bootstrap.py`、`browser_bridge.py`、`bridge_agent.js`）已随本 skill 内置在 `scripts/`，不依赖 asset-inventory。
- 共享 vendor 目录 `<skills根>/.vendor`（装淘工厂登录 skill 时已就位）。

身份字段说明：`erpMobile` 仅为 ERP 绑定信息，不代表当前实际登录手机号；公司名称才是默认身份判据。

## 发布后临时清理与离线回归

- Windows 上 `.work-all` 改名成 `.published-all-<32位唯一值>` 会增长路径；部分账单的 ZIP/解压文件随之超过传统 MAX_PATH（260），普通 `shutil.rmtree` 会报 `FileNotFoundError / WinError 3`，即使文件真实存在。内部清理现对预检、遍历和删除统一使用 Windows extended-length path（本地盘与 UNC），不修改系统长路径开关、ACL 或只读属性。
- 自动清理仅处理**本次**成功发布并退休的内部目录，校验直属运行目录、模块/UUID名称，并在删除任何内容前预检整棵树；拒绝 symlink/junction/reparse point、journal、receipt、备份及未知文件。允许清理下载 ZIP、XLSX 和下载器的 `*.asset-inventory-state.json`；运行根的 journal/receipt、`.previous-*`、公司最终文件不属于清理对象。不扫描或自动清理历史 `.published-*`。
- 清理失败不改成功发布状态、不重试网络导出；警告保留目录绝对路径、异常类型/消息、errno、winerror、filename、filename2，不输出认证状态内容。先核实警告再决定人工处理；不要把 `.published-*` 改回 `.work-*`。目录预检不是抵抗并发恶意替换的事务，不要并发写内部目录。
- 门面及下载器的进度默认 `flush=True`，门面启动下载器同时加 `-u`；这解决重定向/GUI缓冲导致的无输出，不是网络请求期间的心跳或超时保证。
- 离线测试：用已安装 `openpyxl` 的 Python 执行 `python -B -m unittest -v test_taogongchang_export`（工作目录为本 skill 的 `scripts/`）。所有临时 fixture 在本 skill 内创建后清理，子进程下载被 mock，不触发网络导出。覆盖 Windows 实际超260路径、发布后真实 XLSX 可读、保护 journal/receipt/备份/业务文件、拒绝链接与未知文件、异常诊断和进度 flush。

## 编排发布凭证

总编排使用 `--run-id` 与 `--result-file` 在工作目录退休前写出本次公司/日期/模块状态/发布文件SHA256摘要；`--result-file` 必须放在公司导出目录之外，默认由编排存入内部运行目录。不含Cookie、headers或下载签名URL。缺失/过期凭证不允许因旧表存在触发ERP。独立导出原用法不变。公司级续跑仅适用新版编排创建的新批次，历史业务目录不自动迁移。
