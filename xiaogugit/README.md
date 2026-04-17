# xiaogugit

`xiaogugit` 是一个基于 Git 的本体 / JSON 数据版本管理服务。它把每次写入落到本地仓库并自动提交，从而提供版本链、版本读取、差异对比、补偿式回滚、推荐版本、点赞排行，以及基于 Redis 的本体检索加速能力。

服务基于 `FastAPI + GitPython` 实现，默认带登录页和多个可视化页面，适合作为单机部署的轻量级版本管理后端。

## 当前能力

- 按 `project_id` 自动初始化独立 Git 仓库
- 对指定 `filename` 写入 JSON 并生成版本
- 读取最新工作区内容，或按 `commit_id` / `version_id` 读取历史快照
- 查询项目、文件、提交、版本树、时间线
- 对比提交差异或版本差异
- 补偿式回滚到指定提交或指定版本
- 删除当前文件，或彻底清除该文件全部历史
- 版本点赞 / 取消点赞
- 官方推荐、社区推荐、社区排行榜
- 本体名称解析：支持精确匹配、前缀匹配和 Redis 缓存加速
- `write-and-infer` 联动概率服务，并将 `probability` 回填到当前工作区文件

## 最近行为约定

- `POST /write-and-infer` 在主版本写入成功后调用概率服务
- 概率结果只更新当前工作区文件的 `probability` 字段，不额外创建 `_inference/*.json`
- 概率回填不会新增一个 Git 版本
- `POST /version-rollback` 采用补偿式回滚：会新建一个“回滚版本”，不会删除目标版本之后的历史
- 同一 `project_id` 的 `write-and-infer`、官方推荐设置 / 清除接口在单进程内使用串行锁

## 项目结构

```text
.
├─ manager.py                 # Git 版本管理与 Redis 索引核心逻辑
├─ server.py                  # FastAPI HTTP API
├─ config.py                  # 环境变量与配置加载
├─ inference_client.py        # 概率推理服务客户端
├─ frontend*.html             # 多套前端页面
├─ login.html                 # 登录页
├─ requirements.txt           # Python 依赖
├─ Dockerfile
├─ docker-compose.yml
└─ storage/                   # 数据根目录（运行后生成 dev / prod / project 仓库）
```

## 运行环境

- Python 3.9+
- Git 可执行程序可用
- 推荐使用虚拟环境

安装依赖：

```bash
pip install -r requirements.txt
```

依赖列表：

- `fastapi`
- `uvicorn`
- `GitPython`
- `pydantic`
- `redis`

说明：

- `redis` 依赖已包含在 `requirements.txt` 中
- 即使未启用 Redis，服务也可以正常运行；此时相关能力会退回文件系统 / Git 扫描

## 配置说明

服务会按以下顺序合并配置：

1. `.env`
2. `.env.development` 或 `.env.production`
3. 系统环境变量

先复制一份示例配置：

```bash
cp .env.example .env
```

Windows PowerShell 可用：

```powershell
Copy-Item .env.example .env
```

### 环境模式

- `XG_ENV=development`
  - 默认监听 `127.0.0.1:8000`
  - 默认存储目录 `storage/dev`
  - 默认开启 `/docs`
  - 默认开启 `reload`
- `XG_ENV=production`
  - 默认监听 `0.0.0.0:8000`
  - 默认存储目录 `storage/prod`
  - 默认关闭 `/docs`
  - 默认关闭 `reload`

### 常用配置项

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `XG_ENV` | 运行环境 | `development` |
| `XG_HOST` | 监听地址 | 环境相关 |
| `XG_PORT` | 监听端口 | `8000` |
| `XG_STORAGE_ROOT` | 存储根目录 | 环境相关 |
| `XG_DOCS_ENABLED` | 是否启用 `/docs` | 环境相关 |
| `XG_RELOAD` | 是否启用热重载 | 环境相关 |
| `XG_AUTH_SECRET` | 登录 token 签名密钥 | `xiaogugit-auth-secret` |
| `XG_AUTH_COOKIE_NAME` | 登录 cookie 名称 | `xg_session` |
| `XG_AUTH_USERNAME` | 登录用户名 | `mogong` |
| `XG_AUTH_PASSWORD` | 登录密码 | `123456` |
| `XG_INFERENCE_URL` | 概率服务地址 | `http://127.0.0.1:5000/api/llm/probability-reason` |
| `XG_INFERENCE_TIMEOUT` | 概率服务超时秒数 | `10` |
| `XG_REDIS_ENABLED` | 是否启用 Redis 缓存 | `false` |
| `XG_REDIS_HOST` | Redis 地址 | `127.0.0.1` |
| `XG_REDIS_PORT` | Redis 端口 | `6379` |
| `XG_REDIS_PASSWORD` | Redis 密码 | 空 |
| `XG_REDIS_DB` | Redis DB | `0` |
| `XG_REDIS_KEY_PREFIX` | Redis key 前缀 | `xg` |
| `XG_REDIS_SOCKET_TIMEOUT` | Redis 超时秒数 | `1.5` |

## 快速启动

### 方式 1：直接运行

```bash
python server.py
```

### 方式 2：使用 uvicorn

```bash
uvicorn server:app --host 127.0.0.1 --port 8000 --reload
```

启动后常用地址：

- 登录页：`http://127.0.0.1:8000/login`
- 可视化页面：`http://127.0.0.1:8000/ui-visual`
- 经典页面：`http://127.0.0.1:8000/ui`
- 健康检查：`http://127.0.0.1:8000/health`
- 开发环境文档：`http://127.0.0.1:8000/docs`

说明：

- 除 `/login`、`/auth/login`、`/auth/logout`、`/health` 与文档相关路径外，其余接口默认需要登录
- 登录成功后，服务会同时返回 `Bearer access_token`，并写入 HttpOnly Cookie

## Docker 部署

首次启动：

```bash
docker compose up --build -d
```

当前 `docker-compose.yml` 行为：

- 容器内服务监听 `0.0.0.0:8000`
- 对外映射 `${XG_PORT:-8000}:8000`
- 挂载 `./storage:/app/storage`
- 读取 `.env`
- 启动后通过 `/health` 做健康检查

如需切换开发 / 生产环境，只需修改 `.env` 中的 `XG_ENV` 后重新执行：

```bash
docker compose up --build -d
```

## 认证说明

默认账号来自环境变量：

```env
XG_AUTH_USERNAME=mogong
XG_AUTH_PASSWORD=123456
```

登录请求：

```bash
curl -X POST "http://127.0.0.1:8000/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"mogong\",\"password\":\"123456\"}"
```

成功后可以：

- 使用响应里的 `access_token` 走 `Authorization: Bearer <token>`
- 或复用服务写入的登录 Cookie

## Redis 加速说明

启用 `XG_REDIS_ENABLED=true` 后，服务会把部分读模型缓存到 Redis，包括：

- 官方推荐配置缓存
- 社区推荐版本分数与排行榜
- 版本摘要缓存
- 本体解析索引

本体解析索引包含三层：

- `catalog`：文件候选摘要
- `lookup`：唯一 alias 的精确匹配
- `prefix`：alias 每个前缀对应的文件集合

查询 `/ontology-resolve` 时，优先走 Redis 精确 / 前缀索引；未命中时会退回 catalog 或文件扫描。写入和删除文件后，会触发项目级本体索引刷新。

## 核心数据模型

### 项目

每个 `project_id` 对应一个目录：

```text
<XG_STORAGE_ROOT>/<project_id>/
```

该目录本身就是一个独立 Git 仓库，并会维护：

- 业务 JSON 文件
- `project_meta.json` 项目元数据
- `.git/`

点赞数据会额外保存在：

```text
<XG_STORAGE_ROOT>/.xg_meta/<project_id>_version_stars.json
```

### 版本

每次 `write_version` 会：

1. 将 JSON 写入工作区文件
2. 将文件加入 Git
3. 生成一次提交
4. 在提交信息中写入内部元数据，如文件名、版本号、基线版本号、对象名、提交人

因此系统同时支持：

- Git commit 维度查询
- 业务 version_id 维度查询

## 主要接口

以下为当前实现中已开放的主要接口。

### 服务与认证

- `GET /`
- `GET /health`
- `GET /login`
- `POST /auth/login`
- `POST /auth/logout`
- `GET /auth/me`

### 项目

- `POST /projects/init`
- `GET /projects`
- `GET /projects/{project_id}`
- `POST /projects/status`
- `GET /projects/{project_id}/files`
- `GET /projects/{project_id}/commits/{commit_id}`

### 版本 / 时间线 / 检索

- `GET /ontology-resolve`
- `GET /versions/{project_id}`
- `GET /versions/{project_id}/{filename:path}`
- `GET /timelines/{project_id}`
- `GET /version-detail/{project_id}/{version_id}`
- `GET /version-read/{project_id}/{version_id}`

### 写入 / 删除 / 回滚

- `POST /write`
- `POST /write-and-infer`
- `POST /delete`
- `GET /read/{project_id}/{filename}`
- `GET /log/{project_id}`
- `GET /diff`
- `GET /version-diff`
- `POST /rollback`
- `POST /version-rollback`

### 推荐 / 点赞

- `POST /version-star`
- `POST /version-unstar`
- `GET /version-recommend/official`
- `POST /version-recommend/official/set`
- `POST /version-recommend/official/clear`
- `GET /version-recommend/official/history`
- `GET /version-recommend/community`
- `GET /version-recommend/community/history`
- `GET /community-leaderboard`

## 常见调用示例

### 1. 初始化项目

```bash
curl -X POST "http://127.0.0.1:8000/projects/init" \
  -H "Content-Type: application/json" \
  -d "{
    \"project_id\": \"demo\",
    \"name\": \"演示项目\",
    \"description\": \"用于接口联调\",
    \"status\": \"开发中\"
  }"
```

### 2. 写入一个新版本

`basevision` 很重要：

- 首次写入必须传 `0`
- 后续写入通常传当前最新版本号

```bash
curl -X POST "http://127.0.0.1:8000/write" \
  -H "Content-Type: application/json" \
  -d "{
    \"project_id\": \"demo\",
    \"filename\": \"ontology.json\",
    \"data\": {\"name\": \"发动机本体\", \"nodes\": []},
    \"message\": \"AI: create ontology\",
    \"agent_name\": \"agent-1\",
    \"committer_name\": \"teacher\",
    \"basevision\": 0
  }"
```

### 3. 写入并联动概率服务

```bash
curl -X POST "http://127.0.0.1:8000/write-and-infer" \
  -H "Content-Type: application/json" \
  -d "{
    \"project_id\": \"demo\",
    \"filename\": \"ontology.json\",
    \"data\": {\"name\": \"发动机本体\", \"nodes\": []},
    \"message\": \"AI: update ontology\",
    \"agent_name\": \"agent-1\",
    \"committer_name\": \"teacher\",
    \"basevision\": 1
  }"
```

返回中的 `probability_update_result` 仅表示工作区字段回填结果，不会新增版本。

### 4. 读取最新内容与历史版本

```bash
curl "http://127.0.0.1:8000/read/demo/ontology.json"
curl "http://127.0.0.1:8000/version-read/demo/2?filename=ontology.json"
```

### 5. 查看版本树

```bash
curl "http://127.0.0.1:8000/versions/demo/ontology.json?min_stars=0&sort_by=version&order=asc"
```

可选排序：

- `sort_by=version|stars`
- `order=asc|desc`

### 6. 按名称解析本体

```bash
curl "http://127.0.0.1:8000/ontology-resolve?project_id=demo&query=发动机"
```

### 7. 对比两个版本

```bash
curl "http://127.0.0.1:8000/version-diff?project_id=demo&base_version_id=1&target_version_id=2&filename=ontology.json"
```

### 8. 回滚到指定版本

```bash
curl -X POST "http://127.0.0.1:8000/version-rollback?project_id=demo&version_id=1&filename=ontology.json"
```

### 9. 设置官方推荐版本

```bash
curl -X POST "http://127.0.0.1:8000/version-recommend/official/set" \
  -H "Content-Type: application/json" \
  -d "{
    \"project_id\": \"demo\",
    \"filename\": \"ontology.json\",
    \"version_id\": 2,
    \"operator\": \"teacher\",
    \"reason\": \"课堂评审通过\"
  }"
```

### 10. 点赞某个版本

```bash
curl -X POST "http://127.0.0.1:8000/version-star" \
  -H "Content-Type: application/json" \
  -d "{
    \"project_id\": \"demo\",
    \"filename\": \"ontology.json\",
    \"version_id\": 2,
    \"increment\": 1
  }"
```

## 删除与回滚语义

### 删除

`POST /delete` 默认 `purge_history=true`，表示彻底清除该文件历史。

如果你希望“保留历史，只在当前版本删除文件”，需要显式传：

```json
{
  "purge_history": false
}
```

两种行为区别：

- `purge_history=true`：使用 Git 历史重写彻底移除文件
- `purge_history=false`：创建一个“删除版本”，历史仍然保留

### 回滚

- `POST /rollback?project_id=...&commit_id=...`：按提交回滚
- `POST /rollback?project_id=...&version_id=...`：按版本回滚
- `POST /version-rollback`：更明确的按版本回滚接口

版本回滚是补偿式的，会生成一个新版本。

## 安全与限制

当前已实现：

- `project_id` 只允许字母、数字、下划线、短横线
- `filename` 禁止路径穿越
- `filename` 禁止写入 `.git`
- 默认启用登录鉴权

当前仍建议注意：

- 默认账号密码仅适合本地开发，请在部署时修改
- 登录 token 为服务内自签名简化方案，不是标准 OAuth / JWT
- 同项目串行锁仅适用于单机单进程场景
- `purge_history=true` 会改写 Git 历史，需谨慎使用

## 开发建议

- 开发环境优先使用 `XG_ENV=development`
- 联调推荐先打开 `/docs`
- 若要验证 Redis 加速，先启用 `XG_REDIS_ENABLED=true`
- 若要验证推理联调，确保 `XG_INFERENCE_URL` 可访问

## 2026-04-17 更新记录

### 概率推理与当前版本回填

`POST /write-and-infer` 现在的完整行为是：

- 先按正常写入逻辑创建或确认当前版本。
- 再调用 `probability` 服务获取概率推理结果。
- 只把 `probability` 字段写回本体 JSON，不写入 `reason`。
- 概率字段会合入当前版本，不会额外创建一个新的版本号。
- 如果当前提交已经生成，概率写回会通过 `git commit --amend --no-edit` 修正当前提交内容，因此版本号不变，但 Git commit hash 会变化。
- 如果业务 JSON 内容未变化，但当前本体缺少 `probability` 字段，也会触发概率推理并补写到当前版本。

### 概率推理重试

`xiaogugit` 调用概率服务时增加了自动重试，避免概率服务或模型网关偶发失败导致整条写入链路失败。

可配置项：

```env
XG_INFERENCE_RETRY_ATTEMPTS=3
XG_INFERENCE_RETRY_BACKOFF_SECONDS=0.5
```

默认重试 3 次，退避时间按 `0.5s, 1.0s, 2.0s...` 递增。

### 推荐双轨与 Redis 缓存

版本推荐现在分为两条轨道：

- 官方推荐：由接口显式设置或清除。
- 社区推荐：按版本 stars 排序，返回星标最高版本。

Redis 启用后会缓存以下内容：

- 官方推荐结果。
- 社区版本 stars 有序集合。
- 版本摘要。
- 本体名称解析索引。

Redis 不可用时会自动降级到当前文件 / Git 扫描实现，不影响基础功能。

### 节点悬停预览

可视化页面支持鼠标悬停节点时显示本体原始内容卡片。该功能由前端开关控制，默认开启。

## License

仓库当前未包含 License 文件。如需开源或对外分发，建议补充明确许可证。
