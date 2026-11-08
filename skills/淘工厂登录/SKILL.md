---
name: taogongchang-login
description: 淘工厂登录与 token 获取。默认只读本机 cookie 库，读不到自动回退本 skill 自带浏览器桥。恢复固定路线：在当前已打开浏览器中新建自己的 Magpie 标签页，不复用用户或其他 agent 标签；已登录直接桥接，确认未登录才填表；read 随 skill 的 bridge_agent.js 源码并 evaluate 直接执行；以服务端登录态及目标公司验证通过为成功标准。no-session 不等于未登录。凭据不落盘、不回显，验证码人工完成，不新开浏览器或 profile。支持 macOS/Windows、公司及账号校验，供其他 skill 在内存中复用会话。
---

# 淘工厂登录（登录恢复与 token 获取）

## 用途与环境

用于登录淘工厂/天猫、检查登录态、获取 XSRF，或供其他 skill 在调用接口前取得会话。
默认 `--transport auto` 先读本机 cookie 库（不要求浏览器在线），失败自动回退本 skill 自带的浏览器桥。macOS 支持 Chrome/Edge/Brave/Chromium（钥匙串 AES-CBC）；Windows 支持 Edge/Chrome（DPAPI AES-GCM）。v20 应用绑定加密或锁库时走桥，不关闭浏览器、不换 profile、不修改浏览器安全设置。

## 1. 先运行闸门，判据来自服务端

```bash
python3 <skill目录>/scripts/ensure_login.py [--expect-company <用户给定公司>] [--expect-account <目标账号>]
```

- 退出码 `0`：登录态有效；有目标公司时必须带 `--expect-company` 并验证通过。直接继续原流程，不再打开登录页。
- 退出码 `3`：需要恢复或核对。`no-cookie` / `no-session` 表示没有取得可用会话，**no-session 不等于未登录**；按下面固定恢复路线处理，不给“两条路任选”。
- `expired`：服务端未确认有效，先检查自己新建的页面，不盲目填表。
- `wrong-account` / `wrong-company`：服务端/账号核对不符，按第 4 节换号。
- `company-unknown`：服务端公司查询失败，保留 `--expect-company` 重跑一次；仍失败停止原流程并请用户核对，不能把页面显示或人工核对当作服务端验证通过。
- 退出码 `1`：脚本错误，按脱敏诊断排查，不假定未登录。

用户给公司名就必须始终传 `--expect-company`，允许简称与全称互相包含。手机号（11 位数字）只与 accountInfo 的脱敏 `erpMobile` 比对，不与 scm cookie 提示混比。非手机号与 cookie `accountHint` 比对；无法比较时 `accountCheck: unverified` 不阻断，但不能代替公司验证。

## 2. 固定恢复路线：新建自己的页 → 判断 → 必要时登录 → 源码桥接 → 服务端验证

### 2.1 当前浏览器中新建自己的 Magpie 标签页

1. 仅使用 magpie-browser MCP 的 `browser`。先 `browsers`，多浏览器时用 `use_browser` 选定用户当前浏览器，之后不切换。
2. 无连接时提示用户打开原浏览器并启用 Magpie 扩展，等待用户完成；**不得自行新开浏览器实例或 profile**，不使用 chrome-devtools 或 browser-skill 的另开窗口路线。
3. **每次恢复开始一律 `tab_new` 新建自己的 Magpie 标签页**，URL 为 `https://tgc.tmall.com/ds/page/supplier/home`。记录返回 tab，后续操作显式传该 tab；同一次恢复后续判断、登录、换号、注入与重试继续用这个自己新建的页，不是每步再开一个。
4. 不复用、不导航、不注入用户已有标签页、其他 agent 的 `~` 标签页、旧登录页或旧淘工厂页；不能先找一个已登录旧标签来注入。

### 2.2 先判断页面，只有确认未登录才填表

对自己新建的页 `snapshot`：
- 若已登录并处于 `https://tgc.tmall.com`，**直接进入 2.3 桥接，不要填表或重新登录**。
- 若明确跳到登录页/显示登录表单，才在自己页打开 `https://tgcwork.tmall.com/login` 并执行以下填表步骤。
- 若页面加载异常、无明确状态，继续 snapshot/排查，不因 no-session 就认定未登录或索要密码。

填表步骤：
1. 用 snapshot 的 ref 定位 iframe 表单，只检查账号、密码“是否有值”的布尔状态，不读出/回显密码。已有预填保留；缺账号时只补用户本轮给定的账号，然后再次检查密码是否有值，避免覆盖账号输入后触发的浏览器自动填充；仅补仍缺失的字段。
2. 两字段均有值后 `click` 真实登录按钮，不用 evaluate 合成点击。填表用 `type` 按 ref，只用用户本轮提供的凭据，不从密码管理器、系统凭据库、其他脚本挖密码。无可用凭据时 `ask_user_question` 只问一次并记录等待项（不含秘密），结束回复等待；无新输入或新会话证据不重复催问或提交。
3. 滑块、短信、验证码、二次验证都停下来让用户人工完成，绝不绕过。
4. 密码错误提示用户并索要正确密码，最多重试 2 次；账号不存在/停用/冻结/失效则停止，不盲重试。页面无错误且未跳转则再 snapshot 排查。
5. 登录后在**自己的同一标签页**进入 `https://tgc.tmall.com/ds/page/supplier/home`，继续桥接。页面跳转不是最终成功。

### 2.3 本 skill 自带桥：read 源码并 evaluate 直接执行

1. 后台启动本 skill 的桥（使用受管理后台任务并记录 job id；已运行则复用服务）：

   ```bash
   python3 <skill目录>/scripts/browser_bridge.py --port 8899
   ```

2. **直接用 read 工具读取 `<skill目录>/scripts/bridge_agent.js` 的完整源码**，然后将读到的整段源码原样交给 magpie-browser 的 `evaluate`，显式传自己新建的 tab。该页必须是 `https://tgc.tmall.com` 源站。
3. 固定使用上述源码执行，不使用 `<script src=...>` 注入，也不从其他 skill 查找脚本。源码是 IIFE，直接执行即可，不需改写逻辑。导航、刷新或换号后需重新 read 并 evaluate。
4. `bridge agent started` / `already running` 仅说明注入执行，不是登录成功；桥 ready、连接状态、`transport: bridge` 或 `erpMobile` 有值也都不代替服务端验证。桥只监听 127.0.0.1，任务与响应内存流转，浏览器自动附带 HttpOnly cookie。

### 单一有效桥连接边界

- 淘工厂8899与ERP8901各自独立，每种桥只允许一个有效页面代理；恢复前先确认没有业务调用在运行。只关闭自己记录且已不再承载业务的旧桥标签，不关闭用户或其他agent标签。
- 导航、刷新或换号后旧页代理失效，需要重新注入并服务端验证；新代理不能抢占仍活跃的旧代理。遇到代理冲突时先核对自己旧页，必要时关闭自己的旧页并等待旧连接过期，不循环开页或重启业务任务。
- 桥连通仅证明连接，不证明公司身份；淘工厂必须验证目标公司，ERP不得擅自换账号或套用淘工厂同名约束。
- 桥协议代码更新不会热更新已运行的Python桥或已注入脚本。业务空闲时才重启自己管理的桥、刷新自己的旧桥页面以终止旧循环，再重新注入新版脚本；新版服务拒绝无agentId的旧脚本（409），但旧脚本本身可能持续重试，不能只重复注入。旧服务缺少绑定字段时新版客户端会停止提示升级。不杀未知进程，不为升级重跑历史账单。

### 2.4 服务端验证才是成功标准

```bash
python3 <skill目录>/scripts/ensure_login.py --wait 30 [--expect-company <用户给定公司>] [--expect-account <目标账号>]
```

保留本次所有期望参数。有目标公司时必须通过服务端 accountInfo 公司校验且 `loggedIn: true`、退出码 `0` 才继续业务。页面右上角公司名、页面已登录或桥注入成功都不足以宣告成功。

验证码人工完成、换号或页面导航后，同样先重新源码桥接再执行此验证。持续 no-session 排查桥/代理连接；不要反复填表。桥承载后续请求期间保留自己的标签页，不提前关闭；不再需要桥且业务结束时才可关闭自己创建的页，用户标签不动。

## 3. 获取 token 与供其他 skill 引用

```bash
python3 <skill目录>/scripts/tmall_token.py [--expect-company <目标公司>]
python3 <skill目录>/scripts/tmall_token.py --with-headers [--expect-company <目标公司>]
```

退出码同闸门。子进程 stdout JSON 可含 token/完整 headers，必须由调用进程直接捕获到内存并解析，不能回显到聊天或普通日志，不能重定向到文件。退出码 `3` 回到固定恢复路线。

同一 Python 进程也可 import（这是调用接口，不是另一条登录恢复路线）：

```python
import sys
sys.path.insert(0, "<skill目录>/scripts")
import tmall_session
built = tmall_session.build_session()  # auto: cookie -> bridge
headers, session = built["headers"], built["session"]
ok = tmall_session.check_session(headers, session)
info = tmall_session.fetch_company(headers, session)
company_ok = info["ok"] and tmall_session.company_matches("用户给定公司", info["company"])
# ok 和 company_ok 均为 True 才继续目标公司业务；headers/session 仅在内存使用。
```

## 4. 账号或公司不符

`wrong-account` / `wrong-company` 不能忽略。若本次恢复还没创建自己的页，先新建自己的 Magpie 标签页；若已创建则继续用该页（不得操作旧用户页）。退出会影响同 profile 的其他标签会话，按以下边界处理：
- **按顺序自动切换（用户固定偏好）**：用户已给出目标公司顺序及对应凭据时，服务端确认当前公司不是本轮目标公司，自动在自己的Magpie标签真实退出并登录本轮目标公司，**不用再次询问**；开始执行时登录了其他公司也适用。多公司必须严格串行，前一家公司全部完成后才进入下一家，不能跳过失败公司。
- 公司身份未知时先核验，不能把 no-session 当作已确认公司不符而盲目退出；目标不明确、账号不明或要登录用户清单外目标时停止核对。此自动切换授权不扩展为ERP换号、验证码处理或合单范围批准。
满足相应授权后，只在自己页通过真实点击退出并登录目标账号，重新 read/evaluate 随 skill 源码，带原期望参数服务端验证通过才继续业务。
用户明确不要自动登录时停止自动填表，请用户在自己新建的页登录；完成后仍需源码桥接与服务端验证。

## 安全红线

- 密码、cookie、XSRF token、完整请求头不写文件/状态/文档/普通日志/聊天回复，仅在表单和进程内存中使用。只展示 `accountHint`、`company`、`cookieCount`、`hasXsrf` 等脱敏状态。
- 不从浏览器保存密码、系统凭据或其他脚本提取密码；不得把含凭据的历史对话恢复到临时文件，事后删除也不允许。历史必要凭据仅在内存上下文读取；无法安全取得则请用户重新提供。验证码人工完成，不自动绕过。
- 不新开浏览器或 profile，不修改加密安全设置；所有浏览器操作只在自己新建的 Magpie 标签页，不复用用户或其他 agent 标签。
- 公司验证失败不继续业务，不以页面状态、桥连接、手机号字段代替服务端公司验证。

## 依赖与脚本

Python 3.11+；本机 cookie 解密需要 `cryptography`（macOS 钥匙串、Windows ctypes DPAPI 无需 pywin32）。桥是纯标准库，随本 skill 提供，不依赖其他 skill。magpie-browser 仅在浏览器恢复/注入时需要。

| 脚本 | 作用 |
|---|---|
| `scripts/ensure_login.py` | 登录态闸门与恢复提示，支持 wait、公司/账号、transport/bridge-url |
| `scripts/tmall_token.py` | token/完整 headers，调用进程只在内存捕获 |
| `scripts/tmall_session.py` | cookie 解密、会话构建与服务端登录/公司校验；auto 回退桥 |
| `scripts/browser_bridge.py` | 127.0.0.1:8899 桥服务 |
| `scripts/bridge_agent.js` | 用 read 读取完整源码，再于自己 tgc 页 evaluate 直接执行 |
