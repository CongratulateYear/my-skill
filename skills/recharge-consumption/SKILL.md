---
name: recharge-consumption
description: "Generate a fixed-format Chinese recharge/consumption summary Excel workbook from a user-provided source workbook. Use when Codex needs to create or regenerate the Chinese report named by the bundled script as \\u5145\\u503c\\u6d88\\u8017.xlsx from Sheet1 data by grouping date, shop, and trade type amounts, adding daily totals and blank separator rows, and leaving unmatched fixed fields as zero."
---

# Recharge Consumption Workbook

This skill intentionally keeps `SKILL.md` ASCII-only. In Windows PowerShell or some terminal bridges, literal Chinese in skill files can be displayed or re-saved as mojibake. The bundled script stores exact Chinese constants as Python Unicode escapes and writes real Chinese text to Excel.

## Input

Require the user to provide a source Excel workbook path or uploaded workbook. Do not guess the source file.

Read only `Sheet1`. It must contain these source columns:

```text
\u4e1a\u52a1\u53d1\u751f\u65e5\u671f
\u5e97\u94fa
\u4ea4\u6613\u7c7b\u578b
\u91d1\u989d
```

Decoded labels:

```text
business date, shop, trade type, amount
```

Ignore every sheet except `Sheet1`.

## Output

Generate one Excel workbook named exactly:

```text
\u5145\u503c\u6d88\u8017.xlsx
```

The output workbook contains one worksheet named:

```text
\u5145\u503c\u6d88\u8017
```

If the user specifies an output directory, save the workbook there. Otherwise, save it to the current task's writable `outputs` directory when available, or to the source workbook directory.

## Fixed Headers

Use exactly these output headers, in this order:

```text
\u4e1a\u52a1\u53d1\u751f\u65e5\u671f
\u5e97\u94fa
\u3010\u534f\u8bae\u6263\u6b3e\u3011- \u81ea\u52a8\u5145\u503c
\u5145\u503c\u52a0\u7801\u6d88\u8017
\u6d41\u91cf\u5238\u62b5\u6263
\u6295\u6d41\u63a8\u5e7f\u5145\u503c
\u624b\u52a8\u5145\u503c
\u9884\u7b97\u6d88\u8d39
\u62b5\u7528\u5238\u5360\u6bd4
```

The final ratio column (`\u62b5\u7528\u5238\u5360\u6bd4`) is never calculated and must remain blank.

All output cells must use Excel's `General` number format. Do not copy styles, formulas, formatting, widths, number formats, or row heights from the source workbook.

## Mapping Rules

Read detail rows from `Sheet1`. Group and sum amount by:

```text
\u4e1a\u52a1\u53d1\u751f\u65e5\u671f + \u5e97\u94fa + \u4ea4\u6613\u7c7b\u578b
```

Each amount output field matches the source trade type by exact same text:

```text
\u3010\u534f\u8bae\u6263\u6b3e\u3011- \u81ea\u52a8\u5145\u503c
\u5145\u503c\u52a0\u7801\u6d88\u8017
\u6d41\u91cf\u5238\u62b5\u6263
\u6295\u6d41\u63a8\u5e7f\u5145\u503c
\u624b\u52a8\u5145\u503c
\u9884\u7b97\u6d88\u8d39
```

If a date/shop/output-field combination has no exact matching trade type, write `0`.

Ignore any trade type outside the fixed amount fields. Do not infer values from remark, income/expense type, formulas, or manual assumptions.

## Rows And Sorting

Sort dates ascending.

Within each date block, include only shops that appeared on that date in `Sheet1`; sort shops by name ascending.

After each date block's shop rows, add one total row:

- date column: the date
- shop column: `\u5408\u8ba1`
- amount fields: totals for that date
- ratio column: blank

Insert two completely blank rows between date blocks. Do not force blank rows after the last date block.

## Recommended Script

Prefer the bundled script:

```powershell
& '<bundled-or-working-python.exe>' scripts/generate_recharge_consumption.py --input '<source.xlsx>' --output-dir '<output-dir>'
```

The script validates the generated workbook by default. Keep this validation enabled unless there is a specific reason to skip it. A successful run prints an ASCII-safe line like:

```text
validation: ok; rows=206; columns=9; nonblank_rows=193; total_rows=7
```

On Windows, `python` may point to the Microsoft Store placeholder at `WindowsApps\python.exe`. If `python` exits without useful output, use the Codex bundled Python from `load_workspace_dependencies` or another real Python interpreter with `openpyxl` installed.

When PowerShell or the terminal corrupts Chinese paths or filenames into `????`, avoid passing Chinese text on the command line. Use the script's ASCII-safe discovery options instead:

```powershell
& '<python.exe>' scripts/generate_recharge_consumption.py --source-dir 'C:\Users\Administrator\Downloads' --glob '2026-06-15*.xlsx' --name-contains-escaped '\u5168\u90e8\u516c\u53f8' --output-dir '<output-dir>'
```

`--name-contains-escaped` accepts Python-style Unicode escapes. For example, `\u5168\u90e8\u516c\u53f8` matches a filename containing the Chinese text for "all companies".

To validate an existing generated workbook without regenerating it, use the script instead of an ad hoc inline Python snippet:

```powershell
& '<python.exe>' scripts/generate_recharge_consumption.py --validate-only --output-dir '<output-dir>'
```

Avoid inline validation snippets that contain literal Chinese paths, literal Chinese filenames, or Python string literals such as `'\u5145\u503c\u6d88\u8017.xlsx'` inside a PowerShell command. Some terminal bridges may convert those before Python sees them, causing `????.xlsx` paths or `UnicodeEncodeError`. If inline validation is unavoidable, pass only ASCII paths on the command line and build Chinese constants inside Python with numeric `chr(...)` code points.
