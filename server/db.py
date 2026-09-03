# -*- coding: utf-8 -*-
"""17TODO 存储层。

职责：
- SQLite 读写，把前端那份 state 拆成规范化表，方便 API 直接查。
- 计时的唯一权威：running_timer 存在库里，页面关掉也不影响。
- 给 AI / 脚本用的短摘要。

约定：
- 前端仍然拿完整 state 渲染，tasks/todos/completions/plans/settings 由前端整体写回（带 rev 乐观锁）。
- sessions 和 running 只由本模块产生和修改，前端不直接写。
"""

import json
import os
import sqlite3
import threading
import time

DAY_MS = 86400000
MIN_SESSION_MS = 15000          # 不足 15 秒不记，和前端一致
DEFAULT_AUTO_STOP_HOURS = 4     # 忘记结束时的兜底

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(os.path.dirname(HERE), 'db', 'schema.sql')


def now_ms():
    return int(time.time() * 1000)


def day_key(ts):
    return time.strftime('%Y-%m-%d', time.localtime(ts / 1000.0))


def day_start_ms(key):
    y, m, d = [int(x) for x in key.split('-')]
    return int(time.mktime((y, m, d, 0, 0, 0, 0, 0, -1)) * 1000)


def add_days(key, n):
    # 加 3 小时再取整，绕开夏令时/闰秒把日期挪错一天
    return day_key(day_start_ms(key) + n * DAY_MS + 3 * 3600000)


def diff_days(a, b):
    return int(round((day_start_ms(b) - day_start_ms(a)) / float(DAY_MS)))


def today_key():
    return day_key(now_ms())


def _j(v):
    return None if v is None else json.dumps(v, ensure_ascii=False)


def _u(v):
    if v is None:
        return None
    try:
        return json.loads(v)
    except Exception:
        return None


def _b(v):
    return 1 if v else 0


class Conflict(Exception):
    """state 版本对不上，调用方需要重新拉取。"""


class Store(object):
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA foreign_keys=OFF')
        self._init_schema()

    def _init_schema(self):
        with open(SCHEMA_PATH, 'r') as f:
            sql = f.read()
        with self.lock:
            self.conn.executescript(sql)
            self._migrate()
            self.conn.commit()

    def _migrate(self):
        """给老库补上 schema.sql 后来改的东西。

        CREATE TABLE IF NOT EXISTS 碰到已存在的表什么都不做，所以加字段、
        松约束都得在这儿自己来一遍。全部可重复执行。
        """
        info = list(self.conn.execute('PRAGMA table_info(sessions)'))
        if not any(r['name'] == 'task_name' for r in info):
            self.conn.execute('ALTER TABLE sessions ADD COLUMN task_name TEXT')
            info = list(self.conn.execute('PRAGMA table_info(sessions)'))
        # 老库 task_id 是 NOT NULL，删任务时没法把它置空。SQLite 改不了约束，整表重建。
        if any(r['name'] == 'task_id' and r['notnull'] for r in info):
            self.conn.executescript("""
                CREATE TABLE sessions_new (
                  id TEXT PRIMARY KEY,
                  task_id TEXT,
                  todo_id TEXT,
                  start_ts INTEGER NOT NULL,
                  end_ts INTEGER NOT NULL,
                  manual INTEGER NOT NULL DEFAULT 0,
                  auto INTEGER NOT NULL DEFAULT 0,
                  task_name TEXT,
                  created_at INTEGER NOT NULL
                );
                INSERT INTO sessions_new
                  SELECT id,task_id,todo_id,start_ts,end_ts,manual,auto,task_name,created_at
                  FROM sessions;
                DROP TABLE sessions;
                ALTER TABLE sessions_new RENAME TO sessions;
                CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_ts);
                CREATE INDEX IF NOT EXISTS idx_sessions_task ON sessions(task_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_todo ON sessions(todo_id);
            """)

    # ---------------- meta ----------------

    def _meta(self, key, default=0):
        row = self.conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return int(row['value']) if row else default

    def _bump(self, key):
        v = self._meta(key) + 1
        self.conn.execute(
            'INSERT INTO meta(key, value) VALUES(?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(v)))
        return v

    def revs(self):
        with self.lock:
            return {'rev': self._meta('rev'), 'srev': self._meta('srev')}

    # ---------------- state ----------------

    def load_state(self):
        """返回前端形状的 state（不含 sessions / running）。"""
        with self.lock:
            tasks = [{
                'id': r['id'], 'name': r['name'], 'parentId': r['parent_id'],
                'g': _u(r['gradient']), 'target': r['target_minutes'], 'order': r['sort_order'],
            } for r in self.conn.execute('SELECT * FROM tasks ORDER BY sort_order')]

            todos = []
            for r in self.conn.execute('SELECT * FROM todos ORDER BY sort_order'):
                td = {
                    'id': r['id'], 'title': r['title'], 'taskId': r['task_id'], 'kind': r['kind'],
                    'cycleDays': r['cycle_days'], 'onExpire': r['on_expire'],
                    'timeBase': r['time_base'], 'done': bool(r['done']), 'doneAt': r['done_at'],
                    'order': r['sort_order'],
                }
                for src, dst in (('cycle_start', 'cycleStart'), ('start_date', 'startDate'),
                                 ('start_time', 'startTime'), ('due_date', 'dueDate'),
                                 ('due_time', 'dueTime'), ('done_at_was', 'doneAtWas'),
                                 ('plan_id', 'planId'), ('plan_start', 'planStart')):
                    if r[src] is not None:
                        td[dst] = r[src]
                if r['missed']:
                    td['missed'] = r['missed']
                todos.append(td)

            comps = []
            for r in self.conn.execute('SELECT * FROM settlements ORDER BY at_ts'):
                c = {
                    'id': r['id'], 'todoId': r['todo_id'], 'title': r['title'], 'taskId': r['task_id'],
                    'day': r['day'], 'at': r['at_ts'], 'minutes': r['minutes'],
                    'from': r['from_day'], 'cycleDays': r['cycle_days'], 'kind': r['kind'],
                    'mode': r['mode'], 'range': r['range_text'],
                }
                if r['overdue']:
                    c['overdue'] = True
                comps.append(c)

            plans = [{
                'id': r['id'], 'title': r['title'], 'taskId': r['task_id'],
                'startDate': r['start_date'], 'cycleDays': r['cycle_days'], 'dueDays': r['due_days'],
                'active': bool(r['active']), 'lastCreated': r['last_created'], 'createdAt': r['created_at'],
            } for r in self.conn.execute('SELECT * FROM periodic_plans ORDER BY created_at')]

            settings = {}
            for r in self.conn.execute('SELECT * FROM settings'):
                settings[r['key']] = _u(r['value_json'])

            return {
                'tasks': tasks, 'todos': todos, 'completions': comps,
                'plans': plans, 'settings': settings,
            }

    def save_state(self, state, base_rev=None):
        """整体写回 tasks/todos/completions/plans/settings。

        base_rev 传了就做乐观锁；对不上抛 Conflict。
        """
        with self.lock:
            cur_rev = self._meta('rev')
            if base_rev is not None and int(base_rev) != cur_rev:
                raise Conflict()
            ts = now_ms()
            c = self.conn
            c.execute('BEGIN')
            try:
                c.execute('DELETE FROM tasks')
                for i, t in enumerate(state.get('tasks') or []):
                    c.execute(
                        'INSERT INTO tasks(id,name,parent_id,gradient,target_minutes,sort_order,'
                        'created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                        (t['id'], t.get('name') or '', t.get('parentId'), _j(t.get('g')),
                         int(t.get('target') or 0), int(t.get('order', i)), ts, ts))

                c.execute('DELETE FROM todos')
                for i, td in enumerate(state.get('todos') or []):
                    c.execute(
                        'INSERT INTO todos(id,title,task_id,kind,cycle_days,on_expire,cycle_start,'
                        'start_date,start_time,due_date,due_time,time_base,done,done_at,done_at_was,'
                        'missed,plan_id,plan_start,sort_order,created_at,updated_at) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (td['id'], td.get('title') or '', td.get('taskId'), td.get('kind') or 'open',
                         int(td.get('cycleDays') or 0), td.get('onExpire') or 'reset',
                         td.get('cycleStart'), td.get('startDate'), td.get('startTime'),
                         td.get('dueDate'), td.get('dueTime'), int(td.get('timeBase') or 0),
                         _b(td.get('done')), td.get('doneAt'), td.get('doneAtWas'),
                         int(td.get('missed') or 0), td.get('planId'), td.get('planStart'),
                         int(td.get('order', i)), ts, ts))

                c.execute('DELETE FROM settlements')
                for comp in state.get('completions') or []:
                    c.execute(
                        'INSERT INTO settlements(id,todo_id,title,task_id,day,at_ts,minutes,from_day,'
                        'cycle_days,kind,mode,range_text,overdue,created_at) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (comp['id'], comp.get('todoId'), comp.get('title') or '', comp.get('taskId'),
                         comp.get('day') or today_key(), int(comp.get('at') or ts),
                         int(round(comp.get('minutes') or 0)), comp.get('from'),
                         int(comp.get('cycleDays') or 0), comp.get('kind') or 'done',
                         comp.get('mode') or 'open', comp.get('range'), _b(comp.get('overdue')), ts))

                c.execute('DELETE FROM periodic_plans')
                for p in state.get('plans') or []:
                    c.execute(
                        'INSERT INTO periodic_plans(id,title,task_id,start_date,cycle_days,due_days,'
                        'active,last_created,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (p['id'], p.get('title') or '', p.get('taskId'), p.get('startDate') or today_key(),
                         int(p.get('cycleDays') or 7), int(p.get('dueDays') or 7),
                         _b(p.get('active', True)), p.get('lastCreated'), int(p.get('createdAt') or ts), ts))

                c.execute('DELETE FROM settings')
                for k, v in (state.get('settings') or {}).items():
                    c.execute('INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)',
                              (k, _j(v), ts))

                if self._claim_orphans(c):
                    self._bump('srev')      # 认领改了 sessions，页面得知道要重拉
                rev = self._bump('rev')
                c.execute('COMMIT')
                return rev
            except Exception:
                c.execute('ROLLBACK')
                raise

    def _claim_orphans(self, c):
        """孤儿记录找同名任务认领。任务表刚写完就跑一遍，所以新建 / 改名都能接上。

        认领之后 task_id 指向真任务、快照清掉，之后再改任务名统计跟着走。
        重名任务取第一个，反正页面上也分不出来。
        """
        cur = c.execute(
            'UPDATE sessions SET '
            '  task_id=(SELECT id FROM tasks WHERE tasks.name=sessions.task_name LIMIT 1),'
            '  task_name=NULL '
            'WHERE task_id IS NULL AND task_name IS NOT NULL '
            '  AND EXISTS(SELECT 1 FROM tasks WHERE tasks.name=sessions.task_name)')
        return cur.rowcount or 0

    # ---------------- 细粒度写入（给 MCP / 脚本用） ----------------
    # 页面是整体写回 state，脚本要单条插入就得走这里：服务端直接落库并 bump rev，
    # 页面下次 PUT 会撞 409 然后重新加载，不会互相覆盖。

    def find_task(self, name_or_id):
        """按 id 或名字找任务。名字支持唯一子串匹配，找不到/有歧义都抛错并列出候选。"""
        key = (name_or_id or '').strip()
        if not key:
            return None
        with self.lock:
            rows = self.conn.execute('SELECT id,name FROM tasks').fetchall()
        for r in rows:
            if r['id'] == key or r['name'] == key:
                return r['id']
        hits = [r for r in rows if key in (r['name'] or '')]
        if len(hits) == 1:
            return hits[0]['id']
        names = '、'.join(r['name'] for r in rows) or '（一个任务都没有）'
        if len(hits) > 1:
            raise ValueError('「%s」对上了多个任务：%s' % (key, '、'.join(h['name'] for h in hits)))
        raise ValueError('没有叫「%s」的任务。现有：%s' % (key, names))

    def find_todo(self, title_or_id):
        """按 id 或标题找未完成的待办，规则同 find_task。"""
        key = (title_or_id or '').strip()
        if not key:
            return None
        with self.lock:
            rows = self.conn.execute('SELECT id,title FROM todos WHERE done=0').fetchall()
        for r in rows:
            if r['id'] == key or r['title'] == key:
                return r['id']
        hits = [r for r in rows if key in (r['title'] or '')]
        if len(hits) == 1:
            return hits[0]['id']
        if len(hits) > 1:
            raise ValueError('「%s」对上了多条待办：%s' % (key, '、'.join(h['title'] for h in hits)))
        raise ValueError('没有未完成的待办叫「%s」。现有：%s'
                         % (key, '、'.join(r['title'] for r in rows) or '（没有未完成待办）'))

    def add_todo(self, title, task=None, kind='loop', cycle_days=1, due_date=None,
                 on_expire='reset'):
        title = (title or '').strip()
        if not title:
            raise ValueError('待办内容不能为空')
        if kind not in ('loop', 'dated', 'open'):
            raise ValueError('kind 只能是 loop / dated / open')
        task_id = self.find_task(task) if task else None
        today = today_key()
        ts = now_ms()
        td = {
            'id': 'k' + os.urandom(3).hex(), 'title': title, 'taskId': task_id, 'kind': kind,
            'cycleDays': max(1, int(cycle_days or 1)) if kind == 'loop' else 0,
            'onExpire': on_expire if on_expire in ('reset', 'keep') else 'reset',
            'cycleStart': None, 'startDate': None, 'startTime': None,
            'dueDate': None, 'dueTime': None,
        }
        if kind == 'loop':
            td['cycleStart'] = today
            td['timeBase'] = day_start_ms(today)
        elif kind == 'dated':
            due = (due_date or today).strip()
            if due < today:
                raise ValueError('截止日 %s 已经过去了' % due)
            td['startDate'], td['startTime'] = today, '00:00'
            td['dueDate'], td['dueTime'] = due, '23:59'
            td['timeBase'] = day_start_ms(today)
        else:
            td['timeBase'] = ts

        with self.lock:
            n = self.conn.execute('SELECT COUNT(*) AS c FROM todos').fetchone()['c']
            self.conn.execute(
                'INSERT INTO todos(id,title,task_id,kind,cycle_days,on_expire,cycle_start,'
                'start_date,start_time,due_date,due_time,time_base,done,done_at,done_at_was,'
                'missed,plan_id,plan_start,sort_order,created_at,updated_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL,0,NULL,NULL,?,?,?)',
                (td['id'], td['title'], td['taskId'], td['kind'], td['cycleDays'], td['onExpire'],
                 td['cycleStart'], td['startDate'], td['startTime'], td['dueDate'], td['dueTime'],
                 td['timeBase'], n, ts, ts))
            rev = self._bump('rev')
            self.conn.commit()
        td['rev'] = rev
        return td

    def add_plan(self, title, task=None, start_date=None, cycle_days=7, due_days=None):
        title = (title or '').strip()
        if not title:
            raise ValueError('待办内容不能为空')
        task_id = self.find_task(task) if task else None
        cycle = max(1, int(cycle_days or 7))
        due = max(1, int(due_days or cycle))
        start = (start_date or today_key()).strip()
        ts = now_ms()
        with self.lock:
            pid = 'p' + os.urandom(3).hex()
            self.conn.execute(
                'INSERT INTO periodic_plans(id,title,task_id,start_date,cycle_days,due_days,'
                'active,last_created,created_at,updated_at) VALUES(?,?,?,?,?,?,1,NULL,?,?)',
                (pid, title, task_id, start, cycle, due, ts, ts))
            rev = self._bump('rev')
            self.conn.commit()
        return {'id': pid, 'title': title, 'taskId': task_id, 'startDate': start,
                'cycleDays': cycle, 'dueDays': due, 'rev': rev}

    def setting(self, key, default=None):
        with self.lock:
            row = self.conn.execute('SELECT value_json FROM settings WHERE key=?', (key,)).fetchone()
        if not row:
            return default
        v = _u(row['value_json'])
        return default if v is None else v

    # ---------------- sessions ----------------

    def load_sessions(self):
        with self.lock:
            out = []
            for r in self.conn.execute('SELECT * FROM sessions ORDER BY start_ts'):
                s = {
                    'id': r['id'], 'taskId': r['task_id'], 'todoId': r['todo_id'],
                    'start': r['start_ts'], 'end': r['end_ts'],
                    'manual': bool(r['manual']), 'auto': bool(r['auto']),
                }
                if r['task_id'] is None and r['task_name']:
                    s['taskName'] = r['task_name']      # 任务已删，只剩名字
                out.append(s)
            return out

    def add_session(self, sid, task_id, todo_id, start, end, manual=False, auto=False):
        with self.lock:
            self.conn.execute(
                'INSERT INTO sessions(id,task_id,todo_id,start_ts,end_ts,manual,auto,created_at) '
                'VALUES(?,?,?,?,?,?,?,?)',
                (sid, task_id, todo_id, int(start), int(end), _b(manual), _b(auto), now_ms()))
            srev = self._bump('srev')
            self.conn.commit()
            return srev

    def update_session(self, sid, task_id, todo_id, start, end):
        """改一段已有记录的任务 / 挂钩 / 起止时间。改过就算手动记录。"""
        with self.lock:
            row = self.conn.execute('SELECT id FROM sessions WHERE id=?', (sid,)).fetchone()
            if not row:
                raise KeyError(sid)
            self.conn.execute(
                'UPDATE sessions SET task_id=?,todo_id=?,start_ts=?,end_ts=?,'
                'manual=1,auto=0,task_name=NULL WHERE id=?',
                (task_id, todo_id, int(start), int(end), sid))
            srev = self._bump('srev')
            self.conn.commit()
            return srev

    def delete_session(self, sid):
        with self.lock:
            self.conn.execute('DELETE FROM sessions WHERE id=?', (sid,))
            srev = self._bump('srev')
            self.conn.commit()
            return srev

    def delete_sessions(self, ids):
        """按 id 批量删记录。删任务不再走这里，用 orphan_sessions。"""
        if not ids:
            return self._meta('srev')
        with self.lock:
            self.conn.executemany('DELETE FROM sessions WHERE id=?', [(i,) for i in ids])
            srev = self._bump('srev')
            self.conn.commit()
            return srev

    def orphan_sessions(self, tasks):
        """任务删了，专注记录留着：task_id 置空，把任务名存成快照。

        统计从此按这个名字单独成组显示。之后建了同名任务，save_state 里的
        _claim_orphans 会把它们认领回去，再改名就跟着新任务走了。

        tasks: [{'id': ..., 'name': ...}, ...]
        """
        rows = [(t.get('name') or '（已删除）', t.get('id')) for t in (tasks or []) if t.get('id')]
        if not rows:
            return self._meta('srev')
        with self.lock:
            self.conn.executemany(
                'UPDATE sessions SET task_id=NULL, task_name=? WHERE task_id=?', rows)
            srev = self._bump('srev')
            self.conn.commit()
            return srev

    def unhook_sessions(self, todo_id):
        """待办没了，专注记录留着，只解掉挂钩。"""
        with self.lock:
            self.conn.execute('UPDATE sessions SET todo_id=NULL WHERE todo_id=?', (todo_id,))
            srev = self._bump('srev')
            self.conn.commit()
            return srev

    def import_state(self, state):
        """整库替换：导入 JSON、清空重来都走这里。会一并清掉正在跑的计时。"""
        with self.lock:
            self.conn.execute('DELETE FROM sessions')
            self.conn.execute('DELETE FROM running_timer')
            self.conn.commit()
            rev = self.save_state(state)
            ts = now_ms()
            for s in state.get('sessions') or []:
                self.conn.execute(
                    'INSERT OR REPLACE INTO sessions(id,task_id,todo_id,start_ts,end_ts,manual,auto,'
                    'task_name,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    (s['id'], s.get('taskId'), s.get('todoId'), int(s.get('start') or 0),
                     int(s.get('end') or 0), _b(s.get('manual')), _b(s.get('auto')),
                     s.get('taskName'), ts))
            self._bump('srev')
            self.conn.commit()
            return rev

    # ---------------- 计时 ----------------

    def _running_row(self):
        return self.conn.execute('SELECT * FROM running_timer WHERE id=1').fetchone()

    def _running_dict(self, row=None):
        r = row if row is not None else self._running_row()
        if not r or not r['task_id']:
            return None
        return {
            'taskId': r['task_id'], 'todoId': r['todo_id'],
            'startTs': r['start_ts'], 'accum': r['accumulated_ms'],
        }

    def _write_running(self, task_id, todo_id, start_ts, accum):
        mode = 'relax' if not task_id else ('studying' if start_ts else 'paused')
        self.conn.execute(
            'INSERT INTO running_timer(id,task_id,todo_id,start_ts,accumulated_ms,mode,updated_at) '
            'VALUES(1,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
            'task_id=excluded.task_id, todo_id=excluded.todo_id, start_ts=excluded.start_ts, '
            'accumulated_ms=excluded.accumulated_ms, mode=excluded.mode, updated_at=excluded.updated_at',
            (task_id, todo_id, start_ts, int(accum), mode, now_ms()))

    def _commit_slice(self, row, end=None, auto=False):
        """把正在跑的这一段落成 session，返回 (新的 accum, 是否写了 session)。"""
        if not row or not row['task_id'] or not row['start_ts']:
            return (row['accumulated_ms'] if row else 0), False
        end = int(end if end is not None else now_ms())
        start = int(row['start_ts'])
        dur = max(0, end - start)
        wrote = False
        if dur >= MIN_SESSION_MS:
            sid = '%s%s' % (format(end, 'x')[-8:], os.urandom(2).hex())
            self.conn.execute(
                'INSERT INTO sessions(id,task_id,todo_id,start_ts,end_ts,manual,auto,created_at) '
                'VALUES(?,?,?,?,?,0,?,?)',
                (sid, row['task_id'], row['todo_id'], start, end, _b(auto), now_ms()))
            self._bump('srev')
            wrote = True
        return int(row['accumulated_ms']) + dur, wrote

    def timer_get(self):
        with self.lock:
            return self._running_dict()

    def timer_start(self, task_id, todo_id=None, todo_given=False):
        """开始 / 继续。换任务会先把上一段落库并清零。"""
        with self.lock:
            row = self._running_row()
            cur_task = row['task_id'] if row else None
            if cur_task and cur_task != task_id:
                self._commit_slice(row)
                self._write_running(task_id, todo_id, now_ms(), 0)
            elif cur_task:
                start_ts = row['start_ts'] or now_ms()
                new_todo = todo_id if todo_given else row['todo_id']
                self._write_running(task_id, new_todo, start_ts, row['accumulated_ms'])
            else:
                self._write_running(task_id, todo_id, now_ms(), 0)
            self.conn.commit()
            return self._running_dict()

    def timer_pause(self):
        with self.lock:
            row = self._running_row()
            if not row or not row['task_id'] or not row['start_ts']:
                return self._running_dict(row)
            accum, _ = self._commit_slice(row)
            self._write_running(row['task_id'], row['todo_id'], None, accum)
            self.conn.commit()
            return self._running_dict()

    def timer_stop(self, auto=False):
        with self.lock:
            row = self._running_row()
            if not row or not row['task_id']:
                return {'running': None, 'total': 0, 'recorded': False}
            accum, wrote = self._commit_slice(row, auto=auto)
            self._write_running(None, None, None, 0)
            self.conn.commit()
            return {'running': None, 'total': accum, 'recorded': wrote or accum >= MIN_SESSION_MS}

    def timer_hook(self, todo_id):
        with self.lock:
            row = self._running_row()
            if not row or not row['task_id']:
                return None
            self._write_running(row['task_id'], todo_id, row['start_ts'], row['accumulated_ms'])
            self.conn.commit()
            return self._running_dict()

    def autostop_check(self):
        """跑太久多半是忘了结束：落库并标记 auto，避免一觉醒来记了 9 小时。"""
        hours = self.setting('autoStopHours', DEFAULT_AUTO_STOP_HOURS)
        try:
            hours = float(hours)
        except (TypeError, ValueError):
            hours = DEFAULT_AUTO_STOP_HOURS
        if hours <= 0:
            return None
        with self.lock:
            row = self._running_row()
            if not row or not row['task_id'] or not row['start_ts']:
                return None
            if now_ms() - int(row['start_ts']) < hours * 3600000:
                return None
            return self.timer_stop(auto=True)

    # ---------------- 摘要 ----------------

    def _task_map(self):
        rows = self.conn.execute('SELECT id,name,parent_id FROM tasks').fetchall()
        return dict((r['id'], {'name': r['name'], 'parent': r['parent_id']}) for r in rows)

    def _top_task(self, tm, tid):
        seen = 0
        cur = tid
        while cur in tm and tm[cur]['parent'] and seen < 8:
            cur = tm[cur]['parent']
            seen += 1
        return cur

    def _todo_due_ts(self, r):
        kind = r['kind']
        if kind == 'loop':
            start = r['cycle_start'] or today_key()
            return day_start_ms(add_days(start, int(r['cycle_days'] or 1)))
        if kind == 'dated':
            if not r['due_date']:
                return None
            return day_start_ms(r['due_date']) + _hm_ms(r['due_time'] or '23:59')
        return None

    def _todo_due_day(self, r):
        if r['kind'] == 'loop':
            return add_days(r['cycle_start'] or today_key(), max(1, int(r['cycle_days'] or 1)) - 1)
        if r['kind'] == 'dated':
            return r['due_date']
        return None

    def _todo_overdue(self, r):
        if r['done']:
            return False
        due = self._todo_due_ts(r)
        if due is None:
            return False
        if r['kind'] == 'loop':
            return today_key() >= add_days(r['cycle_start'] or today_key(), int(r['cycle_days'] or 1))
        return now_ms() > due

    def _todo_minutes(self, r):
        base = int(r['time_base'] or 0)
        row = self.conn.execute(
            'SELECT COALESCE(SUM(end_ts-start_ts),0) AS ms FROM sessions '
            'WHERE todo_id=? AND start_ts>=?', (r['id'], base)).fetchone()
        return int(round(row['ms'] / 60000.0))

    def _day_window(self, day=None):
        d = day or today_key()
        a = day_start_ms(d)
        return d, a, a + DAY_MS

    # ---- 周期自定待办：模板本身不是待办，每轮起点提醒她建一条有期限的出来 ----

    def _plan_cycle_start(self, p):
        start = p['start_date'] or today_key()
        cycle = max(1, int(p['cycle_days'] or 7))
        passed = max(0, diff_days(start, today_key()))
        return add_days(start, (passed // cycle) * cycle)

    def _plan_due_date(self, p, start):
        return add_days(start, max(1, int(p['due_days'] or p['cycle_days'] or 1)) - 1)

    def _plan_rows(self, tm, todos):
        """每个在用的周期模板当前是什么状态。

        本轮已经建出待办就报那条的真实进度，还没建就是「待确认」。
        排序：逾期 → 今日到期 → 待确认 → 还有几天 → 已完成。
        """
        d = today_key()
        out = []
        for p in self.conn.execute('SELECT * FROM periodic_plans WHERE active=1'):
            start = self._plan_cycle_start(p)
            td = next((r for r in todos
                       if r['plan_id'] == p['id'] and r['plan_start'] == start), None)
            row = {'title': p['title'], 'task': tm.get(p['task_id'], {}).get('name'),
                   'every_days': max(1, int(p['cycle_days'] or 7)), 'minutes': 0, 'id': None}
            if td is None:
                row['due'] = self._plan_due_date(p, start)
                row['state'] = '待确认'
                rank = (2, diff_days(d, row['due']))
            else:
                row['id'] = td['id']
                row['minutes'] = self._todo_minutes(td)
                row['due'] = self._todo_due_day(td) or self._plan_due_date(p, start)
                if td['done']:
                    row['state'], rank = '已完成', (4, 0)
                elif self._todo_overdue(td):
                    n = max(1, diff_days(row['due'], d))
                    row['state'], rank = '逾期%d天' % n, (0, -n)
                elif row['due'] == d:
                    row['state'], rank = '今日到期', (1, 0)
                else:
                    left = diff_days(d, row['due'])
                    row['state'], rank = '剩%d天' % left, (3, left)
            out.append((rank, row))
        out.sort(key=lambda x: x[0])
        return [r for _, r in out]

    def summary_today(self, day=None):
        with self.lock:
            d, a, b = self._day_window(day)
            tm = self._task_map()
            running = self._running_dict()

            rows = self.conn.execute(
                'SELECT * FROM sessions WHERE end_ts>? AND start_ts<? ORDER BY start_ts', (a, b)).fetchall()
            by_top = {}
            focus_ms = 0
            for s in rows:
                ms = min(s['end_ts'], b) - max(s['start_ts'], a)
                focus_ms += ms
                top = self._top_task(tm, s['task_id'])
                if top in tm:
                    by_top[top] = by_top.get(top, 0) + ms

            todos = self.conn.execute('SELECT * FROM todos').fetchall()
            plan_rows = self._plan_rows(tm, todos)
            # 周期自定待办只在 plans 段露面，不在 due / overdue 里重复一遍
            in_plan = set(r['id'] for r in todos if r['plan_id'])

            due_today = []
            overdue = []
            for r in todos:
                if r['done'] or r['id'] in in_plan:
                    continue
                dd = self._todo_due_day(r)
                item = {
                    'id': r['id'], 'title': r['title'],
                    'task': tm.get(r['task_id'], {}).get('name'),
                    'minutes': self._todo_minutes(r),
                    'due': (r['due_time'] or '23:59') if r['kind'] == 'dated' else '23:59',
                    'due_day': dd,
                }
                if self._todo_overdue(r):
                    item['overdue_days'] = max(1, diff_days(dd, d)) if dd else 1
                    overdue.append(item)
                elif dd == d:
                    due_today.append(item)
            due_today.sort(key=lambda x: x['due'])
            overdue.sort(key=lambda x: x['overdue_days'])       # 刚逾期的排前面

            comps = self.conn.execute(
                'SELECT * FROM settlements WHERE day=? ORDER BY at_ts DESC', (d,)).fetchall()
            completed = [{
                'id': c['todo_id'], 'title': c['title'],
                'task': tm.get(c['task_id'], {}).get('name'),
                'minutes': c['minutes'], 'status': c['kind'],
            } for c in comps]

            status = {'mode': 'relax', 'task': None, 'todo': None, 'elapsed_minutes': 0}
            if running:
                elapsed = running['accum']
                if running['startTs']:
                    elapsed += now_ms() - running['startTs']
                todo_name = None
                if running['todoId']:
                    tr = self.conn.execute('SELECT title FROM todos WHERE id=?',
                                           (running['todoId'],)).fetchone()
                    todo_name = tr['title'] if tr else None
                status = {
                    'mode': 'studying' if running['startTs'] else 'paused',
                    'task': tm.get(running['taskId'], {}).get('name'),
                    'todo': todo_name,
                    'elapsed_minutes': int(round(elapsed / 60000.0)),
                }

            top_list = sorted(by_top.items(), key=lambda kv: -kv[1])[:5]
            return {
                'date': d,
                'status': status,
                'today': {
                    'focus_minutes': int(round(focus_ms / 60000.0)),
                    'session_count': len(rows),
                    'by_top_task': [{'task': tm[k]['name'], 'minutes': int(round(v / 60000.0))}
                                    for k, v in top_list],
                    'settled_count': len(comps),
                    'overdue_count': len(overdue),
                    'due_today_count': len(due_today),
                    'plan_count': len(plan_rows),
                },
                'due_today_unfinished': due_today[:8],
                'overdue_unfinished': overdue[:5],
                'periodic_plans': plan_rows[:6],
                'completed_today': completed[:8],
            }

    def summary_brief(self):
        """summary_today 的压缩版：同样三段，每段只留 3 条。巡逻高频调这个。"""
        t = self.summary_today()
        return {
            'status': t['status']['mode'],
            'task': t['status']['task'],
            'todo': t['status']['todo'],
            'today_min': t['today']['focus_minutes'],
            'top': [[x['task'], x['minutes']] for x in t['today']['by_top_task'][:3]],
            'due': [[x['title'], x['due'], x['minutes']] for x in t['due_today_unfinished'][:3]],
            'overdue': [[x['title'], '逾期%d天' % x['overdue_days'], x['minutes']]
                        for x in t['overdue_unfinished'][:3]],
            'plans': [[x['title'], x['state'], x['minutes']] for x in t['periodic_plans'][:3]],
            'done': [[x['title'], x['minutes'], x['status']] for x in t['completed_today'][:3]],
        }


def _hm_ms(hm):
    try:
        h, m = hm.split(':')[:2]
        return (int(h) * 60 + int(m)) * 60000
    except Exception:
        return 23 * 3600000 + 59 * 60000
