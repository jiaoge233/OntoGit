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

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GATEWAY_PORT` | `8080` | compose 暴露端口 |
| `GATEWAY_ADDR` | `:8080` | gateway 监听地址 |
| `GATEWAY_XIAOGUGIT_URL` | `http://127.0.0.1:8000` | `xiaogugit` 上游地址 |
| `GATEWAY_PROBABILITY_URL` | `http://127.0.0.1:5000` | `probability` 上游地址 |
| `GATEWAY_SERVICE_API_KEY` | `change-me` | 服务调用 API Key |
| `GATEWAY_XG_AUTH_SECRET` | 继承 `XG_AUTH_SECRET` 或默认值 | 生成 `xiaogugit` 兼容 token 的签名密钥 |
| `GATEWAY_XG_AUTH_USERNAME` | `mogong` | 服务调用场景下注入的用户名 |

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
docker build -t data-infra-gateway .
```

### 使用 compose 启动

```powershell
docker compose up --build
```

默认 compose 假设宿主机上已经有：

- `xiaogugit` 在 `http://host.docker.internal:8000`
- `probability` 在 `http://host.docker.internal:5000`

如果要接容器内服务，改这两个环境变量即可：

- `GATEWAY_XIAOGUGIT_URL`
- `GATEWAY_PROBABILITY_URL`

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

Gateway 已补充 `Dockerfile` 与 `docker-compose.yml`。如果修改了 Go 代码或内嵌 HTML 页面，需要重新执行：

```powershell
go build .
.\gateway.exe
```
