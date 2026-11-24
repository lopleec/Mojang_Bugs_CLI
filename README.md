# Mojang_Bugs_CLI

[English](#english) | [简体中文](#简体中文)

## English

A Python CLI for searching and exporting public reports from the [Mojang Bug Tracker](https://bugs.mojang.com/browse/MC).

- Keyword search and lookup by one or more bug IDs.
- Filters for ID ranges, dates, status, resolution, versions, labels, and categories.
- JSON and JSON Lines output, field selection, and grouped JSON exports.
- Automatic pagination, retries, and atomic file writes.
- Python 3.10+ with no third-party runtime dependencies.

### Install

Python and Git are required. Install inside a virtual environment:

~~~bash
python -m pip install "git+https://github.com/lopleec/Mojang_Bugs_CLI.git"
mojangbugs --help
~~~

For a local checkout, use `python -m pip install .`. The package name is `Mojang_Bugs_CLI`; the command is `mojangbugs`.

### Usage

~~~bash
# Search public reports
mojangbugs search "zombie door"
mojangbugs search chunk --status Open --status Reopened --limit 50

# Look up one or more IDs
mojangbugs get MC-1
mojangbugs get MC-1 MC-2 --json
mojangbugs get 123 --project MCPE

# Export inclusive ID or creation-date ranges
mojangbugs export --ids 100-200 -o bugs.json
mojangbugs export --created 2026-01-01 2026-01-31 -o january.json

# Combine filters and group results
mojangbugs export zombie --resolution Fixed --group-by status -o fixed.json

# Stream JSON Lines with selected fields
mojangbugs search zombie --jsonl --fields key,summary,status
mojangbugs export --ids 1-100 --format jsonl --fields key,summary -o bugs.jsonl
~~~

Use `--project` to select a project (default: `MC`). Filters such as `--status`, `--resolution`, `--affects-version`, `--label`, and `--category` are repeatable. Different filters use AND; repeated values use OR.

Search defaults to 20 reports, export to all matches; `--limit 0` removes the limit. Date ranges include both endpoints and use the tracker's query timezone. Use exact tracker names for versions and categories.

JSON exports include query metadata and report counts. JSON Lines writes one report per line and does not support grouping. `--group-by` supports status, resolution, versions, labels, category, confirmation, and creation month. Existing files require `--force`; failed exports preserve the destination. Attachments are linked, not downloaded.

Run `mojangbugs COMMAND --help` for all filters, output options, and network settings.

### Tests

~~~bash
python -m unittest discover -s tests
~~~

### License

[MIT](LICENSE)

## 简体中文

使用 Python 搜索、查看和导出 [Mojang Bug Tracker](https://bugs.mojang.com/browse/MC) 中的公开报告。

- 支持关键词搜索、单个或多个 Bug ID 查询。
- 支持编号范围、日期、状态、解决结果、版本、标签和分类筛选。
- 支持 JSON、JSON Lines、自选输出字段和 JSON 分组导出。
- 自动分页、失败重试，导出完成后才写入目标文件。
- 需要 Python 3.10+，无第三方运行依赖，CLI 界面为英文。

### 安装

需要 Python 和 Git，在虚拟环境中安装：

~~~bash
python -m pip install "git+https://github.com/lopleec/Mojang_Bugs_CLI.git"
mojangbugs --help
~~~

本地源码可使用 `python -m pip install .` 安装。包名为 `Mojang_Bugs_CLI`，调用命令为 `mojangbugs`。

### 使用

~~~bash
# 关键词搜索
mojangbugs search "zombie door"
mojangbugs search chunk --status Open --status Reopened --limit 50

# 查询一个或多个 Bug ID
mojangbugs get MC-1
mojangbugs get MC-1 MC-2 --json
mojangbugs get 123 --project MCPE

# 按编号或创建日期导出，包含起止边界
mojangbugs export --ids 100-200 -o bugs.json
mojangbugs export --created 2026-01-01 2026-01-31 -o january.json

# 组合筛选，按状态分组
mojangbugs export zombie --resolution Fixed --group-by status -o fixed.json

# 输出 JSON Lines，并选择字段
mojangbugs search zombie --jsonl --fields key,summary,status
mojangbugs export --ids 1-100 --format jsonl --fields key,summary -o bugs.jsonl
~~~

不同筛选条件之间取交集，同一选项重复传入的值取并集。搜索默认显示 20 条，导出默认获取全部；`--limit 0` 表示不限条数。`--project` 默认 `MC`，可切换为 `MCPE` 等项目。日期格式为 `YYYY-MM-DD`，按站点查询时区解释；版本和分类名称须使用站点原值。

`--group-by` 支持状态、解决结果、影响版本、修复版本、标签、分类、确认状态和创建月份。多个分类值会进入各自对应的组。JSON 导出包含查询信息、报告数量和数据；JSON Lines 每行一条报告，不含查询信息，也不支持分组。

已有文件需要 `--force` 才能覆盖，导出中断时保留原文件。报告包含附件链接，不下载附件文件和评论。

使用 `mojangbugs COMMAND --help` 查看完整参数，包括超时、请求间隔、重试次数和每页条数。

### 测试

~~~bash
python -m unittest discover -s tests
~~~

### 许可证

[MIT](LICENSE)
