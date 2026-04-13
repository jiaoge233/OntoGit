# xiaogugit

## 2026-04-09 更新

### 概率联调

- `write-and-infer` 会在主版本写入成功后调用概率服务。
- 概率只回填到当前工作区文件的 `probability` 字段。
- 概率回填不会再生成 `_inference/*.json` 文件，也不会因为概率回填新增一个版本。
- 最新工作区内容使用 `GET /read/{project_id}/{filename}` 读取；历史快照仍使用 `GET /version-read/{project_id}/{version_id}` 读取。

### 回滚语义

- `POST /version-rollback` 现在采用补偿式回滚。
- 回滚到某个版本时，会基于目标版本内容新生成一个回滚版本。
- 目标版本之后的历史节点不会被删除。

### 双轨推荐接口

- 官方推荐：
  `GET /version-recommend/official?project_id=<project_id>&filename=<filename>`
- 社区推荐：
  `GET /version-recommend/community?project_id=<project_id>&filename=<filename>`

当前规则：

- 官方推荐优先读取项目元数据中的显式配置；未配置时回退到最新版本。
- 社区推荐返回 `stars` 最高的版本；若并列则返回版本号更高的版本。

### 并发与交互

- 前端提交按钮已增加进行中禁用，减少重复点击。
- 后端 `write-and-infer` 已对同一 `project_id` 加串行锁，适用于单机单实例场景。
- 可视化页面新增“悬停预览节点内容”开关。
- 悬停最新节点时，预览读取当前工作区内容；悬停历史节点时，预览读取对应历史快照。

### 存储目录

- 默认 `XG_STORAGE_ROOT` 已统一到 `storage`。
- 当前约定使用 `storage/demo` 这套业务数据。

### 日志

- `xiaogugit` 接口请求日志现在统一带时间戳。
- `write-and-infer` 内部的概率调用摘要日志也统一使用带时间的 logger 输出。

一个基于 Git 的“数据版本管理”小服务：把每次写入的 JSON 数据落到本地仓库并自动提交，从而获得可追溯的版本历史、差异对比与回滚能力。

## 功能

- 按 `project_id` 自动初始化一个本地 Git 仓库（位于 `./storage/<project_id>`）
- 写入：把 `data`（JSON）写入指定 `filename` 并提交（commit author 使用 `agent_name`）
- 读取：读取当前版本或指定 `commit_id` 的历史版本
- 日志：查看该项目的提交历史
- Diff：对比两个提交之间某文件的差异
- 回滚：将某个提交之后的变更批量 revert（生成一次新的回滚提交）
- 路径安全校验：限制非法 `project_id` 和 `filename`，避免路径穿越

## 开发中功能

以下扩展能力已经预留设计，但当前版本暂未正式开放：

- 项目元数据管理：项目名称、描述、状态
- 项目总览：项目列表、项目详情、文件列表、单次提交详情
- 服务首页与健康检查接口

## 项目结构

```
.
├─ manager.py   # Git 版本管理核心逻辑（GitPython）
├─ server.py    # FastAPI HTTP API
└─ storage/     # 数据与本地 Git 仓库根目录（运行后自动生成）
```

`storage/test_project` 是一个示例/测试项目目录（其中包含自己的 `.git`）。

## 环境依赖

- Python 3.9+（推荐）
- 依赖库：见 `requirements.txt`

安装示例：

```bash
pip install -r requirements.txt
```

## 环境切换

项目现在支持通过 `XG_ENV` 一键切换 **开发环境** 和 **线上部署环境**。

默认规则：

- `development`：监听 `127.0.0.1`，默认存储目录 `storage/dev`，开放 `/docs`，开启自动重载
- `production`：监听 `0.0.0.0`，默认存储目录 `storage/prod`，关闭 `/docs`，关闭自动重载

推荐做法：

1. 先复制一份环境文件

```bash
cp .env.example .env
```

2. 只改 `.env` 里的一个值

```env
XG_ENV=development
```

或：

```env
XG_ENV=production
```

程序会自动读取：

- `.env`
- `.env.development` 或 `.env.production`

如果你还需要自定义端口、监听地址、存储目录，也可以在 `.env` 中覆盖这些变量：

- `XG_HOST`
- `XG_PORT`
- `XG_STORAGE_ROOT`
- `XG_DOCS_ENABLED`
- `XG_RELOAD`

## 启动

方式 1：直接运行（`server.py` 内置 uvicorn 启动）

```bash
python server.py
```

方式 2：命令行启动（推荐开发调试）

```bash
uvicorn server:app --host 127.0.0.1 --port 8000 --reload
```

启动后访问：

- 开发环境 API 文档：`http://127.0.0.1:8000/docs`
- 前端页面：`http://127.0.0.1:8000/ui`
- 健康检查：`http://127.0.0.1:8000/health`

线上部署环境默认关闭 `/docs`，你可以通过 `XG_DOCS_ENABLED=true` 临时开启。

## Docker 部署

已提供：

- `Dockerfile`
- `docker-compose.yml`
- `.dockerignore`

首次启动：

```bash
docker compose up --build -d
```

切换环境时，只需要修改 `.env`：

```env
XG_ENV=development
```

或：

```env
XG_ENV=production
```

然后重新启动容器：

```bash
docker compose up --build -d
```

说明：

- 容器内服务固定监听 `0.0.0.0:8000`
- 对外端口由 `.env` 中的 `XG_PORT` 控制，默认 `8000`
- 数据目录挂载为 `./storage:/app/storage`
- 开发环境默认使用 `/app/storage/dev`
- 生产环境默认使用 `/app/storage/prod`

## API 说明

说明：项目管理类接口当前已经可用；生产环境如果关闭 `/docs`，仍然可以通过前端或 HTTP 调用这些接口。

### 0) 服务状态（开发中，暂未开放）

- `GET /`：服务首页信息
- `GET /health`：健康检查

### 1) 初始化项目（开发中，暂未开放）

`POST /projects/init`

请求体（JSON）：

```json
{
  "project_id": "demo",
  "name": "发动机项目",
  "description": "课程作业演示项目",
  "status": "开发中"
}
```

### 2) 查询项目（开发中，暂未开放）

- `GET /projects`：查看全部项目
- `GET /projects/{project_id}`：查看单个项目详情
- `GET /projects/{project_id}/files`：查看项目中的文件列表
- `GET /projects/{project_id}/commits/{commit_id}`：查看某次提交详情

### 3) 写入并提交

`POST /write`

请求体（JSON）：

```json
{
  "project_id": "demo",
  "filename": "ontology.json",
  "data": { "hello": "world" },
  "message": "AI: update ontology",
  "agent_name": "agent-1"
}
```

返回示例：

```json
{ "status": "success", "commit_id": "..." }
```

当内容未变化时：

```json
{ "status": "no_change" }
```

### 4) 更新项目状态（开发中，暂未开放）

`POST /projects/status`

请求体（JSON）：

```json
{
  "project_id": "demo",
  "status": "测试中",
  "operator": "teacher"
}
```

支持状态：

- `开发中`
- `测试中`
- `已完成`
- `已暂停`
- `已归档`
- `已回滚`

### 5) 读取（支持历史版本）

`GET /read/{project_id}/{filename}`

- 读取最新：不带 `commit_id`
- 读取历史：加 query 参数 `commit_id`

示例：

```bash
curl "http://127.0.0.1:8000/read/demo/ontology.json"
curl "http://127.0.0.1:8000/read/demo/ontology.json?commit_id=<commit_sha>"
```

### 6) 日志

`GET /log/{project_id}`

示例：

```bash
curl "http://127.0.0.1:8000/log/demo"
```

### 7) Diff

`GET /diff?project_id=...&filename=...&base=...&target=...`

示例：

```bash
curl "http://127.0.0.1:8000/diff?project_id=demo&filename=ontology.json&base=<base_sha>&target=<target_sha>"
```

### 8) 回滚

`POST /rollback?project_id=...&commit_id=...`

示例：

```bash
curl -X POST "http://127.0.0.1:8000/rollback?project_id=demo&commit_id=<commit_sha>"
```

## 数据与版本存储规则

- 每个 `project_id` 对应一个目录：`<XG_STORAGE_ROOT>/<project_id>/`
- 该目录是一个独立 Git 仓库（含 `.git`）
- `filename` 是写入到该仓库根目录下的文件名（通常为 `*.json`）
- 项目会自动维护一个 `project_meta.json`，保存名称、描述、状态、更新时间等元数据

## 安全提示（重要）

当前版本已加入基础路径校验，但如果对外开放服务，仍建议继续增强：

- 增加更严格的文件名白名单
- 增加认证鉴权与访问控制
- 为同一项目写入增加并发锁

## License

未包含 License 文件；如需开源/分发，建议补充明确的许可证。
