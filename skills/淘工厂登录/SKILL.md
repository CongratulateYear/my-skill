---
name: taogongchang-login
description: 淘工厂（tgcwork/tgc.tmall.com）登录与 token 获取：默认直接复用本机浏览器已登录的天猫会话（只读本机 cookie 库，无需浏览器在线），cookie 库读不到时（v20 应用绑定加密 / Edge 独占锁定 / 沙箱拦临时区）自动回退浏览器桥（后台起内置 browser_bridge.py + magpie 在已登录的 tgc 标签页注入 agent，同样不新开浏览器）；登录态失效时用 magpie-browser 在当前已经打开的浏览器里自动登录（先判断 magpie-browser 是否可用，不可用给出提示；绝不新开浏览器/新 profile，一律在新开的 Magpie 标签页里操作、不复用已有标签页；不落盘密码）；支持公司名校验（用户发来公司名称时用 --expect-company 验证登录账号是否属于该公司，不匹配报 wrong-company）；公司优先：同公司多个登录手机号不因 erpMobile 不同阻断；--expect-account 默认诊断，--strict-account 显式严格核对；登录失败分类处理（滑块验证提示用户滑动、账号密码错误提示用户、账号失效提示用户）；获取的 cookie/XSRF token/完整请求头可供其他 skill 以子进程 JSON 或 import tmall_session 两种方式在内存中引用，绝不落盘。跨平台：macOS+Chrome（钥匙串 AES-CBC）与 Windows+Edge（DPAPI AES-GCM，v20 及锁库场景走桥）。当用户说「登录淘工厂/天猫」「获取天猫 token」「拿一下 XSRF」「检查天猫登录态」，或其他 skill 调天猫接口前需要登录态/token 时使用。
---

# 淘工厂登录（淘工厂登录与 token 获取）

## 用途

用 magpie-browser 在**用户当前已经打开的浏览器**（绝不新开浏览器/新 profile）里、于**新开的 Magpie 标签页**中完成淘工厂（tgcwork.tmall.com）登录，然后从本机浏览器 cookie 库解密出会话，校验并输出可直接调天猫接口的 **XSRF token**（及完整请求头）。用户发来**公司名称**时，登录后必须校验登录账号是否属于该公司。

适用于：
- 用户说「登录天猫 / 淘工厂」「获取天猫 token」「拿一下 XSRF」「检查天猫登录态」；
- 用户发来「公司名 + 账号 + 密码」要求登录某个公司的淘工厂账号；
- 其他 skill（账单下载、数据导出等）在调用天猫接口前需要登录态/token 时，先走本 skill。

## 支持的环境

| 环境 | 浏览器 | cookie 加密 |
|---|---|---|
| macOS + Chrome（magpie-browser） | Chrome（也支持 Edge/Brave/Chromium） | macOS 钥匙串 Safe Storage + AES-CBC |
| Windows + Edge（magpie-browser） | Edge（也支持 Chrome） | DPAPI（Local State 主密钥）+ AES-GCM |

脚本自动按 `sys.platform` 分支：macOS 走钥匙串，Windows 走 `%LOCALAPPDATA%\<浏览器>\User Data\Local State` 的 `os_crypt.encrypted_key`（经 `CryptUnprotectData` 解出）。cookie 库位置两种布局都兼容：`<profile>/Cookies` 与 `<profile>/Network/Cookies`。

## 流程（agent 按此执行）

### 0. 先判断 magpie-browser 是否可用（登录动作的前置检查）

调 magpie-browser `browser` 工具 `action: "browsers"`：

- 返回已有会话（如 `b1 Chrome 156`）→ 可用，后面登录动作就用它。
- 返回为空（浏览器没启动）→ macOS 先 `open -a "Google Chrome"`、Windows 先启动用户原本的 Edge（同一个 profile，不是新装实例），然后每 2 秒调一次 `browsers`，最多等 20 秒等 Magpie 扩展连上。
- 20 秒后仍为空（扩展没装/没启用）→ **明确提示用户**：「magpie-browser 不可用：请确认 Magpie Browser 扩展已安装并启用、浏览器已打开」，然后停止登录动作。**不要自己另起浏览器或另建 profile**。
- 纯检查登录态 / 取 token 优先走本机 cookie 库（不需要浏览器在线），可跳过本步直接跑脚本；cookie 读不到时脚本自动回退浏览器桥（见 1b），那时才需要浏览器在线并已注入 agent。

### 1. 判据永远来自脚本，不要肉眼猜

```bash
python3 <skill目录>/scripts/ensure_login.py
```

输出一个 JSON 对象，退出码：`0`=登录态有效（**直接结束，不要再打开登录页**）；`3`=需要干预（`reason` 为 `no-cookie` / `no-session` / `expired` / `wrong-account` / `wrong-company` / `company-unknown`）；`1`=脚本自身出错。只有退出码 3 才需要动手，并优先照抄 JSON 里的 `action` 文案。`no-session` 表示 cookie 库与浏览器桥两路都拿不到会话，按 `action` 指引起桥（1b）或登录（2）。

**用户给了公司名称就一定要校验**（用户通常发简称，如「埔鸣」；接口返回全称如「义乌市埔鸣日用品有限公司」，脚本按互相包含匹配）：

```bash
python3 <skill目录>/scripts/ensure_login.py --expect-company 埔鸣
```

**身份以公司名称为准，不以登录手机号为准**：同一公司可以有多个手机号登录；`erpMobile` 仅为平台返回的 ERP 绑定信息（手机号），不是当前实际登录手机号，不能用于认定实际登录身份。用户提供的登录手机号只用于需要登录时填表，**不得自动变成 `--expect-account` 身份约束**。

- 公司先校验：不匹配 → `wrong-company`；公司名查不到 → `company-unknown`。即使手机号相同、或账号不可核对，也不得放行；保留原公司简称/全称匹配规则。
- 默认只传 `--expect-company`。显式给了 `--expect-account` 且公司已匹配时，账号不匹配仅输出 `accountCheck: mismatch-ignored-company-match`，仍成功，不要求退出/换号。
- 用户**明确要求严格核对特定账号**时，同时传 `--expect-account <账号提示> --strict-account`；可比账号不匹配 → `wrong-account`。仅给 `--strict-account` 不给账号是参数错误。
- 没有 `--expect-company` 时，显式 `--expect-account` 保持原账号单独核对行为（可比不符即阻断）。
- 严格/单独账号核对的比对来源不变：11 位手机号与 `accountInfo.erpMobile` 脱敏串比较；非手机号与 cookie `accountHint` 比较。缺失可比证据仍是 `unverified`，不将未知当作已确认匹配；公司校验绝不因此跳过。

```bash
# 常规：只按目标公司验证；不要自动附加用户登录手机号
python3 <skill目录>/scripts/ensure_login.py --expect-company 埔鸣
# 只有用户明确要求严格账号核对时
python3 <skill目录>/scripts/ensure_login.py --expect-company 埔鸣 --expect-account <指定账号> --strict-account
```

`tmall_token.py` 使用完全相同的公司优先/显式严格账号策略与参数。

### 1b. cookie 库读不到时：起浏览器桥（`reason: no-session`）

Windows 上 Edge 正在运行会把 cookie 库锁死（`immutable` 直读也失败）、新版 Chrome 的 v20 应用绑定加密无法离线解密——这些环境下 `--transport auto`（默认）会在 cookie 路线失败后自动回退**浏览器桥**。桥不可用时 `ensure_login.py` 返回 `no-session`，`action` 文案就是下面两步：

1. **后台起桥**（本 skill 自带，无第三方依赖；已在跑会自动复用）：

   ```bash
   python3 <skill目录>/scripts/browser_bridge.py --port 8899
   ```

2. **注入 agent**：用 magpie-browser 找一个**已登录淘工厂的标签页**（如 `https://tgc.tmall.com/ds/page/supplier/home`；没有就先走第 2 步登录，登录完的标签页正好可用），对该标签执行 `evaluate`：

   ```js
   () => { const s = document.createElement('script'); s.src = 'http://127.0.0.1:8899/agent.js'; document.head.appendChild(s); return 'agent injected'; }
   ```

   随后重跑 `ensure_login.py`，JSON 里 `transport: "bridge"` 且登录态有效即桥路生效（有目标公司时必须公司匹配，不要求 `erpMobile` 有值）。桥只监听 127.0.0.1，凭据内存流转不落盘。

### 2. 登录动作（仅退出码 3 时）

1. **当前已打开的浏览器 + 新开 Magpie 标签页**：绝不新开浏览器、不新建 profile，也不要用 browser-skill 的 bsk_*（Agent Window 会另开浏览器窗口）。只用 magpie-browser MCP 的 `browser` 工具接管用户**当前已经打开**的那个浏览器（多浏览器时先 `browsers` 再 `use_browser` 选定，之后不换）。登录动作**一律 `tab_new` 新开一个 Magpie 标签页**（带 url `https://tgcwork.tmall.com/login`），**不复用任何已有标签页**：用户自己的标签、`tabs` 里带 `~`（其他 agent 在用）的标签、旧的 tgcwork 登录页标签都不要动。也不要用 chrome-devtools。登录完成并验证通过后，可 `tab_close` 关闭自己开的这个标签页。
2. **判断登录框状态**：`snapshot`（interactive）读登录页 iframe，里面应有「请输入账号名/手机号/邮箱」「请输入登录密码」「登录」三个元素。
   - 两个输入框都已有值（用户或密码管理器预填）→ 直接进入第 4 步，**不要改动内容**。
   - 输入框为空 → 进入第 3 步。
3. **凭据来源只有两个，都不落盘**：用户在本轮对话里已给出账号/密码就用它；否则用 `ask_user_question` 问一次（账号 + 密码），说明只用于本次填表。**绝不把密码写进技能文件、状态文件、日志或回复**；**绝不从系统凭据管理器、浏览器保存密码或其他脚本里挖密码**。
4. 用 `type` 按 ref 填表，再用 `click` 点「登录」按钮（真实点击，不要用 `evaluate` 合成点击）。
5. **登录要验证，不能假定成功**：

   ```bash
   python3 <skill目录>/scripts/ensure_login.py --wait 30 [--expect-company 埔鸣]
   ```

   轮询到 `loggedIn: true`（退出码 0）才算成功。

### 3. 登录失败的处理（分类，不盲目重试）

点完「登录」后等 2~3 秒 `snapshot` 一次，按页面状态分流；`ensure_login.py --wait` 轮询结束仍不是 0 时也走这里：

| 页面表现 | 判定 | 处理 |
|---|---|---|
| 出现滑块/拖动验证（iframe 里有「滑动验证」「请按住滑块拖动」等字样） | 人机验证 | **停下来提示用户**：「登录页出现滑块验证，请在浏览器里手动滑动完成验证」，等用户完成后重跑 `ensure_login.py --wait 30` 继续。**绝不尝试自动绕过验证码** |
| 提示「账号名或登录密码错误」「密码错误」等 | 密码错 | **提示用户密码错误**，用 `ask_user_question` 要正确密码，重新填表登录，最多重试 2 次；用户也改不了就停止 |
| 提示「账号不存在」「账号已停用/冻结/失效」等 | 账号失效 | **提示用户该账号已失效**，让用户确认账号状态或换账号，不要重试 |
| 提示需要短信验证码 | 二次验证 | 停下来让用户在浏览器里自己输，完成后重跑 `ensure_login.py --wait 30` |
| 页面无任何错误提示、也没跳转 | 未知 | 重跑 `ensure_login.py --wait 30` 一次；仍失败则 `snapshot` 人工判断 |

登录成功但 `reason` 为 `wrong-company`：说明登录的账号不属于用户给的公司 → 在自己新开的那个 Magpie 标签页里打开 tgc.tmall.com 退出登录（右上角公司名 → 退出），再按第 2 步重新登录（又是新开一个 Magpie 标签页）换目标公司的账号。

若用户明确要求“不要自动登录”，则只提示用户在浏览器里登录一次后重试。除此之外，只有遇到短信/滑块等验证码时才需要人工介入。

### 4. 获取 token

```bash
python3 <skill目录>/scripts/tmall_token.py                              # 只拿 token + 账号提示
python3 <skill目录>/scripts/tmall_token.py --with-headers               # 附带完整请求头（含 cookie）
python3 <skill目录>/scripts/tmall_token.py --with-headers --expect-company 埔鸣
```

退出码与 `ensure_login.py` 相同（0 有效 / 3 需登录或校验不过 / 1 出错）。成功输出示例：

```json
{ "ok": true, "reason": "logged-in", "accountHint": "scm65253097",
  "company": "义乌市埔鸣日用品有限公司",
  "cookieCount": 37, "xsrfToken": "…", "waitedSeconds": 0.4 }
```

调用天猫接口时用 `--with-headers` 拿到的 `headers` 直接作为请求头（已含 `cookie`、`x-xsrf-token`、UA、referer 等）。

### 5. 供其他 skill 引用（cookie / token 复用）

其他 skill（资产盘算、罚款导出等）需要天猫登录态时，按以下**任一**方式引用，凭据全程只在内存：

1. **子进程方式（推荐，语言无关）**：

   ```bash
   python3 <skill目录>/scripts/tmall_token.py --with-headers
   ```

   stdout 输出一个 JSON：`ok` / `reason` / `accountHint` / `company` / `cookieCount` / `xsrfToken` / `headers`（含 `cookie`、`x-xsrf-token`、`user-agent`、`referer`、`origin`、`content-type`）。调用方解析后直接发请求；退出码 3 表示先要走上面的登录流程。调用方使用 `subprocess.run(..., capture_output=True)` 在内存中解析 stdout，禁止将输出重定向到文件、日志或聊天。不要把凭据 JSON 直接接入占用 stdin 的 heredoc；不要在终端工具中直接展示 `--with-headers` 的输出。

2. **import 方式（同 Python 进程）**：

   ```python
   import sys
   sys.path.insert(0, "<skill目录>/scripts")
   import tmall_session
   built = tmall_session.build_session()          # {"headers", "session", "transport"}；transport 默认 auto（cookie→桥）
   headers, session = built["headers"], built["session"]
   ok = tmall_session.check_session(headers, session)   # bool（桥会话也要传 session）
   hint = tmall_session.account_hint(headers)     # 账号提示（脱敏）
   info = tmall_session.fetch_company(headers, session) # {"company": 全称, "erpMobile": 脱敏手机号, "ok": bool, "detail": str}
   tmall_session.company_matches("埔鸣", info["company"])  # 公司名匹配（简称↔全称）
   tmall_session.account_verdict("13128826864", info["erpMobile"], hint)  # 仅账号证据，不代替公司校验；公司匹配默认不据此阻断
   ```

**红线**：`headers` / `xsrfToken` / cookie 只在调用方进程内存里用，不写入任何文件、日志、状态或聊天回复；需要展示时只给 `accountHint`、`company`、`cookieCount` 这类脱敏字段。

## 安全红线

- **凭据只在内存**：cookie、XSRF token、请求头绝不写入技能文件、文档、状态文件、普通日志或聊天回复。需要展示时只给 `accountHint`、`company`、`cookieCount`、`hasXsrf` 这类脱敏字段（公司名不是机密，页面右上角本来就在展示）。
- 不落盘密码：账号密码只在表单里出现一次，不进任何文件。
- 不绕过验证码：出现短信/滑块/二次验证时停下来让用户自己过。
- 不新开浏览器实例、不新建 profile：永远接管用户当前已经打开的那个浏览器；浏览器动作一律在**新开的 Magpie 标签页**里做，不复用、不打扰用户或其他 agent 的标签页。

## 依赖

- Python 3.11+，`cryptography`（AES-GCM/CBC 解密）。
- macOS 无需额外依赖（`security` 命令读钥匙串）；Windows 无需 pywin32（ctypes 调 crypt32）。
- 浏览器桥自包含：`scripts/browser_bridge.py` + `scripts/bridge_agent.js` 随本 skill 附带（纯标准库），不依赖任何其他 skill。
- 优先使用当前带依赖的 Python。macOS 可用 `python3`；Windows 可用 `py -3` 或 DSH 自带 Python（基于当前用户路径，不限定 Administrator）。缺 `cryptography` 时用 `uv venv .venv --python 3.12` 建环境；macOS 安装：`uv pip install --python .venv/bin/python cryptography`，Windows 安装：`uv pip install --python .venv/Scripts/python.exe cryptography`，之后用对应解释器运行脚本。不要跨平台复制二进制依赖。
- magpie-browser MCP（登录动作与 agent 注入用；纯检查/取 token 且 cookie 库可读时不需要浏览器在线）。

## 已知限制

- Chrome/Edge 启用 v20 应用绑定加密（App-Bound Encryption）的 profile 无法离线解密，会明确报错；此时用浏览器桥（`--transport bridge` 或默认 auto 回退）即可，无需换 profile。
- Windows 上浏览器正在运行时 cookie 库可能被锁定——脚本先用 SQLite `immutable` 只读方式直读活库，失败才复制一份再读（支持 WAL）；再失败自动走桥，全程无需关闭浏览器。
- `wrong-account` / `wrong-company` 场景需要先在浏览器里退出当前账号再登录目标账号；退出动作让用户确认后操作。
- 公司名校验与手机号核对依赖 `https://tgc.tmall.com/api/v1/supplierHome/accountInfo` 接口；接口偶发异常时返回 `company-unknown`（或账号 `unverified`，不阻断），重跑即可。
- 沙箱把 `%TEMP%` 拦掉时复制路线会失败——`immutable` 直读与桥都不经过临时目录，不受影响。

## 脚本

| 脚本 | 作用 |
|---|---|
| `scripts/ensure_login.py` | 登录态闸门（唯一判据来源，含 `--wait` / `--expect-account` / `--strict-account` / `--expect-company` / `--transport` / `--bridge-url`） |
| `scripts/tmall_token.py` | 获取 XSRF token / 完整请求头（含 `--with-headers` / `--expect-account` / `--strict-account` / `--expect-company` / `--transport` / `--bridge-url`） |
| `scripts/tmall_session.py` | 跨平台 cookie 解密与会话构建（macOS 钥匙串 / Windows DPAPI；`build_session` 支持 cookie→桥自动回退），供其他 skill import |
| `scripts/browser_bridge.py` | 浏览器桥服务（127.0.0.1:8899，纯标准库，配合 magpie 注入 agent 使用） |
| `scripts/bridge_agent.js` | 注入到已登录 tgc 标签页的桥 agent（由 browser_bridge.py 托管在 `/agent.js`） |
