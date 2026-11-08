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

罚款编排仅当**本次罚款导出成功且本次文件存在有效十进制主订单号**才预备ERP；不扫旧账单，不在无罚款/导出失败/空订单时提前启动8901。普通用户主动ERP导出不受罚款成功条件限制。

```bash
python scripts/jushuitan.py --shop-name "店铺" --fine-file "本次处罚数据.xlsx" --trigger fine-export --fine-export-succeeded --prepare-only
```

`--fine-export-succeeded`是调用方对本次成功的显式声明，不是脚本从旧文件推断成功。`--prepare-only`仅离线验证输入，不读cookie、不联网、不建ERP任务、不回填；缺本次成功声明返回`preparation_status=skipped`，有效则`agent_required`及机器可读`recovery_action`，**不是已起桥或已登录**。空/无效订单失败且不返回浏览器预备动作。需要预备时，先运行`jst_session.py`尝试复用现有有效cookie，成功则无需浏览器；失败后执行计划：

1. 实际agent按`bridgeStartArgv`后台起桥（记录job id），不是让Python假装控制Magpie；已有桥只复用，不随意重启其他任务。
2. 实际agent用Magpie新建自己的ERP标签，read `bridgeAgentSource`完整源码并evaluate注入；检查真实表单的预填布尔值，已有预填保留并用真实按钮提交，只补缺失ERP凭据。短信/滑块/验证码/风险校验交人工。
3. 原公司约束不变，运行`jst_session.py --transport bridge --expect-company ...`验证服务端查询；验证通过后才运行原ERP命令。不要把注入、URL变化或预填提交本身当成功。
4. `session_status=bridge_not_running`表示桥未启动，`bridge_agent_missing`表示已起桥未注入；二者`loginState=unverified`，**不表示未登录**。`login_or_permission_required`是服务端登录/权限证据，仍需实际检查表单，不臆测密码缺失。兼容旧`reason=login-required`仅供旧方显示，以新分类为准。
5. 失败结果含`stage/recovery_action.resumePolicy`；导出已尝试（含创建请求不确定）必须保留原输入、公司、输出目录、内部状态，用原命令加`--resume`，禁止删状态或换目录重新建单。会话阶段失败尚未创建任务，按`requiredResume`决定；若原命令已有`--resume`必须保留。交付已发布而匹配失败同样resume核验后再回填。不得自动重试未知下载域、公司不符或输入损坏；先解决相应错误，不能靠重新登录绕过。

## 一、登录获取 Cookie

入口：`https://www.erp321.com/login.aspx`；工作台/订单导出入口：`https://www.erp321.com/epaas`。

**必须先检查，已经登录只取Cookie/复用会话，绝不先退出或重复登录。**

```bash
python scripts/jst_session.py
# 若用户明确要求验证目标公司
python scripts/jst_session.py --expect-company 公司名称
```

退出码0表示服务端订单页校验有效，立即复用，不打开登录页。退出码3只表示会话未验证（兼容字段 `loggedIn=false/reason=login-required` 不是已确认退出），先执行结构化 `nextAction`；`action` 保持字符串便于旧调用方展示，`diagnostic` 为安全诊断。**不得看到失败就立即要求用户登录/提供密码**：先在当前浏览器自己的新Magpie标签检查会话与真实登录表单。Cookie读取被锁或v20不代表未登录，先尝试浏览器桥。公司校验目前从订单页显式公司字段提取：查不到就阻断，不以Cookie自报公司作身份凭证；真实页面若字段结构不同，必须补采样适配，不能放宽为无校验。

Python 不能直接控制 Magpie；`nextAction` 是给 agent 的操作计划，不是脚本已经执行的浏览器动作。不得伪造“已自动登录/点击/检查预填”；必须实际调用 magpie-browser，只有服务端订单查询和本次公司约束均通过才算成功。

### 跨平台读取

本地Chromium数据库只解密适用 `www.erp321.com` 的Cookie；macOS钥匙串Safe Storage + AES-CBC，Windows Local State + DPAPI/AES-GCM；兼容Chrome/Edge/Brave/Chromium及Cookies、Network/Cookies两种布局。只读到内存CookieJar，保留域/path/secure，不输出Cookie。新版v20/库锁则auto回退浏览器桥，不要求关闭浏览器。

### 浏览器桥

专用端口 **8901**，与淘工厂8899互不影响；不要复用淘工厂桥。

1. 后台运行 `python scripts/browser_bridge.py --port 8901`，记住后台job id。
2. 用magpie检查当前浏览器。在自己的新Magpie标签打开 `/epaas`。已登录则不去登录页；需要登录才进入 `/login.aspx`。
3. 登录后在同一个自己开的ERP标签注入技能自带 `scripts/bridge_agent.js` 文件完整内容（read后evaluate），不要打印document.cookie。也可加载桥的agent.js；站点CSP/Private Network阻止加载时直接evaluate源码；浏览器拒绝本地连接需用户批准，不能绕过。
4. 重跑 `python scripts/jst_session.py --transport bridge --wait 30`。服务端有效才算成功；URL或isLogin=true本身不是成功依据。注入桥不会读取HttpOnly Cookie，实际请求由浏览器自动附带。
5. 若仍需登录，先snapshot真实表单，取得账号框、密码框、提交按钮ref。只检查“是否有值”的布尔状态，**不读出/返回/打印密码值**；不要调用返回密码的evaluate、text或表单序列化，不截图含可见密码的表单，不将凭据写入脚本/文件/日志/回复。
   - 账号和密码已有预填（包括浏览器自动填充）时，**保留原值，不覆盖、不索取凭据，直接用真实按钮ref click提交**。预填可自动提交，不因用户当轮未再次给密码而拒绝。
   - 缺账号时只补用户当轮给定的**聚水潭**账号，再次仅检查密码是否有值；账号输入可能触发浏览器自动填充，保留新出现的预填密码，仅补仍缺失字段。绝不使用淘工厂密码，不从浏览器密码库提取凭据，已有预填字段不得用空串覆盖。
   - 无可用凭据只询问一次，在当前会话任务记录中标明等待项与原任务（不记录秘密），结束回复等待用户。无新输入或新会话证据不重复催问、重新提交或重启；自动续轮不算用户授权。密码过期提醒仅在页面明确允许继续且无强制修改/风险校验时确认继续，不擅自更改密码。
   - 短信、滑块、验证码/风险校验必须让用户在该标签人工完成，不能自动获取短信或绕过。错误密码不得循环提交同一预填；仅有用户更正后再试，总计最多重试2次。无Magpie连接/站点拒绝等工具限制才报告具体阻塞，不冒充已检查表单。
   - 提交后在同一个自己的ERP标签注入桥并重跑第4步（保留原 `--expect-company`）；URL变化、cookie标志或表单消失均不替代服务端验证。桥标签在任务完成前不要关闭。

桥绑定一个明确的ERP标签代理；第二个标签不会随机抢走任务，而会被拒绝。切换账号必须先结束前一任务，再停止/重启本次后台桥，向新的自己开的ERP标签重新注入。桥仅限可信本机同用户环境，Origin检查与agent绑定不构成本地恶意进程隔离。不要把8901暴露到网络。

纯程序调用：`jst_session.build_session(transport='auto', browser='auto', bridge_url=None, expect_company=None)` 返回内存 `session/headers/transport/identity`；不把返回对象落盘。session提供get/post；Cookie模式拒绝HTTP及跨域跳转。账号切换必须在前一家公司全部完成后，并让用户确认退出。

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

示例订单号应替换为真实十进制主订单号，不接受占位词。可加 `--expect-company`、`--transport bridge`、`--browser edge`；默认优先已登录会话。罚款存在多候选Sheet时用 `--sheet` 明确。订单下载名为 `店铺名称_聚水潭订单.xlsx` 或 `.zip`；已有文件需--resume核验同一任务后复用，否则停止；不覆盖ERP原始导出。罚款默认ERP目录为原表目录下“聚水潭订单”，禁止与处罚数据放同一层。

## 待验收下载与显式合单授权

### 用户已记录的合单处理偏好（2026-11-07）

用户已明确同意：有本任务真实查询合单证据的额外伙伴，仅保留于ERP原始导出，不新增处罚行或金额；此类问题记录后不再重复询问。永夜任务207249359、原订单5127747193392051313、伙伴5127761341001006121已获本任务明确批准。后续同类情形可依据此固定授权处理，但必须逐任务核验完整合单证据并重新生成绑定本任务六字段的授权文件，不能复用旧JSON。此偏好不是无证据额外订单、未知格式、扩大处罚范围、重新建单/下载或ERP换号的授权；这些异常仍应停止。以下“本任务同意”对该严格边界内情形由本固定授权满足，不再逐次向用户提问，其余验证规则不变。

- 下载取得的合法Excel/ZIP先原子保存于内部运行目录，绑定任务ID、原始输出参数、请求有序订单集合及SHA-256；范围不符仍保留，不作为交付。相同输入`--resume`优先离线核验，不再次取下载地址或下载。待验收文件缺失、损坏或绑定不一致必须停止，不能自动重下掩盖状态。
- 普通订单必须精确命中请求；逗号/冒号复合字段只接受明确合单标志和合法十进制结构，记录内部订单关联证据。未知格式或无证据额外订单阻断。公共`validate_order_coverage`始终精确集合核验。
- 错误中的`error_details.approval_required`只是**待人工批准的业务证据**，`approved=false`不能执行。先告知用户额外订单、合单证据及仅ERP保留、处罚不新增行/金额的边界；明确获得本任务同意后，才用下述离线工具生成授权。除上述用户已记录固定授权严格覆盖的有证据合单情形外，不得自动批准；任何情形均不得扩大已证实集合或复用其他公司/批次的授权JSON；计划内账号切换本身不涵盖额外订单。不要手工用PowerShell拼接授权JSON。
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
