---
name: taobao-products
description: >
  淘宝商品获取（淘宝商品抓取 / 淘宝导出 Excel）— MUST USE when the user wants to
  抓取、获取、导出、采集、整理 淘宝商品数据，e.g. 「淘宝搜索 X 导出 Excel」
  「帮我抓淘宝上卖得最好的 X」「淘宝 X 销量排行」「获取淘宝商品列表」「淘宝商品获取」
  「抓取淘宝商品」「taobao products export」。固定按销量降序排序，输出 Excel。
  输入：搜索商品关键词（必填）、抓取条数（默认 50）。输出字段：排名(销量序)、商品标题、
  商品ID、价格(元)、销量(原文)、销量(约/笔)、店铺、发货地、商品链接、商品图片、图片链接、备注。
  依赖 OpenCLI + 本机 Chrome 的已登录淘宝会话；未登录时会提醒用户登录而非自动登录。
---

# 淘宝商品获取

把「淘宝搜索 → 按销量排序 → 翻页取 N 条 → 输出 Excel」这套流程固定下来。

## 输入

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| 搜索商品 | ✅ | — | 搜索关键词，如「手套」 |
| 抓取条数 | ❌ | **50** | 目标条数；每页约 48 条，实际上限约 142 条 |

用户没给条数就用 50；没给关键词就问一句，不要自己编。

## 固定行为

1. **默认按销量降序排序**（点页面上的「销量」标签，不是改 URL —— 原因见下）。
2. **输出 Excel**，字段固定为（12 列，顺序固定）：
   `排名(销量序)`、`商品标题`、`商品ID`、`价格(元)`、`销量(原文)`、`销量(约/笔)`、`店铺`、`发货地`、`商品链接`、`商品图片`、`图片链接`、`备注`
3. 第二个工作表 `说明` 记录口径、时间、翻页次数与风控/登录情况。
4. Excel 存到**用户工作区**（交付物）；抓取中间产物一律放 `/tmp`。

---

## 用到的技能与依赖

### 用到的技能 / 机制

| 机制 | 在本 skill 里的作用 |
|---|---|
| OpenCLI 用户适配器（`cli({...})`） | 把「点销量 + 翻页 + 抽字段」封成一条命令 `taobao top-sales` |
| OpenCLI `Page` API | `goto` / `evaluate`（在页面内点控件、读 DOM）/ `wait` / `autoScroll` |
| 浏览器会话复用（`Strategy.COOKIE`） | 复用本机 Chrome 已登录的淘宝会话，**不读取、不导出 Cookie** |
| OpenCLI 官方 site-auth | `opencli taobao whoami`（登录检查）、`opencli taobao login`（登录引导） |
| openpyxl 浮层图片 + 单元格超链接 | 商品缩略图嵌入 `商品图片` 列；`商品链接`（商品页）与 `图片链接`（原图）各自独立成列 |
| DSH `present` | 把 xlsx 作为交付物呈现给用户 |

### 外部依赖清单（缺一不可）

| 依赖 | 用途 | 检验命令 | 缺失时怎么办 |
|---|---|---|---|
| OpenCLI CLI | 驱动 Chrome、加载适配器 | `opencli --version` | `npm i -g @jackwener/opencli` |
| Chrome + OpenCLI Browser Bridge 扩展 | 复用已登录会话；扩展须处于 connected | `opencli doctor` | 在 Chrome 安装扩展并连接 |
| **淘宝登录态** | 搜索 / 销量排序 / 翻页的前提 | `opencli taobao whoami` | 见「第 1 步 登录态检查」 |
| 本 skill 的适配器 `top-sales` | 抓取主命令 | `opencli taobao top-sales --help` | 第 0 步自检自动补装 |
| `~/.opencli/clis/taobao/` 可写 | OpenCLI 加载用户 CLI 的唯一目录 | `ls -l ~/.opencli/clis/taobao/top-sales.js` | 文件沙箱下需对该路径授权（工作区外） |
| Python ≥ 3.9 虚拟环境 `~/.taobao-skill-venv` | 跑 Excel 生成器 | `~/.taobao-skill-venv/bin/python -V` | 第 0 步自检自动创建 |
| openpyxl | 写 `.xlsx` | `~/.taobao-skill-venv/bin/python -c "import openpyxl"` | `~/.taobao-skill-venv/bin/pip install openpyxl` |
| Pillow | 下载并缩放商品图，嵌入单元格 | `~/.taobao-skill-venv/bin/python -c "import PIL"` | `~/.taobao-skill-venv/bin/pip install pillow` |
| 图片 CDN 网络（`img.alicdn.com`、`g-search*.alicdn.com`） | 商品缩略图 | 抓取日志里的 `images=N/M` | 自动降级为「查看图片」链接，不影响其它字段 |
| DSH skill 加载器 | 让本 skill 可被发现/调用 | skill 列表出现 `taobao-products` | — |

**明确不依赖**（评估过，别绕路）：
- **Scrapling / Camoufox**：反指纹解决不了「登录态 + 服务端行为风控」，且拿不到用户 Chrome 的会话。
- **agent-reach**：本 skill 自带淘宝路径。若 agent-reach 也在装，淘宝任务以**本 skill** 为准。
- 任何第三方淘宝 API / 代理：数据全部来自本机 Chrome 渲染的真实页面。

---

## 执行步骤

### 0. 环境自检（幂等，可重复执行）

```bash
SKILL_DIR=~/.agents/skills/taobao-products

# 适配器：OpenCLI 只从 ~/.opencli/clis/ 加载用户 CLI
if [ ! -f ~/.opencli/clis/taobao/top-sales.js ]; then
  mkdir -p ~/.opencli/clis/taobao
  cp "$SKILL_DIR/assets/top-sales.js" ~/.opencli/clis/taobao/top-sales.js
fi

# Python 环境：openpyxl（表格）+ pillow（嵌商品图）
if [ ! -x ~/.taobao-skill-venv/bin/python ]; then
  python3 -m venv ~/.taobao-skill-venv
  ~/.taobao-skill-venv/bin/pip install -q --upgrade pip openpyxl pillow
fi

# 三件套自检
opencli --version
opencli taobao top-sales --help | head -3
~/.taobao-skill-venv/bin/python -c "import openpyxl, PIL; print('excel deps ok')"
```

写入 `~/.opencli/`、`~/.taobao-skill-venv` 都在工作区之外：若文件沙箱拒绝，
按沙箱提示用**最小必要权限重试这一条命令**，不要改装到工作区里。

### 1. 登录态检查（**必须先做；未登录要提醒用户**）

```bash
if ! opencli taobao whoami -f json --window background > /tmp/taobao_whoami.json 2>/tmp/taobao_whoami.err; then
  cat /tmp/taobao_whoami.err
  echo "❌ 淘宝未登录，停止抓取"
  exit 1
fi
cat /tmp/taobao_whoami.json     # {"user_id":"...","nickname":"..."} —— 可用昵称跟用户确认账号
```

**未登录时，对用户说这段（照说，不要自己动手登录）：**

> 检测到淘宝账号未登录，抓取无法进行。
> 请在 Chrome 里打开 taobao.com 完成登录（扫码或密码均可），登录好跟我说一声；
> 或者我直接运行 `opencli taobao login`，它会打开登录页并等你完成登录。
> 未登录状态下我不会自动登录、也不会重试抓取。

用户确认登录后，重跑第 1 步；通过再进第 2 步。

适配器内部还有一层 DOM 级兜底：页面若跳到 `login.taobao.com/member/login`，
或页面上只出现「亲，请登录」且没有任何结果卡片，会直接返回 `auth-required`，
抛出与上面同一套提醒话术，**不会硬跑翻页**。

> 老版本 OpenCLI 没有 `whoami`：升级 OpenCLI；临时可用
> `opencli taobao top-sales "<关键词>" --limit 1 --sale-tab false --pages 0 -f json`
> 观察是否报未登录（代价很小，不翻页、不排序）。

### 2. 抓取（默认销量排序）

```bash
KW="手套"          # ← 搜索商品
N=50               # ← 抓取条数，默认 50

# 每页约 48 条：第 1 页免费，之后每页多约 48 条；上限 3
PAGES=$(( (N + 47) / 48 - 1 )); [ "$PAGES" -lt 0 ] && PAGES=0; [ "$PAGES" -gt 3 ] && PAGES=3

OPENCLI_BROWSER_COMMAND_TIMEOUT=900 OPENCLI_CACHE_DIR=/tmp/opencli-cache \
  opencli taobao top-sales "$KW" --limit "$N" --sale-tab true --pages "$PAGES" \
  -f json --window background > "/tmp/taobao_${KW}_raw.json" 2>"/tmp/taobao_${KW}.err"

grep top-sales "/tmp/taobao_${KW}.err"    # tabs / cards / 每页新增 / images=N/M
```

关键点：
- **必须 `--sale-tab true`**（默认已是 true）才能真正按销量排序。
- `--window background` 避免抢占用户前台窗口。
- 命令默认 60 秒超时，必须给 `OPENCLI_BROWSER_COMMAND_TIMEOUT`（抓 3 页约 2–4 分钟）。

### 3. 生成 Excel

```bash
~/.taobao-skill-venv/bin/python "$SKILL_DIR/scripts/build_xlsx.py" \
  --input "/tmp/taobao_${KW}_raw.json" \
  --output "./淘宝_${KW}_销量TOP${N}.xlsx" \
  --keyword "$KW" --requested "$N" --pages "$PAGES"
```

- 商品标题：**纯文本**，不挂超链接。
- 商品链接：独立列（在 `商品图片` 之前），整格超链接 + 显示完整 URL，可点可复制。
- 商品图片：只放浮层缩略图，**该单元格文本留空**。
- 图片链接：独立列放「查看图片」超链接。**图与链接必须分列**——Excel 浮层图的绘制层级
  高于单元格文字，同一格既放图又放链接会把链接压住（看得见图、点不到链接）；
  分列后网页预览 / Quick Look 这类不渲染浮层图的查看器下，也有 `图片链接` 列兜底、不会空白。
- 列宽约束：`商品图片` 列宽须 ≥ 缩略图像素宽（当前 64px 图 / 列宽 13 ≈ 96px），
  否则浮层图会横向溢出、压到右侧 `图片链接` 列。

### 4. 交付与汇报

用 `present` 交付 xlsx，并汇报：
- 实际条数 / 用户要求条数（**不足要如实说明，不要凑数**）
- 唯一商品 ID 数、销量区间、价格区间
- 排序验证结论（销量是否严格降序）
- `说明` 页里的推广位条数、风控与登录状态

---

## 风控与登录铁律

1. **未登录 → 提醒用户登录，然后停手**。绝不自动登录、绝不读取/导出 Cookie、绝不重试。
2. **命中风控验证页 → 立即停**（适配器返回 `检测到淘宝风控验证页…`）。
   让用户去 Chrome 手动完成验证，`sleep 90` 后再重跑**一次**；仍失败就停手上报。
3. **同一任务内多轮抓取，轮与轮之间 `sleep 90`**。一次能抓完就不要多跑。
4. 用户是主号登录态时提醒一句：高频抓取建议改用小号。

## 为什么不能用「改 URL」的简单办法（已实测，别重复踩）

| 手段 | 实测结果 |
|---|---|
| URL `&sort=sale-desc` | ❌ 被淘宝规范化丢弃；加载后页面「综合」仍为选中、销量乱序 |
| URL `&s=44` / `&page=2` | ❌ 无效；第二页与第一页重合 49/50 |
| 内置 `opencli taobao search` | ❌ `--limit` 硬上限 40，且无分页 |
| **点「销量」标签 + 点「下一页」** | ✅ 唯一可行（点击后标签状态变为选中，销量严格降序） |

## 字段口径

- **销量(原文)**：页面原文，如 `5万+人收货`、`4000+人付款`。
- **销量(约/笔)**：由原文换算的约数，仅供排序参考。**必须出现「N人收货 / N人付款」这类真实成交文案才算销量**；
  推广位角标（`本月行业热销`、`行业销量Top3`、`行业销量前20`）里的数字是**名次、不是成交笔数** → 一律留空。
  ⚠️ 不要用「取文本里第一个数字」的宽松正则：它会把 `行业销量Top3` 解析成「3 笔」、`行业销量前20` 解析成「20 笔」，
  既编造了数据，又会让销量列出现逆序（已踩过）。
- **备注**：`推广位·页面未显示成交数` 指列表中的推广位（角标 `本月行业热销` / `行业销量TopN`），页面不给成交数——如实留空，不要编。
- **商品链接**：商品详情页 URL（`https://item.taobao.com/item.htm?id=…`），独立成列、可点击。
- **商品图片**：淘宝 CDN 缩略图，浮层嵌入单元格；该格无文字。
- **图片链接**：`查看图片` 超链接指向 CDN 原图，与图片分列，不会被浮层图遮挡。
- 价格/销量均为页面展示值，会随促销与时间波动。

## 故障处理

| 现象 | 处理 |
|---|---|
| `淘宝账号未登录` / `auth-required` | 按第 1 步话术提醒用户登录；**不自动登录、不重试** |
| `whoami` 命令不存在 | OpenCLI 版本旧：升级；或按第 1 步的临时检查 |
| `unknown command 'top-sales'` | 第 0 步适配器没装好；确认 `~/.opencli/clis/taobao/top-sales.js` 是普通文件 |
| 命令 60 秒超时 | 没设 `OPENCLI_BROWSER_COMMAND_TIMEOUT=900` |
| `检测到淘宝风控验证页` | 让用户手动过验证，`sleep 90` 后重跑一次；仍失败停手上报 |
| 抓取条数 < 要求条数 | 如实汇报实际条数（推广位与去重会损耗少量），不要伪造排名凑数 |
| 图片全变成「查看图片」链接 | venv 缺 Pillow：`~/.taobao-skill-venv/bin/pip install pillow` |
| 图片列在预览器里空白 | 查看器不渲染浮层图（正常）；用 Excel/WPS/Numbers 打开，或点「图片链接」列的「查看图片」 |
| 缩略图盖住/点不到「查看图片」 | 图与链接被放在同一格了；必须分列（`商品图片` 只放图、`图片链接` 放链接） |
| 销量列出现逆序 | 推广位角标数字被当成销量（如 `行业销量Top3` → 3）；确认 `parse_sales` 要求「N人」成交文案 |
| 浏览器扩展未连接 | Chrome 里点扩展图标重连；`opencli doctor` 复核 |

## 文件

- `assets/top-sales.js` — OpenCLI 用户适配器源码（安装到 `~/.opencli/clis/taobao/top-sales.js`）
- `scripts/build_xlsx.py` — JSON → Excel 生成器（固定字段、嵌图 + 链接兜底、说明页）