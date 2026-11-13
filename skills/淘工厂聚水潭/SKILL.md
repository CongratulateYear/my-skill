---
name: taogongchang-jushuitan
description: 聚水潭：跨Windows/macOS复用浏览器登录与Cookie，按主订单号查询并导出ERP订单；支持从淘工厂罚款Excel提取主订单号，仅罚款导出触发时将商品id、商品标题匹配回填，直接回填原处罚数据，商品id和商品标题以ERP为准，ERP原始导出独立保存。
---

# 淘工厂聚水潭

分为三部分：**登录获取 Cookie → 数据导出 → 数据匹配**。用户说“淘工厂聚水潭”“聚水潭订单导出”“罚款数据匹配聚水潭”时使用。

## 安全与运行环境

- Python 3.11+；本地Cookie解密使用 `cryptography`，Excel使用 `openpyxl`。Windows、macOS共用同一份Python源码；不要复制跨平台二进制。优先使用宿主已配置、带依赖的Python；缺依赖时取得安装授权后在独立venv安装。
- 账号密码只用于浏览器表单；Cookie、token、请求头、签名下载URL、原始接口响应只在内存，不写文档、日志、状态、文件或回复。用户贴的fetch仅用于理解接口结构，不把其中Cookie复制进脚本。
- 不得把含凭据的历史对话恢复到临时文件，事后删除也不允许；必要凭据仅在内存上下文读取，无法安全取得则请用户重新提供。
- 业务订单Excel本来就包含订单号，输出属于用户业务数据，不混入技能仓库。未经用户要求不上传GitHub。
- 浏览器登录只使用 magpie-browser，当前已打开的浏览器，新开自己的Magpie标签页；不新开浏览器实例/profile，不动用户/其他agent标签。
- 登录遇到短信、滑块、验证码/风险校验即停，让用户手动处理，绝不绕过。
- 多公司严格串行；有公司约束时验证不通过绝不执行导出。不要把公司与手机号混为一谈。

## 条件预备与恢复协议（agent必须实际执行）

罚款编排仅当**本次罚款导出成功且本次文件存在有效十进制主订单号**才预备ERP；不扫旧账单，不在无罚款/导出失败/空订单时提前获取ERP Cookie。普通用户主动ERP导出不受罚款成功条件限制。

```bash
python scripts/jushuitan.py --shop-name "店铺" --fine-file "本次处罚数据.xlsx" --trigger fine-export --fine-export-succeeded --prepare-only
```

`--fine-export-succeeded`是调用方对本次成功的显式声明，不是脚本从旧文件推断成功。`--prepare-only`仅离线验证输入，不读Cookie、不联网、不建ERP任务、不回填；缺本次成功声明返回`preparation_status=skipped`，有效则`agent_required`及机器可读`recovery_action`，不代表已获取Cookie或已登录。空/无效订单失败且不返回浏览器预备动作。

1. 默认`auto`使用同级淘工厂登录skill的官方Playwright扩展适配器；不启动旧桥、不注入页面代理。扩展为前置条件，仅支持当前已打开的Windows Edge/macOS Chrome，不开新浏览器或profile。
2. 失败时实际agent用Magpie新建自己的ERP标签，先检查登录态。**已登录直接复用，不对比账号、不退出、不重复登录**。显式公司约束验证不通过只停止，不自动换号，也不从淘工厂账号推断ERP账号。
3. 确认未登录才检查真实表单预填布尔值；已有预填保留并用真实按钮提交，只补缺失ERP凭据。短信/滑块/验证码/风险校验交人工。Magpie只做登录和页面动作，Playwright只获取Cookie，Python HTTP执行原订单接口。
4. 登录成功后开启新的执行会话范围，运行`jst_session.py --transport playwright --expect-company ...`（仅在原命令已有公司约束时保留它），验证真实服务端订单查询；URL变化或预填提交本身不算成功。扩展未连接、浏览器未运行等失败是`unverified`，不是未登录证据。
5. 同一执行范围的共享内存broker按ERP/Tmall分开缓存Cookie，只获取一次供子命令复用；范围过期或失效就停止，不偷偷重新连接或读取其他账号。登录/账号变化后须结束旧范围，由调用方明确开启新范围。Cookie、token不落盘。
6. 失败结果含`stage/recovery_action.resumePolicy`；导出已尝试（含创建请求不确定）必须保留原输入、公司、输出目录、内部状态，用原命令加`--resume`，禁止删状态或换目录重新建单。会话阶段失败尚未创建任务，按`requiredResume`决定；若原命令已有`--resume`必须保留。交付已发布而匹配失败同样resume核验后再回填。不得自动重试未知下载域、公司不符或输入损坏；不能靠重新登录绕过。

## 一、登录获取 Cookie

入口：`https://www.erp321.com/login.aspx`；工作台/订单导出入口：`https://www.erp321.com/epaas`。

**必须先检查，已经登录只取Cookie/复用会话，绝不先退出或重复登录。**

```bash
python scripts/jst_session.py
# 若用户明确要求验证目标公司
python scripts/jst_session.py --expect-company 公司名称
```

退出码0表示服务端订单页校验有效，立即复用，不打开登录页。退出码3只表示会话未验证（兼容字段 `loggedIn=false/reason=login-required` 不是已确认退出），先执行结构化 `nextAction`；`action` 保持字符串便于旧调用方展示，`diagnostic` 为安全诊断。**不得看到失败就立即要求用户登录/提供密码**：先在当前浏览器自己的新Magpie标签检查会话与真实登录表单。Cookie读取被锁或v20不代表未登录；默认用官方Playwright扩展，不要求关闭浏览器或降低加密。公司校验目前从订单页显式公司字段提取：查不到就阻断，不以Cookie自报公司作身份凭证；真实页面若字段结构不同，必须补采样适配，不能放宽为无校验。

Python 不能直接控制 Magpie；`nextAction` 是给 agent 的操作计划，不是脚本已经执行的浏览器动作。不得伪造“已自动登录/点击/检查预填”；必须实际调用 magpie-browser，只有服务端订单查询和本次公司约束均通过才算成功。

### 官方Playwright Cookie与HTTP会话

默认`auto`和`playwright`从同级登录skill调用`acquire_cookies(["https://www.erp321.com/"], browser=...)`，官方扩展可取得HttpOnly Cookie，仅在内存传给受限CookieJar。HTTP仅允许`https://www.erp321.com`的443端口，禁止认证会话跨域重定向；外域下载继续使用独立无凭据连接。服务端订单页与只读查询仍是成功依据，不以Cookie名或页面URL认定已登录。

`--transport cookie`仅作为显式兼容选项读取本地Chromium数据库，不属于默认共享缓存路径。旧`--transport bridge`只是`playwright`别名，绝不会启动旧桥；`--bridge-url`非空明确报停。旧桥源码只留作历史测试，禁止调用、启动或注入。无需8899/8901页面桥端口。

需要恢复时：
1. 用Magpie检查当前浏览器自己开的ERP标签，已登录直接复用，不比较账号。确认未登录才进入`/login.aspx`。
2. snapshot真实表单，只检查账号/密码是否有值的布尔状态，绝不读出/返回/打印密码，不用返回密码的evaluate或表单序列化，不截图可见密码，不写凭据到脚本/文件/日志/回复。
3. 已预填账号和密码时保留原值，直接真实按钮提交。仅补用户本次明确给出的缺失**聚水潭**凭据，不使用淘工厂密码、不提取密码库、不清空预填。缺凭据在检查后只询问一次并等待；错误密码不得循环重试。验证码/滑块/短信/风险校验停给人工。
4. Magpie登录后用新的执行范围获取ERP Cookie一次并验证服务端；原公司约束保留，验证失败停止，不退出/换号。用户明确要求换ERP账号属于新的单独操作，不能从公司不符推导授权。

纯程序调用：`jst_session.build_session(transport='auto', browser='auto', bridge_url=None, expect_company=None)` 返回内存`session/headers/transport/identity`，session提供get/post。总编排应在共享`SessionScope()`中运行子命令，复用ERP独立缓存；独立脚本没有范围时只能本进程复用，不能声称跨命令已复用。不得把返回对象落盘。

## 二、数据导出

输入主订单号两种途径：
1. 用户提供的逗号/换行分隔主订单号；
2. 用户指定的罚款Excel，或本轮前一步淘工厂/罚款导出完成后得到的Excel路径。不要默认选取旧账单或其他公司的文件。

主订单号永远字符串，严禁float；超15位Excel数值可能已失真，必须阻断并要求文本原始数据，不能猜回原订单号。

接口顺序（基于本次真实页面与回包，离线回归覆盖）：
1. GET实时订单页隐藏字段 → LoadDataToJSON按so_id查询全部页（含dp分页），收集o_id与查到/未查到订单。
2. 获取导出日志任务快照；ExportCheckConfirm只检查；ExportBySync才创建，参数为筛选、数量字符串、"true"、"false"、"false"、null。成功ReturnValue可为空。
3. 用导出日志新增任务的整数auto_id关联本次任务，绝不将回调RequestId当任务编号。以新增auto_id、有效类型、创建时间窗关联，候选不唯一则停止；数量仅诊断，可能为商品行数，不要求等于内部订单数。filter_data可能为0，不能依赖它。新任务created按上海UTC+8解析，有限±120秒时钟容差；续跑不扩大原创建时间窗。旧状态无历史时间时不伪造时间，并明确标记未校验时间窗，仍要求唯一候选及最终订单集合精确核验。
4. `CheckAndGetExportData`按auto_id取得动态HTTPS地址，回调为`JTable1/{page}`；创建检查和创建导出使用`ACall1/{page}`。不再用旧`GetExportData`盲目重试。接口结构未知时安全分类后停止，不反复消耗远端下载次数。下载后先保存内部待验收业务文件及绑定凭证，再严格核验原始线上订单号集合，未验收文件禁止发布和回填。

默认导出已查到的部分，JSON明确列出missing_ids，complete/ok=false不是全部成功；--require-all-orders要求全部查询到才导出。全部缺失不伪造成功文件。
任务状态只存订单集合/任务编号/本地路径与阶段，不含cookie/headers/签名URL。创建前持久化creating，创建请求结果不确定时不得重新创建；用同一目录--resume继续关联/下载。输出已完成时验证原文件再复用，不重复建任务。状态与本次订单不一致必须停止。同一目录不可并行运行。

下载限制：HTTPS、显式域允许列表；外域无Cookie无认证头，重定向逐跳校验；不自动接受未知CDN。ZIP拒绝目录穿越/符号链接/炸弹，Excel实际内容需校验。原文件保留，不覆盖已有下载。

### 命令行

在当前技能目录运行（Windows可用 `py` 或已配置Python路径）：

```bash
# 普通导出：不匹配/不改罚款表
python scripts/jushuitan.py --shop-name "店铺名称" --order-ids "主订单号1,主订单号2" --output-dir "下载目录"
# 罚款文件只作为订单号来源：仍不自动回填
python scripts/jushuitan.py --shop-name "店铺名称" --fine-file "罚款文件.xlsx"
# 仅罚款导出触发：直接回填原表；ERP放聚水潭订单子目录
python scripts/jushuitan.py --shop-name "店铺名称" --fine-file "罚款文件.xlsx" --trigger fine-export
# 只检查输入，无Cookie/网络/输出目录写入
python scripts/jushuitan.py --shop-name "店铺名称" --order-ids "123456789" --dry-run
```

示例订单号应替换为真实十进制主订单号，不接受占位词。可加 `--expect-company`、`--transport playwright`、`--browser edge`；默认优先已登录会话。罚款存在多候选Sheet时用 `--sheet` 明确。订单下载名为 `店铺名称_聚水潭订单.xlsx` 或 `.zip`；已有文件需--resume核验同一任务后复用，否则停止；不覆盖ERP原始导出。罚款默认ERP目录为原表目录下“聚水潭订单”，禁止与处罚数据放同一层。

## 待验收下载与显式合单授权

### 授权来源与适用范围

本skill不内置任何个人或历史任务的持续授权。历史示例、旧错误和本文件本身都不是当前用户同意。只有当前用户对本任务的明确批准，或当前用户另行明确授予、可在会话中核对范围的持续授权，才允许生成合单授权文件。每次仍须完整核验本任务合单证据及六字段，不能复用旧JSON。该授权不涵盖无证据额外订单、未知格式、扩大处罚范围、重新建单/下载或ERP换号。没有可核实授权就停止询问，不从错误结果推导同意。

- 下载取得的合法Excel/ZIP先原子保存于内部运行目录，绑定任务ID、原始输出参数、请求有序订单集合及SHA-256；范围不符仍保留，不作为交付。相同输入`--resume`优先离线核验，不再次取下载地址或下载。待验收文件缺失、损坏或绑定不一致必须停止，不能自动重下掩盖状态。
- 普通订单必须精确命中请求；逗号/冒号复合字段只接受明确合单标志和合法十进制结构，记录内部订单关联证据。未知格式或无证据额外订单阻断。公共`validate_order_coverage`始终精确集合核验。
- 错误中的`error_details.approval_required`只是**待人工批准的业务证据**，`approved=false`不能执行。先告知用户额外订单、合单证据及仅ERP保留、处罚不新增行/金额的边界；明确获得本任务同意后，才用下述离线工具生成授权。除当前用户明确授予且本次可核实的持续授权严格覆盖的有证据合单情形外，不得自动批准；任何情形均不得扩大已证实集合或复用其他公司/批次的授权JSON；计划内账号切换本身不涵盖额外订单。不要手工用PowerShell拼接授权JSON。
- 授权JSON恰好六字段：`approved`、`task_id`、`sha256`、`output`、`requested_ids`、`extra_ids`。任务/哈希/原始输出/原请求顺序/额外集合均严格一致；额外订单必须有本任务查询合单证据。旧状态缺证据不能凭空批准。
- 原命令保持所有身份与输入不变，增加`--resume --scope-approval-file "内部授权JSON路径"`。总编排对应`--erp-scope-approval-file`，只在同任务续跑使用。不得编辑状态、替换验证函数、monkeypatch下载或删除任务记录以恢复。
- 授权仅允许保留已证明的合单伙伴，处罚回填仍只遍历原处罚行；关联伙伴不得增加处罚行或金额。批准范围单独保存在状态/安全结果，普通验收规则不变。
- 在首次获取下载地址前持久化下载尝试标记；取地址/下载中断且未得到已绑定本地文件时，返回`download_outcome_unknown`并停止，不能再次取址或下载。此策略宁可要求人工检查，也不冒险消耗第二次下载次数。
- 下载次数耗尽或任务创建结果不确定，禁止自动重建；如确需重建必须另行取得用户明确授权，保留旧状态和失败证据。

### 离线生成已批准授权

仅在用户核对并批准**本任务六字段模板**、任务已停止且无并发写入时运行（使用已验证Python绝对路径）：

```bash
python scripts/jst_scope_approval.py --state-file "内部export.state.json" --evidence-file "本任务安全失败结果.json" --approval-file "内部scope_approval.json" --human-approved
```

`--evidence-file`接受现有`scope_approval_required`安全失败结果，或其中原样提取的六字段`approved=false`模板；不得凭空补造。`--human-approved`仅记录已经取得的人工批准，不代表工具自行批准。工具离线重验固定的`state-file + .pending`、v2候选阶段、唯一任务、SHA-256、原始输出、请求顺序、额外集合与合单证据，输出恰好六字段、**UTF-8无BOM**，先经现有`load_scope_approval`校验再原子创建并读回。已有目标或覆盖状态/证据/待验收/业务输出一律拒绝。

工具不联网、不建任务、不下载、不发布ERP、不改状态；本地证据不能证明文件未被有意伪造，缺旧证据时停止而非修复。生成成功后仍按原命令加`--resume --scope-approval-file`（总编排用`--erp-scope-approval-file`）续跑，现有验收再次检查；无BOM修正不放宽任何绑定。后台任务记录job id，优先等通知，确实阻塞或需诊断才读结果，不短间隔重复查询。

## 三、数据匹配（仅罚款导出触发）

**只有本技能因“罚款数据导出后”被触发，明确 `--trigger fine-export` 才执行。** 普通聚水潭订单导出到此结束；即使输入来自Excel也不能隐式回填。

匹配映射：

| 聚水潭订单列 | 罚款/处罚数据目标列 |
|---|---|
| 原始线上订单号 | 主订单编号（或主订单号） |
| 商品名称 | 商品标题 |
| 店铺款式编码 | 商品id（兼容商品ID） |

用本次下载并核验过的ERP订单匹配，**商品id、商品标题均以聚水潭为权威来源**：唯一、无歧义且有效的字段覆盖原值，不再因原值非空而跳过。未匹配、多商品歧义或ERP字段缺失时不猜测、不清空原值；详情以JSON返回。

仅直接回填原处罚数据文件，不生成店铺_聚水潭匹配副本、不加报告Sheet或列。保留文件名、所有原Sheet、表头、行序、金额、公式和格式。只修改指定商品字段。先备份到合并输入目录之外（可--backup-dir），校验临时输出后原子替换；重复匹配幂等。输出两个业务文件：回填后的处罚数据 + 聚水潭原始导出（独立子目录）；状态和外部备份不是额外交付物。不得将ERP原始文件或备份混进合并输入。

命令行示例与模块签名见scripts脚本的 `--help`；只用当前技能目录下脚本，路径有空格必须引号。技能安装不代表线上验证通过。离线测试用合成fixture，不冒充真实API回包。

## 结果状态（旧字段不改变）

保留`ok/complete`原语义与旧调用兼容；不要仅据`ok=false`报告“程序执行失败”。新增`execution_status=completed|execution_failed|not_started`，`matching_status=complete_match|partial_match|not_run|not_requested|execution_failed`，`coverage_status=complete|partial_match`。成功导出部分订单是`execution_status=completed`，缺单或待核查行时`partial_match`；会话/下载/匹配异常才是`execution_failed`（CLI会话退出3、其他退出2，部分覆盖完成仍退出0）。

精简stdout列`missing_order_count`（去重不同缺单号数）、`missing_ids`、`review_row_count`（未匹配/歧义/无效/冲突的罚款行数，重复订单多行仍各计一行）。有唯一明确金额列表头（罚款金额/处罚金额/扣款金额/金额）时列`review_amount`十进制字符串与known/unknown行数；公式/无金额/多候选不猜测，不可用为null，部分可用金额不冒充全部合计。仍保留`matching.counts/reason_counts`。

`--result-file`成功及运行失败均保存完整安全结果（失败可能尚无receipt，已产生的输出和报告不丢弃）；`--verbose`完整stdout。输入阶段非法结果路径不写文件；dry-run不写文件。完整`matching.report/erp_issues/receipt`继续保留，不拿stdout摘要替代完整结果。异常文本可能带认证下载URL，结果只记录白名单`error_code`、`stage`、`error_details`与`export_recovery`，不存原始异常/请求。范围证据可含任务ID、订单号、SHA与本地输出路径，不含下载URL、Cookie、VIEWSTATE或服务器响应正文。

输出交付目录只保留固定命名的 Excel：`店铺名称_聚水潭订单.xlsx`，多个分片用 `_2.xlsx`、`_3.xlsx`，全部分片都参与匹配。CLI 的原始 ZIP、解包工作目录、任务状态、锁文件及默认处罚备份统一放系统内部运行目录（Windows 为 `%LOCALAPPDATA%/DeepSeekHarness/taogongchang-runtime/<路径摘要>/`；其他系统为 `~/.local/state/DeepSeekHarness/taogongchang-runtime/<路径摘要>/`），不在导出根创建 `.jst-fine-backups`。需要续跑仍用原输出目录加 `--resume`；内部状态不可随意删除，否则无法确认旧任务。显式 `--backup-dir` 仅用于用户主动指定外部备份。最终订单集合和所有发布分片哈希均记录在安全凭证中；已有交付 Excel 被修改时阻断，不覆盖。

匹配JSON保留原counts，并新增reason_counts分类：无订单号、ERP缺单、多商品歧义、无效字段分别报告；不因未匹配清空原值。`jushuitan.py` 默认stdout仅保留 `ok/complete`、订单统计、`missing_ids` 和 `matching.counts/reason_counts`，不刷出逐行报告、全量已查订单、凭证或内部路径；`--verbose` 显式恢复完整安全业务结果stdout（仍不是原始网络回包）。`--result-file` 始终保存完整安全结果，含 `matching.report/erp_issues` 与 `receipt`，供总编排续跑及待核查页读取；不能拿精简stdout替代它。只有dry-run沿用原输入校验摘要，不写result-file。已检查现有company_run通过result-file取报告而非解析stdout，无须更改调用方；其他依赖完整stdout的外部调用方应加 `--verbose` 或改读result-file。
