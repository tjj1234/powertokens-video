# PT Gateway —— 跨模型统一的异步任务接口（独立服务）

把 PowerTokens 的**视频 / Seedance 异步生成**收敛成一个统一接口，调用方不再需要为
「Seedance 要用 media-text + asset://」「各家返回字段位置不同」「各家轮询逻辑」各写一套。

> 关键前提纠偏：PT 底层是 OneAPI 透明路由，**所有视频/Seedance 模型本来就共用**
> `POST /v1/videos` + `GET /v1/videos/{id}`，并不存在「各家独立接口」。本服务归一化的是
> **请求体构造差异**与**返回字段位置差异**，并补上**模型自动发现/上下架同步**。

---

## 1. 架构

```
调用方 (任意 agent / 网页 / 脚本)
        │  POST /submit │ GET /status │ POST /wait │ POST /sync │ GET /models │ POST /cancel
        ▼
┌──────────────────────── pt-gateway (stdlib http.server, 零依赖) ────────────────────────┐
│  TaskGateway                                                                             │
│   ├─ submit(model,...)  → build_submit_body(kind 适配) → POST /v1/videos → {task_id}      │
│   ├─ get_status(tid)    → GET /v1/videos/{id} → normalize_status()                       │
│   ├─ wait(tid,timeout)  → 复用引擎 poll_task 轮询到终态 → 归一化                          │
│   └─ cancel(tid)        → 本轮 {supported:false}（PT 未暴露取消端点）                     │
│  ModelRegistry (registry.json)                                                           │
│   └─ sync(key): GET /v1/models → diff → 新上架 enable(is_new) / 下架 delisted            │
└───────────────────────────────┬──────────────────────────────────────────────────────────┘
                                 │  Bearer <POWERTOKENS_API_KEY>
                                 ▼
                   api.powertokens.ai  (/v1/videos, /v1/models)
```

`pt_gateway.py` 复用同仓 `pt_video.py` 的 REST 原语（`_api_call` / 端点常量 / 模型识别），
**不依赖 CLI 子命令**——它是独立模块，只是借用了已验证的传输层实现。

---

## 2. 接口契约

服务地址：`http://<host>:8787`（端口取 `PT_GATEWAY_PORT`，默认 8787）。Key 走
`POWERTOKENS_API_KEY` 环境变量或 `~/.config/powertokens-video/config.json`（与 CLI 共用）。

### GET /health
```json
{ "ok": true, "service": "pt-gateway" }
```

### POST /submit
请求体：
```json
{
  "model": "seedance-1-5-pro-251215",   // 必填；video/seedance 任一模型 ID
  "prompt": "一只猫在草地上走",           // 文生视频必填；图生视频也可留空
  "duration": 5,                          // 秒，2-30
  "resolution": "720p",                   // 480P/720P/1080P
  "ratio": "16:9",
  "first_frame": "asset://<id>",          // 可选，图生视频（Seedance 必须 asset://）
  "ref_image": "https://...",             // 可选
  "ref_video": "https://...",             // 可选
  "no_audio": false,
  "no_prompt_extend": false,
  "watermark": false,
  "seed": 42,                             // 可选
  "wait": false,                          // true 则轮询到终态再返回
  "timeout": 600                          // wait=true 时的轮询上限（秒）
}
```
响应（`wait=false`）：
```json
{ "task_id": "abc123", "kind": "seedance" }
```
响应（`wait=true`）：归一化状态（见 §3）。

### GET /status?task_id=<id>
```json
{ "task_id": "abc123", "state": "RUNNING", "progress": 30, "url": null, "raw": { ...厂商原文... } }
```

### POST /wait
```json
{ "task_id": "abc123", "timeout": 600 }
```
→ 归一化终态（成功带 `url`，失败带 `error`）。

### POST /sync
用当前 Key 拉 `GET /v1/models` 刷新注册表。
```json
{ "added": ["new-model-id"], "removed": ["delisted-model-id"], "total_enabled": 9, "last_synced": "2026-10-29T..." }
```

### GET /models?kind=video|seedance
返回注册表里 enabled 的异步模型列表（调用时惰性自动同步）。
```json
{ "kind": "seedance", "count": 3, "models": ["..."], "last_synced": "..." }
```

### POST /cancel
```json
{ "task_id": "abc123", "supported": false, "note": "PT 当前未提供取消任务的公共端点；本轮未实现" }
```

---

## 3. 归一化状态机

`normalize_status()` 把各厂商动词映射为统一枚举：

| 厂商原始 status | 归一化 |
|---|---|
| `succeeded` / `completed` | `SUCCEEDED` |
| `pending` / `queued` / `submitted` | `QUEUED` |
| `running` / `processing` / `in_progress` / `generating` | `RUNNING` |
| `failed` | `FAILED` |
| `cancelled` / `canceled` | `CANCELLED` |
| 其他 | `UNKNOWN` |

结果 URL 兼容 `metadata.url` / `metadata.content_url` / `task.url` 多处位置，统一放到 `url` 字段。

---

## 4. 模型注册表与自动更新机制

文件：`~/.config/pt-gateway/registry.json`（可用 `PT_GATEWAY_REGISTRY` 覆盖；已 gitignore）。

schema：
```json
{
  "last_synced": "2026-10-29T15:00:00+00:00",
  "models": {
    "seedance-1-5-pro-251215": {
      "kind": "seedance", "enabled": true, "delisted": false,
      "first_seen": "2026-10-20T...", "last_seen": "2026-10-29T...", "is_new": false
    }
  }
}
```

同步策略（`ModelRegistry.sync`）：
- **新上架**：出现在 live `/v1/models` 但不在注册表 → 新增 `enabled=true`，打 `is_new=true`。
- **下架**：曾在注册表但不再出现在 live → `enabled=false`，`delisted=true`（保留历史，不物理删除）。
- **惰性自动重拉**：`auto_sync_if_stale()` 在注册表为空或超过 `max_age`（默认 3600s，
  对齐 PT 的 1 小时缓存）时，于 `/models`、`/submit` 等调用前 best-effort 重拉；失败不抛，回退到
  现有/内置列表。也可显式 `POST /sync` 强制刷新。

> 这样「自动获取最新上架的异步模型 + 禁用下架的」就由注册表 diff 兜底，调用方无感。

---

## 5. 启动

```bash
cd pt-gateway
python server.py                 # 默认 8787
PT_GATEWAY_PORT=9000 python server.py
```

调用示例（curl）：
```bash
curl http://localhost:8787/health
curl -X POST http://localhost:8787/submit -H 'Content-Type: application/json' \
  -d '{"model":"seedance-1-5-pro-251215","prompt":"一只猫在草地上走","wait":false}'
curl "http://localhost:8787/status?task_id=abc123"
curl -X POST http://localhost:8787/sync
```

作为 Python 库直接调用：
```python
from pt_gateway import TaskGateway, ModelRegistry
gw = TaskGateway()                      # Key 自动从环境变量/config 取
tid = gw.submit("wan3.0-video", prompt="a cat", wait=False)["task_id"]
print(gw.wait(tid))
```

---

## 6. 与现有 CLI 的关系

`pt_video.py`（CLI + MCP）继续负责**直接生成**（含图片同步生成、Seedance 协议引导、下载等）。
`pt-gateway` 是**独立部署的任务中枢**：把「提交/查状态/轮询/模型目录」抽成服务，适合给多个 agent
或前端统一调用，避免每个接入方重复实现轮询与字段归一化。

## 7. 已知缺口（本轮）

- **cancel 未实现**：PT 当前未暴露取消任务的公共端点（官方文档与 `powertokens-integration`
  skill 均无记录）。`/cancel` 诚实返回 `{supported:false}`；确认端点后在此探测
  `DELETE /v1/videos/{id}` 或 `POST /v1/videos/{id}/cancel` 并实现分发。
- 轮询仍沿用引擎的 10s 间隔 + 600s 上限（无 429 退避/5xx 熔断，见 `pt_video.py` 注释 A.02/A.03）。

---

## 8. 手动触发开关 + 定时同步（自动更新不产生额外负担）

注册表的「自动发现新上架 / 自动下线已下架」由 §4 的 diff 兜底。**但它只在有人调用时才惰性重拉**；
若服务长期空闲，注册表不会自己变新鲜。因此提供两种显式触发方式：

### 8.1 手动触发开关（两种方式，任选）

1. **终端直跑（不用起服务）** —— `pt_gateway.py` 自带 `sync` 子命令：
   ```bash
   python pt_gateway.py sync        # 拉 /v1/models 刷新 registry.json，打印 {added,removed,total_enabled,last_synced}
   python pt_gateway.py selfcheck   # 状态归一化自检（默认，无需 Key）
   ```
   这条命令和 HTTP 服务**写入同一个** `~/.config/pt-gateway/registry.json`，所以手动刷新后服务立即可见。

2. **服务内热刷新** —— 对运行中的网关发：
   ```bash
   curl -X POST http://localhost:8787/sync
   ```

### 8.2 定时同步（推荐 12:00 + 19:00，零资源负担）

同步动作本身只是一次 `GET /v1/models`（返回几 KB JSON），服务空闲时 CPU≈0，
**每日两次完全无感**。已用 WorkBuddy 自动化建好每日 12:00 / 19:00 的定时任务，本质是定时执行：

```
C:/Users/admin/.workbuddy/binaries/python/versions/3.13.12/python.exe ^
  c:/Users/admin/WorkBuddy/2026-09-28-11-17-14/powertokens-video/pt-gateway/pt_gateway.py sync
```

- 若想改成**每小时**一次（同样免费），把自动化 rrule 改成 `FREQ=HOURLY;INTERVAL=1` 即可。
- 前提：定时任务执行时需 `POWERTOKENS_API_KEY` 在环境变量中可用（与网关取 Key 方式一致）。
  若自动化环境未带该变量，定时刷新会跳过——此时用 8.1 的手动开关在带 Key 的终端里跑即可。

> 设计取舍：PT 不提供 webhook，故采用「pull 模式」——惰性重拉（调用时 ≤1h 过期即刷）+
> 每日两次主动定时 + 手动开关三者互补，覆盖「调用中实时新鲜」与「空闲时也不陈旧」。
