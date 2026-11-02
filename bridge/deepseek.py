#!/usr/bin/env python3
"""DeepSeek 只读采集：官方 API 余额、明确归属 DeepSeek 的本机 Codex 会话。"""
import json
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

CONFIG = Path.home() / ".config/config.json"
SESSIONS = Path.home() / ".codex/sessions"
TAIL_BYTES = 131072
_usage_cache = {"checked": 0, "value": None}
_tasks_cache = {"checked": 0, "value": {}}
_session_providers = {}


def is_deepseek_session(path):
    path = Path(path)
    if path not in _session_providers:
        try:
            with path.open("rb") as stream:
                meta = json.loads(stream.readline()).get("payload") or {}
        except (OSError, ValueError):
            return False
        _session_providers[path] = str(meta.get("model_provider", "")).lower() == "deepseek"
    return _session_providers[path]


def _api_key(config):
    for upstream in config.get("responsesUpstream", []):
        url = urlparse(upstream.get("baseUrl", ""))
        if (url.scheme != "https" or url.hostname != "api.deepseek.com"
                or upstream.get("status") != "active"
                or upstream.get("serviceType") != "openai"):
            continue
        for key in upstream.get("apiKeys", []):
            if isinstance(key, str) and key.strip():
                return key.strip()
    raise ValueError("official DeepSeek configuration unavailable")


def _amount(raw):
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        raise ValueError("invalid balance") from None
    if not value.is_finite() or value < 0:
        raise ValueError("invalid balance")
    return float(value)


def _parse_usage(payload):
    infos = payload.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        raise ValueError("missing balances")
    # 一次只显示一种币种，不能把人民币和美元相加。优先级与本机 DeepSeek 面板相同。
    info = next((b for b in infos if b.get("currency") == "CNY"), None)
    info = info or next((b for b in infos if b.get("currency") == "USD"), None)
    if info is None or not isinstance(payload.get("is_available"), bool):
        raise ValueError("invalid balance response")
    currency = info["currency"]
    total, granted = _amount(info.get("total_balance")), _amount(info.get("granted_balance"))
    return {
        "primary": {"amount": total, "currency": currency,
                    "note": "API available" if payload["is_available"] else "API unavailable"},
        "secondary": {"amount": granted, "currency": currency, "note": "Included in total"},
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_usage():
    key = _api_key(json.loads(CONFIG.read_text()))
    request = Request("https://api.deepseek.com/user/balance", headers={
        "Authorization": "Bearer " + key, "Accept": "application/json",
        "User-Agent": "LX04-Status/1.0",
    })
    with build_opener(_NoRedirect).open(request, timeout=10) as response:
        return _parse_usage(json.load(response))


def read_usage():
    now = time.time()
    if now - _usage_cache["checked"] >= 60:
        _usage_cache["checked"] = now
        try:
            _usage_cache["value"] = _fetch_usage()
        except Exception:
            # 凭证和异常不写日志，失败不继续展示旧余额。
            _usage_cache["value"] = None
    return _usage_cache["value"]


def _read_task(path):
    with path.open("rb") as stream:
        meta = json.loads(stream.readline()).get("payload") or {}
        if str(meta.get("model_provider", "")).lower() != "deepseek" or not meta.get("cwd"):
            return None
        size = path.stat().st_size
        if size > TAIL_BYTES:
            stream.seek(size - TAIL_BYTES)
            stream.readline()  # 丢弃截断的首行
        lines = stream.read().decode("utf-8", "ignore").splitlines()
    state, pending = "Ready", None
    question_tools = {"request_user_input", "request_user_input_async"}
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        data = item.get("payload") or {}
        if item.get("type") == "event_msg":
            kind = data.get("type")
            if kind == "task_started":
                state = "Working"
            elif kind == "task_complete":
                state = "Done"
                pending = None
            elif kind == "turn_aborted":
                state = "Stopped"
                pending = None
        elif item.get("type") == "response_item":
            if data.get("type") == "function_call" and data.get("name") in question_tools:
                state = "NeedsApproval"
                pending = data.get("call_id")
            elif (data.get("type") == "function_call_output" and pending
                  and data.get("call_id") == pending):
                state = "Working"
                pending = None
    return Path(meta["cwd"]).name, {
        "state": state, "summary": None, "duration_ms": None,
        "total_ms": None, "turn_start": None, "updated_at": path.stat().st_mtime,
    }


def read_tasks(limit=3):
    now = time.time()
    if now - _tasks_cache["checked"] >= 10:
        _tasks_cache["checked"] = now
        tasks = {}
        try:
            paths = sorted(SESSIONS.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            paths = []
        for path in paths[:300]:
            try:
                task = _read_task(path)
            except (OSError, ValueError):
                continue
            if task and task[0] not in tasks:
                tasks[task[0]] = task[1]
            if len(tasks) >= limit:
                break
        _tasks_cache["value"] = tasks
    return dict(list(_tasks_cache["value"].items())[:limit])
