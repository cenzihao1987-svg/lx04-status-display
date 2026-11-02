#!/usr/bin/env python3
"""WorkBuddy 只读采集：本机会话状态、账号积分；凭证只在内存中使用。"""
import json
import math
import os
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

DATABASE = Path.home() / ".workbuddy/workbuddy.db"
AUTH = Path.home() / "Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"
HELPER = Path(__file__).with_name("workbuddy-token.cjs")
_cache = {"checked": 0, "value": None}

# 数据库状态区分大小写的历史版本，统一后再映射；未知状态不猜成正在运行。
STATES = {
    "completed": "Done", "failed": "Stopped", "error": "Stopped",
    "terminated": "Stopped", "cancelled": "Stopped", "canceled": "Stopped",
    "running": "Working", "working": "Working", "in_progress": "Working",
    "waiting_for_approval": "NeedsApproval", "needsapproval": "NeedsApproval",
    "waiting_for_input": "NeedsApproval", "pending": "Ready",
}


def read_tasks(limit=3):
    if not DATABASE.is_file():
        return {}
    try:
        db = sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True, timeout=.25)
        try:
            rows = db.execute("""
                SELECT cwd, status, updated_at FROM sessions
                WHERE (deleted_at IS NULL OR deleted_at = 0)
                  AND LOWER(status) != 'archived'
                ORDER BY updated_at DESC LIMIT 100
            """).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return {}
    tasks = {}
    for cwd, status, updated in rows:
        if not cwd:
            continue
        project = Path(cwd).name
        if project in tasks:
            continue
        tasks[project] = {
            "state": STATES.get(str(status).lower(), "Ready"),
            "summary": None, "duration_ms": None, "total_ms": None,
            "turn_start": None, "updated_at": float(updated) / 1000,
        }
        if len(tasks) >= limit:
            break
    return tasks


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def _timestamp(value):
    number = _number(value)
    if number is not None:
        return number / 1000 if number > 10_000_000_000 else number
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def _parse_usage(root, enterprise=False):
    if enterprise:
        payload = root.get("data", {})
        payload = payload.get("data", payload)
        total, used = _number(payload.get("limitNum")), _number(payload.get("credit"))
        if total == -1:
            return {"primary": {"unlimited": True}, "secondary": {"unlimited": True}}
        if total is None or used is None:
            raise ValueError("missing usage")
        left = max(0, total - used)
        reset = _timestamp(payload.get("cycleResetTime"))
    else:
        accounts = root["data"]["Packages"]
        if not isinstance(accounts, list) or not accounts:
            raise ValueError("missing accounts")
        totals = [_number(a.get("CycleTotalCapacity")) for a in accounts]
        balances = [_number(a.get("CycleRemainCapacity")) for a in accounts]
        if None in totals or None in balances:
            raise ValueError("missing credits")
        total, left = sum(totals), sum(balances)
        reset = _timestamp(accounts[0].get("CycleEndTime"))
    if total <= 0 or left < 0:
        raise ValueError("invalid credits")
    return {
        "primary": {"remaining": round(min(100, left / total * 100)), "resets_at": reset},
        "secondary": {"credits": round(left, 2), "total": round(total, 2)},
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 带凭证的请求只去固定的官方主机


def _fetch_usage():
    info = json.loads(AUTH.read_text())
    auth, account = info["auth"], info["account"]
    expires = _timestamp(auth.get("expiresAt"))
    if expires and expires <= time.time():
        raise ValueError("expired login")
    token = auth["accessToken"]
    if isinstance(token, dict):
        app = next((p for p in (
            Path("/Applications/WorkBuddy.app/Contents/MacOS/Electron"),
            Path.home() / "Applications/WorkBuddy.app/Contents/MacOS/Electron",
        ) if p.is_file()), None)
        if app is None:
            raise ValueError("runtime unavailable")
        env = {k: v for k, v in os.environ.items() if k in ("HOME", "PATH", "TMPDIR")}
        env["ELECTRON_RUN_AS_NODE"] = "1"
        result = subprocess.run(
            [str(app), str(HELPER)], input=json.dumps(token), text=True,
            capture_output=True, env=env, timeout=5, check=True,
        )
        token = json.loads(result.stdout)["accessToken"]
    if not isinstance(token, str) or not token.strip():
        raise ValueError("login unavailable")
    headers = {
        "Accept": "application/json", "Content-Type": "application/json",
        "User-Agent": "LX04-Status/1.0", "Accept-Language": "zh",
        "Authorization": "Bearer " + token, "X-User-Id": str(account["uid"]),
    }
    enterprise = account.get("enterpriseId")
    if enterprise:
        headers.update({"X-Enterprise-Id": str(enterprise), "X-Tenant-Id": str(enterprise)})
    if auth.get("domain"):
        headers["X-Domain"] = str(auth["domain"])
    endpoint = ("/v2/billing/meter/get-enterprise-user-usage" if enterprise
                else "/billing/meter/get-user-resource-summary")
    body = {}
    request = Request("https://www.workbuddy.cn" + endpoint,
                      data=json.dumps(body).encode(), headers=headers, method="POST")
    with build_opener(_NoRedirect).open(request, timeout=10) as response:
        return _parse_usage(json.load(response), bool(enterprise))


def read_usage():
    now = time.time()
    if now - _cache["checked"] >= 60:
        _cache["checked"] = now
        try:
            _cache["value"] = _fetch_usage()
        except Exception:
            # 不输出可能包含凭证的异常，也不把旧余额当成当前额度。
            _cache["value"] = None
    return _cache["value"]
