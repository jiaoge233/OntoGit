# agent

`agent` 目录提供了两个面向上层业务的智能能力：

- `Git Query Agent`：把自然语言问题转换成对 `gateway` / `xiaogugit` 的查询，再生成中文答案
- `Data Warehouse Agent`：对输入的 JSON 上下文做治理分析，输出结构化决策结果

这部分代码本身不保存业务数据，也不直接操作 Git 仓库；它主要负责：

- 调用大模型
- 调用 `gateway` 暴露的 HTTP 接口
- 把工具结果整理成更适合问答或治理决策的输出

## 目录结构

```text
.
├─ .env.example              # agent 本地环境变量示例
├─ git_query_tools.py       # Git 查询工具定义与 GatewayClient
├─ git_query_agent.py       # 自然语言查询 Agent（规划 + 调工具 + 生成答案）
├─ run_git_query_agent.py   # Git Query Agent CLI 入口
├─ run_git_tool.py          # 单工具直调 CLI 入口
├─ requirements.txt         # agent 目录依赖
├─ warehouse_agent.py       # 数据仓库治理 Agent
└─ frontend_agent.html      # Agent 控制台原型页面
```

## 依赖关系

这部分代码依赖两个外部能力：

1. 大模型服务
通常通过兼容 OpenAI SDK 的接口访问，默认读取：

- `DMXAPI_API_KEY`
- `DMXAPI_BASE_URL`
- `DMXAPI_MODEL`

2. Gateway 服务
用于转发和聚合 `xiaogugit` 的查询接口，默认读取：

- `GATEWAY_BASE_URL`
- `GATEWAY_SERVICE_API_KEY`
- `GATEWAY_BEARER_TOKEN`

因此，想让 `agent` 正常工作，通常需要先启动：

- `xiaogugit`
- `gateway`

## 环境变量加载顺序

`agent` 会同时尝试读取自身和 `xiaogugit` 的环境文件，合并优先级大致如下：

1. `xiaogugit/.env`
2. `xiaogugit/.env.development` 或 `xiaogugit/.env.production`
3. `agent/.env`
4. `agent/.env.development` 或 `agent/.env.production`
5. 系统环境变量

其中环境模式由 `XG_ENV` 控制：

- `development`
- `production`

## 推荐配置

先复制示例环境文件：

```bash
cp .env.example .env
```

Windows PowerShell 可用：

```powershell
Copy-Item .env.example .env
```

然后按需修改 `agent/.env`：

```env
XG_ENV=development

DMXAPI_API_KEY=your_api_key
DMXAPI_BASE_URL=https://www.dmxapi.cn/v1
DMXAPI_MODEL=gpt-5.4

GATEWAY_BASE_URL=http://127.0.0.1:8080
GATEWAY_SERVICE_API_KEY=
GATEWAY_BEARER_TOKEN=
```

说明：

- 如果 `gateway` 走登录鉴权，可以不填 `GATEWAY_BEARER_TOKEN`，转而在 CLI 中传 `--username` / `--password`
- 如果 `gateway` 支持服务间 API Key，也可以直接配置 `GATEWAY_SERVICE_API_KEY`
- `warehouse_agent.py` 和 `git_query_agent.py` 都依赖 `DMXAPI_API_KEY`

## Python 依赖

当前目录已经提供独立的 `requirements.txt`，按代码实际依赖安装即可：

```bash
pip install -r requirements.txt
```

如果你打算联调整套系统，还需要安装并运行：

- `xiaogugit` 目录中的依赖
- `gateway` 目录中的依赖

## 5 分钟快速开始

如果你只是想先把 Agent 跑起来，可以按下面顺序：

1. 启动 `xiaogugit`
2. 启动 `gateway`
3. 在当前目录执行 `pip install -r requirements.txt`
4. 复制 `.env.example` 为 `.env`，填入 `DMXAPI_API_KEY`
5. 确认 `GATEWAY_BASE_URL` 指向可访问的网关
6. 执行：

```bash
python run_git_query_agent.py "学校当前社区推荐版本是什么？" --project-id demo
```

如果你的 Gateway 需要登录，再补上：

```bash
python run_git_query_agent.py "student 的官方推荐版本是什么？" --project-id demo --username mogong --password 123456
```

## Git Query Agent

`Git Query Agent` 的执行流程是：

1. 读取用户问题
2. 让大模型从当前工具集中选一个最合适的 tool
3. 自动补齐参数
4. 调用 `gateway`
5. 基于工具结果再生成一段简洁中文答案

### 当前内置工具

目前 `git_query_tools.py` 里只注册了两个工具：

- `get_community_top_version`
- `get_official_recommendation`

它们都支持：

- 直接传 `filename`
- 或传 `ontology_name`，再通过 `/xg/ontology-resolve` 解析到真实文件

也就是说，这个 Agent 当前最适合回答的问题是：

- “学校这个本体当前社区推荐的是哪个版本？”
- “student 的官方推荐版本是什么？”
- “demo 项目里老师本体现在推荐哪一版？”

暂时不适合回答所有通用 Git 问题；如果工具集无法回答，Agent 会返回 `unsupported`。

### CLI 用法

入口文件：

```bash
python run_git_query_agent.py "学校当前推荐版本是什么？" --project-id demo
```

常见参数：

- `question`：自然语言问题，必填
- `--project-id`：默认项目 ID
- `--filename`：默认文件名，弱提示
- `--base-url`：Gateway 地址
- `--api-key`：Gateway 服务 API Key
- `--bearer-token`：Gateway Bearer Token
- `--username` / `--password`：先登录 Gateway 再查询
- `--include-raw`：返回原始 LLM 响应

示例 1：使用默认配置

```bash
python run_git_query_agent.py "学校当前社区推荐版本是什么？" --project-id demo
```

示例 2：先登录再问答

```bash
python run_git_query_agent.py "student 的官方推荐版本是什么？" \
  --project-id demo \
  --username mogong \
  --password 123456
```

输出为 JSON，核心字段通常包括：

- `status`
- `question`
- `plan`
- `tool_result`
- `answer`

`plan` 的典型结构如下：

```json
{
  "tool_name": "get_official_recommendation",
  "arguments": {
    "project_id": "demo",
    "ontology_name": "学校"
  },
  "reason": "用户在查询某个本体的官方推荐版本"
}
```

## 单工具直调

如果你不想经过“自然语言规划 -> 选工具 -> 生成答案”这一步，可以直接调用工具。

入口文件：

```bash
python run_git_tool.py get_official_recommendation --project-id demo --filename student.json
```

支持的 `tool_name`：

- `get_community_top_version`
- `get_official_recommendation`

示例：

```bash
python run_git_tool.py get_community_top_version --project-id demo --filename school.json
```

也可以先登录：

```bash
python run_git_tool.py get_official_recommendation \
  --project-id demo \
  --filename teacher.json \
  --username mogong \
  --password 123456
```

注意：

- 当前 `run_git_tool.py` 的 CLI 只暴露了 `--filename`，没有直接暴露 `--ontology-name`
- 如果你需要基于本体名调用，可以直接在 Python 中调用 `run_tool(...)` 或使用 `run_git_query_agent.py`

### Python 直调示例

如果你想跳过 CLI，也可以直接在代码中调用：

```python
from git_query_tools import GatewayClient, run_tool

client = GatewayClient()
result = run_tool(
    name="get_official_recommendation",
    arguments={
        "project_id": "demo",
        "ontology_name": "学校"
    },
    client=client,
)

print(result)
```

## Data Warehouse Agent

`warehouse_agent.py` 提供的是一个“治理分析 Agent”。它接收 JSON 上下文，输出结构化治理建议，而不是自然语言自由文本。

### 输出结构

它会尽量返回如下 JSON：

```json
{
  "summary": "中文摘要",
  "risks": ["风险1"],
  "suggested_actions": ["动作1"],
  "affected_objects": ["对象1"],
  "decision": "observe"
}
```

其中 `decision` 只会是以下之一：

- `observe`
- `review`
- `recommend_official`
- `recompute`

### Python 调用示例

当前仓库里没有单独的 CLI 包装脚本，推荐直接在 Python 中调用：

```python
from warehouse_agent import DataWarehouseAgent

agent = DataWarehouseAgent()
result = agent.analyze(
    {
        "project_id": "demo",
        "ontology_name": "学校",
        "community_recommendation": {"version_id": 3, "stars": 12},
        "official_recommendation": {"version_id": 2},
        "probability": "0.81"
    }
)

print(result)
```

返回结果通常包含：

- `model`
- `status`
- `analysis`
- `text`

如果设置 `include_raw=True`，还会附带大模型原始响应。

## GatewayClient

`git_query_tools.py` 中的 `GatewayClient` 是这部分代码访问后端服务的统一入口，支持：

- `GET` / `POST` JSON 请求
- Bearer Token
- API Key
- 用户名密码登录后自动保存 token

默认地址：

```text
http://127.0.0.1:8080
```

如果连接失败，通常是以下原因：

- `gateway` 没启动
- 地址或端口不对
- 鉴权配置不匹配

它支持三种常见鉴权方式：

- 预先配置 `GATEWAY_BEARER_TOKEN`
- 预先配置 `GATEWAY_SERVICE_API_KEY`
- 在 CLI 中用 `--username` / `--password` 先登录再获取 token

## 本体名解析能力

`git_query_tools.py` 内置了两层对象解析：

1. 别名扩展
内置了少量中英文映射，例如：

- `student` <-> `学生`
- `teacher` <-> `老师` / `教师`
- `school` <-> `学校`

2. 通过 Gateway 调用 `/xg/ontology-resolve`
把 `ontology_name` 解析成真实的 `filename`

因此，在自然语言问答场景中，用户更适合提“学校”“老师”“student”这类对象名，而不是必须记住确切文件名。

## frontend_agent.html

`frontend_agent.html` 是一个前端控制台原型页面，适合作为后续 Agent UI 的基础模板。当前目录下没有配套的 Python 服务脚本专门托管它；通常可以：

- 直接在浏览器中打开
- 或放到现有 Web 服务中托管

README 当前将它视为原型资源，而不是已完整接线的生产页面。

## 与其它目录的关系

- `agent`：负责大模型调用、规划和问答整合
- `gateway`：负责统一暴露对外 API，并转发 / 聚合后端能力
- `xiaogugit`：负责版本存储、版本树、推荐、点赞、本体解析等底层能力

可以简单理解为：

```text
用户问题 -> agent -> gateway -> xiaogugit
```

`warehouse_agent.py` 则更偏离线治理分析，它不依赖固定某个查询工具，但通常仍会消费来自业务侧或后端侧整理好的 JSON 上下文。

## 常见问题

### 1. 报错 `DMXAPI_API_KEY is not configured`

说明大模型密钥没有配置。请在 `agent/.env` 或系统环境变量中设置 `DMXAPI_API_KEY`。

### 2. 报错 `failed to connect to gateway`

说明 `gateway` 服务不可达。请确认：

- `gateway` 已启动
- `GATEWAY_BASE_URL` 正确
- 本机端口未被占用

### 3. 问答返回 `unsupported`

说明当前工具集不足以回答这个问题。当前 Git Query Agent 只覆盖了“官方推荐 / 社区推荐”相关查询，不是通用知识库问答。

## 后续扩展建议

如果你准备继续完善这个目录，优先级建议如下：

1. 为 `warehouse_agent.py` 增加 CLI 入口
2. 为 `run_git_tool.py` 增加 `--ontology-name` 参数
3. 扩展更多 query tools，例如版本详情、差异、时间线、官方历史、社区排行
4. 给 `frontend_agent.html` 接入真实后端接口
5. 为工具执行链补充自动化测试

## 2026-04-17 更新记录

### Git Query Agent 最小闭环

当前已经形成可用链路：

```text
用户自然语言问题 -> run_git_query_agent.py -> git_query_agent.py -> git_query_tools.py -> gateway -> xiaogugit
```

Gateway 可通过 `X-API-Key` 完成服务调用鉴权，因此 CLI 和前端都不需要每次手动登录。

### 当前工具集

`git_query_tools.py` 目前支持以下工具：

- `get_community_top_version`：查询社区星标最高版本。
- `get_official_recommendation`：查询官方推荐版本。
- `get_file_timeline`：查询某个本体文件的版本时间线。
- `get_version_content`：查询指定版本的本体内容。
- `compare_versions`：对比两个版本的差异。
- `find_governance_gaps`：检查治理缺口，例如缺官方推荐、缺社区推荐、缺概率字段等。

工具参数既支持 `filename`，也支持 `ontology_name`。当用户输入“学校 / school / student”等本体名时，会优先通过 Gateway 的 `/xg/ontology-resolve` 解析到真实文件名；Redis 启用时解析会走 Redis 索引，Redis 不可用时降级到后端扫描。

### Agent 前端

Agent 查询台页面已提供暗黑科技风格前端，并通过 Gateway 挂载到：

```text
http://127.0.0.1:8080/ui-agent
```

页面可以输入自然语言问题，提交到 Agent 链路并展示最终回答、工具计划和工具原始结果。

### Redis 加速关系

Agent 本身不直接读写 Redis。它通过 Gateway 调用 `xiaogugit`，由 `xiaogugit` 负责 Redis 缓存和降级：

- 社区星标最高版本优先走 Redis 有序集合。
- 官方推荐优先走 Redis 缓存。
- 本体名称解析优先走 Redis 索引。
- Redis 不可用时自动回退到当前文件 / Git 扫描。

