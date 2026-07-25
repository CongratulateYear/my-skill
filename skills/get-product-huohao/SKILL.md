---
name: get-product-huohao
description: 从 Excel 中筛选“运营”为空的商品 ID，然后用真实已登录的 Chrome/Edge 浏览器会话查询淘宝/天猫商品“参数信息”模块里的货号和型号。核心使用经验证的天猫精简 URL 模板：id + item_type + mi_id + mm_sceneid + spm。支持扫码登录、参数信息状态分类、断点续跑和单个 XLSX 导出。
---

# 获取商品货号

## Workflow

1. 如果用户提供 Excel 工作簿，先提取 `运营` 为空的商品 ID。
2. 启动或附加到真实 Chrome/Edge 浏览器的 Chrome DevTools Protocol (CDP) 会话。默认不要向用户索要 `fetch(...)` 或 Cookie。
3. 如果浏览器打开淘宝登录页，停下来让用户在该浏览器里扫码或完成登录，然后从剩余 ID 继续。
4. 天猫商品默认使用经验证的精简详情 URL：`id + item_type + mi_id + mm_sceneid + spm`。这是本技能的核心功能点。
5. 每个商品只替换 `id`，只保留 `item_type`、`mi_id`、`mm_sceneid`、`spm`。默认丢弃 `ali_refid`、`ali_trackid`、模板里的跨商品 `skuId`、`sku_properties` 和其它广告/跟踪参数。
6. 不要使用纯 `https://detail.tmall.com/item.htm?id=<id>` 作为默认查询 URL；已验证纯 `id` 链接可能加载不出 `参数信息` 模块。
7. 默认串行查询，并加入随机 1-3 秒等待。不要并发请求商品页面。
8. 页面打开前启用 CDP Network，优先读取 `detail.tmall.com/item.htm` 主 Document 响应体里的 SSR 数据（`window.__ICE_APP_CONTEXT__`），从 `industryParamVO.basicParamList` 或 `BASE_PROPS.items` 提取 `货号` 和 `型号`。
9. 如果主 Document SSR 数据里没有取全，再从商品页 `参数信息` 模块 DOM 兜底提取。页面加载后必须滚动/点击足够多步骤，让 `参数信息` 暴露出来，不能只看页面是否打开。
10. 每个商品都按下方参数信息状态分类，避免把未检查参数模块的页面误判为失败或误填旧页面结果。
11. 保留中间 CSV/XLSX 以便登录、验证或中断后断点续跑。
12. 最终只导出一个给用户的 Excel。中间 CSV/TXT 放 `work/`，`outputs/` 只放最终交付文件，除非用户另有要求。

## 参数信息状态规则

主脚本报告这些状态：

```text
NO_PARAM_INFO_MODULE
PARAM_INFO_NO_HUOHAO_XINGHAO
PARAM_INFO_PARTIAL
PARAM_INFO_BOTH
```

含义：

```text
1. 查询不到参数信息: NO_PARAM_INFO_MODULE
2. 参数信息没有货号、型号: PARAM_INFO_NO_HUOHAO_XINGHAO
3. 参数信息货号、型号只有其中一个: PARAM_INFO_PARTIAL; missing=货号 or missing=型号
4. 参数信息货号、型号都有: PARAM_INFO_BOTH
```

出现 `PARAM_INFO_PARTIAL` 时，保留已找到的值，缺失列留空。不要从标题、推荐商品、评价或全页无关文本里补缺失值。

## 登录处理

- 优先让用户在受控浏览器里扫码登录，不默认使用复制 Cookie 的旧流程。
- 只有用户明确要求 legacy MTOP/Cookie 路径时，才索要 Cookie 或 `fetch(...)`。
- 当页面标题或状态为 `登录` 时，停止并让用户在打开的浏览器中完成登录，然后从剩余 ID 继续。
- 除非用户要求，不要关闭浏览器配置目录；保持打开可保存登录态。

## 脚本

使用本技能目录里的脚本：

- `scripts/extract-empty-operator-ids.py`: 读取 Excel，写出 `运营` 为空的商品 ID 清单。
- `scripts/fetch-taobao-param-info-cdp.mjs`: 默认主路径。通过 CDP 控制真实浏览器，使用精简天猫 URL 打开商品页，优先从主 Document SSR 数据提取 `货号` 和 `型号`，不足时再滚动/点击到 `参数信息` 模块做 DOM 兜底，并支持断点续跑。
- `scripts/fetch-taobao-properties-cdp.mjs`: 仅作 fallback。使用 MTOP/全页 DOM/SKU 重试。只有参数模块脚本被阻塞或用户明确要旧行为时使用。
- `scripts/fetch-taobao-properties.mjs`: legacy fallback。只有用户明确提供 Cookie/fetch 时使用。
- `scripts/csv-to-xlsx.py`: 将 CSV 结果转换为中文表头 Excel。

## 从 Excel 提取 ID

优先使用 bundled Python：

```powershell
& '<python.exe>' '<skill-dir>\scripts\extract-empty-operator-ids.py' `
  --input '<source.xlsx>' `
  --ids-output '.\work\item-ids.txt' `
  --pending-output '.\work\pending_empty_operator_ids.xlsx'
```

期望源列：

```text
ID | 运营 | 商品标题
```

其中 `商品标题` 可选。只选择 `运营` 去除空白后为空的行。

## 真实浏览器查询

默认使用真实浏览器。先尝试用户真实 Chrome 配置；如果 Chrome 退出或调试端口不可用，再用工作区专用配置如 `.\work\chrome-profile`。

```powershell
Start-Process `
  -FilePath 'C:\Program Files\Google\Chrome\Application\chrome.exe' `
  -ArgumentList '--remote-debugging-port=9238 --user-data-dir=C:\Users\Administrator\AppData\Local\Google\Chrome\User Data --no-first-run --no-default-browser-check about:blank'
```

然后附加：

```powershell
$env:TAOBAO_PAGE_URL='https://detail.tmall.com/item.htm?id=1037023345787&item_type=ad&mi_id=0000hantVH_npQO6sllemeOYTPTL23tRP9DVrhmCVvE-TYQ&mm_sceneid=0_0_121472174_0&spm=tbpc.pc_sem_alimama%2Fa.201876.d14'
& '<node.exe>' '<skill-dir>\scripts\fetch-taobao-param-info-cdp.mjs' `
  --attach `
  --input '.\work\item-ids.txt' `
  --output '.\work\taobao_param_info.csv' `
  --delay 1000 `
  --jitter 2000 `
  --resume `
  --stop-on-validate `
  --param-wait 850 `
  --param-steps 28 `
  --port 9238
Remove-Item Env:\TAOBAO_PAGE_URL
```

脚本会先读取详情页主 Document 响应体里的 SSR 数据；如果未取全，再点击/滚动详情区域，只读取标题为 `参数信息`、`商品参数` 或 `规格参数` 的模块。输出列：

```text
itemId | huohao | xinghao | status
```

不要把宽泛的 `document.body.innerText` 作为主数据源。全页文本只能用于人工诊断，不能用于补最终 `货号`/`型号`。

不要默认加 `--close-other-pages`。新版 Chrome 在所有页面被关闭时可能退出，导致 `/json/new?about:blank` 返回非 JSON 文本。只有用户明确要求时才关闭其它标签页，并至少保留一个页面。

## Tmall URL Template

默认模板必须是精简版：

```text
https://detail.tmall.com/item.htm?id=1037023345787&item_type=ad&mi_id=0000hantVH_npQO6sllemeOYTPTL23tRP9DVrhmCVvE-TYQ&mm_sceneid=0_0_121472174_0&spm=tbpc.pc_sem_alimama%2Fa.201876.d14
```

规则：

- 必须保留：`id`、`item_type`、`mi_id`、`mm_sceneid`、`spm`。
- 每个商品只替换 `id`。
- 默认不要保留：`ali_refid`、`ali_trackid`、模板 `skuId`、`sku_properties` 和其它跟踪参数。
- 纯 `id` URL 不是等价替代品；它可能打开商品页但没有 `参数信息` 数据。

## 断点续跑

继续中断任务时使用 `--resume`。CDP 脚本会读取已有输出 CSV，保留非登录/验证失败的行，跳过已完成商品，并追加新结果。

如果只重试子集，创建小输入文件，例如 `work/item-ids-retry.txt`，只跑这些 ID。用户缩小范围后，不要继续跑全量列表。

## 转换为最终 Excel

只转换最终 CSV 为一个交付工作簿：

```powershell
& '<python.exe>' '<skill-dir>\scripts\csv-to-xlsx.py' `
  --input '.\work\taobao_param_info.csv' `
  --output '.\outputs\商品货号型号查询结果.xlsx'
```

最终列：

```text
商品ID | 货号 | 型号 | 状态
```

最终回复前检查 `outputs/`，尽量只保留最终 Excel。旧工作簿如果因为 Excel 打开而无法删除，要明确说明并指向新的最终文件。
