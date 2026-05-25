---
name: weekly-report-summary
description: Generate a city-specific weekly summary workbook from multiple supply-chain daily profit/loss Excel files or an uploaded zip archive. Use when the user asks for 周报汇总, 城市周报, 苏州/商丘/徐州/驻马店等城市汇总, or wants to merge several 供应链业务盈亏日报 workbooks into a new weekly Excel report while preserving the original sheet structure, merged headers, borders, colors, and styles.
---

# 周报汇总

## Workflow

1. Locate all source `.xlsx` files from the user-provided files, folders, or zip archives.
2. If the target city is not explicit, ask one concise question: `需要合并哪个城市？`
3. Run `scripts/build_weekly_report.py` with the source paths and city.
4. Verify the output workbook has:
   - `营收概况`: original header/merge structure, daily city rows, and a final `总计` row.
   - `基地汇总`: original `昨日销售盈利` / `每日仓库固定支出` / `基地利润` merged headers, daily city rows, and a final `总计` row.
   - `商丘苏州徐州驻马店多多买菜` or the matching `多多买菜` sheet: each day shown with a date row, only the chosen city block, one `平台` column and one `基地` column, original-like borders, merged platform/base cells, and centered merged labels.
5. Return only the final `.xlsx` path and a short summary.

## Script Usage

Use the bundled script for deterministic output:

```bash
python3 skills/weekly-report-summary/scripts/build_weekly_report.py \
  --city 苏州 \
  --output-dir /path/to/output \
  /path/to/file1.xlsx /path/to/file2.xlsx /path/to/archive.zip
```

Prefer the Codex bundled Python runtime when available because it includes spreadsheet libraries:

```bash
/Users/rabbit/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  skills/weekly-report-summary/scripts/build_weekly_report.py ...
```

The script accepts any mix of:

- individual `.xlsx` files
- directories containing `.xlsx`
- `.zip` archives containing `.xlsx`

The generated filename is `<城市> 周报汇总表.xlsx`.

## Notes

- Keep source workbook styles by copying cells from the original sheets instead of applying custom report colors.
- Sort daily files by dates in filenames like `5月11日供应链业务盈亏日报.xlsx`; fall back to filename order if no date is found.
- Do not guess the city when the user provides multiple possible cities and no target city.
