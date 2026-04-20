# Gateway API

本文档以 `gateway` 为统一入口描述当前对外可用接口。

- 默认入口：`http://127.0.0.1:8080`
- 浏览器会话：`/login`
- 服务调用鉴权：`Authorization: Bearer <token>` 或 `X-API-Key: <key>`

## 鉴权说明

| 模式 | 说明 |
| --- | --- |
| `none` | 无需鉴权，可直接调用 |
| `browser session` | 浏览器登录后通过 cookie 或前端保存的 token 调用 |
| `bearer` | 直接携带 `Authorization: Bearer <token>` |
| `X-API-Key` | 由 gateway 校验后自动换成下游 `xiaogugit` 可识别的 Bearer |

## Gateway 自身接口

| 名称 | 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- | --- |
| gateway_root | `GET` | `/` | `none` | 网关根信息、示例链接、后端地址 |
| gateway_health | `GET` | `/health` | `none` | 网关健康检查，同时汇总 `xiaogugit` 和 `probability` 健康状态 |
| gateway_login_page | `GET` | `/login` | `none` | 网关登录页，默认登录成功后跳转 `/ui-dashboard` |
| gateway_dashboard_page | `GET` | `/ui-dashboard` | `browser session or bearer` | 中台总览页面 |
| gateway_dashboard_summary | `GET` | `/api/dashboard/summary` | `browser session, bearer, or X-API-Key` | 聚合项目、时间线、当前文件内容和后端健康状态 |
| gateway_route_catalog | `GET` | `/api/routes` | `none` | 返回统一路由目录 |

## 认证透传接口

| 名称 | 方法 | 路径 | 鉴权 | 上游 | 说明 |
| --- | --- | --- | --- | --- | --- |
| gateway_auth_proxy | `ANY` | `/auth/*` | `none for login; cookie/bearer after login` | `xiaogugit` | 网关透传认证接口 |

当前常用认证接口如下：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/auth/login` | 登录，返回访问令牌 |
| `POST` | `/auth/logout` | 登出 |
| `GET` | `/auth/me` | 获取当前登录用户 |

## XiaoGuGit 统一入口

所有 `xiaogugit` 接口统一经 `gateway` 暴露在 `/xg/*` 下。

| 名称 | 方法 | 路径 | 鉴权 | 上游 | 说明 |
| --- | --- | --- | --- | --- | --- |
| xiaogugit_proxy | `ANY` | `/xg/*` | `browser session, bearer, or X-API-Key` | `xiaogugit` | `xiaogugit` 全量统一前缀入口 |
| xg_projects | `GET` | `/xg/projects` | `browser session, bearer, or X-API-Key` | `xiaogugit:/projects` | 列出项目 |
| xg_project_timelines | `GET` | `/xg/timelines/{project_id}` | `browser session, bearer, or X-API-Key` | `xiaogugit:/timelines/{project_id}` | 读取项目下各文件时间线 |
| xg_read_current | `GET` | `/xg/read/{project_id}/{filename}` | `browser session, bearer, or X-API-Key` | `xiaogugit:/read/{project_id}/{filename}` | 读取当前工作区文件内容 |
| xg_write_and_infer | `POST` | `/xg/write-and-infer` | `browser session, bearer, or X-API-Key` | `xiaogugit:/write-and-infer` | 写新版本并触发概率推理 |
| xg_official_recommend | `GET` | `/xg/version-recommend/official` | `browser session, bearer, or X-API-Key` | `xiaogugit:/version-recommend/official` | 读取官方推荐版本 |
| xg_official_recommend_set | `POST` | `/xg/version-recommend/official/set` | `browser session, bearer, or X-API-Key` | `xiaogugit:/version-recommend/official/set` | 设置官方推荐版本 |
| xg_community_recommend | `GET` | `/xg/version-recommend/community` | `browser session, bearer, or X-API-Key` | `xiaogugit:/version-recommend/community` | 读取社区推荐版本 |

说明：

- 这里列的是当前 gateway 已显式归档的核心接口。
- 其余 `xiaogugit` 原生路由仍然可以通过 `/xg/<原始路径>` 调用。
- 根路径下未命中的请求也会透传到 `xiaogugit`，但对外建议统一使用 `/xg/*`。

## Probability 统一入口

所有 `probability` 接口统一经 `gateway` 暴露在 `/probability/*` 下。

| 名称 | 方法 | 路径 | 鉴权 | 上游 | 说明 |
| --- | --- | --- | --- | --- | --- |
| probability_proxy | `ANY` | `/probability/*` | `browser session, bearer, or X-API-Key; health is public` | `probability` | `probability` 全量统一前缀入口 |
| probability_health | `GET` | `/probability/health` | `none` | `probability:/health` | 概率服务健康检查 |
| probability_reason | `POST` | `/probability/api/llm/probability-reason` | `browser session, bearer, or X-API-Key` | `probability:/api/llm/probability-reason` | 返回概率和理由 |
| probability_score_only | `POST` | `/probability/api/llm/probability` | `browser session, bearer, or X-API-Key` | `probability:/api/llm/probability` | 仅返回概率 |

## 示例

### 0. 如何获取 Bearer Token

#### 方式 A：通过登录接口获取

```powershell
$loginBody = @{
  username = "你的用户名"
  password = "你的密码"
} | ConvertTo-Json

$loginResp = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8080/auth/login" `
  -ContentType "application/json; charset=utf-8" `
  -Body $loginBody

$token = $loginResp.access_token
$headers = @{ Authorization = "Bearer $token" }
```

获取到 `$token` 后，就可以调用需要 bearer 的接口：

```powershell
Invoke-RestMethod `
  -Method Get `
  -Uri "http://127.0.0.1:8080/api/dashboard/summary" `
  -Headers $headers
```

#### 方式 B：直接使用服务 API Key

如果 gateway 已配置 `GATEWAY_SERVICE_API_KEY`，可以不显式拿 token，直接带：

```powershell
$headers = @{ "X-API-Key" = "change-me" }
```

gateway 会自动为下游 `xiaogugit` 生成兼容的 Bearer token。

### 1. 获取统一路由目录

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8080/api/routes"
```

### 2. 服务调用 dashboard 聚合接口

```powershell
Invoke-RestMethod `
  -Method Get `
  -Uri "http://127.0.0.1:8080/api/dashboard/summary" `
  -Headers @{ "X-API-Key" = "change-me" }
```

### 3. 调用 Probability 推理接口

```powershell
$body = @{
  name = "学生"
  agent = "agent3.2"
  abilities = @("学习", "完成作业")
  interactions = @(
    @{
      target = "老师"
      type   = "请教"
    }
  )
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8080/probability/api/llm/probability-reason" `
  -Headers @{ "X-API-Key" = "change-me" } `
  -ContentType "application/json; charset=utf-8" `
  -Body $body
```

### 4. 调用 XiaoGuGit 官方推荐接口

```powershell
Invoke-RestMethod `
  -Method Get `
  -Uri "http://127.0.0.1:8080/xg/version-recommend/official?project_id=demo&filename=student.json" `
  -Headers @{ "X-API-Key" = "change-me" }
```

## Gateway 用户与 API Key

用户与 API Key 由 gateway 自身管理，数据存储在 MySQL：

- `gateway_users`：用户账号、展示名、密码哈希、状态。
- `gateway_api_keys`：API Key 哈希、前缀、状态、最后使用时间。

API Key 明文只在创建时返回一次，数据库只保存哈希。

| 名称 | 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- | --- |
| gateway_user_register | `POST` | `/api/users/register` | `none` | 注册用户，同时返回用户 token 和初始 API Key |
| gateway_user_login | `POST` | `/api/users/login` | `none` | 用户名密码登录，返回用户 Bearer token |
| gateway_user_api_keys_list | `GET` | `/api/users/api-keys` | `user bearer or user X-API-Key` | 列出当前用户的 API Key 元数据，不返回明文 |
| gateway_user_api_keys_create | `POST` | `/api/users/api-keys` | `user bearer or user X-API-Key` | 为当前用户创建新 API Key，明文只返回一次 |
| gateway_user_api_keys_revoke | `DELETE` | `/api/users/api-keys/{id}` | `user bearer or user X-API-Key` | 吊销当前用户拥有的 API Key |

前端页面：

```text
GET /ui-users
```

该页面可完成用户注册、登录、API Key 发放、列表查看和吊销。

注册示例：

```powershell
$body = @{
  username = "alice"
  password = "alice123456"
  display_name = "Alice"
} | ConvertTo-Json

$resp = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8080/api/users/register" `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))

$token = $resp.access_token
$apiKey = $resp.api_key
```

发放新 API Key：

```powershell
$body = @{ name = "demo-script" } | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8080/api/users/api-keys" `
  -Headers @{ Authorization = "Bearer $token" } `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

使用用户 API Key 调用受保护接口：

```powershell
Invoke-RestMethod `
  -Method Get `
  -Uri "http://127.0.0.1:8080/api/dashboard/summary" `
  -Headers @{ "X-API-Key" = $apiKey }
```

## Gateway Idempotent Version Stars

Gateway stores per-caller star votes in MySQL table `gateway_version_stars`.
The unique key is `(project_id, filename, version_id, voter_key)`, so the same user/API key/browser session can only add one active star to the same version.
Gateway only forwards to xiaogugit `/version-star` or `/version-unstar` when the stored vote state actually changes.

| Name | Method | Path | Auth | Description |
| --- | --- | --- | --- | --- |
| gateway_version_star | `POST` | `/api/stars/star` | `browser session, bearer, or X-API-Key` | Idempotently star one ontology version. |
| gateway_version_unstar | `POST` | `/api/stars/unstar` | `browser session, bearer, or X-API-Key` | Idempotently remove the current caller's star. |

PowerShell example:

```powershell
$body = @{
  project_id = "demo"
  filename = "student.json"
  version_id = 2
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8080/api/stars/star" `
  -Headers @{ "X-API-Key" = $apiKey } `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```
