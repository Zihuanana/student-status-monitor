# 学生状态监测与考勤预警系统

> 把校园考勤 / 教务数据变成「学生今天来没来、要不要提醒辅导员」——多数据源采集、状态判定、四级预警与邮件通知。
> FastAPI + SQLite + React 的轻量全栈实现。

> **说明**：本仓库为**脱敏源码版**。已移除全部真实学号、接口地址、密钥与个人信息，示例值均为占位符；运行数据默认写入本地，不入库。

## ✨ 功能

- **多数据源适配器**（`app/adapters/`）：统一接口下实现智慧课堂、教务选课/课表、通用第三方适配器与内置 mock 数据源，新增数据源只需实现一个 adapter。
- **状态判定**（`app/services/attendance.py`）：
  - `严重异常`：最近 3 个有课日均整日无签到
  - `异常`：最近 1 个有课日整日无签到
  - `今日未签到`：今日有课但尚未产生签到
  - `活跃` / `无课`
  - 判定优先级：严重异常 > 异常 > 今日未签到 > 活跃 > 无课
- **四级预警**（`app/services/warnings.py`）：
  1. 无记录预警：连续 N 个「有效统计日」无有效考勤记录
  2. 覆盖不足提醒：一段时间内所有有课日都拿不到数据 —— 提醒老师换方式核实，而不是误报学生
  3. 长期缺少记录：回看窗口内记录持续偏少（覆盖不足时不判）
  4. 数据异常：课表缺失 / 课程无法匹配 / 采集失败
- **邮件通知**（`app/services/notify.py`、`alerts.py`）：学生类预警按班级路由给辅导员、数据类回落到管理员；支持冷却去重与每日汇总。
- **采集调度**（`app/services/collect.py`）：后台定时轮询 + 课程结束后回补窗口 + 持久化事件 outbox；支持手动触发与 CLI 回填。
- **名单作用域**（`app/services/scope.py`）：学号正则 + 校区关键词双闸门，避免把其他院系学生算进来；支持手动标记（视作一次「无课签到」）与课表灰显。
- **前端**：React 18 + Vite 5 + TypeScript（无组件库），状态卡片、预警列表、辅导员联系人配置、课表视图。

## 🧱 技术栈

`Python 3.11` · `FastAPI` · `SQLAlchemy 2.0` · `SQLite (WAL)` · `React 18` · `Vite 5` · `TypeScript` · `pytest`

## 📁 目录结构

```
app/
  adapters/     数据源适配器（智慧课堂 / 教务 / 通用 / mock）
  api/          REST API、鉴权、路由
  core/         配置、数据库、枚举、日志、安全
  models/       SQLAlchemy 模型
  schemas/      Pydantic 模型
  services/     采集、判定、预警、通知、名单作用域、回填
  cli.py        命令行工具（seed / serve / collect / rebuild / ...）
  main.py       FastAPI 入口
tests/          pytest 测试（24 个测试文件）
web/            React 前端（Vite 构建，产物输出到 web/dist）
```

## 🚀 快速开始（mock 模式，无需任何外部接口）

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env          # Windows: copy .env.example .env

python -m app.cli seed        # 生成模拟数据
python -m app.cli serve       # 启动服务，默认 http://127.0.0.1:8000
```

前端：

```bash
cd web
npm install
npm run build                 # 产物由后端直接托管；开发时用 npm run dev
```

## ⚙️ 配置

所有配置集中在 `.env`（模板见 `.env.example`）：运行模式、采集间隔与回补窗口、判定阈值、邮件 SMTP、数据源开关与字段映射。

- 真实接口地址、Token / 密钥只填写在**本机** `.env`（已在 `.gitignore` 中排除），不要提交到仓库。
- 数据源默认 `DATA_MODE=mock`，无外部接口也能自测全流程。

## 🧪 测试

```bash
python -m pytest -q
```

## 🔐 数据与隐私

- 本仓库为**脱敏源码**：真实学号、接口地址、内网地址、密钥与个人信息均已移除或替换为占位符。
- 运行时数据（`data/`、`logs/`、导出的表格与截图）默认被 `.gitignore` 排除。
- 部署到实际环境时，请自行在服务器上填写 `.env`，并遵循学校的数据管理规范。
