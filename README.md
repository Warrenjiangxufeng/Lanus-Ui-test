# Lanus UI Test

Lanus UI Test 是一个由 LangGraph 编排的自然语言 UI 自动化测试引擎。它读取 Excel 中的“用例名 / 操作步骤”，让规划模型根据当前页面快照逐步生成受控的 `playwright-cli` 操作，并用命令记录、页面快照、截图和验收结论形成可审计证据链。

## 快速开始

本机已经安装好依赖时，直接执行：

```bash
cd ~/Lanus-Ui-test
source .venv/bin/activate

lanus-ui-test run \
  "/Users/jiangxufeng1/Desktop/KNOWLEDGE/交易模型AI自动化测试用例版本1.xlsx" \
  --headed \
  --max-step-attempts 2 \
  --max-action-rounds 10
```

`--headed` 会打开可见 Chrome。执行结束后报告位于：

```text
~/Desktop/Lisa输出/Lanus-Ui-test/tasks/<task_id>/reports/report.html
```

## 核心设计

```text
START
  → bootstrap
  → gate
  → plan
  → execute
  → gate
  → observe
  → verify
  → gate
  → finalize
  → release
  → END
```

- **bootstrap**：固定完成浏览器启动、打开目标页、snapshot 和首屏截图，不消耗模型步骤重试。
- **LangGraph 管流程**：暂停、终止、步骤游标、重试与结束状态都由图节点控制。
- **模型只管当前步**：规划模型不能宣布整条用例完成，只能为当前编号步骤生成动作。
- **Planner 修复**：模型返回空命令、非法 JSON 或无进展计划时，先进行结构修复和重新观测。
- **验收有证据**：每个步骤结束时自动采集 snapshot、full-page screenshot 和命令输出，再由断言或独立验收模型判断。
- **命令不经过 Shell**：使用参数数组调用 `playwright-cli`，不使用 `shell=True`，操作白名单由引擎控制。
- **中断可恢复**：每个节点提交 SQLite 快照；暂停保留浏览器 session，恢复时从持久化游标继续。
- **报告可审计**：最终生成 JSON、Markdown 和独立 HTML 报告，包含步骤状态、尝试次数、断言、截图和执行命令。

### 1. 设计目标

Lanus 采用“有界自主”架构，不把整个 UI 测试交给模型自由操作，也不把业务流程写死成 Playwright 脚本。

- **确定性基础设施**：浏览器启动、session、命令权限、暂停、终止、重试、状态推进和证据保存由 LangGraph 控制。
- **模型局部决策**：页面理解、元素语义定位、测试数据绑定、验证码识别、异常分支和局部重规划由视觉模型完成。
- **步骤级审计**：每条自然语言步骤都必须有命令记录、页面快照、截图和验收结论。
- **可恢复执行**：每个节点执行后持久化完整 `TestState`，支持暂停后恢复和进程重启后的状态恢复。
- **接口解耦**：CLI、FastAPI、Planner、Browser、Repository 和 Report 之间通过明确的数据模型交互。

### 2. 分层架构

```text
┌──────────────────────────────────────────────────────┐
│ 调用层：CLI / FastAPI / 上层自动化测试平台           │
└───────────────────────┬──────────────────────────────┘
                        │ 创建、暂停、恢复、终止、查询
┌───────────────────────▼──────────────────────────────┐
│ TaskManager：线程池、并发控制、任务生命周期            │
└───────────────────────┬──────────────────────────────┘
                        │ TestState
┌───────────────────────▼──────────────────────────────┐
│ LangGraph TestEngine                                 │
│ bootstrap → gate → plan → execute → observe → verify │
└───────────────┬──────────────────────┬───────────────┘
                │                      │
┌───────────────▼────────────┐  ┌──────▼────────────────┐
│ Planner                    │  │ PlaywrightCli          │
│ 页面理解 / 单步动作 / 验收 │  │ 受控浏览器命令执行     │
└───────────────┬────────────┘  └──────┬────────────────┘
                │                      │
┌───────────────▼──────────────────────▼───────────────┐
│ TaskRepository / Evidence / Report                    │
│ SQLite 快照、命令记录、snapshot、截图、JSON/MD/HTML   │
└──────────────────────────────────────────────────────┘
```

### 3. 组件职责

| 组件 | 主要职责 | 不负责的事情 |
| --- | --- | --- |
| `cli.py` | 命令行启动、参数解析、任务等待 | 页面决策 |
| `api.py` | 任务创建、上传、暂停、恢复、终止、报告查询 | 执行浏览器命令 |
| `service.py` | 后台线程池、并发任务、运行时浏览器管理 | 判断测试是否通过 |
| `engine.py` | LangGraph 节点、路由、重试、状态持久化 | 识别页面元素 |
| `planner.py` | 调用视觉模型、生成单步动作、语义验收 | 直接操作浏览器 |
| `browser.py` | 安全调用 `playwright-cli`、管理 session、终止进程树 | 决定下一步测试动作 |
| `xlsx_reader.py` | 解析用例表头、编号步骤和 URL | 执行测试 |
| `repository.py` | SQLite 状态快照和控制信号 | 修改业务流程 |
| `report.py` | 基于执行状态生成 JSON、Markdown、HTML | 修改执行结果 |

### 4. LangGraph 节点

| 节点 | 输入重点 | 处理逻辑 | 输出和路由 |
| --- | --- | --- | --- |
| `bootstrap` | `target_url`、控制信号 | 固定执行浏览器 `open`，然后 snapshot 和 screenshot | 进入 `gate` |
| `gate` | `TaskStatus`、暂停/终止信号、步骤游标 | 检查是否暂停、终止、完成或需要推进用例 | 路由到 `plan`、`execute`、`observe`、`finalize` 或 `release` |
| `plan` | 当前步骤、完整用例、snapshot、截图、最近命令、历史证据、反馈 | 调用 Planner 生成受控命令和验收断言 | 有命令去 `execute`，无命令且完成去 `observe`，异常进入修复或重试 |
| `execute` | `pending_commands` 队首 | 通过 BrowserController 执行一条命令 | 回到 `gate` |
| `observe` | 当前任务和步骤 | 自动采集 snapshot 和 full-page screenshot | 进入 `verify` |
| `verify` | 页面证据、命令结果、断言 | 执行确定性断言或调用独立语义验收 | 成功推进步骤；失败进入重试 |
| `finalize` | 完整 `TestState` | 生成 JSON、Markdown、HTML 报告 | 进入 `release` |
| `release` | 最终状态 | 正常结束或终止时关闭 session；暂停时保留 session | 进入 `END` |

### 5. 状态模型

`TestState` 是整个 Agent 的唯一执行上下文，每个 LangGraph 节点返回更新后的状态，并由 `TaskRepository` 落盘。

| 分类 | 字段 | 说明 |
| --- | --- | --- |
| 任务身份 | `task_id`、`source_file`、`sheet_name` | 任务、Excel 和工作表标识 |
| 执行目标 | `target_url`、`headed`、`cases` | 目标地址、浏览器模式和完整用例 |
| 游标 | `case_index`、`step_index` | 当前执行到哪个用例和步骤 |
| 重试 | `step_attempt`、`action_round`、`planner_repairs` | 业务尝试、动作轮次、Planner 修复次数 |
| 阈值 | `max_step_attempts`、`max_action_rounds` | 单步骤最大业务重试和动作轮数 |
| 工作流 | `status`、`pending_commands`、`awaiting_verify` | 当前任务状态、待执行命令、是否待验收 |
| 浏览器 | `browser_started`、`last_snapshot`、`last_screenshot` | 是否已初始化以及最新页面证据 |
| 决策 | `current_plan`、`feedback` | 当前动作计划与重规划反馈 |
| 审计 | `executions`、`evidence` | 全量命令记录和步骤证据 |
| 结果 | `error`、`report_path` | 最终错误与报告路径 |

### 6. Planner 契约

Planner 每轮只处理当前自然语言步骤，不负责推进整条用例。

输入包括：

- 任务目标地址和当前用例。
- 当前步骤及其完整上下文。
- 最近一次 snapshot 和截图。
- 最近命令执行结果。
- 此前步骤证据摘要。
- 上一轮失败原因或 Planner 修复反馈。

输出必须符合以下结构：

```json
{
  "commands": [
    {"op": "fill", "args": ["e97", "admin"]}
  ],
  "step_complete": false,
  "assertion": null,
  "note": "填写账号"
}
```

完成当前步骤时：

```json
{
  "commands": [],
  "step_complete": true,
  "assertion": {
    "kind": "text_contains",
    "expected": "首页",
    "description": "登录后应进入首页"
  },
  "note": "登录结果已可见"
}
```

约束：

- `commands=[]` 且 `step_complete=false` 是非法计划。
- 确定性断言缺少 `expected` 时自动降级为 `semantic` 验收。
- 模型不能指定 session、截图文件、配置文件、持久化目录或关闭浏览器。
- 模型看不到或不确定元素时应先 `snapshot`/`find`。
- 验证码、图片文字等视觉任务依赖最近截图作为下一轮视觉输入。

### 7. 单步执行时序

```text
bootstrap 打开页面
  ↓
Planner 读取 snapshot + screenshot
  ↓
返回一批受控命令
  ↓
逐条执行 playwright-cli
  ↓
页面变化后重新 snapshot/screenshot
  ↓
模型局部重规划，直到 step_complete=true
  ↓
断言或语义验收
  ├─ PASS：推进 step_index
  └─ FAIL：记录原因，增加 step_attempt 或重新规划
```

一个自然语言步骤可以对应多个动作轮。例如验证码步骤可能需要：

```text
snapshot / screenshot
→ 视觉识别验证码
→ fill 验证码
→ click 登录
→ snapshot 校验结果
```

这不是固定的登录脚本。模型仍需要根据当前页面识别输入框、读取验证码、处理错误提示和决定是否刷新验证码。

### 8. 失败、重试与修复

系统区分三类失败：

| 类型 | 示例 | 处理方式 |
| --- | --- | --- |
| Planner 结构失败 | 空命令、非法 JSON、无进展计划 | 进入 Planner Repair，先 snapshot 并重新规划 |
| 命令执行失败 | 元素不存在、浏览器未打开、click 超时 | 结束当前命令批次，按步骤重试 |
| 验收失败 | 页面没有预期文本、未进入目标页面 | 保留证据和反馈，重规划或重试 |

相关参数：

- `LANUS_MAX_PLANNER_REPAIRS=2`：Planner 结构修复次数。
- `LANUS_MAX_STEP_ATTEMPTS=2/3`：同一业务步骤最大尝试次数。
- `LANUS_MAX_ACTION_ROUNDS=10`：同一业务步骤最大动作规划轮数。
- `LANUS_COMMAND_TIMEOUT=90`：单条命令超时。
- `LANUS_OBSERVE_TIMEOUT=30`：snapshot/screenshot 超时。

当前实现中，一个步骤耗尽业务重试后，会记录该步骤失败，并把当前用例后续步骤标记为 `SKIPPED`，然后进入下一条用例。报告仍会保留失败原因和已执行证据。

### 9. 暂停、恢复与终止

控制信号保存在 SQLite `signals` 表中，`gate` 节点在流程边界读取信号。

- `pause`：当前命令完成后暂停；保留浏览器 session 和最新状态。
- `resume`：清除 pause 信号，从 `case_index`、`step_index` 和持久化状态继续。
- `stop`：设置 stop 信号，终止当前 CLI 进程组，关闭对应浏览器 session，并生成终止报告。

每个 LangGraph 节点通过 `_durable()` 包装：节点执行完成后立即保存完整 `TestState`。因此服务重启后可以重新加载最新检查点。

### 10. 浏览器执行边界

- 使用参数数组调用 `playwright-cli`，不拼接 Shell 字符串。
- `task_id` 同时作为 Playwright session ID。
- 引擎负责 `-s=<session>`，模型不能覆盖。
- 命令必须在白名单中。
- 模型生成的命令逐条执行，不批量注入长脚本。
- 截图文件名、snapshot 文件名和产物目录由引擎控制。
- `stop` 使用独立进程组终止执行中的命令，避免浏览器继续操作。

### 11. 证据链与报告

每个步骤的证据包括：

- 自然语言步骤和步骤编号。
- Planner 生成的断言。
- 每条 CLI 命令、返回码、stdout、stderr 和耗时。
- 页面 snapshot。
- full-page screenshot。
- 验收模型结论和证据引用。
- 尝试次数、失败原因、开始和结束时间。

`report.json` 是结构化事实源；`report.md` 便于快速查看；`report.html` 提供独立样式、汇总指标、命令详情和截图预览。HTML 可以从已有 JSON 单独重建：

```bash
lanus-ui-test html path/to/report.json
```

### 12. 扩展点

- **扩展 Planner**：实现 `plan_step()` 和 `judge_step()` 即可接入其他多模态模型。
- **扩展 BrowserController**：可替换底层工具，但必须保留 session、超时和终止语义。
- **增加断言类型**：在 `AssertionKind` 和 `_evaluate()` 中增加可视化、元素属性或网络状态断言。
- **增加领域技能**：建议通过通用能力实现表单填写、验证码、上传、弹窗和文件下载，不针对具体页面写死 ref。
- **增加持久化后端**：可将 SQLite TaskRepository 替换为 Redis/PostgreSQL，同时保持状态模型不变。

### 13. 当前边界

- 当前 Planner 以“单个自然语言步骤”为主要决策单元，复杂跨步骤循环仍需要进一步做显式子图。
- 验证码准确率取决于视觉模型和截图质量。
- `COMPLETED` 只表示 LangGraph 图执行结束，不代表测试通过；最终结论看 `verdict`。
- 页面中的敏感字段会在记录和报告中脱敏，但模型仍需要获得用例数据进行输入。

## 项目结构

```text
Lanus-Ui-test/
├── src/lanus_ui_test/
│   ├── engine.py          # LangGraph 图、节点、重试和状态流转
│   ├── planner.py         # 直接调用视觉模型的 Planner
│   ├── browser.py         # playwright-cli 安全适配器
│   ├── models.py          # TestState、步骤、断言和执行记录
│   ├── xlsx_reader.py     # Excel 用例解析
│   ├── repository.py      # SQLite 任务快照与暂停信号
│   ├── report.py          # JSON、Markdown、HTML 报告
│   ├── service.py         # 后台任务与并发管理
│   ├── api.py             # FastAPI 接口
│   └── cli.py             # 命令行入口
├── tests/                 # 单元与图执行测试
├── pyproject.toml
├── .env.example
└── README.md
```

## 用例格式

当前直接支持用户提供的 Excel 模板：

| 用例名 | 操作步骤 |
| --- | --- |
| 登录功能验证 | `1:进入登录页`<br>`2:输入账号：sample-user` |
| 交易计划功能验证 | `3:点击交易计划菜单`<br>`4:点击新增便签` |

要求：

- 至少包含“用例名”和“操作步骤”两列。
- 一行可以写多个编号步骤，也可以用多行续写同一条用例。
- 编号必须能表达执行顺序；条件文本（如“若失败则重试”）会原样交给规划模型。
- 可通过可选的“目标地址”列提供 URL，也可以在 API/CLI 请求中覆盖。

## 安装

前置条件：

- Python 3.10 或更高版本。
- Google Chrome。
- Node.js 22。
- 已安装 `@playwright/cli`。
- 可访问支持视觉输入的模型接口。

首次安装：

```bash
cd ~/Lanus-Ui-test
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e '.[dev]'
```

网络较慢时可以使用国内镜像：

```bash
python -m pip install \
  -i https://pypi.tuna.tsinghua.edu.cn/simple \
  -e '.[dev]'
```

本机 `playwright-cli` 需要 Node 22。默认自动使用：

```text
/Users/jiangxufeng1/.local/node-v22.23.2-darwin-x64/bin/node
/usr/local/lib/node_modules/@playwright/cli/playwright-cli.js
```

手工验证：

```bash
/Users/jiangxufeng1/.local/node-v22.23.2-darwin-x64/bin/node \
  /usr/local/lib/node_modules/@playwright/cli/playwright-cli.js \
  --version
```

## 模型后端

默认由 Lanus 进程直接调用支持视觉输入的 Responses API，不启动 Codex 或任何其他智能体子进程：

```bash
export LANUS_PLANNER_BACKEND=responses
export LANUS_LLM_BASE_URL=https://api.deepseek.com
export LANUS_LLM_MODEL=deepseek-v4-flash-vision-exp
export LANUS_LLM_API_KEY=...
```

本机未显式设置上述变量时，Lanus 只读获取本地模型供应商配置中的地址、模型和凭据；不会执行 Codex，也不会创建 Codex 会话。

也可以使用任意 OpenAI-compatible Chat Completions 接口：

```bash
export LANUS_PLANNER_BACKEND=openai_compatible
export LANUS_LLM_BASE_URL=http://127.0.0.1:11434/v1
export LANUS_LLM_MODEL=your-vision-model
export LANUS_LLM_API_KEY=optional
```

验证码和图片文字依赖视觉能力，建议选择支持图片输入的模型。

主要环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LANUS_PLANNER_BACKEND` | `responses` | `responses` 或 `openai_compatible` |
| `LANUS_LLM_BASE_URL` | 本地供应商配置 | 模型接口地址 |
| `LANUS_LLM_MODEL` | 本地供应商配置 | 模型名称 |
| `LANUS_LLM_API_KEY` | 本地供应商配置 | API 密钥 |
| `LANUS_LLM_TIMEOUT` | `120` | 单次模型请求超时秒数 |
| `LANUS_HEADLESS` | `true` | `false` 时默认打开可见浏览器 |
| `LANUS_MAX_CONCURRENCY` | `3` | API 后台任务并发数 |
| `LANUS_COMMAND_TIMEOUT` | `90` | 单条 playwright-cli 命令超时秒数 |
| `LANUS_OBSERVE_TIMEOUT` | `30` | snapshot/screenshot 超时秒数 |
| `LANUS_MAX_STEP_ATTEMPTS` | `3` | 单个业务步骤最大尝试次数 |
| `LANUS_MAX_PLANNER_REPAIRS` | `2` | Planner 结构错误修复次数 |
| `LANUS_MAX_ACTION_ROUNDS` | `8` | 单个业务步骤最大规划/动作轮数 |
| `LANUS_ARTIFACTS_DIR` | `~/Desktop/Lisa输出/Lanus-Ui-test` | 产物目录 |

## 启动执行

### 1. 启动被测应用

例如交易模型应用：

```bash
open ~/Desktop/Trading-model-web.app
curl -I "http://127.0.0.1:5000/login?next=/"
```

登录页返回 `200` 后再启动 Lanus。

### 2. 预览用例解析

```bash
cd ~/Lanus-Ui-test
source .venv/bin/activate

lanus-ui-test parse \
  "/Users/jiangxufeng1/Desktop/KNOWLEDGE/交易模型AI自动化测试用例版本1.xlsx"
```

### 3. 直接执行用例

```bash
lanus-ui-test run \
  "/Users/jiangxufeng1/Desktop/KNOWLEDGE/交易模型AI自动化测试用例版本1.xlsx" \
  --target-url http://127.0.0.1:5000 \
  --headed \
  --max-step-attempts 2 \
  --max-action-rounds 10
```

`run` 参数：

| 参数 | 说明 |
| --- | --- |
| `--target-url` | 覆盖 Excel 中提取的目标地址 |
| `--headed` | 显示 Chrome 窗口 |
| `--max-step-attempts` | 业务步骤最大尝试次数 |
| `--max-action-rounds` | 每步最大规划/动作轮数 |
| `--timeout` | CLI 等待整个任务的最大秒数 |

也可以使用环境变量设置默认值：

```bash
LANUS_MAX_STEP_ATTEMPTS=2 \
LANUS_MAX_ACTION_ROUNDS=10 \
lanus-ui-test run <用例文件> --headed
```

### 4. 通过 API 执行

启动 Lanus API：

```bash
lanus-ui-test serve --port 8765
```

提交本地用例路径：

```bash
curl -X POST http://127.0.0.1:8765/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "case_file": "/Users/jiangxufeng1/Desktop/KNOWLEDGE/交易模型AI自动化测试用例版本1.xlsx",
    "target_url": "http://127.0.0.1:5000",
    "headed": true,
    "max_step_attempts": 2
  }'
```

上传用例文件：

```bash
curl -X POST http://127.0.0.1:8765/api/tasks/upload \
  -F "file=@/Users/jiangxufeng1/Desktop/KNOWLEDGE/交易模型AI自动化测试用例版本1.xlsx" \
  -F "target_url=http://127.0.0.1:5000" \
  -F "headed=true" \
  -F "max_step_attempts=2"
```

查询和控制任务：

```bash
curl http://127.0.0.1:8765/api/tasks/<task_id>
curl -X POST http://127.0.0.1:8765/api/tasks/<task_id>/pause
curl -X POST http://127.0.0.1:8765/api/tasks/<task_id>/resume
curl -X POST http://127.0.0.1:8765/api/tasks/<task_id>/stop
curl http://127.0.0.1:8765/api/tasks/<task_id>/report
```

### 5. 生成和查看 HTML 报告

每次任务自动生成 `report.html`。也可以从已有 JSON 单独重建：

```bash
lanus-ui-test html \
  "/Users/jiangxufeng1/Desktop/Lisa输出/Lanus-Ui-test/tasks/<task_id>/reports/report.json"
```

打开报告：

```bash
open \
  "/Users/jiangxufeng1/Desktop/Lisa输出/Lanus-Ui-test/tasks/<task_id>/reports/report.html"
```

## 产物目录

```text
~/Desktop/Lisa输出/Lanus-Ui-test/tasks/<task_id>/
├── snapshots/
├── screenshots/
├── reports/
│   ├── report.json
│   ├── report.md
│   └── report.html
└── uploads/
```

## 执行状态

- `RUNNING`：图正在规划或执行。
- `RETRYING`：当前步骤正在进行恢复、重规划或重试。
- `PAUSED`：收到暂停信号，浏览器 session 保留。
- `STOPPED`：用户终止，浏览器 session 关闭。
- `COMPLETED`：图已经运行完成，最终测试结论仍需查看报告。

注意：`COMPLETED` 不等于测试 `PASS`。最终结论使用报告中的 `verdict`：

- `PASS`：所有步骤通过。
- `FAIL`：存在失败或被跳过步骤。
- `STOPPED` / `PAUSED`：任务未正常完成。

## 为什么执行较慢

一次 UI Agent 执行通常比普通脚本慢，主要耗时来自：

1. 每个动作轮都需要调用视觉模型。
2. 页面变化后需要采集 snapshot 和 full-page screenshot。
3. 验证码步骤可能需要截图、识图、刷新和重新识别。
4. `playwright-cli click` 默认可能等待最多 30 秒。
5. 失败步骤会按照 `--max-step-attempts` 和 `--max-action-rounds` 重试。

查看当前浏览器会话：

```bash
/Users/jiangxufeng1/.local/node-v22.23.2-darwin-x64/bin/node \
  /usr/local/lib/node_modules/@playwright/cli/playwright-cli.js \
  list
```

查看任务报告：

```bash
open \
  "/Users/jiangxufeng1/Desktop/Lisa输出/Lanus-Ui-test/tasks/<task_id>/reports/report.html"
```

## 安全边界

- 模型不能指定 session、配置文件、截图输出路径或持久化目录。
- 禁止任意 JavaScript、网络 mock、存储修改、文件上传和 shell 拼接。
- 密码、验证码等敏感输入在命令展示和输出中会被脱敏。
- `stop` 会终止当前 CLI 进程组并关闭对应浏览器 session；`pause` 保留 session。

## 测试

```bash
pytest
```
