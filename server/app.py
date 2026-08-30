#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""17TODO 本机后台服务。

- 托管 index.html（同源，前端不用管跨域）。
- 计时状态放在 SQLite，关掉页面继续走。
- 给 AI / 脚本留了短摘要接口。

默认只绑 127.0.0.1:8310。要放到局域网再改 TODO_HOST，并自己加认证。
"""

import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import db as dbm  # noqa: E402

HOST = os.environ.get('TODO_HOST', '127.0.0.1')
PORT = int(os.environ.get('TODO_PORT', '8310'))
DB_PATH = os.environ.get('TODO_DB', os.path.join(ROOT, 'data', '17todo.db'))
INDEX = os.path.join(ROOT, 'index.html')

# 设了就全站要认证。走 tunnel 暴露出去之前必须设。
TOKEN = os.environ.get('TODO_TOKEN', '').strip()
COOKIE = 'todo_token'
LOOPBACK = ('127.0.0.1', '::1', 'localhost')

STORE = dbm.Store(DB_PATH)


LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>17TODO</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;display:grid;place-items:center;background:#EDEBE7;color:#33302D;
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans SC",system-ui,sans-serif;padding:20px}
.box{background:#FAF9F7;border:1px solid #E1DDD7;border-radius:16px;padding:26px 22px;width:100%;max-width:340px}
h1{font-size:16px;font-weight:600;margin-bottom:6px}
p{font-size:12.5px;color:#A6A099;margin-bottom:18px}
input{width:100%;background:#F2F0EC;border:1px solid #E1DDD7;border-radius:9px;padding:11px;
  font-size:14px;font-family:ui-monospace,Menlo,monospace;color:#33302D}
input:focus{outline:none;border-color:#6E7F77;background:#FAF9F7}
button{width:100%;margin-top:12px;background:#6E7F77;color:#fff;border:none;border-radius:9px;
  padding:11px;font-size:14px;font-weight:500;cursor:pointer}
.err{color:#AD7E72;font-size:12.5px;margin-top:10px}
</style></head><body>
<form class="box" onsubmit="location='/?token='+encodeURIComponent(this.t.value.trim());return false">
  <h1>17TODO</h1>
  <p>输一次就记住，换设备再输。</p>
  <input name="t" type="password" placeholder="口令" autofocus autocomplete="current-password">
  <button type="submit">进去</button>
  <div class="err">{{err}}</div>
</form></body></html>"""


def log(msg):
    sys.stdout.write('[%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    sys.stdout.flush()


def full_snapshot():
    snap = STORE.load_state()
    r = STORE.revs()
    snap['sessions'] = STORE.load_sessions()
    snap['running'] = STORE.timer_get()
    snap['rev'] = r['rev']
    snap['srev'] = r['srev']
    return snap


class Handler(BaseHTTPRequestHandler):
    server_version = '17TODO'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass  # 正常请求不刷屏，出错时另外打

    # ---------- 基础 ----------

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _err(self, code, msg):
        self._json({'error': msg}, code)

    # ---------- 认证 ----------

    def _client_token(self):
        h = self.headers.get('Authorization') or ''
        if h.startswith('Bearer '):        # 脚本 / MCP 走这个
            return h[7:].strip()
        for part in (self.headers.get('Cookie') or '').split(';'):   # 页面走 cookie
            k, _, v = part.strip().partition('=')
            if k == COOKIE:
                return unquote(v)
        return ''

    def _authed(self):
        if not TOKEN:
            return True
        return hmac.compare_digest(self._client_token(), TOKEN)

    def _https(self):
        return (self.headers.get('X-Forwarded-Proto') or '').lower() == 'https'

    def _login_with(self, tok):
        """?token=xxx 进来就换成 cookie，之后地址栏就干净了。"""
        if not TOKEN or not hmac.compare_digest(tok, TOKEN):
            time.sleep(1)               # 慢一点，别让人拿着 tunnel 域名硬猜
            log('token 不对，来源 %s' % self.client_address[0])
            return self._login_page('口令不对')
        cookie = '%s=%s; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax' % (COOKIE, quote(tok))
        if self._https():
            cookie += '; Secure'
        self.send_response(302)
        self.send_header('Location', '/')
        self.send_header('Set-Cookie', cookie)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _login_page(self, err=''):
        html = LOGIN_HTML.replace('{{err}}', err)
        self._send(401 if err else 200, html, 'text/html; charset=utf-8')

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return None

    # ---------- 路由 ----------

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        try:
            if p in ('/', '/index.html') and q.get('token'):
                return self._login_with(q['token'][0])
            if p == '/api/ping':          # 唯一不要认证的：页面用它探后台活没活
                return self._json({'ok': True})
            if not self._authed():
                if p in ('/', '/index.html'):
                    return self._login_page()
                return self._err(401, 'unauthorized')
            if p in ('/', '/index.html'):
                return self._serve_index()
            if p == '/api/state':
                return self._json(full_snapshot())
            if p == '/api/sync':
                return self._sync(q)
            if p == '/api/sessions':
                return self._json({'sessions': STORE.load_sessions(), 'srev': STORE.revs()['srev']})
            if p == '/api/timer':
                return self._json({'running': STORE.timer_get()})
            if p == '/api/summary/brief':
                return self._json(STORE.summary_brief())
            if p == '/api/summary/today':
                day = (q.get('date') or [None])[0]
                return self._json(STORE.summary_today(day))
            return self._err(404, 'not found')
        except Exception as e:
            log('GET %s failed: %r' % (p, e))
            return self._err(500, str(e))

    def do_POST(self):
        p = urlparse(self.path).path
        if not self._authed():
            return self._err(401, 'unauthorized')
        try:
            body = self._body()
            if body is None:
                return self._err(400, 'bad json')
            if p == '/api/timer/start':
                task_id = body.get('taskId')
                if not task_id and body.get('task'):     # 脚本可以直接给任务名
                    try:
                        task_id = STORE.find_task(body['task'])
                    except ValueError as e:
                        return self._err(400, str(e))
                if not task_id:
                    return self._err(400, 'taskId or task required')
                todo_id, given = body.get('todoId'), 'todoId' in body
                if not todo_id and body.get('todo'):
                    try:
                        todo_id, given = STORE.find_todo(body['todo']), True
                    except ValueError as e:
                        return self._err(400, str(e))
                running = STORE.timer_start(task_id, todo_id, given)
                return self._json({'running': running, 'srev': STORE.revs()['srev']})
            if p == '/api/timer/pause':
                running = STORE.timer_pause()
                return self._json({'running': running, 'srev': STORE.revs()['srev'],
                                   'sessions': STORE.load_sessions()})
            if p == '/api/timer/stop':
                res = STORE.timer_stop()
                res['srev'] = STORE.revs()['srev']
                res['sessions'] = STORE.load_sessions()
                return self._json(res)
            if p == '/api/timer/hook':
                return self._json({'running': STORE.timer_hook(body.get('todoId'))})
            if p == '/api/todos':
                try:
                    return self._json(STORE.add_todo(
                        body.get('title'), body.get('task'), body.get('kind') or 'loop',
                        body.get('cycleDays') or 1, body.get('dueDate'),
                        body.get('onExpire') or 'reset'))
                except ValueError as e:
                    return self._err(400, str(e))
            if p == '/api/plans':
                try:
                    return self._json(STORE.add_plan(
                        body.get('title'), body.get('task'), body.get('startDate'),
                        body.get('cycleDays') or 7, body.get('dueDays')))
                except ValueError as e:
                    return self._err(400, str(e))
            if p == '/api/sessions':
                return self._add_session(body)
            if p == '/api/sessions/delete':
                srev = STORE.delete_sessions(body.get('ids') or [])
                return self._json({'srev': srev, 'sessions': STORE.load_sessions()})
            if p == '/api/sessions/unhook':
                if not body.get('todoId'):
                    return self._err(400, 'todoId required')
                srev = STORE.unhook_sessions(body['todoId'])
                return self._json({'srev': srev, 'sessions': STORE.load_sessions()})
            if p == '/api/import':
                state = body.get('state')
                if not isinstance(state, dict) or 'tasks' not in state:
                    return self._err(400, 'state required')
                STORE.import_state(state)
                log('整库导入：%d 任务 / %d 待办 / %d 专注段' % (
                    len(state.get('tasks') or []), len(state.get('todos') or []),
                    len(state.get('sessions') or [])))
                return self._json(full_snapshot())
            return self._err(404, 'not found')
        except Exception as e:
            log('POST %s failed: %r' % (p, e))
            return self._err(500, str(e))

    def do_PUT(self):
        p = urlparse(self.path).path
        if not self._authed():
            return self._err(401, 'unauthorized')
        try:
            body = self._body()
            if body is None:
                return self._err(400, 'bad json')
            if p == '/api/state':
                return self._put_state(body)
            return self._err(404, 'not found')
        except Exception as e:
            log('PUT %s failed: %r' % (p, e))
            return self._err(500, str(e))

    def do_DELETE(self):
        p = urlparse(self.path).path
        if not self._authed():
            return self._err(401, 'unauthorized')
        try:
            if p.startswith('/api/sessions/'):
                sid = p[len('/api/sessions/'):]
                if not sid:
                    return self._err(400, 'id required')
                srev = STORE.delete_session(sid)
                return self._json({'srev': srev, 'sessions': STORE.load_sessions()})
            return self._err(404, 'not found')
        except Exception as e:
            log('DELETE %s failed: %r' % (p, e))
            return self._err(500, str(e))

    # ---------- 具体处理 ----------

    def _serve_index(self):
        try:
            with open(INDEX, 'rb') as f:
                data = f.read()
        except OSError:
            return self._err(404, 'index.html missing')
        self._send(200, data, 'text/html; charset=utf-8')

    def _sync(self, q):
        """页面轮询用：只有对不上的部分才回传。"""
        r = STORE.revs()
        try:
            crev = int((q.get('rev') or ['-1'])[0])
            csrev = int((q.get('srev') or ['-1'])[0])
        except ValueError:
            crev = csrev = -1
        out = {'rev': r['rev'], 'srev': r['srev'], 'running': STORE.timer_get()}
        if crev != r['rev']:
            out['state'] = STORE.load_state()
        if csrev != r['srev']:
            out['sessions'] = STORE.load_sessions()
        return self._json(out)

    def _put_state(self, body):
        state = body.get('state')
        if not isinstance(state, dict):
            return self._err(400, 'state required')
        base = body.get('rev')
        try:
            rev = STORE.save_state(state, base)
        except dbm.Conflict:
            snap = full_snapshot()
            snap['error'] = 'conflict'
            return self._json(snap, 409)
        return self._json({'rev': rev})

    def _add_session(self, body):
        task_id = body.get('taskId')
        start, end = body.get('start'), body.get('end')
        if not task_id or not start or not end:
            return self._err(400, 'taskId/start/end required')
        if int(end) <= int(start):
            return self._err(400, 'end must be after start')
        sid = body.get('id') or ('m%s' % format(dbm.now_ms(), 'x')[-8:])
        srev = STORE.add_session(sid, task_id, body.get('todoId'), int(start), int(end), manual=True)
        return self._json({'srev': srev, 'sessions': STORE.load_sessions()})


def autostop_loop():
    while True:
        time.sleep(60)
        try:
            res = STORE.autostop_check()
            if res:
                log('计时跑太久，自动结束，共 %d 分钟' % round(res['total'] / 60000.0))
        except Exception as e:
            log('autostop failed: %r' % e)


def main():
    if not TOKEN and HOST not in LOOPBACK:
        log('拒绝启动：绑到了 %s 却没设 TODO_TOKEN，等于裸奔。' % HOST)
        sys.exit(1)
    t = threading.Thread(target=autostop_loop, daemon=True)
    t.start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    log('17TODO 启动：http://%s:%d  db=%s  认证=%s'
        % (HOST, PORT, DB_PATH, '开' if TOKEN else '关'))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log('bye')


if __name__ == '__main__':
    main()
