# 本地验证与执行报告接口

全部接口仅读取本地文件，不发网络请求。验证器绝不保存输入工作簿；报告只能输出为独立 JSON 文件。

```powershell
python scripts/validate_asset_workbook.py --workbook output.xlsx --shop 店铺 --report validation.json
python scripts/validate_asset_workbook.py --workbook output.xlsx --shop 店铺 --state state.json --expected-modules product-loan --report validation.json
```

模板默认 `scripts/../assets/盘算模板.xlsx`，可用 `--template` 覆盖。`--expected-modules` 可重复；省略表示全部17模块。`--expected-products N` 可选。默认 state 是盘算文件同名前缀的 `.asset-inventory-state.json`。源文件优先取 state `results[*].outputDir` 下全部 xlsx 与 `savedFiles` 中的 xlsx，跨文件仅删除完全相同的映射明细行；因此可在交付前验证。对没有 state 记录的目标，回退读取 `--delivery-dir`（默认 `<盘算文件名>_输出`）中的同目标名 xlsx；有 state 的目标不会回退旧交付目录。无源文件且目标有明细时验证失败。

验证覆盖结构、固定表头、来源整行比对、边框、原有样式、展示元数据、主题、#REF!、店铺、总计及罚款公式、整表合计公式与只读输入 SHA256。公式缓存不重算；不会再次查询远程接口。退出码0表示全部通过，1表示失败，2表示CLI参数不合法。异常仅记录异常类型，避免错误信息包含凭据。

```python
from scripts.asset_report import build_report, write_report
report = build_report(state, builder_result, validation, shop="店铺", workbook="output.xlsx")
paths = write_report(report, "execution-report.json")  # 同时生成 execution-report.md
```

`state` 接受下载器结构 `{completedModules, results, dates}`；每个 result 使用 `queryRows`（回退 queryCount）、taskId、savedFiles、extractedFiles、targetSheet、writtenRows、sheetWriteVerified。`builder_result` 接受 builder.merge() 返回结果；匹配 target 的 import_stats.written_rows 覆盖两阶段下载时的 writtenRows=0，且使用 builder 的 sheet_write_verified。缺失查询数或验证值保留 null，不能伪装为已验证。validation 接受验证器 run(args) 返回对象，报告仅纳入白名单字段。

```powershell
python scripts/asset_report.py --state state.json --builder-result builder.json --validation validation.json --shop 店铺 --workbook output.xlsx --report execution-report.json
```

`--builder-result`、`--validation`、`--shop`、`--workbook` 可省略；`--markdown` 可覆盖默认同名前缀的 md 输出。报告忽略未知字段，删除密码/cookie/token/authorization/signature/credential等键，并屏蔽全部 HTTP URL 和常见凭据赋值文本。绝不传入 fetch 原文或请求头。报告中的源文件仅是本地路径；签名下载 URL 不会保留。

测试：`python -m unittest discover -s tests -p test_asset_report.py -v`。
