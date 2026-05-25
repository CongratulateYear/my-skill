---
name: duoduo-price-query
description: 多多买菜商品价格查询和排期 JSON 转 CSV/Excel。Use when the user asks for “多多价格查询”, “多多买菜价格”, “供应商价格”, calls the mc.pinduoduo.com supplierPrice query API, or provides a 多多买菜商品/排期 JSON and wants goodsId/goodsName/quantity/activity/warehouse fields parsed into a standard CSV and Excel workbook with supplier prices queried by goodsId.
---

# 多多价格查询

## Goal

Use the bundled script to turn 多多买菜 goods/schedule data into a standard CSV and Excel workbook. The normal flow is:

1. Get goods records from a JSON file/stdin, or fetch them from a pasted browser `pageQuery` request snippet.
2. Query each goodsId's supplier price with the same browser request context.
3. Compute `销售数量` and `销售单价`.
4. Write the final CSV only after all records parse and all required prices are available.
5. Export the same final result to Excel (`.xlsx`) after the CSV is successfully written.

## Standard Interactive Flow

When the user is querying prices from a browser API snippet, follow this exact loop:

1. The user sends a `pageQuery` `fetch(...)` request snippet. Use it to fetch/parse the goods list.
2. Extract and use the same snippet's `cookie`, `anti-content`, `verifyauthtoken` / `verifyAuthToken`, captcha token cookie, `Referer`, and other realistic browser headers to query the supplier price API.
3. If the supplier price API hits risk control/frequency limiting, stop immediately and report the endpoint, `goodsId`, and parsed error code/message.
4. Ask the user to refresh the logged-in supplier page and send another successful API request snippet that carries the updated `cookie` and verification headers/tokens. Prefer a full `pageQuery fetch(...)` snippet over a bare cookie because it may include `verifyauthtoken` and `mc-pc-cookie-captcha-token`.
5. Extract the fresh cookie/context from the new snippet and rerun with the same checkpoint so already successful goods prices are skipped.
6. Repeat until all goods prices are cached and the final CSV/Excel outputs are written.

In this flow, do not store the cookie-bearing snippets. Pass them through stdin at runtime only. The checkpoint is the continuity mechanism, not the request snippet.

Primary script:

```bash
python3 skills/duoduo-price-query/scripts/duoduo_price_query.py
```

## Safety Rules

- Never store cookies or cookie-bearing request snippets in skill files or repo files. Pass them at runtime with `--cookie`, `DUODUO_COOKIE`, or `--request-snippet -` via stdin.
- Never execute user-provided JavaScript/Node snippets. Parse request details as text and let the Python script perform HTTP requests.
- Prefer Edge DevTools `fetch(...)` snippets because they include realistic headers. Preserve browser headers such as `User-Agent`, `accept-language`, `sec-ch-ua*`, `sec-fetch-*`, `Referer`, `Origin`, `cache-control`, `pragma`, `priority`, `anti-content`, `verifyauthtoken`, and `cookie` when the script supports them. Let the HTTP client generate transport headers such as `Content-Length`, `Connection`, and `Host`.
- Price queries must be slow, randomly paced, and resumable. Use `--sleep-min 3 --sleep-max 5` and a checkpoint for full runs.
- If the supplier price API returns frequency limiting, for example `54001 操作太过频繁`, stop immediately. Do not cooldown and retry in the same run. Report the endpoint, affected `goodsId`, parsed error code/message, and ask the user for a fresh logged-in request snippet with updated cookie/token context. Reuse the same checkpoint after the user sends it.
- If the API returns auth or verification errors, for example `43001 会话已过期`, stop and ask for a fresh logged-in request snippet with updated cookie/token context.

## Input Collection

If the user has not provided enough input:

1. Ask for either goods/schedule JSON or an Edge/Node `fetch(...)` snippet for the goods list API such as `pageQuery`.
2. If they provide JSON first, parse it locally enough to confirm valid JSON and identify goods records or abnormal entries. Only after that ask for cookie/request context.
3. If they provide a cookie first, keep it only for the current turn and ask for JSON or a goods-list request snippet before querying.

Accepted request context sources:

- Direct cookie string, such as `api_uid=...; PASS_ID=...`
- `fetch`, `axios`, or similar request code containing a `cookie`/`Cookie` header
- Edge DevTools `fetch(...)` for `pageQuery` or another goods-list endpoint
- `DUODUO_COOKIE`

Extract from snippets when present: `cookie`, `anti-content`, `verifyauthtoken` / `verifyAuthToken`, captcha token cookies such as `mc-pc-cookie-captcha-token`, request URL, method, headers, and JSON body fields such as `bizDate` and `areaId`.

If `bizDate` or `areaId` is missing, infer it from the request body or from one unique value in the goods records. Ask the user only when it cannot be inferred safely.

## Main Commands

### Pasted pageQuery snippet, no separate JSON

Use this when the user pasted a cookie-bearing goods-list `fetch(...)` snippet. By default the script writes both `duoduo_prices_<bizDate>.csv` and `duoduo_prices_<bizDate>.xlsx`:

```bash
python3 skills/duoduo-price-query/scripts/duoduo_price_query.py \
  --request-snippet - \
  --fetch-goods-list \
  --sleep-min 3 \
  --sleep-max 5 \
  --checkpoint duoduo_prices_YYYY-MM-DD.checkpoint.json
```

Pass the snippet through stdin. If `--output` is omitted, the script defaults to `duoduo_prices_<bizDate>.csv`; if `--excel-output` is omitted, it writes the same basename with `.xlsx`; if `--checkpoint` is omitted, it defaults to the matching checkpoint path.

### JSON file plus runtime cookie/context

```bash
python3 skills/duoduo-price-query/scripts/duoduo_price_query.py INPUT.json \
  --output OUTPUT.csv \
  --excel-output OUTPUT.xlsx \
  --cookie 'COOKIE_STRING' \
  --anti-content 'ANTI_CONTENT_IF_AVAILABLE' \
  --biz-date YYYY-MM-DD \
  --area-id AREA_ID \
  --sleep-min 3 \
  --sleep-max 5 \
  --checkpoint OUTPUT.checkpoint.json
```

### JSON file plus request snippet

```bash
python3 skills/duoduo-price-query/scripts/duoduo_price_query.py INPUT.json \
  --output OUTPUT.csv \
  --excel-output OUTPUT.xlsx \
  --request-snippet - \
  --sleep-min 3 \
  --sleep-max 5 \
  --checkpoint OUTPUT.checkpoint.json
```

`--request-snippet` can supply `cookie`, `anti-content`, `verifyauthtoken`, `bizDate`, and sometimes `areaId`. Explicit CLI arguments take priority.

### Preview without price API

Use only when the user explicitly asks for a quick preview before cookie/API lookup, or asks to ignore nested helper objects:

```bash
python3 skills/duoduo-price-query/scripts/duoduo_price_query.py INPUT.json \
  --output OUTPUT.csv \
  --excel-output OUTPUT.xlsx \
  --top-level-only \
  --preview-no-api
```

Preview mode skips supplier price API calls. It fills `价格` from JSON `supplierPrice` when present and leaves it blank when missing.

## Record Parsing

Default parsing recursively scans JSON for complete goods records. A complete record contains:

- `goodsId`
- `goodsName`
- `totalQuantity`
- `quantity`

If an object contains some but not all of those fields, treat it as abnormal. Report the JSON path and missing fields, and do not output CSV.

Top-level schedule JSON often contains nested helper objects such as `quantityExtraVO` and `activityIntents[*]` that look like broken records. If the top-level array entries are complete goods records and the nested objects are false positives, use `--top-level-only`; in `--fetch-goods-list` mode, the script already extracts complete records from matching record lists and ignores nested helper objects.

## CSV Fields

Output this exact header:

```csv
商品id,商品名称,总数,剩余数量,销售数量,价格,活动价,销售单价,活动数量,仓库
```

For each valid record:

- `商品id`: `goodsId`
- `商品名称`: `goodsName`
- `总数`: `totalQuantity`
- `剩余数量`: `quantity`
- `销售数量`: `totalQuantity - quantity`
- `价格`: supplier price API result, cents converted to yuan with two decimals
- `活动价`: `activityIntents[0].supplierPrice / 100`, or `0` when missing
- `活动数量`: `activityIntents[0].currentTotalQuantity`, or `0` when missing
- `仓库`: `warehouseGroupVOList[0].warehouseGroupName`, or empty string when missing
- `销售单价`: calculate after `价格` is known:
  - If `活动数量 > 0` and `销售数量 - 活动数量 > 0`: `(活动数量 * 活动价 + (销售数量 - 活动数量) * 价格) / 销售数量`
  - If `活动数量 > 0` and `销售数量 - 活动数量 <= 0`: use `活动价`
  - If `活动数量 <= 0`: use `价格`

Use Python's `csv` module or equivalent standard CSV writer. Do not leave a partial final CSV when parsing or price lookup fails. A failed checkpointed run may update the checkpoint, but the checkpoint is only a resumable cache and never a deliverable.

## Excel Output

After writing the CSV, also write an Excel workbook by default:

- Default Excel path: same basename as the CSV with `.xlsx`
- Explicit Excel path: `--excel-output OUTPUT.xlsx`
- Skip Excel only when explicitly requested: `--no-excel`

The Excel workbook must contain the same columns and row order as the CSV, freeze the header row when possible, and keep text values as text so IDs and Chinese names are not changed by spreadsheet software.

## Error Handling

On any price query failure, report concrete details:

- endpoint: `https://mc.pinduoduo.com/orianna-mms/goods/schedule/supplierPrice/query`
- affected `goodsId`
- parsed `errorCode` / `errorMsg` when available, such as `54001 操作太过频繁`
- a short sanitized raw-response excerpt if useful

For frequency limit or auth errors, stop the run and ask the user to refresh the logged-in browser page and paste a new successful request snippet. Continue later with the same checkpoint so already successful goods prices are skipped.

When the user sends the next snippet, rerun the same command with the same `--checkpoint` and `--output`. Do not delete or recreate the checkpoint; it should contain only successful prices and will let the script skip goods that were already queried.

## Quick Check

For a one-record API check, use the CLI with a tiny JSON file or the script function `query_supplier_price(cookie, goods_id, biz_date, area_id, anti_content=None, verify_auth_token=None)`. A successful response contains `success: true` and `result.supplierPrice` in cents; for example `1720` becomes `17.20` in CSV.
