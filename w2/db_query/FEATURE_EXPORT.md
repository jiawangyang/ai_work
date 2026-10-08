# FEATURE_EXPORT.md — 数据导出功能设计文档

> 项目：智能数据库查询工具（w2/db_query）
> 版本：v1.1
> 状态：已实现（P1 后端导出 / P2 前端交互 / P3 CLI + Claude Code 命令）

---

## 1. 背景与目标

现有系统已实现：数据库连接管理、元数据浏览、SQL 执行、自然语言转 SQL（NL2SQL）、查询历史。
本次升级新增**数据导出功能模块**，目标：

1. 查询结果可导出为 **CSV**、**JSON** 两种（及以上）格式；
2. 通过 **Claude Code 自定义 Command / Agent** 实现"执行查询 + 导出结果"的一键自动化；
3. 提供**主动式用户交互**：查询成功后，AI 助手/界面主动询问是否导出，用户以一次点击或一句自然语言完成导出。

### 1.1 现状梳理（代码事实）

| 模块 | 位置 | 现状 |
|---|---|---|
| 查询执行 API | `backend/app/api/v1/queries.py` | `POST /api/v1/dbs/{name}/query` 返回 `QueryResult` |
| 查询服务 | `backend/app/services/query.py` | PG/MySQL 双执行路径，结果为 `list[dict]`，强制 `LIMIT 1000` |
| NL2SQL | `backend/app/services/nl2sql.py` | `POST /api/v1/dbs/{name}/query/natural` |
| 查询历史 | `QueryHistory`（SQLite） | 每库保留最近 50 条，含 sql/rowCount/成功与否 |
| 前端查询页 | `frontend/src/pages/queries/execute.tsx` | SQL 编辑器 + ResultTable + 历史列表，无导出入口 |
| AI 工程化配置 | — | 无 `.cursor/`、`.claude/` 目录，需新建 |

数据流：`前端 SQL → POST /query → sql_validator（校验+LIMIT）→ 执行 → QueryResult{columns, rows, rowCount, executionTimeMs, sql} → ResultTable 渲染`。

---

## 2. 需求分析

| 编号 | 需求 | 说明 |
|---|---|---|
| FR1 | 多格式导出 | 至少支持 CSV、JSON；架构上预留格式扩展点（Excel/Markdown） |
| FR2 | 自动化流程 | 一条命令/一次点击完成"查询→导出"，无需人工分步操作 |
| FR3 | 主动交互 | 查询成功后系统主动询问"是否导出为 CSV 或 JSON？"，支持自然语言触发 |

---

## 3. 总体设计

### 3.1 架构决策

**采用"服务端导出为主、前端触发下载"的方案**：

- 导出逻辑放在后端（格式化、类型序列化、转义集中一处，且支持从**历史记录重导出**与**CLI/Agent 直接调用**）；
- 前端仅负责触发（按钮/弹窗）并用 `blob` 接收文件流；
- CLI 脚本与 Claude Code Command 复用同一套后端导出 API，保证三种入口行为一致。

```
触发入口（三选一）                        统一导出核心
┌─────────────────────┐
│ ① 前端 UI           │  POST /api/v1/dbs/{name}/export
│    导出按钮 / 主动弹窗 ├──────────────► ┌──────────────────────────┐
├─────────────────────┤                 │ queries.py (新路由)       │
│ ② CLI 脚本          │  HTTP           │  └ exporter.py (新服务)   │
│    scripts/export   ├──────────────►  │      ├ to_csv()          │
├─────────────────────┤                 │      └ to_json()         │
│ ③ Claude Code       │  调用②或直接调①  │  └ query.py (复用执行)    │
│    /export 命令      ├──────────────►  └────────────┬─────────────┘
└─────────────────────┘                              ▼
                                          StreamingResponse(.csv/.json 文件)
```

### 3.2 导出语义

- **默认导出"最近一次成功查询"的结果**：请求体只带 `format`，后端从查询历史取该库最近一条成功记录重放执行后导出（保证数据新鲜、逻辑简单）；
- **支持显式指定 SQL**：请求体带 `sql` 时直接执行并导出（一键流程用这条路径）；
- 导出上限沿用现有 `LIMIT 1000`（sql_validator 已保证）。

---

## 4. 后端详细设计

### 4.1 新增文件

```
backend/app/services/exporter.py        # 导出核心：纯函数，易测试
backend/app/api/v1/exports.py           # 导出 API 路由（也可并入 queries.py，独立更清晰）
backend/scripts/export_query.py         # CLI 入口（供 Agent/命令行调用）
backend/tests/unit/test_exporter.py     # 单测
backend/tests/unit/test_api_exports.py  # API 测试
```

### 4.2 API 设计

```
POST /api/v1/dbs/{name}/export
Content-Type: application/json

{
  "sql": "SELECT ...",        // 可选；缺省时取该库最近一条成功查询重放
  "format": "csv",            // 必填："csv" | "json"（422 校验）
  "filename": "users_report"  // 可选；缺省自动生成 query_<db>_<yyyyMMdd_HHmmss>
}

→ 200
Content-Type: text/csv; charset=utf-8
Content-Disposition: attachment; filename="users_report.csv"
（JSON 则为 application/json）
```

注册到 `main.py`：`app.include_router(exports.router)`。

### 4.3 导出器（exporter.py）关键规则

```python
def export_to_csv(columns: list[QueryColumn], rows: list[dict]) -> str:
    """csv.writer 逐行写出；首行为列头。"""

def export_to_json(columns: list[QueryColumn], rows: list[dict]) -> str:
    """对象数组形式：[{"col": value, ...}, ...]，ensure_ascii=False 缩进2。"""
```

类型序列化规则（两种格式统一）：

| 数据库类型 | 导出值 |
|---|---|
| `None` | CSV → 空字符串；JSON → `null` |
| `datetime/date` | ISO 8601（`isoformat()`） |
| `Decimal` | 字符串（避免精度丢失） |
| `bytes` | UTF-8 解码为字符串，失败则 `"<binary N bytes>"` |
| `bool` | CSV → `true/false`；JSON → 原生布尔 |

CSV 附加规范：**UTF-8 with BOM**（`\ufeff` 前缀，保证 Excel 直接打开不乱码）；换行符 `\r\n`；依赖 `csv` 标准库自动处理引号/逗号/换行转义。

### 4.4 错误处理

| 场景 | 行为 |
|---|---|
| `format` 非法 | 422（Pydantic Literal 校验） |
| 连接不存在 | 404（与现有路由一致） |
| SQL 校验失败 | 400（复用 `SqlValidationError`） |
| 执行失败 | 500（详情写日志，不泄露连接串） |
| 结果为空 | 正常导出：CSV 仅表头，JSON 为 `[]` |
| `filename` 非法字符 | 服务端正则清洗 `[^\w\-.]` → `_`，防注入 |

### 4.5 大结果集（本次范围外，预留）

当前 `LIMIT 1000` 全量内存导出足够。后续若放开上限，切换为 `StreamingResponse` + 数据库游标逐行生成（`csv.writer` 写入 `io.StringIO` 分块 flush），导出器接口已按"行迭代器"友好设计。

---

## 5. 前端详细设计

### 5.1 新增/修改文件

```
frontend/src/components/ExportButton.tsx     # 导出下拉按钮（CSV/JSON）
frontend/src/components/ExportPromptModal.tsx# 查询成功后的主动询问弹窗
frontend/src/types/export.ts                 # ExportInput 类型
frontend/src/services/export.ts              # 服务端导出 API + 浏览器下载封装
frontend/src/pages/Home.tsx                  # 主页面：旧客户端导出切换为服务端导出，并接入主动询问弹窗
frontend/src/pages/queries/execute.tsx       # 查询页：结果卡片挂载导出按钮与主动询问弹窗
```

### 5.2 交互流程

1. **显式入口**：`Query Results` 卡片 `extra` 处新增 `ExportButton`（antd `Dropdown`：Export CSV / Export JSON），仅在 `result && result.rowCount > 0` 时可用；
2. **主动询问（FR3 核心）**：查询成功且 `rowCount > 0` 时弹出轻量 `Modal.confirm`：

   > 查询成功，返回 128 行。需要将这次查询结果导出为 CSV 或 JSON 文件吗？
   > [导出 CSV] [导出 JSON] [暂不需要]

   - 选择"暂不需要"后，本次会话（`sessionStorage` 标记）不再重复打扰；
3. **下载实现**：`apiClient.post(url, input, { responseType: "blob" })` → `URL.createObjectURL` → 创建 `<a>` 触发下载 → `revokeObjectURL` 释放。

---

## 6. 自动化流程设计（Claude Code）

### 6.1 自定义 Command（主要方案）

新增 `.claude/commands/export.md`（仓库级命令，输入 `/export` 触发）：

```markdown
---
description: 执行查询并导出结果（一键完成）
allowed-tools: Bash(bash backend/scripts/export_query.py:*), Read
---
# 任务
用户输入：$ARGS
（形如：/export 从 interview 库查每个部门的平均薪资，导出 CSV）

# 步骤
1. 解析 $ARGS：目标数据库、查询意图（自然语言或 SQL）、目标格式（默认 csv）。
2. 若是自然语言：调用 backend/scripts/export_query.py --db <name> --nl "<意图>"，
   脚本内部先走 /query/natural 生成 SQL 并展示给用户确认；
3. 若是 SQL：直接 --sql "<SQL>"；
4. 执行导出，报告生成文件的绝对路径、行数、耗时；
5. 若失败，读取 stderr 给出修复建议（常见：表名错误→查元数据）。
```

### 6.2 CLI 脚本（export_query.py）

```
python backend/scripts/export_query.py \
  --db interview \
  --sql "SELECT dept, AVG(salary) FROM employees GROUP BY dept" \
  --format csv \
  --out exports/result.csv

# 自然语言模式（脚本内部串联 NL2SQL → 确认 → 执行 → 导出）
python backend/scripts/export_query.py --db interview --nl "每个部门的平均薪资" --format json
```

实现：`httpx`（项目依赖已有）调用后端 API，逻辑薄、无业务耦合。

### 6.3 自动化时序

```
用户：/export interview 查平均薪资 导出CSV
  └─ Claude Code Agent
       ├─ 解析意图 → export_query.py --db interview --nl "..." --format csv
       │     ├─ POST /query/natural   → SQL + 解释
       │     ├─（终端确认 SQL，y/N）
       │     ├─ POST /export {sql, format:"csv"}
       │     └─ 落盘 exports/query_interview_20260818_143000.csv
       └─ 汇报：文件路径 / 行数 / 耗时，并提示可直接打开
```

三种入口的能力对照：

| 入口 | 查询 | 导出 | 交互方式 |
|---|---|---|---|
| 前端 UI | 手动/NL2SQL | 按钮/弹窗 | 界面点击 |
| CLI | SQL/NL | 参数指定 | 命令行 |
| `/export` 命令 | 自然语言 | 意图解析自动定 | 一句话 |

---

## 7. 测试计划

- **单元测试**（`test_exporter.py`）：CSV 转义（值含逗号/引号/换行）、BOM 头、None/datetime/Decimal/bytes 序列化、空结果、中文内容；
- **API 测试**（`test_api_exports.py`）：响应头（Content-Type/Disposition）、默认取最近成功查询、显式 SQL 路径、非法 format 422、404、错误 SQL 400；
- **手工验收**：前端下载 Excel 打开 CSV 无乱码；`/export` 命令端到端跑通。

---

## 8. 实施计划

| 阶段 | 内容 | 交付物 |
|---|---|---|
| P1 | 后端：exporter.py + exports.py 路由 + 单测/API 测试 | 可用 curl 导出文件 |
| P2 | 前端：ExportButton + 主动询问弹窗 + blob 下载 | UI 完整闭环 |
| P3 | 自动化：export_query.py CLI + `.claude/commands/export.md` + README 更新 | `/export` 一键流程 |

---

## 9. 验收标准

1. ✅ 查询结果可导出 CSV、JSON，文件内容与页面 ResultTable 一致；
2. ✅ 前端查询成功后出现主动询问，一次点击完成导出下载；
3. ✅ Claude Code 中 `/export <库> <自然语言> [格式]` 一条命令完成"生成 SQL → 确认 → 执行 → 导出 → 报告路径"；
4. ✅ 空结果、非法格式、失败 SQL 均有友好错误提示，无 5xx 裸奔；
5. ✅ 新增测试全部通过，存量测试不回归。

---

## 10. 未来扩展（非本期）

- Excel（xlsx，openpyxl）与 Markdown 表格格式（导出器插件化后各加一个函数）；
- 从查询历史一键重导出（历史条目加导出按钮）；
- 导出任务异步化 + 进度条（大结果集）；
- 将导出能力封装为 MCP Tool，供 Claude Code 直接原生调用（替代"命令+脚本"的中转）。
