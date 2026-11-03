---
name: nodeseek-checkin
description: >
  NodeSeek 签到 — MUST USE when the user wants to NodeSeek 签到 / nodeseek 签到 /
  每日签到 / check in on nodeseek.com. 用 magpie-browser 打开 nodeseek.com，
  判断登录态：未登录则去 signIn.html 登录（账号密码输入框为空时先向用户询问，
  已填写则直接过 Cloudflare Turnstile 验证再点登录），然后进入 /board 页面
  点「试试手气」完成签到并报告获得鸡腿数。依赖本机 magpie-browser MCP（用户
  已登录的真实浏览器会话），不使用 chrome-devtools。
---

# NodeSeek 签到

固定流程：打开 NodeSeek → 判断登录态 → （必要时）过 CF 登录 → /board 点「试试手气」→ 报告结果。

**工具约束**：只用 magpie-browser MCP 的 `browser` 工具（tabs/tab_new/navigate/snapshot/click/fill/wait/screenshot）。不使用 chrome-devtools。所有点击用 snapshot 里的 ref 真点击，不要用 evaluate 合成点击。

## 执行流程

### 第 1 步：打开站点，判断登录态

- 先 `tabs` 找已打开的 nodeseek 标签；没有就 `tab_new` 打开 https://www.nodeseek.com/
- `snapshot` 判断：
  - 右侧有用户卡片（用户名 / 等级 / 鸡腿 / 星辰）→ 已登录，跳到第 4 步
  - 出现「登录」链接 / 用户卡片缺失 → 未登录，进入第 2 步

### 第 2 步：登录页与凭据

- 导航到 https://www.nodeseek.com/signIn.html
- `snapshot` 查看用户名、密码两个输入框：
  - **输入框为空** → 用 ask_user_question 向用户询问账号和密码（单选提问即可让用户在回答里给）。凭据只用于本次填表，**不得写入任何文件、日志或对话总结**
  - **输入框已预填** → 直接进入第 3 步，不要改动内容

### 第 3 步：过 Cloudflare Turnstile 验证，然后登录

- `snapshot` 里 Turnstile 勾选框在 iframe 内，表现为 checkbox「请验证您是真人」→ click 它
- 等待其变为 checked/disabled、旁边出现「成功!」；未通过时等几秒再 snapshot 看一次，最多重试 2 次，仍不行就请用户在浏览器里手动点一下
- 验证通过后点击「登录」按钮 → 跳回首页且出现用户卡片即登录成功
- 若提交后仍停在登录页，检查是否有新的未通过验证，重复本步

### 第 4 步：签到（固定选「试试手气」）

- 导航到 https://www.nodeseek.com/board （签到入口图标只是跳转 /board 的链接，直接导航最稳）
- `snapshot` 判断页面状态：
  - 文本「今日还未签到，」→ 未签到：点击按钮**「试试手气」**（用户指定固定选这个；「鸡腿 x 5」是保底 5 个的固定档，不要点）
  - 文本「今日签到获得鸡腿X个…」→ 今天已签过，直接报告，不重复点
- 点击后可能弹出「今天的签到收益是X个鸡腿」→ 有弹窗就点「OK」关闭；实测很多情况不弹窗，页面文字直接变成「今日签到获得鸡腿X个，当前排名第X」
- 结果核对最稳的方式：`evaluate` 读 `document.body.innerText` 开头几百字（snapshot 里偶尔看不到那段提示文本）
- 向用户报告：获得的鸡腿数、当前排名（页面文本「今日签到获得鸡腿X个，当前排名第X」）

## ⚠️ 凭据规则（务必遵守）

- `signIn.html` 的表单会用浏览器自动填充或上次填写的内容**预填用户名和密码**；只要输入框非空就直接用，**不要在对话或总结里暴露、复述、记录账号名或密码**。
- 凭据只用于本次填表，不得写入任何文件、日志、脚本或 README。
- 只有在输入框确实为空时才用 ask_user_question 向用户询问。

## 坑位备忘

- 首页右侧用户卡片下的日历图标（span[title=签到]）click 处理器绑在 svg 子元素上，脚本合成点击经常无效——它本身也只是跳转 /board，所以永远直接导航 /board。
- 旧签到接口 /api/attendance/random 已 404，不要尝试 API 方式签到。
- 「试试手气」随机 4~16 个鸡腿（4 是下限）；保底固定档是「鸡腿 x 5」= 5 个。
- Turnstile 勾选框在 iframe 里，a11y snapshot 能直接给出 ref，click 一次即可。链接进去时 widget 先显示「正在验证…」，等 2~3 秒 `wait` 后再 snapshot 才会出现 checkbox。
- 登录成功判定：点「登录」后 URL 从 `signIn.html` 跳回 `https://www.nodeseek.com/`，且左侧导航多出「内版」等登录后板块（比找用户卡片更可靠）。
- 名单页 `/board` 的「今日签到鸡腿排行榜」很长，`interactive: true` 的 snapshot 依然会很长；要读结果优先用 `evaluate` 取 innerText。
- Cloudflare 拦截或验证反复失败时，让用户在浏览器窗口里手动完成，不要绕过。
