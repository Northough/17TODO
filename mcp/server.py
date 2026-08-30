#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""17TODO 的 stdio MCP server。

给 Nortia 用：查 17 在不在学、学了多久、今天还有什么没做完，必要时替她收尾计时、记待办。

由 Claude Code 的 --mcp-config 拉起，不需要单独的 systemd 服务和端口。
只包 docs/api.md 里列的安全接口，一律薄封装——名字解析、字段默认值、校验全在
HTTP 那边做，这里不碰数据结构，更不做读-改-写整个 state。

懒加载：tools/list 里只给一行描述和裸参数名，完整说明（什么时候别用、字段细节、
报错怎么办）放在 DETAIL 里，Nortia 需要时调 get_todo_schema 取。
名字带 todo 是为了跟 galatea-garden 的 get_tool_schema 区分开，光靠命名空间前缀
容易挑错，两边参数还都叫 name。
这样常驻在 system prompt 里的固定开销压到最低。

口令默认从 17TODO 的 data/.env 读，所以 nortia-mcp.json 里不用写密钥。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get('TODO_URL', 'http://127.0.0.1:8310').rstrip('/')


def _token() -> str:
    tok = os.environ.get('TODO_TOKEN', '').strip()
    if tok:
        return tok
    path = os.environ.get('TODO_ENV', os.path.join(ROOT, 'data', '.env'))
    try:
        with open(path) as f:
            for line in f:
                k, _, v = line.strip().partition('=')
                if k == 'TODO_TOKEN':
                    return v.strip()
    except OSError:
        pass
    return ''


def _req(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
    data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    tok = _token()
    if tok:
        req.add_header('Authorization', 'Bearer ' + tok)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode('utf-8')
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        try:                                    # 400 的报错文案是给人看的，原样透出去
            msg = json.loads(raw).get('error') or raw
        except ValueError:
            msg = raw
        if e.code == 401:
            msg = '口令不对或没读到。检查 %s/data/.env' % ROOT
        raise RuntimeError(msg[:300])
    except urllib.error.URLError as e:
        raise RuntimeError('连不上 17TODO（%s）：%s。先看 systemctl status 17todo' % (BASE, e.reason))


# ---------------- 工具注册 ----------------

Handler = Callable[[Dict[str, Any]], Any]
TOOLS = []  # type: List[Tuple[Dict[str, Any], Handler]]
DETAIL = {}  # type: Dict[str, str]


def register(spec: Dict[str, Any], detail: str = '') -> Callable[[Handler], Handler]:
    def wrap(fn: Handler) -> Handler:
        TOOLS.append((spec, fn))
        if detail:
            DETAIL[spec['name']] = detail.strip()
        return fn
    return wrap


def text_result(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False, indent=2)
    return {'content': [{'type': 'text', 'text': payload}]}


def obj(**props: Any) -> Dict[str, Any]:
    return {'type': 'object', 'properties': props}


STR = {'type': 'string'}
INT = {'type': 'integer'}


# ---------------- 只读 ----------------

@register(
    {'name': 'get_brief_summary', 'description': '17 现在在不在学、学什么、今天多久。很短，可常问。',
     'inputSchema': obj()},
    """
返回：status / task / todo / today_min / top / due / done

- `status`：`studying` 正在计时 / `paused` 有任务但暂停 / `relax` 没在学
- `top`：今天各顶级科目时长 `[[科目, 分钟], ...]`，子任务算到根上
- `due`：今天到期还没做完的 `[[标题, 截止时间, 已投入分钟], ...]`
- `done`：今天已结算的 `[[标题, 分钟, 状态], ...]`

高频查这个就够。要看细节再用 get_today_summary，别两个都调。
""")
def _brief(args: Dict[str, Any]) -> Any:
    return _req('GET', '/api/summary/brief')


@register(
    {'name': 'get_today_summary', 'description': '今天详细：各科时长、到期未完成、已结算。',
     'inputSchema': obj(date=STR)},
    """
参数：`date` 可选，`YYYY-MM-DD`，不填是今天。

比 get_brief_summary 多的东西：专注次数、已结算条数、逾期条数、
每条待办的 id 和已投入时长。列表有截断（各 8 条）。

一次问够，别反复刷。

注意：周期待办的跨天滚动是页面打开时才算的。17 几天没开页面的话，
上一轮的循环待办会显示成逾期——这是实话，不是 bug，但别拿它当"她真的拖了 N 天"的证据，
先看她最近有没有打开过。
""")
def _today(args: Dict[str, Any]) -> Any:
    d = str(args.get('date') or '').strip()
    return _req('GET', '/api/summary/today' + ('?date=' + d if d else ''))


# ---------------- 计时 ----------------

@register(
    {'name': 'stop_timer', 'description': '结束当前计时并记账。', 'inputSchema': obj()},
    """
返回 `minutes` 和 `recorded`。不足 15 秒不记账（`recorded: false`）。

**只在 17 明确让你收尾时用。** 她可能只是离开一会儿还会回来，你替她停了，
这段时间就断成两截。拿不准就先 get_brief_summary 看看，然后问她。

开始和继续计时没有对应工具，那是她自己在页面上按的，你不要代劳。
超过 4 小时忘记结束的情况后端会自己兜底，也不用你管。
""")
def _stop(args: Dict[str, Any]) -> Any:
    r = _req('POST', '/api/timer/stop', {})
    return {'ok': True, 'minutes': round((r.get('total') or 0) / 60000.0, 1),
            'recorded': r.get('recorded')}


# ---------------- 写待办 ----------------

@register(
    {'name': 'create_todo', 'description': '新建一条待办。',
     'inputSchema': obj(title=STR, task=STR, kind=STR, cycle_days=INT,
                        due_date=STR, on_expire=STR)},
    """
参数：

- `title` 必填，待办内容
- `task` 可选，挂到哪个任务名下，计时会归到它
- `kind`：`loop` 按天循环（默认，配 `cycle_days`，默认 1 天）/
  `dated` 有期限（配 `due_date`，`YYYY-MM-DD`，不能是过去）/ `open` 不限期
- `on_expire`（只对 loop）：`reset` 逾期后重置已投入时间（默认）/ `keep` 一直挂着标红

`task` 直接传任务名，服务端解析：先精确匹配，再唯一子串匹配。
写错或有歧义会报错，报错里列出现有任务名，照着改就行——不用先去查一遍任务列表。

**确认 17 真的要你记再写。** 她随口提一句"我得背单词"不等于要你建待办，
往她清单里塞东西她要一条条删。
""")
def _create_todo(args: Dict[str, Any]) -> Any:
    body = {'title': args.get('title'), 'kind': args.get('kind') or 'loop'}
    for src, dst in (('task', 'task'), ('cycle_days', 'cycleDays'),
                     ('due_date', 'dueDate'), ('on_expire', 'onExpire')):
        if args.get(src):
            body[dst] = args[src]
    r = _req('POST', '/api/todos', body)
    return {'ok': True, 'created': r.get('title'), 'kind': r.get('kind'), 'id': r.get('id')}


@register(
    {'name': 'create_periodic_plan', 'description': '新建周期提醒模板（不是待办本身）。',
     'inputSchema': obj(title=STR, task=STR, cycle_days=INT, due_days=INT, start_date=STR)},
    """
参数：`title` 必填；`task` 可选；`cycle_days` 几天提醒一次（默认 7）；
`due_days` 创建出来的待办给几天期限（默认同 cycle_days）；`start_date` 周期起点（默认今天）。

它本身**不是**待办，是个模板：每到周期起点，页面会弹窗提醒 17 创建一条有期限的待办，
她确认了才真的建出来。适合「每周一套真题」这种她需要自己决定什么时候做的事。

想直接建一条现在就要做的事，用 create_todo，别用这个。
""")
def _create_plan(args: Dict[str, Any]) -> Any:
    body = {'title': args.get('title')}
    for src, dst in (('task', 'task'), ('cycle_days', 'cycleDays'),
                     ('due_days', 'dueDays'), ('start_date', 'startDate')):
        if args.get(src):
            body[dst] = args[src]
    r = _req('POST', '/api/plans', body)
    return {'ok': True, 'created': r.get('title'), 'every_days': r.get('cycleDays'),
            'id': r.get('id')}


# ---------------- 懒加载入口 ----------------

@register(
    {'name': 'get_todo_schema', 'description': '取某个 17todo 工具的完整说明。写操作前先查。',
     'inputSchema': obj(name=STR)},
    '')
def _schema(args: Dict[str, Any]) -> Any:
    name = str(args.get('name') or '').strip()
    if not name:
        return {'tools': [s['name'] for s, _ in TOOLS]}
    if name not in DETAIL:
        raise RuntimeError('没有这个工具：%s。现有：%s'
                           % (name, '、'.join(s['name'] for s, _ in TOOLS)))
    spec = next(s for s, _ in TOOLS if s['name'] == name)
    return '## %s\n\n%s\n\n参数结构：\n```json\n%s\n```' % (
        name, DETAIL[name], json.dumps(spec['inputSchema'], ensure_ascii=False, indent=2))


# ---------------- MCP 协议 ----------------

def handle(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = msg.get('method')
    req_id = msg.get('id')
    try:
        if method == 'initialize':
            return {'jsonrpc': '2.0', 'id': req_id, 'result': {
                'protocolVersion': '2024-11-05',
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {'name': '17todo', 'version': '0.2.0'},
            }}
        if method == 'notifications/initialized':
            return None
        if method == 'ping':
            return {'jsonrpc': '2.0', 'id': req_id, 'result': {}}
        if method == 'tools/list':
            return {'jsonrpc': '2.0', 'id': req_id,
                    'result': {'tools': [spec for spec, _ in TOOLS]}}
        if method in ('resources/list', 'prompts/list'):
            return {'jsonrpc': '2.0', 'id': req_id,
                    'result': {method.split('/')[0]: []}}
        if method == 'tools/call':
            params = msg.get('params') if isinstance(msg.get('params'), dict) else {}
            name = params.get('name')
            args = params.get('arguments') if isinstance(params.get('arguments'), dict) else {}
            for spec, fn in TOOLS:
                if spec.get('name') == name:
                    return {'jsonrpc': '2.0', 'id': req_id, 'result': text_result(fn(args))}
            raise RuntimeError('没有这个工具：%s' % name)
        if req_id is None:
            return None
        return {'jsonrpc': '2.0', 'id': req_id,
                'error': {'code': -32601, 'message': 'Method not found: %s' % method}}
    except Exception as exc:
        return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': -32000, 'message': str(exc)}}


def main() -> None:
    buffer = ''
    for chunk in sys.stdin:
        buffer += chunk
        while '\n' in buffer:
            line, buffer = buffer.split('\n', 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            resp = handle(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + '\n')
                sys.stdout.flush()


if __name__ == '__main__':
    main()
