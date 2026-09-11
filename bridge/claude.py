#!/usr/bin/env python3
"""Claude Code 侧的采集：会话状态 + 订阅额度。

只暴露两个纯函数，不认识 agent 字典、不认识锁、不认识 HTTP：
    read_tasks(limit=3) -> {project: task}
    read_usage()        -> {"primary": {...}, "secondary": {...}} | None

mibe 只支持 Codex 和 Kimi，Claude 这一侧全部自己读。
"""
import json
import os
import re
import struct
import subprocess
import time
from datetime import datetime
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
CACHE = Path.home() / "Library" / "Application Support" / "Claude" / "Cache" / "Cache_Data"
TAIL = 131072  # 只读尾部 128KB
ZSTD = next(
    (p for p in ("/opt/homebrew/bin/zstd", "/usr/local/bin/zstd") if os.path.exists(p)),
    None,
)

# 这些行是 UI 记账，不代表会话在干什么。
# 尤其是 last-prompt：它是一轮「开始」时写的 resume 书签，不是收尾标记——
# 把它当边界会把所有正在干活的会话判成空闲。
NOISE = {
    "attachment", "queue-operation", "custom-title", "mode",
    "atis-latch", "last-prompt", "summary", "system",
}


def _encode(path):
    """Claude 给项目目录起名的规则。中文会全部塌成横杠，所以不可逆。"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _project_root(cwd, dirname):
    """从 cwd 上溯到「编码后等于目录名」的那一级。

    不能拿目录名反解：中文项目名编码后是一串横杠，不同项目还会撞名。
    cwd 本身可能是子目录（实测拿到过 小米音箱改造/效果图），所以要逐级往上试。
    """
    p = Path(cwd)
    while True:
        if _encode(p) == dirname:
            return p
        if p.parent == p:
            return None
        p = p.parent


def _tail_objects(path):
    size = path.stat().st_size
    with path.open("rb") as fh:
        fh.seek(max(0, size - TAIL))
        text = fh.read().decode("utf-8", "ignore")
    lines = text.split("\n")
    if size > TAIL:
        lines = lines[1:]  # 丢掉第一个残行
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _state(objs):
    """最后一条有意义的行说了算。顺序扫，后面的覆盖前面的。"""
    st = "Done"
    for o in objs:
        if o.get("isSidechain") is True:  # subagent 不算主会话
            continue
        kind = o.get("type")
        if kind in NOISE:
            continue
        if kind == "user":
            st = "Working"  # 用户提问 或 tool_result 回填，都意味着模型要跑
        elif kind == "assistant":
            content = (o.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            kinds = {b.get("type") for b in content if isinstance(b, dict)}
            if "tool_use" in kinds or "thinking" in kinds:
                st = "Working"
            elif "text" in kinds:
                st = "Done"  # 纯文本 = 这一轮的回答，多半收尾了
    return st


def _last_cwd(objs):
    for o in reversed(objs):
        cwd = o.get("cwd")
        if cwd:
            return cwd
    return None


def read_tasks(limit=3):
    """最近几个 Claude 项目的状态。键是项目名，值的形状和 Codex 侧一致。"""
    try:
        entries = list(PROJECTS.iterdir())
    except OSError:
        return {}

    dirs = []
    for d in entries:
        if not d.is_dir():
            continue
        top = list(d.glob("*.jsonl"))  # subagents/ 下的不参与状态判定
        if not top:
            continue
        try:
            # 新鲜度取整棵树：一次 10 分钟的 subagent 会让主文件安静到被误判 Stale
            fresh = max(f.stat().st_mtime for f in d.rglob("*.jsonl"))
            main = max(top, key=lambda p: p.stat().st_mtime)
        except (OSError, ValueError):
            continue
        dirs.append((fresh, main, d.name))
    dirs.sort(key=lambda x: x[0], reverse=True)

    now = time.time()
    out = {}
    for fresh, main, dirname in dirs:
        if len(out) >= limit:
            break
        try:
            objs = _tail_objects(main)
        except OSError:
            continue
        cwd = _last_cwd(objs)
        if not cwd:
            continue
        root = _project_root(cwd, dirname)
        if root is None:
            continue
        # .claude-mem 那类常驻后台会话永远在跑，不排掉会永久霸占「活跃」判定
        if any(part.startswith(".") for part in root.parts):
            continue
        name = root.name
        if name in out:
            continue

        state = _state(objs)
        # 模型说一句话再调工具，中间那一瞬会被读成 Done；文件还在写就不算结束
        if state == "Done" and now - main.stat().st_mtime < 3:
            state = "Working"

        out[name] = {
            "state": state,
            "summary": None,
            "duration_ms": None,
            "updated_at": fresh,
            "total_ms": None,  # Claude 没有 duration_ms，不硬造
            "turn_start": None,
        }
    return out


# ── 额度：解 Claude Desktop 的 Chromium 磁盘缓存 ──
_cache = {"path": None, "mtime": 0.0, "value": None, "scanned": 0.0}


def _decode_entry(raw):
    """Chromium Simple Cache 条目：byte 12 起 4 字节小端是 key 长度，key 之后是 body。"""
    klen = struct.unpack("<I", raw[12:16])[0]
    if not 0 < klen < 4096:
        return None, None
    return raw[24 : 24 + klen].decode("utf-8", "ignore"), raw[24 + klen :]


def _unzstd(body):
    i = body.find(b"\x28\xb5\x2f\xfd")
    if i < 0 or not ZSTD:
        return None
    out = subprocess.run(
        [ZSTD, "-d", "-c", "--no-progress"],
        input=body[i:], capture_output=True, timeout=10,
    ).stdout.decode("utf-8", "ignore")
    # zstd frame 后面还跟着 Chromium 的元数据，按最后一个右括号截断
    a, b = out.find("{"), out.rfind("}")
    return out[a : b + 1] if 0 <= a < b else None


def _window(block, minutes):
    if not isinstance(block, dict) or block.get("utilization") is None:
        return None
    resets = None
    raw = block.get("resets_at")
    if raw:
        try:
            resets = datetime.fromisoformat(raw).timestamp()
        except ValueError:
            pass
    return {
        "remaining": round(100 - block["utilization"]),
        "window_minutes": minutes,
        "resets_at": resets,
    }


def _parse(text):
    u = json.loads(text)
    weekly = u.get("seven_day")
    if not isinstance(weekly, dict) or weekly.get("utilization") is None:
        # 某些套餐分开计，取用得更多的那个
        alts = [u.get(k) for k in ("seven_day_opus", "seven_day_sonnet")]
        alts = [a for a in alts if isinstance(a, dict) and a.get("utilization") is not None]
        weekly = max(alts, key=lambda a: a["utilization"]) if alts else None
    out = {
        "primary": _window(u.get("five_hour"), 300),
        "secondary": _window(weekly, 10080),
    }
    return out if out["primary"] or out["secondary"] else None


def _read_path(path):
    raw = path.read_bytes()
    key, body = _decode_entry(raw)
    if not key or "/usage" not in key or "organizations" not in key:
        return None
    text = _unzstd(body)
    return _parse(text) if text else None


def read_usage():
    """订阅额度。形状和 bridge/run.py 的 _usage() 一致，页面不用分支。

    来源是 Claude Desktop 的磁盘缓存，所以它没在跑就会陈旧——
    宁可返回 None 让页面显示「—」，也不显示过期的数字。
    """
    now = time.time()

    # 上次命中的条目还在且没变，直接用缓存（一次 stat 的事）
    if _cache["path"]:
        try:
            mtime = _cache["path"].stat().st_mtime
            if mtime != _cache["mtime"]:
                value = _read_path(_cache["path"])
                if value:
                    _cache.update(mtime=mtime, value=value)
            fresh = _fresh(_cache["value"], _cache["mtime"], now)
            if fresh:
                return fresh
            # 拿到的是陈旧数据：不能就这么返回 None 卡死，
            # Claude Desktop 多半已经把新响应写进另一个条目了，往下重扫
        except (OSError, ValueError, struct.error, subprocess.SubprocessError):
            pass
        _cache["path"] = None

    if now - _cache["scanned"] < 30:  # Claude Desktop 没装时别每 2 秒白扫上万个文件
        return _fresh(_cache["value"], _cache["mtime"], now)
    _cache["scanned"] = now

    try:
        files = sorted(CACHE.glob("*_0"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in files:
        try:
            value = _read_path(path)
        except (OSError, ValueError, struct.error, subprocess.SubprocessError):
            continue
        if value:
            mtime = path.stat().st_mtime
            _cache.update(path=path, mtime=mtime, value=value)
            return _fresh(value, mtime, now)
    return None


def _fresh(value, mtime, now):
    """只看缓存本身的新鲜度。

    不因为「窗口 resets_at 已过」就丢弃：额度不会凭空消失，窗口重置只会让它变多，
    所以旧值是保守的（低估剩余），比什么都不显示强——那会让屏幕上出现两个大横线。
    Codex 那边也没有这条检查，这样两边行为一致。
    """
    if not value:
        return None
    if now - mtime > 6 * 3600:
        return None
    return value
