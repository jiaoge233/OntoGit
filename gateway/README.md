# Data Infra Gateway

`gateway` 是当前数据中台的统一入口层，负责：

- 聚合中台总览页面
- 统一浏览器登录入口
- 统一服务调用入口
- 统一暴露 `xiaogugit` 和 `probability` 的路由
- 汇总基础健康检查和 dashboard 聚合数据

## 目录

- `main.go`: 网关主程序
- `route_catalog.go`: 统一路由目录定义
- `frontend_login.html`: 网关登录页
- `frontend_dashboard.html`: 中台总览页
- `API.md`: 对外接口说明
- `Dockerfile`: 网关镜像构建
- `docker-compose.yml`: 网关单独部署配置

## 对外入口

- 根路径：`/`
- 登录页：`/login`
- 中台页：`/ui-dashboard`
- 路由目录：`/api/routes`
- Dashboard 聚合接口：`/api/dashboard/summary`
- XiaoGuGit 统一入口：`/xg/*`
- Probability 统一入口：`/probability/*`

## 鉴权模型

### 浏览器会话

浏览器访问流程：

1. 打开 `/login`
2. 登录成功后默认跳转 `/ui-dashboard`
3. 后续页面请求通过 cookie 或 bearer 透传到下游

### 服务调用

服务端脚本有两种方式：

- 直接带 `Authorization: Bearer <token>`
- 带 `X-API-Key: <key>`

当请求带 `X-API-Key` 且匹配时，gateway 会自动生成下游 `xiaogugit` 可识别的 Bearer token，因此不需要先手动登录。

`/xg/*`、`/probability/*`、`/api/dashboard/summary` 和 `/api/agent/query` 现在都先由 gateway 统一鉴权；`/health`、`/api/routes`、`/login`、`/auth/login` 以及 `/probability/health` 保持公开。

## 环境变量

Gateway 使用和下游服务一致的分层配置：`.env` 只负责选择 `GATEWAY_ENV`，然后加载 `.env.development` 或 `.env.production`，最后由系统环境变量覆盖。

```env
GATEWAY_ENV=development
```

切到生产环境只需要改成：

```env
GATEWAY_ENV=production
```

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GATEWAY_PORT` | `8080` | compose 暴露端口 |
| `GATEWAY_ADDR` | `:8080` | gateway 监听地址 |
| `GATEWAY_XIAOGUGIT_URL` | `http://127.0.0.1:8000` | `xiaogugit` 上游地址 |
| `GATEWAY_PROBABILITY_URL` | `http://127.0.0.1:5000` | `probability` 上游地址 |
| `GATEWAY_SERVICE_API_KEY` | `change-me` | 服务调用 API Key |
| `GATEWAY_XG_AUTH_SECRET` | 继承 `XG_AUTH_SECRET` 或默认值 | 生成 `xiaogugit` 兼容 token 的签名密钥 |
| `GATEWAY_XG_AUTH_USERNAME` | `mogong` | 服务调用场景下注入的用户名 |
| `GATEWAY_AGENT_DIR` | 自动推断；容器内为 `/app/agent` | Agent 脚本目录 |
| `DMXAPI_API_KEY` | 空 | Gateway 容器内运行 Agent 时传给 Agent 的模型密钥 |
| `DMXAPI_BASE_URL` | `https://www.dmxapi.cn/v1` | Gateway 容器内运行 Agent 时传给 Agent 的模型服务地址 |
| `DMXAPI_MODEL` | `gpt-5.4` | Gateway 容器内运行 Agent 时传给 Agent 的模型名称 |

示例见 [`./.env.example`](./.env.example)。

## 本地启动

### 直接运行

```powershell
cd "C:\Users\gaozh\Desktop\data infra\gateway"
$env:GATEWAY_ADDR=":8080"
$env:GATEWAY_XIAOGUGIT_URL="http://127.0.0.1:8000"
$env:GATEWAY_PROBABILITY_URL="http://127.0.0.1:5000"
.\gateway.exe
```

如果本地重新编译：

```powershell
$env:GOTELEMETRY="off"
go build .
.\gateway.exe
```

## Docker

### 构建镜像

```powershell
cd "C:\Users\gaozh\Desktop\data infra"
docker build -f gateway/Dockerfile -t data-infra-gateway .
```

### 使用 compose 启动

```powershell
cd "C:\Users\gaozh\Desktop\data infra\gateway"
docker compose up --build
```

默认 compose 假设宿主机上已经有：

- `xiaogugit` 在 `http://host.docker.internal:8000`
- `probability` 在 `http://host.docker.internal:5000`
- Agent 作为脚本依赖打包在 Gateway 镜像内，不需要单独启动容器

如果要接容器内服务，改这两个环境变量即可：

- `GATEWAY_XIAOGUGIT_URL`
- `GATEWAY_PROBABILITY_URL`

如果要在容器内使用 `/ui-agent` 或 `POST /api/agent/query`，需要给 gateway 容器提供 `DMXAPI_API_KEY`。compose 会把该环境变量传给容器内的 Agent 脚本。

## 核心能力

### 1. 聚合 Dashboard

`GET /api/dashboard/summary` 当前会聚合：

- 项目列表
- 每个项目的时间线
- 每个文件的当前内容
- `xiaogugit` 健康状态
- `probability` 健康状态

### 2. 统一路由目录

`GET /api/routes` 返回当前 gateway 已归档的接口目录，便于前端和外部系统统一对接。

### 3. 上游透传

- `/auth/*` -> `xiaogugit`
- `/xg/*` -> `xiaogugit`
- `/probability/*` -> `probability`

此外，根路径下未命中的请求会继续透传给 `xiaogugit`，用于兼容已有页面和旧入口。

## 常用测试命令

### 网关健康检查

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8080/health"
```

### 获取 dashboard 数据

```powershell
Invoke-RestMethod `
  -Method Get `
  -Uri "http://127.0.0.1:8080/api/dashboard/summary" `
  -Headers @{ "X-API-Key" = "change-me" }
```

### 获取统一路由目录

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8080/api/routes"
```

更多接口见 [`./API.md`](./API.md)。

## 2026-04-17 更新记录

### 中台总览与页面入口

Gateway 现在作为数据中台统一入口，已挂载以下页面：

- `/login`：统一登录入口。
- `/ui-dashboard`：数据中台总览页面。
- `/ui-agent`：Agent 查询台页面。
- `/probability/*`：代理到 probability 前端和接口。
- `/xg/*`：代理到 xiaogugit API。

中台总览接口 `GET /api/dashboard/summary` 会聚合：

- `xiaogugit` 健康状态。
- `probability` 健康状态。
- 项目、文件、版本与当前内容摘要。

### 浏览器会话与服务调用鉴权

Gateway 同时支持两类调用方式：

- 浏览器访问：通过 `/login` 登录后使用 Cookie / Bearer 会话。
- 服务调用：通过 `X-API-Key` 访问，Gateway 会为下游 `xiaogugit` 注入兼容 Bearer token。

本地开发推荐配置：

```env
GATEWAY_SERVICE_API_KEY=local-gateway-key
GATEWAY_XIAOGUGIT_URL=http://127.0.0.1:8000
GATEWAY_PROBABILITY_URL=http://127.0.0.1:5000
```

### 统一接口目录

`GET /api/routes` 和 `API.md` 用于集中记录网关层暴露的接口。后续外部系统优先对接 Gateway，不直接依赖各子模块端口。

### 部署说明

Gateway 的 Docker 镜像现在会同时打包 `agent` 目录，并安装 `agent/requirements.txt`。如果修改了 Go 代码、内嵌 HTML 页面或 Agent 脚本，需要重新构建镜像：

```powershell
cd "C:\Users\gaozh\Desktop\data infra\gateway"
docker compose up --build
```
## 2026-04-20 用户与 API Key 管理

Gateway 新增 MySQL 持久化用户体系：

- `GET /ui-users`：用户注册、登录和 API Key 管理页面。
- `POST /api/users/register`：注册用户，返回用户 Bearer token 和初始 API Key。
- `POST /api/users/login`：用户登录，返回用户 Bearer token。
- `GET /api/users/api-keys`：查看当前用户 API Key 元数据。
- `POST /api/users/api-keys`：发放新的 API Key，明文只返回一次。
- `DELETE /api/users/api-keys/{id}`：吊销指定 API Key。

MySQL 配置：

```env
GATEWAY_MYSQL_DSN=root:123456@tcp(localhost:3306)/app?parseTime=true&charset=utf8mb4&loc=Local
```

本地和线上配置继续分离：

- `gateway/.env.development`：默认连接 `localhost:3306`。
- `gateway/.env.production`：默认连接 compose 内的 `mysql:3306`。

Docker Compose 默认不再启动 MySQL，建议连接已经发布到宿主机 3306 的 MySQL 容器。
## 2026-04-20 Idempotent Version Stars

Gateway now owns the idempotency layer for version stars:

- `gateway_version_stars` in MySQL stores one vote row per caller and version.
- Unique key: `(project_id, filename, version_id, voter_key)`.
- `POST /api/stars/star` activates the caller's vote. Repeated calls return `already_starred` and do not increment xiaogugit again.
- `POST /api/stars/unstar` revokes the caller's vote. Repeated calls return `already_unstarred` and do not decrement xiaogugit again.
- xiaogugit remains the source of the displayed aggregate `stars` count; gateway forwards only when the vote state changes.

Request body:

```json
{
  "project_id": "demo",
  "filename": "student.json",
  "version_id": 2
}
```

## Docker MySQL

Gateway compose does not start a MySQL container by default. It expects an existing MySQL service published on the host:

```text
mysql-container -> 0.0.0.0:3306->3306/tcp
```

When gateway runs in Docker, use:

```env
GATEWAY_MYSQL_DSN=root:123456@tcp(host.docker.internal:3306)/app?parseTime=true&charset=utf8mb4&loc=Local
```

When gateway runs directly on the host, use:

```env
GATEWAY_MYSQL_DSN=root:123456@tcp(localhost:3306)/app?parseTime=true&charset=utf8mb4&loc=Local
```

The target database `app` must already exist in MySQL, or MySQL must be configured to create it before gateway starts.

