# probability

概率判别服务，提供两个接口：

- `/api/llm/probability`：只返回概率
- `/api/llm/probability-reason`：返回概率和原因

## 2026-04-09 更新

### 接口入参更新

这两个接口现在支持直接接收业务 JSON 字段，不再要求外部显式传入模型名。

当前约定：

- 模型由后端统一读取 `.env` 中的 `DMXAPI_MODEL`
- 业务侧直接传 JSON 字段
- `include_raw` 仍可选
- 旧格式 `{ "message": "..." }` 仍兼容，便于平滑联调

推荐请求体示例：

```json
{
  "name": "学生",
  "agent": "agent3.2",
  "abilities": ["学习", "完成作业"],
  "interactions": [
    { "target": "老师", "type": "请教" }
  ]
}
```

### 与 xiaogugit 的联调约定

- `xiaogugit` 现在直接提交业务 JSON，不再额外包一层 `message` 字符串。
- 概率服务只负责返回模型结果，不负责版本管理。
- 概率回填由 `xiaogugit` 在主版本写入后完成。

### 日志

- `probability` 服务的接口请求日志现在统一带时间戳。
- 日志格式包含：请求时间、方法、路径、状态码、耗时。

## 环境变量

```env
DMXAPI_API_KEY=sk-xxx
DMXAPI_BASE_URL=https://www.dmxapi.cn/v1
DMXAPI_MODEL=gpt-5.4
DMXAPI_SYSTEM_PROMPT_PROBABILITY=...
DMXAPI_SYSTEM_PROMPT_PROBABILITY_REASON=...
HOST=0.0.0.0
PORT=5000
UVICORN_RELOAD=false
```

## 启动

安装依赖：

```bash
pip install -r requirements.txt
```

启动后端：

```bash
python app/main.py
```

启动前端静态页：

```bash
python -m http.server 3000 --directory frontend
```

## 接口说明

### `POST /api/llm/probability`

请求体可以直接传业务 JSON：

```json
{
  "name": "学生",
  "agent": "agent3.2",
  "abilities": ["学习", "完成作业"],
  "interactions": [
    { "target": "老师", "type": "请教" }
  ]
}
```

也兼容旧格式：

```json
{
  "message": "{\"name\":\"学生\"}",
  "include_raw": false
}
```

返回示例：

```json
{
  "model": "gpt-5.4",
  "text": "99%",
  "raw": null
}
```

### `POST /api/llm/probability-reason`

请求体格式与上面一致。

返回示例：

```json
{
  "model": "gpt-5.4",
  "text": "{\"probability\":\"99%\",\"reason\":\"...\"}",
  "raw": null
}
```
