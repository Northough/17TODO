# 17TODO

专注计时 + 待办。数据和计时都在本机后台服务里，关掉页面计时照样走。

## 用

打开 <http://127.0.0.1:8310>。就这一个地址，直接双击 index.html 是打不开的——
数据和计时都在后台服务里，页面本身不存东西，也没有离线兜底。

## 认证

口令在 `data/.env`（权限 600，不进 git）。服务读到 `TODO_TOKEN` 就全站要认证。

- 浏览器：访问一次 `http://127.0.0.1:8310/?token=<口令>`，换成 cookie，一年内不用再输。
- 脚本 / Nortia / MCP：请求头带 `Authorization: Bearer <口令>`。
- 只有 `/api/ping` 不要认证。

没有"本机免认证"。cloudflared 转发进来的请求源地址也是 127.0.0.1，
放行本机等于放行整条 tunnel。

换口令：改 `data/.env` 然后 `sudo systemctl restart 17todo`，所有设备重新输一次。

## 跑在哪

- systemd 服务 `17todo`，开机自启，挂了自动重启。
- 只绑 `127.0.0.1:8310`，没有暴露到局域网和公网。
- 数据库 `data/17todo.db`，不进 git。

常用命令：

```bash
sudo systemctl restart 17todo      # 改完 server/ 里的代码要重启
sudo systemctl status 17todo
journalctl -u 17todo -n 50         # 看日志
```

改 `index.html` 不用重启，刷新页面就行。改端口或数据库路径改 `deploy/17todo.service`，
拷到 `/etc/systemd/system/` 再 `daemon-reload` + `restart`。

## 谁管什么

| 东西 | 归谁 | 说明 |
| --- | --- | --- |
| 任务 / 待办 / 结算 / 周期计划 / 设置 | 页面 | 改完整体写回后端，带 `rev` 乐观锁 |
| 专注记录 sessions | 后端 | 只有后端写：计时落库、补记、删除 |
| 正在跑的计时 | 后端 | `start_ts` 在数据库里，页面只负责显示 |

页面每 10 秒和后端对一次版本号，只有对不上才传数据。

## 计时怎么变的

以前开始时间存在浏览器里，关页面就把这段结掉。现在开始时间写进数据库，页面显示的是
「现在 − 开始时间」。关页面、刷新、换浏览器都不影响，回来还在走。

配套加了个兜底：计时超过 N 小时（我的 → 偏好里改，默认 4，填 0 关掉）后端会自动收尾，
那段记录标成 `auto`。下次打开页面会弹窗告诉你哪个任务、多长时间，可以当场删掉——
毕竟跑满 4 小时多半是人已经不在了。

## 备份和恢复

我的 → 导出 JSON / 导入 JSON。导入走的是整库替换，会把后台数据全换成这份，不做合并。
直接备份 `data/17todo.db` 也行。

## 目录

- `index.html`：前端，单文件，由后端托管。
- `server/app.py`：HTTP 服务、路由、自动结束的后台线程。
- `server/db.py`：SQLite 读写、计时权威状态、摘要计算。
- `db/schema.sql`：表结构，服务启动时执行，语句都是 `IF NOT EXISTS`。
- `deploy/17todo.service`：systemd unit。
- `docs/api.md`：接口。
- `data/`：数据库，已 gitignore。

## 还没做

- MCP 包装（接口清单见 `docs/api.md` 末尾）。
- 周期待办的跨天滚动仍在前端做，页面打开时补算。所以页面几天没开的话，
  后端摘要里那些循环待办会显示成逾期——这本身也是实话。
- 页面加载时会去 Google Fonts 取一个等宽字体。断网时会退回系统字体，不影响用。
  介意的话把 `index.html` 头部那两个 `<link>` 删掉即可。
