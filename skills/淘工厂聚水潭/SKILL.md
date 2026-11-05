---
name: taogongchang-jushuitan
description: 聚水潭：跨Windows/macOS复用浏览器登录与Cookie，按主订单号查询并导出ERP订单；支持从淘工厂罚款Excel提取主订单号，仅罚款导出触发时将商品id、商品标题匹配回填，直接回填原处罚数据，商品id和商品标题以ERP为准，ERP原始导出独立保存。
---

# 淘工厂聚水潭

分为三部分：**登录获取 Cookie → 数据导出 → 数据匹配**。用户说“淘工厂聚水潭”“聚水潭订单导出”“罚款数据匹配聚水潭”时使用。

## 安全与运行环境

- Python 3.11+；本地Cookie解密使用 `cryptography`，Excel使用 `openpyxl`。Windows、macOS共用同一份Python源码；不要复制跨平台二进制。优先使用宿主已配置、带依赖的Python；缺依赖时取得安装授权后在独立venv安装。
- 账号密码只用于浏览器表单；Cookie、token、请求头、签名下载URL、原始接口响应只在内存，不写文档、日志、状态、文件或回复。用户贴的fetch仅用于理解接口结构，不把其中Cookie复制进脚本。
- 业务订单Excel本来就包含订单号，输出属于用户业务数据，不混入技能仓库。未经用户要求不上传GitHub。
- 浏览器登录只使用 magpie-browser，当前已打开的浏览器，新开自己的Magpie标签页；不新开浏览器实例/profile，不动用户/其他agent标签。
- 登录遇到短信、滑块、验证码/风险校验即停，让用户手动处理，绝不绕过。
- 多公司严格串行；有公司约束时验证不通过绝不执行导出。不要把公司与手机号混为一谈。

## 一、登录获取 Cookie

入口：`https://www.erp321.com/login.aspx`；工作台/订单导出入口：`https://www.erp321.com/epaas`。

**必须先检查，已经登录只取Cookie/复用会话，绝不先退出或重复登录。**

```bash
python scripts/jst_session.py
# 若用户明确要求验证目标公司
python scripts/jst_session.py --expect-company 公司名称
```

退出码0表示服务端订单页校验有效，立即复用，不打开登录页。退出码3表示需要干预，先看action；Cookie读取被锁或v20不代表未登录，先尝试浏览器桥。公司校验目前从订单页显式公司字段提取：查不到就阻断，不以Cookie自报公司作身份凭证；真实页面若字段结构不同，必须补采样适配，不能放宽为无校验。

### 跨平台读取

本地Chromium数据库只解密适用 `www.erp321.com` 的Cookie；macOS钥匙串Safe Storage + AES-CBC，Windows Local State + DPAPI/AES-GCM；兼容Chrome/Edge/Brave/Chromium及Cookies、Network/Cookies两种布局。只读到内存CookieJar，保留域/path/secure，不输出Cookie。新版v20/库锁则auto回退浏览器桥，不要求关闭浏览器。

### 浏览器桥

专用端口 **8901**，与淘工厂8899互不影响；不要复用淘工厂桥。

1. 后台运行 `python scripts/browser_bridge.py --port 8901`，记住后台job id。
2. 用magpie检查当前浏览器。在自己的新Magpie标签打开 `/epaas`。已登录则不去登录页；需要登录才进入 `/login.aspx`。
3. 登录后在同一个自己开的ERP标签注入技能自带 `scripts/bridge_agent.js` 文件完整内容（read后evaluate），不要打印document.cookie。也可加载桥的agent.js；站点CSP/Private Network阻止加载时直接evaluate源码；浏览器拒绝本地连接需用户批准，不能绕过。
4. 重跑 `python scripts/jst_session.py --transport bridge --wait 30`。服务端有效才算成功；URL或isLogin=true本身不是成功依据。注入桥不会读取HttpOnly Cookie，实际请求由浏览器自动附带。
5. 若仍需登录，snapshot真实表单ref，用户当轮给账号密码才填；没有就问一次。已有预填不覆盖；真实click登录。错误密码最多重试2次；验证码让用户处理。成功后再次跑脚本验证。桥标签在任务完成前不要关闭。

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
4. GetExportData按auto_id取得动态HTTPS地址。下载后核验原始线上订单号集合必须与本次查到的订单完全一致，防止误拿其他任务。

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

匹配JSON保留原counts，并新增reason_counts分类：无订单号、ERP缺单、多商品歧义、无效字段分别报告；不因未匹配清空原值。总编排可用 --result-file 记录安全结果摘要，非网络原始回包。
