# 17TODO API

`http://127.0.0.1:8310`，所有请求和响应都是 JSON。

## 认证

服务端设了 `TODO_TOKEN` 就全站要认证（`/api/ping` 除外）。口令在
`data/.env`，不进 git。

脚本 / Nortia / MCP：

```
Authorization: Bearer <口令>
```

浏览器：访问 `/?token=<口令>` 换成 cookie，之后地址栏就干净了，一年内不用再输。
没带凭据时 `/` 返回登录页，`/api/*` 返回 401。

**没有"本机免认证"这种优待。** cloudflared 转发进来的请求源地址也是 127.0.0.1，
放行本机等于放行整条 tunnel。

## 给 AI / 脚本用的

### `GET /api/summary/brief`

高频查这个，回包尽量短。

```json
{
  "status": "studying",
  "task": "树与二叉树",
  "todo": "树题二刷",
  "today_min": 132,
  "top": [["408", 90], ["英语", 42]],
  "due": [["政治马原", "23:59", 35]],
  "done": [["背词200", 28, "done"]]
}
```

`status`：`studying` 计时在跑 / `paused` 有任务但暂停 / `relax` 没在计时。

### `GET /api/summary/today?date=YYYY-MM-DD`

比 brief 详细，列表有截断。`date` 不传就是今天。

```json
{
  "date": "2026-08-31",
  "status": { "mode": "studying", "task": "树与二叉树", "todo": "树题二刷", "elapsed_minutes": 42 },
  "today": {
    "focus_minutes": 132,
    "session_count": 5,
    "by_top_task": [{ "task": "408", "minutes": 90 }],
    "settled_count": 3,
    "overdue_count": 1,
    "due_today_count": 2
  },
  "due_today_unfinished": [
    { "id": "td1", "title": "政治马原", "task": "政治", "minutes": 35, "due": "23:59", "overdue": false }
  ],
  "completed_today": [
    { "id": "td2", "title": "背词200", "task": "英语", "minutes": 28, "status": "done" }
  ]
}
```

`by_top_task` 按顶级任务聚合，子任务的时间算到根上。

## 计时

计时的权威在后端，页面只是显示。

- `GET /api/timer` → `{ "running": { "taskId", "todoId", "startTs", "accum" } | null }`
  - `startTs` 为 null 表示暂停中；已计时长 = `accum + (startTs ? now - startTs : 0)`。
- `POST /api/timer/start` body `{ "taskId": "...", "todoId": "..." }`
  - 换任务会先把上一段落库再清零；同任务是继续。
  - 不带 `todoId` 字段时保留原来的挂钩，带了（含 `null`）才覆盖。
- `POST /api/timer/pause` → 落库当前这段，返回 `running` 和最新 `sessions`。
- `POST /api/timer/stop` → `{ "running": null, "total": 毫秒, "recorded": bool, "sessions": [...] }`
- `POST /api/timer/hook` body `{ "todoId": "..." | null }` → 改当前计时挂钩的待办。

不足 15 秒的片段不记，和页面里的规则一致。

## 状态同步

页面用的，脚本一般不碰。

- `GET /api/state` → 全量：`tasks / todos / completions / plans / settings / sessions / running / rev / srev`
- `PUT /api/state` body `{ "rev": N, "state": { tasks, todos, completions, plans, settings } }`
  - `rev` 对不上返回 **409** + 最新全量，调用方重新加载。
  - 不接受 `sessions` 和 `running`，那两样归后端。
- `GET /api/sync?rev=N&srev=M` → 永远回 `rev / srev / running`，只有版本对不上才附带 `state` / `sessions`。
- `GET /api/ping` → `{"ok": true}`，唯一不需要认证的接口

## 专注记录

- `GET /api/sessions` → `{ sessions, srev }`
- `POST /api/sessions` body `{ taskId, todoId, start, end }` → 手动补记，标 `manual`
- `DELETE /api/sessions/:id`
- `POST /api/sessions/delete` body `{ ids: [...] }` → 删任务时连带清理
- `POST /api/sessions/unhook` body `{ todoId }` → 待办删了，记录留着只解挂钩

session 字段：`id, taskId, todoId, start, end, manual, auto`。
`auto` 表示后端超时兜底自动结束的，时长多半不实，页面会弹出来让人确认。

## 待办 / 周期计划（单条写入）

页面是整体写回 state，脚本和 MCP 单条插入走这两个。服务端直接落库并推进 `rev`，
页面下次 PUT 会撞 409 然后重新加载，不会互相覆盖。

- `POST /api/todos` body `{ title, task?, kind?, cycleDays?, dueDate?, onExpire? }`
  - `kind`：`loop`（默认，配 `cycleDays`）/ `dated`（配 `dueDate`）/ `open`
  - 返回建好的整条待办 + 新的 `rev`
- `POST /api/plans` body `{ title, task?, cycleDays?, dueDays?, startDate? }`

`task` 传**任务名**就行，不用查 id：先精确匹配，再唯一子串匹配；
匹配不到或有歧义会返回 400，报错文案里列出现有任务名。
`POST /api/timer/start` 的 `task` / `todo` 字段同理。

## 整库替换

- `POST /api/import` body `{ "state": { tasks, todos, sessions, completions, plans, settings } }`

清空所有表（含 sessions 和正在跑的计时）再写入，返回新的全量快照。
「导入 JSON」和「清空重来」走这条。**不做增量合并，会覆盖。**

## MCP

`mcp/server.py`，stdio 协议，没有端口也没有 systemd 服务，由 Claude Code 的
`--mcp-config` 拉起。口令自己从 `data/.env` 读，配置文件里不用写密钥。

全是薄封装，名字解析和校验都在 HTTP 那边：

| 工具 | 打到 |
| --- | --- |
| `get_brief_summary` | `GET /api/summary/brief` |
| `get_today_summary` | `GET /api/summary/today` |
| `stop_timer` | `POST /api/timer/stop` |
| `create_todo` | `POST /api/todos` |
| `create_periodic_plan` | `POST /api/plans` |
| `get_todo_schema` | 本地，取完整说明 |

**没有 start / pause。** 开始和继续计时是 17 自己在页面上按的，AI 不代劳；
`POST /api/timer/start` 和 `/pause` 接口还在，只是不给 MCP。

**懒加载。** `tools/list` 里只放一行描述和裸参数名，完整说明（什么时候别用、
字段细节、报错怎么办）放在 `DETAIL` 里，用 `get_todo_schema` 按需取。
（叫 `get_todo_schema` 不叫 `get_tool_schema`，是为了避开 galatea-garden 的同名工具。）
常驻在 system prompt 里的固定开销从 ~830 tokens 压到 ~335。
加工具时描述保持一行，长的写进 `register()` 第二个参数。

核心数据和权限留在 HTTP + SQLite，MCP 只是 AI 的入口。**不做读-改-写整个 state**，
要新增写操作就先在 HTTP 补一个细粒度接口，别在 MCP 里拼 state。

注册（Nortia 用的是 `/home/admin/.tidal/nortia-mcp.json`）：

```json
"17todo": {
  "command": "python3",
  "args": ["/opt/workspace/17TODO/mcp/server.py"]
}
```

换别的 host 或改端口：设 `TODO_URL`；口令不想从 `data/.env` 读就设 `TODO_TOKEN`。
