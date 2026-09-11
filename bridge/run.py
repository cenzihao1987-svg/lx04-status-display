#!/usr/bin/env python3
"""状态屏服务：挂在 mibe 的 Codex 事件流上，把状态输出给 LX04。

用法（参数原样透传给 mibe monitor）：
    vendor/mibe/.venv/bin/python bridge/run.py --codex-only --no-speaker
"""
import asyncio
import json
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vendor" / "mibe"))

import mibe  # noqa: E402

import claude  # noqa: E402  bridge/claude.py，Claude 侧的采集

DISPLAY = ROOT / "display"
# 页面要的静态资源就这几类，多的不开
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".mp4": "video/mp4",
}
PORT = 8477
IDLE_END_SECONDS = 900     # 15 分钟没动静就当它结束了。
# 这里没有「可能卡住」的中间态，是故意的：Working 靠「最后一条事件不是结束事件」判出来，
# 而正常收尾却没留下 task_complete 的会话会一直挂在 Working。那种误判占绝大多数，
# 报成红色告警只是噪音——真卡死的会话，你在终端那头会先发现。
APPROVAL_TTL = 43200       # 「该你了」最多喊 12 小时——你处理完会有新事件把它顶掉，
                           # 顶不掉的只有一种情况：那个终端已经被关了

_lock = threading.Lock()  # 一把锁管住 AGENTS 全部，两边写入频率都极低，不拆


def _new_agent(name):
    """一个 agent 的全部状态。两边形状必须一致，_agent_view 才能一份代码服务两个。"""
    return {
        "name": name,
        "tasks": {},        # project -> {state, summary, duration_ms, updated_at, total_ms, turn_start}
        "current": None,    # 最近有动静的项目，主状态取它
        "usage": None,      # 额度是账号级的，不跟项目走
        "last_event": time.time(),
        "pending": {},      # project -> call_id，审批分项目，否则会被别的项目的输出清掉
    }


AGENTS = {"codex": _new_agent("Codex"), "claude": _new_agent("Claude")}
_active = "codex"  # 当前该显示谁

# 播报开关。没登录小米（--no-speaker）时 _can_speak 为 False，屏幕上不显示开关。
_can_speak = False
_speak_on = False
_notifier = None
_loop = None


def _first_line(text):
    if not text:
        return None
    line = text.strip().split("\n")[0]
    line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
    line = re.sub(r"[*`]", "", line)
    return line[:80]


def _usage(limits):
    """额度。屏幕显示的是「剩余」，在这里就把用量翻过来，页面不再做算术。
    resets_at / window_minutes 是倒计时和「3.9h / 5h」那行的数据源，别丢。
    来源不稳定时返回 None，由页面显示「暂不可用」，不显示猜测值。"""
    if not limits:
        return None
    out = {}
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if isinstance(window, dict) and window.get("used_percent") is not None:
            out[key] = {
                "remaining": round(100 - window["used_percent"]),
                "window_minutes": window.get("window_minutes"),
                "resets_at": window.get("resets_at"),
            }
    credits = limits.get("credits")
    if isinstance(credits, dict) and credits.get("balance"):
        try:
            out["credits"] = round(float(credits["balance"]), 2)
        except (TypeError, ValueError):
            pass
    return out or None


def _approval_prompt(payload):
    """Codex 是否卡在等你？是则返回屏幕上要显示的那句话。"""
    args = payload.get("arguments")
    parsed = None
    if isinstance(args, str) and args.strip():
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            parsed = None
    if not isinstance(parsed, dict):
        return None

    if str(payload.get("name") or "").startswith("request_user_input"):
        for item in parsed.get("questions") or []:
            if not isinstance(item, dict):
                continue
            # header 只是分类标签（「数据口径」这种），走到音箱前看不出在问什么
            for key in ("question", "title", "header"):
                text = mibe._sanitize_codex_question_text(item.get(key))
                if text:
                    return text[:80]
        return "Codex 在等你回答"

    if parsed.get("sandbox_permissions") == "require_escalated":
        text = mibe._sanitize_codex_question_text(
            parsed.get("justification")
        ) or mibe._sanitize_codex_question_text(parsed.get("cmd"))
        return (text or "Codex 请求授权")[:80]

    return None


def _payload(line):
    try:
        return json.loads(line).get("payload") or {}
    except ValueError:
        return {}


def _seed_codex(agent, limit=3):
    """启动时把最近几个会话填进来，屏幕一开就有内容。

    不用 mibe 的 --replay-existing：latest 只给一个会话，all 要读 5000+ 个文件。
    cwd 在首行 session_meta 里；累计耗时得把整个文件过一遍，靠子串预筛做到很快。
    """
    ENDED = {"task_complete": "Done", "turn_aborted": "Stopped"}
    try:
        sessions = Path(os.path.expanduser(str(mibe.CODEX_SESSIONS_DIR)))
        recent = sorted(
            sessions.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )[: limit * 4]
    except OSError:
        return

    for path in recent:
        if len(agent["tasks"]) >= limit:
            break
        try:
            with path.open(encoding="utf-8", errors="ignore") as fh:
                try:
                    cwd = (json.loads(fh.readline()).get("payload") or {}).get("cwd")
                except ValueError:
                    continue
                if not cwd:
                    continue
                project = Path(cwd).name
                if project in agent["tasks"]:  # 同项目更早的会话，最近那次已经代表它了
                    continue

                # 关心的行占比极低，先用子串挡掉 99%，再解析。
                # 111MB 全扫 0.16 秒 —— 累计耗时必须全量，只读文件尾会漏掉大半。
                total_ms, state, limits = 0, "Done", None
                for line in fh:
                    if '"duration_ms"' in line:
                        payload = _payload(line)
                        if payload.get("type") in ENDED:
                            state = ENDED[payload["type"]]
                            total_ms += payload.get("duration_ms") or 0
                    elif '"task_started"' in line:
                        if _payload(line).get("type") == "task_started":
                            state = "Working"
                    elif '"rate_limits"' in line:
                        payload = _payload(line)
                        if payload.get("type") == "token_count":
                            limits = payload.get("rate_limits")
        except OSError:
            continue

        agent["tasks"][project] = {
            "state": state,
            "summary": None,
            "duration_ms": None,
            "updated_at": path.stat().st_mtime,
            "total_ms": total_ms,
            "turn_start": None,  # 历史会话没有正在跑的 turn
        }
        if agent["current"] is None:
            agent["current"] = project
        if agent["usage"] is None and limits:
            agent["usage"] = _usage(limits)


def observe(event, path, notifier):
    """mibe.STATUS_OBSERVER 挂载点，每条 Codex 事件都会走这里。"""
    global _notifier

    agent = AGENTS["codex"]
    # 开关真值在 bridge 这边，趁 mibe 用它之前同步过去（本函数就跑在播报逻辑之前）
    _notifier = notifier
    notifier.muted = not _speak_on

    payload = event.get("payload")
    if not isinstance(payload, dict):
        return
    kind = event.get("type")
    etype = payload.get("type")

    with _lock:
        agent["last_event"] = time.time()

        cwd = payload.get("cwd")
        project = Path(cwd).name if cwd else agent["current"]
        if project is None:
            return
        agent["current"] = project
        task = agent["tasks"].setdefault(
            project,
            {
                "state": "Ready",
                "summary": None,
                "duration_ms": None,
                "updated_at": 0.0,
                "total_ms": 0,      # 已完成 turn 的耗时之和
                "turn_start": None, # 正在跑的 turn 从什么时候开始
            },
        )
        task["updated_at"] = agent["last_event"]

        if kind == "event_msg":
            if etype == "task_started":
                task.update(
                    state="Working", summary=None, duration_ms=None,
                    turn_start=agent["last_event"],
                )
                agent["pending"].pop(project, None)
            elif etype == "task_complete":
                task.update(
                    state="Done",
                    summary=_first_line(payload.get("last_agent_message")),
                    duration_ms=payload.get("duration_ms"),
                    turn_start=None,
                )
                task["total_ms"] += payload.get("duration_ms") or 0
                agent["pending"].pop(project, None)
            elif etype == "turn_aborted":
                task.update(
                    state="Stopped",
                    summary=None,
                    duration_ms=payload.get("duration_ms"),
                    turn_start=None,
                )
                task["total_ms"] += payload.get("duration_ms") or 0
                agent["pending"].pop(project, None)
            elif etype == "token_count":
                agent["usage"] = _usage(payload.get("rate_limits")) or agent["usage"]

        elif kind == "response_item":
            if etype == "function_call":
                prompt = _approval_prompt(payload)
                if prompt:
                    task.update(state="NeedsApproval", summary=prompt, duration_ms=None)
                    agent["pending"][project] = payload.get("call_id")
            elif etype in ("function_call_output", "custom_tool_call_output"):
                if agent["pending"].get(project) == payload.get("call_id"):
                    agent["pending"].pop(project, None)
                    task.update(state="Working", summary=None)


def _claude_tick():
    """把 Claude 侧的采集结果换进 AGENTS。

    解析全在锁外做完，锁内只换指针——绝不能拿着 _lock 去跑 zstd 子进程，
    HTTP 线程正等着同一把锁应答 /api/status。
    """
    tasks = claude.read_tasks(limit=3)
    usage = claude.read_usage()
    with _lock:
        agent = AGENTS["claude"]
        agent["tasks"] = tasks
        agent["current"] = next(iter(tasks), None)
        agent["usage"] = usage
        if tasks:
            agent["last_event"] = max(t["updated_at"] for t in tasks.values())


def _claude_loop():
    """独立线程轮询。不塞进 mibe 的 asyncio 循环：这里全是阻塞 I/O，
    会卡住 Codex 事件跟读和播报。"""
    while True:
        try:
            _claude_tick()
        except Exception as exc:  # 采集出问题绝不能带走进程
            print(f"[claude] 采集失败：{exc}", file=sys.stderr)
        time.sleep(2)


# 谁在干活谁优先；同档看谁刚有动静
_PRIORITY = {"NeedsApproval": 3, "Working": 2, "Done": 1, "Stopped": 1, "Ready": 0}


def _rank(view):
    head = view["tasks"][0] if view["tasks"] else None
    if not head:
        return (0, 0)
    return (_PRIORITY.get(head["state"], 0), -head["age_seconds"])


def _pick_active(views):
    """严格更优才换台，打平留在原地——这一条就是完整的迟滞，不需要额外防抖。"""
    global _active
    best = max(views, key=lambda k: _rank(views[k]))
    if _rank(views[best]) > _rank(views[_active]):
        _active = best
    return _active


def _agent_view(agent, now):
    """一个 agent 的对外视图。两个 agent 共用这一份，靠形状一致换来的。"""
    tasks = []
    for project, t in agent["tasks"].items():
        idle = now - t["updated_at"]
        state = t["state"]
        # 告警要有时效：一个被关掉的终端最后一个事件是 task_started，
        # 永远等不到收尾，不设上限它会一直挂着
        if state == "Working" and idle > IDLE_END_SECONDS:
            state = "Stopped"
        elif state == "NeedsApproval" and idle > APPROVAL_TTL:
            state = "Stopped"
        total_ms = t["total_ms"]
        if total_ms is not None and t["turn_start"]:
            total_ms += int((now - t["turn_start"]) * 1000)
        tasks.append(
            {
                "project": project,
                "state": state,
                "summary": t["summary"],
                "duration_ms": t["duration_ms"],
                "total_ms": total_ms,
                "age_seconds": int(idle),
            }
        )
    tasks.sort(key=lambda x: x["age_seconds"])
    head = next((t for t in tasks if t["project"] == agent["current"]), None)
    return {
        "name": agent["name"],
        "state": head["state"] if head else "Ready",
        "project": head["project"] if head else None,
        "summary": head["summary"] if head else None,
        "duration_ms": head["duration_ms"] if head else None,
        "usage": agent["usage"],
        "tasks": tasks,
        "age_seconds": int(now - agent["last_event"]),
    }


def snapshot():
    now = time.time()
    with _lock:
        views = {k: _agent_view(a, now) for k, a in AGENTS.items()}
    active = _pick_active(views)

    return {
        "active": active,
        "agents": views,
        # None = 这次启动没有播报能力，页面据此隐藏开关
        "speaking": _speak_on if _can_speak else None,
    }


async def _mute_now(notifier):
    """关播报时把音箱交还给用户：任务中音量被 mibe 压成 0，得还回去。"""
    notifier.muted = True
    await notifier.stop_keepalive()
    await notifier.restore_volume()


def set_speaking(on):
    """从 HTTP 线程切播报。返回新状态，没有播报能力时返回 None。"""
    global _speak_on
    if not _can_speak:
        return None
    _speak_on = bool(on)
    # _notifier 为 None 说明还没处理过任何事件，也就没动过音量，无需善后
    if not _speak_on and _notifier is not None and _loop is not None:
        asyncio.run_coroutine_threadsafe(_mute_now(_notifier), _loop)
    return _speak_on


def _stamp(html):
    """把页面里的 __V__ 换成媒体文件指纹。

    视频重烘后文件名不变，浏览器揣着 max-age=86400 的旧副本不撒手；
    更糟的是烘到一半被读走的那个残缺副本也会被缓存住，
    之后每次都报 MEDIA_ERR_SRC_NOT_SUPPORTED，看起来像文件坏了。
    """
    try:
        v = int(max(p.stat().st_mtime for p in DISPLAY.glob("*.mp4")))
    except (ValueError, OSError):
        v = 0
    return html.replace(b"__V__", str(v).encode())


def _parse_range(header, total):
    """只认单段 bytes=s-e。多段是给下载器用的，播放器不发，不解析直接当没有。

    返回 None 表示「按 200 全量发」——对无效 Range 这是可接受的降级，
    比 416 更不容易让老播放器卡住。
    """
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    s, _, e = header[6:].strip().partition("-")
    try:
        if s:
            start, end = int(s), int(e) if e else total - 1
        elif e:  # bytes=-500 是「最后 500 字节」
            start, end = max(0, total - int(e)), total - 1
        else:
            return None
    except ValueError:
        return None
    end = min(end, total - 1)
    return (start, end) if 0 <= start <= end else None


class Handler(BaseHTTPRequestHandler):
    # Range 是 HTTP/1.1 的东西。用 1.0 回 206 属于协议违规，浏览器缓存这种响应的
    # 行为是未定义的——实测会把某个分片当成整个文件存住，之后每次都报
    # MEDIA_ERR_SRC_NOT_SUPPORTED，看起来像视频文件坏了，其实文件好好的。
    protocol_version = "HTTP/1.1"
    def do_GET(self):
        if self.path.startswith("/api/status"):
            body = json.dumps(snapshot(), ensure_ascii=False).encode()
            self._send(body, "application/json; charset=utf-8")
        elif urlparse(self.path).path in ("/", "/index.html"):
            try:
                body = (DISPLAY / "index.html").read_bytes()
            except FileNotFoundError:
                self.send_error(404)
                return
            self._send(_stamp(body), "text/html; charset=utf-8")
        else:
            self._send_file(unquote(urlparse(self.path).path).lstrip("/"))

    def _send_file(self, name):
        """display/ 下的静态资源。只放行白名单后缀，且不许 ../ 爬出目录。"""
        root = DISPLAY.resolve()
        target = (root / name).resolve()
        if (
            not name
            or target.suffix.lower() not in MIME
            or root not in target.parents
            or not target.is_file()
        ):
            self.send_error(404)
            return
        # 没有验证器，浏览器就没法确认手里那份（尤其是分片拼起来的）还算不算数
        st = target.stat()
        etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
        if target.suffix.lower() not in (".html", ".mp4") \
                and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = target.read_bytes()
        ctype = MIME[target.suffix.lower()]
        # 老 Chromium 的 <video> 后端一定发 Range，不给 206 它就干脆不播。
        # 这里的文件都是 MB 级，整读再切片就够，不值得为它引入流式读。
        rng = _parse_range(self.headers.get("Range"), len(body))
        if rng:
            self._send_partial(body, ctype, rng[0], rng[1], etag)
            return
        # 视频一律不缓存。Chromium 61 缓存分片响应的行为不可靠——实测会把半个文件
        # 存住，之后每次刷新都报 MEDIA_ERR_SRC_NOT_SUPPORTED。局域网重传 3MB 不到一秒，
        # 拿这点带宽换掉一整类偶发故障是划算的。
        # HTML 同样不缓存：改了页面刷新看不到，排查时会误判成代码没生效。
        ext = target.suffix.lower()
        cache = "no-store" if ext in (".html", ".mp4") else "max-age=86400"
        self._send(body, ctype, cache=cache, ranges=True, etag=etag)

    def _send_partial(self, body, ctype, start, end, etag=None):
        chunk = body[start:end + 1]
        self.send_response(206)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, len(body)))
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "max-age=86400")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/api/speak":
            self.send_error(404)
            return
        # /api/speak?on=1 开，on=0 关，不带参数就翻转
        raw = parse_qs(url.query).get("on")
        on = not _speak_on if raw is None else raw[0] in ("1", "true", "on")
        result = set_speaking(on)
        if result is None:
            self.send_error(409, "no speaker this run")
            return
        body = json.dumps({"speaking": result}).encode()
        self._send(body, "application/json; charset=utf-8")

    def _send(self, body, ctype, cache="no-store", ranges=False, etag=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        if ranges:  # 不声明的话，有些播放器根本不会来试 Range
            self.send_header("Accept-Ranges", "bytes")
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _lan_ip():
    """局域网地址。gethostbyname 在 Mac 上常返回 127.0.0.1，LX04 用不了。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))  # TEST-NET 地址，不发包，只为让内核选出网卡
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    global _can_speak, _speak_on

    args = mibe.build_parser().parse_args(["monitor", *sys.argv[1:]])
    # 没登录小米就没有播报能力，运行时也开不出来——这就是「默认关闭」
    _can_speak = not args.no_speaker
    _speak_on = _can_speak

    mibe.STATUS_OBSERVER = observe
    _seed_codex(AGENTS["codex"])
    try:
        _claude_tick()  # 同步跑一次，第一次 /api/status 就有 Claude 数据
    except Exception as exc:
        print(f"[claude] 首次采集失败：{exc}", file=sys.stderr)
    threading.Thread(target=_claude_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    ip = _lan_ip()
    print(f"[status] 状态屏  http://{ip}:{PORT}/")
    print(f"[status] 接口    http://{ip}:{PORT}/api/status")

    async def runner():
        global _loop
        _loop = asyncio.get_running_loop()
        return await mibe.cmd_monitor(args)

    return asyncio.run(runner())


if __name__ == "__main__":
    raise SystemExit(main())
