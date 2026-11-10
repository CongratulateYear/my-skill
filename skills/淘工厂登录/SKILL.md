---
name: taogongchang-login
description: 淘工厂登录与会话验证。Magpie在当前浏览器处理真实登录表单，官方Playwright Extension获取含HttpOnly的Cookie，Python接口继续业务。单公司执行期内存复用会话，子进程共享、不落盘；连接失败不等于未登录。
---

# 淘工厂登录（登录恢复与 token 获取）

## 用途与环境

用于登录淘工厂/天猫、检查登录态、获取 XSRF，或供其他 skill 在调用接口前取得会话。
默认 `--transport auto` 使用官方 Playwright Extension（预先安装扩展和本机缓存的 `@playwright/mcp@0.0.84`，脚本不联网安装）。只连接当前已打开的 macOS Chrome / Windows Edge，不新开实例或 profile。`playwright` 显式选择同一路径；`cookie` 保留旧本地库读取兼容；`bridge` 仅为 playwright 的兼容别名，非空 bridge-url 明确拒绝。

## 前置条件：Magpie与Playwright分别准备

开始前确认以下条件，缺失就停止并指导配置，不能把插件未连接解释为未登录：

1. **浏览器**：仅支持当前已打开的macOS Chrome或Windows Edge；两种插件必须安装/启用在同一浏览器、同一用户profile，不能让Magpie登录一个profile、Playwright从另一个profile取Cookie。不新开浏览器实例或profile。
2. **Magpie浏览器插件**：安装并启用Magpie Browser扩展，连接当前使用的Magpie/DSH工具端；用`browsers`确认目标浏览器在线。负责真实页面点击和登录表单；只有安装插件但工具端未连接，仍不满足条件。
3. **Playwright浏览器插件**：安装并启用官方Playwright Extension；本机Node/npm可用且`@playwright/mcp@0.0.84`已缓存（适配器使用offline模式，不自动联网安装）。此扩展与Magpie扩展不同，不能互相替代。
4. **配置Playwright扩展令牌**：从该浏览器profile的官方扩展连接界面取得令牌，私密配置`PLAYWRIGHT_MCP_EXTENSION_TOKEN`，或写入当前用户的`~/.config/tgc-playwright/extension-token`（Windows同样按用户主目录解析）。环境变量优先；POSIX令牌目录0700、文件0600，拒绝符号链接及不安全文件权限。Windows使用当前用户私有目录并限制ACL；脚本不自动修改ACL。令牌不放技能文档、命令参数、仓库、聊天或普通日志。未配置令牌时官方扩展可能要求每次人工授权；无人值守/减少重复确认必须先完成令牌配置。令牌无效或轮换后重新私密配置，不绕过授权。
5. **业务登录与依赖**：扩展令牌只授权浏览器连接，不等于淘工厂/ERP已登录；仍需服务端登录态/公司验证。Python 3.11+及业务所需依赖可用。验证码、短信、风险校验由用户完成。

不要在回复中展示已有令牌。只报告插件是否在线、令牌是否已配置等布尔状态，不读取回显令牌内容。

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

## 2. 固定恢复路线：新建自己的页 → 判断 → 必要时登录 → 获取内存Cookie → 服务端验证

### 2.1 当前浏览器中新建自己的 Magpie 标签页

1. 仅使用 magpie-browser MCP 的 `browser`。先 `browsers`，多浏览器时用 `use_browser` 选定用户当前浏览器，之后不切换。
2. 无连接时提示用户打开原浏览器并启用 Magpie 扩展，等待用户完成；**不得自行新开浏览器实例或 profile**，不使用 chrome-devtools 或 browser-skill 的另开窗口路线。
3. **每次恢复开始一律 `tab_new` 新建自己的 Magpie 标签页**，URL 为 `https://tgc.tmall.com/ds/page/supplier/home`。记录返回 tab，后续操作显式传该 tab；同一次恢复后续判断、登录、换号与重试继续用这个自己新建的页，不是每步再开一个。
4. 不复用、不导航、不注入用户已有标签页、其他 agent 的 `~` 标签页、旧登录页或旧淘工厂页；不能先找一个已登录旧标签来获取会话。

### 2.2 先判断页面，只有确认未登录才填表

对自己新建的页 `snapshot`：
- 若已登录并处于 `https://tgc.tmall.com`，**直接进入 2.3 获取Cookie，不要填表或重新登录**。
- 若明确跳到登录页/显示登录表单，才在自己页打开 `https://tgcwork.tmall.com/login` 并执行以下填表步骤。
- 若页面加载异常、无明确状态，继续 snapshot/排查，不因 no-session 就认定未登录或索要密码。

填表步骤：
1. 用 snapshot 的 ref 定位 iframe 表单，只检查账号、密码“是否有值”的布尔状态，不读出/回显密码。已有预填保留；缺账号时只补用户本轮给定的账号，然后再次检查密码是否有值，避免覆盖账号输入后触发的浏览器自动填充；仅补仍缺失的字段。
2. 两字段均有值后 `click` 真实登录按钮，不用 evaluate 合成点击。填表用 `type` 按 ref，只用用户本轮提供的凭据，不从密码管理器、系统凭据库、其他脚本挖密码。无可用凭据时 `ask_user_question` 只问一次并记录等待项（不含秘密），结束回复等待；无新输入或新会话证据不重复催问或提交。
3. 滑块、短信、验证码、二次验证都停下来让用户人工完成，绝不绕过。
4. 密码错误提示用户并索要正确密码，最多重试 2 次；账号不存在/停用/冻结/失效则停止，不盲重试。页面无错误且未跳转则再 snapshot 排查。
5. 登录后在**自己的同一标签页**进入 `https://tgc.tmall.com/ds/page/supplier/home`，继续获取Cookie。页面跳转不是最终成功。

### 2.3 官方 Playwright Extension 与内存复用

- Magpie 只做登录和真实页面交互；Python 使用官方扩展的 `context.cookies()` 获取含 HttpOnly 的 Cookie，再使用按域/路径/secure约束的 CookieJar 发送 HTTP，不向网页注入代理、不启动8899/8901桥。
- 扩展连接需要用户授权；若已配置私有扩展token则复用，不在聊天、日志或命令中展示。没有连接不等于没有登录，先解决扩展连接。
- 一次公司级流水线由 `SessionScope()` 创建仅监听回环地址的随机认证内存服务，闸门和导出子进程继承连接引用，首次获取后复用同一服务快照，不为每个流程重复连接扩展。淘工厂和ERP独立缓存，ERP仅确有有效罚款订单才获取。
- 独立Python进程内也缓存；进程结束缓存销毁。独立执行完ensure_login后另起一个不共享scope的命令，仍须重新获取。跨退出/重启的永久复用不提供，不写Cookie文件。
- 缓存上限1800秒（或最早Cookie到期时间），过期/连接失败停止，不无限刷新；登录、换号后结束旧scope，原参数续跑创建新scope。每家公司一个scope，禁止跨公司借用旧快照。服务端公司验证仍必须通过。
- 官方扩展会向现有浏览器打开连接标签；脚本先检查浏览器进程。获取Cookie后在断开MCP前自动清理当前连接页：每个MCP实例使用随机`memory-session-<nonce>`名称，仅关闭当前Page中官方扩展`connect.html`、本实例名称、protocolVersion=2及本机回环relay路径全部吻合的页面；不遍历旧标签，不关闭业务页或整个浏览器。关闭连接页可能使扩展立即断流，清理错误不覆盖已取得的内存Cookie。旧固定名称遗留页或无法证明归属的页不自动清理。若浏览器在检查后退出，存在官方实现的启动竞态，发现应停止，不承诺操作系统级绝不新进程。

### 2.4 服务端验证才是成功标准

```bash
python3 <skill目录>/scripts/ensure_login.py --wait 30 [--expect-company <用户给定公司>] [--expect-account <目标账号>]
```

保留本次所有期望参数。有目标公司时必须通过服务端 accountInfo 公司校验且 `loggedIn: true`、退出码 `0` 才继续业务。页面右上角公司名、页面已登录或Cookie获取成功都不足以宣告成功。

验证码人工完成或换号后，用新scope重新获取Cookie并执行服务端验证。持续no-session先排查扩展，不反复填表。业务结束只关闭自己创建的页面，用户标签不动。

## 3. 获取 token 与供其他 skill 引用

```bash
python3 <skill目录>/scripts/tmall_token.py [--expect-company <目标公司>]
```

退出码同闸门。CLI只输出hasXsrf等脱敏状态，不输出token或完整headers；`--with-headers`已拒绝。需要认证数据时用下面的Python import接口，仅在内存持有headers/session。退出码 `3` 回到固定恢复路线。

同一 Python 进程也可 import（这是调用接口，不是另一条登录恢复路线）：

```python
import sys
sys.path.insert(0, "<skill目录>/scripts")
import tmall_session
built = tmall_session.build_session()  # auto: Playwright + 内存复用
headers, session = built["headers"], built["session"]
ok = tmall_session.check_session(headers, session)
info = tmall_session.fetch_company(headers, session)
company_ok = info["ok"] and tmall_session.company_matches("用户给定公司", info["company"])
# ok 和 company_ok 均为 True 才继续目标公司业务；headers/session 仅在内存使用。
```

## 4. 账号或公司不符

`wrong-account` / `wrong-company` 不能忽略。若本次恢复还没创建自己的页，先新建自己的 Magpie 标签页；若已创建则继续用该页（不得操作旧用户页）。退出会影响同 profile 的其他标签会话，按以下边界处理：
- **当前任务已授权的顺序切换**：用户已给出目标公司顺序及对应凭据时，服务端确认当前公司不是本轮目标公司，自动在自己的Magpie标签真实退出并登录本轮目标公司，**不用再次询问**；开始执行时登录了其他公司也适用。多公司必须严格串行，前一家公司全部完成后才进入下一家，不能跳过失败公司。
- 公司身份未知时先核验，不能把 no-session 当作已确认公司不符而盲目退出；目标不明确、账号不明或要登录用户清单外目标时停止核对。此自动切换授权不扩展为ERP换号、验证码处理或合单范围批准。
满足相应授权后，只在自己页通过真实点击退出并登录目标账号，以新scope重新获取Cookie，带原期望参数服务端验证通过才继续业务。
用户明确不要自动登录时停止自动填表，请用户在自己新建的页登录；完成后仍需获取内存Cookie与服务端验证。

## 安全红线

- 密码、cookie、XSRF token、完整请求头不写文件/状态/文档/普通日志/聊天回复，仅在表单和进程内存中使用。只展示 `accountHint`、`company`、`cookieCount`、`hasXsrf` 等脱敏状态。
- 不从浏览器保存密码、系统凭据或其他脚本提取密码；不得把含凭据的历史对话恢复到临时文件，事后删除也不允许。历史必要凭据仅在内存上下文读取；无法安全取得则请用户重新提供。验证码人工完成，不自动绕过。
- 不新开浏览器或 profile，不修改加密安全设置；所有浏览器操作只在自己新建的 Magpie 标签页，不复用用户或其他 agent 标签。
- 公司验证失败不继续业务，不以页面状态、扩展连接、手机号字段代替服务端公司验证。

## 适配器与恢复交接契约

- 本skill的 `scripts/playwright_session.py` 是会话适配器唯一实现；淘工厂导出同名模块仅为兼容入口，修复与回归集中在此处，不能再复制实现。
- 总盘算输出 `awaiting_agent` 时，当前agent按本skill立即恢复自己的页面，再原命令续跑；Python不操作Magpie或读取用户凭据。`awaiting_user` 仅用于真实缺输入/人工验证，不能从连接异常或退出码3直接推断。相同原因恢复一次仍失败，停止诊断，不反复提交表单。
- 连接页清理诊断只保存有界状态和类型；不保存页面URL、relay地址、nonce、Cookie或异常原文。清理失败/断流不覆盖已成功取得的Cookie；不能把无法证明关闭完成的断流报告成确定关闭。

## 依赖与脚本

Python 3.11+、Node/npm及已缓存的官方MCP；本地cookie兼容选项需要cryptography。Magpie仅在浏览器登录恢复时需要。

| 脚本 | 作用 |
|---|---|
| `scripts/ensure_login.py` | 登录态闸门与恢复提示，支持wait、公司/账号、transport；旧bridge-url停用 |
| `scripts/tmall_token.py` | 仅输出脱敏状态；认证数据用import接口 |
| `scripts/tmall_session.py` | cookie 解密、会话构建与服务端登录/公司校验；默认官方扩展 |
| `scripts/playwright_session.py` | 官方扩展适配、内存缓存与SessionScope |
旧桥源码仅保留历史兼容测试，禁止启动或注入。

### 同一执行期复用示例

```python
from playwright_session import SessionScope
with SessionScope():
    # 本进程build_session与继承环境的子进程共享Cookie快照。
    # 不打印headers/cookies；每个实际业务会话仍执行服务端公司验证。
    run_company_pipeline()
```
