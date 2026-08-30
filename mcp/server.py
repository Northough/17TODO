#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""17TODO 的 stdio MCP server。

给 Nortia 用：查 17 在不在学、学了多久、今天还有什么没做完，必要时替她开关计时、记待办。

由 Claude Code 的 --mcp-config 拉起，不需要单独的 systemd 服务和端口。
只包 docs/api.md 里列的那几个安全接口，一律薄封装——名字解析、字段默认值、
校验全在 HTTP 那边做，这里不碰数据结构，更不做读-改-写整个 state。

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


def register(spec: Dict[str, Any]) -> Callable[[Handler], Handler]:
    def wrap(fn: Handler) -> Handler:
        TOOLS.append((spec, fn))
        return fn
    return wrap


def text_result(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False, indent=2)
    return {'content': [{'type': 'text', 'text': payload}]}


# ---------------- 只读 ----------------

@register({
    'name': 'get_brief_summary',
    'description': '17 现在在不在学、学的什么、今天学了多久。回包很短，可以频繁问。'
                   'status：studying 正在计时 / paused 有任务但暂停 / relax 没在学。',
    'inputSchema': {'type': 'object', 'properties': {}},
})
def _brief(args: Dict[str, Any]) -> Any:
    return _req('GET', '/api/summary/brief')


@register({
    'name': 'get_today_summary',
    'description': '今天的详细情况：各科时长、专注次数、今天到期还没做完的待办、已结算的待办。'
                   '比 get_brief_summary 详细，一次问够，别反复刷。',
    'inputSchema': {
        'type': 'object',
        'properties': {'date': {'type': 'string', 'description': '日期 YYYY-MM-DD，不填是今天'}},
    },
})
def _today(args: Dict[str, Any]) -> Any:
    d = str(args.get('date') or '').strip()
    return _req('GET', '/api/summary/today' + ('?date=' + d if d else ''))


# ---------------- 计时 ----------------
# 任务名和待办名由服务端解析，写错了报错里会列出现有的名字，照着改就行。

@register({
    'name': 'start_timer',
    'description': '开始给某个任务计时。换任务会自动把上一段落库。'
                   '只在 17 明确要开始、或让你替她开的时候用，别自己判断她该学习了就开。',
    'inputSchema': {
        'type': 'object',
        'properties': {
            'task': {'type': 'string', 'description': '任务名，比如「数据结构」'},
            'todo': {'type': 'string', 'description': '可选，把这段时间挂到某条待办上'},
        },
        'required': ['task'],
    },
})
def _start(args: Dict[str, Any]) -> Any:
    body = {'task': str(args.get('task') or '')}
    if args.get('todo'):
        body['todo'] = str(args['todo'])
    r = _req('POST', '/api/timer/start', body)
    return {'ok': True, 'running': r.get('running')}


@register({
    'name': 'pause_timer',
    'description': '暂停计时。这一段会落库，累计时长保留，可以再 start_timer 继续。',
    'inputSchema': {'type': 'object', 'properties': {}},
})
def _pause(args: Dict[str, Any]) -> Any:
    return {'ok': True, 'running': _req('POST', '/api/timer/pause', {}).get('running')}


@register({
    'name': 'stop_timer',
    'description': '结束计时并记账。不足 15 秒不记。',
    'inputSchema': {'type': 'object', 'properties': {}},
})
def _stop(args: Dict[str, Any]) -> Any:
    r = _req('POST', '/api/timer/stop', {})
    return {'ok': True, 'minutes': round((r.get('total') or 0) / 60000.0, 1),
            'recorded': r.get('recorded')}


# ---------------- 写待办 ----------------

@register({
    'name': 'create_todo',
    'description': '新建一条待办。loop 按天循环（配 cycle_days）、dated 有期限（配 due_date）、'
                   'open 不限期。确认 17 真的要你记，别自作主张往她清单里塞东西。',
    'inputSchema': {
        'type': 'object',
        'properties': {
            'title': {'type': 'string', 'description': '待办内容'},
            'task': {'type': 'string', 'description': '可选，挂到哪个任务名下，计时会归到它'},
            'kind': {'type': 'string', 'enum': ['loop', 'dated', 'open'], 'description': '默认 loop'},
            'cycle_days': {'type': 'integer', 'description': 'loop 用：几天一轮，默认 1（每天）'},
            'due_date': {'type': 'string', 'description': 'dated 用：截止日 YYYY-MM-DD'},
            'on_expire': {'type': 'string', 'enum': ['reset', 'keep'],
                          'description': 'loop 逾期后：reset 重置已投入时间（默认）/ keep 挂着标红'},
        },
        'required': ['title'],
    },
})
def _create_todo(args: Dict[str, Any]) -> Any:
    body = {'title': args.get('title'), 'kind': args.get('kind') or 'loop'}
    for src, dst in (('task', 'task'), ('cycle_days', 'cycleDays'),
                     ('due_date', 'dueDate'), ('on_expire', 'onExpire')):
        if args.get(src):
            body[dst] = args[src]
    r = _req('POST', '/api/todos', body)
    return {'ok': True, 'created': r.get('title'), 'kind': r.get('kind'), 'id': r.get('id')}


@register({
    'name': 'create_periodic_plan',
    'description': '新建周期自定待办：每隔 cycle_days 天，页面会提醒 17 创建一条有期限的待办。'
                   '它本身不是待办，是个提醒模板。适合「每周一套真题」这种。',
    'inputSchema': {
        'type': 'object',
        'properties': {
            'title': {'type': 'string', 'description': '每轮要创建的待办内容'},
            'task': {'type': 'string', 'description': '可选，挂到哪个任务名下'},
            'cycle_days': {'type': 'integer', 'description': '几天提醒一次，默认 7'},
            'due_days': {'type': 'integer', 'description': '创建出来的待办给几天期限，默认同 cycle_days'},
            'start_date': {'type': 'string', 'description': '周期起点 YYYY-MM-DD，不填是今天'},
        },
        'required': ['title'],
    },
})
def _create_plan(args: Dict[str, Any]) -> Any:
    body = {'title': args.get('title')}
    for src, dst in (('task', 'task'), ('cycle_days', 'cycleDays'),
                     ('due_days', 'dueDays'), ('start_date', 'startDate')):
        if args.get(src):
            body[dst] = args[src]
    r = _req('POST', '/api/plans', body)
    return {'ok': True, 'created': r.get('title'), 'every_days': r.get('cycleDays'),
            'id': r.get('id')}


# ---------------- MCP 协议 ----------------

def handle(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = msg.get('method')
    req_id = msg.get('id')
    try:
        if method == 'initialize':
            return {'jsonrpc': '2.0', 'id': req_id, 'result': {
                'protocolVersion': '2024-11-05',
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {'name': '17todo', 'version': '0.1.0'},
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
