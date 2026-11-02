"""离线验证 DeepSeek 余额、币种、凭证筛选和任务归属。"""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))
import deepseek as d


def balance(currency, total, granted):
    return {"currency": currency, "total_balance": total, "granted_balance": granted}


value = d._parse_usage({"is_available": True, "balance_infos": [
    balance("USD", "10.00", "1.00"), balance("CNY", "30.71", "0.00"),
]})
assert value["primary"]["amount"] == 30.71
assert value["primary"]["currency"] == "CNY"
assert value["secondary"]["amount"] == 0
zero = d._parse_usage({"is_available": False, "balance_infos": [balance("USD", "0", "0")]})
assert zero["primary"]["amount"] == 0 and zero["primary"]["note"] == "API unavailable"
for payload in ({}, {"is_available": True, "balance_infos": [balance("CNY", "NaN", "0")]}):
    try:
        d._parse_usage(payload)
        raise AssertionError("Invalid balance accepted")
    except ValueError:
        pass
upstream = {"baseUrl": "https://api.deepseek.com/v1", "status": "active",
            "serviceType": "openai", "apiKeys": ["", "test-key"]}
assert d._api_key({"responsesUpstream": [upstream]}) == "test-key"
for override in ({"baseUrl": "https://api.deepseek.com.example.org"}, {"status": "inactive"}):
    try:
        d._api_key({"responsesUpstream": [{**upstream, **override}]})
        raise AssertionError("Unapproved upstream accepted")
    except ValueError:
        pass
with tempfile.TemporaryDirectory() as folder:
    path = Path(folder) / "session.jsonl"
    def write(provider, events):
        rows = [{"type": "session_meta", "payload": {"model_provider": provider, "cwd": "/tmp/Test"}}]
        rows += events
        path.write_text("\n".join(json.dumps(row) for row in rows))
        d._session_providers.clear()
    started = {"type": "event_msg", "payload": {"type": "task_started"}}
    write("openai", [started])
    assert d._read_task(path) is None and not d.is_deepseek_session(path)
    write("deepseek", [started])
    assert d._read_task(path)[1]["state"] == "Working" and d.is_deepseek_session(path)
    question = {"type": "response_item", "payload": {"type": "function_call",
                "name": "request_user_input_async", "call_id": "question"}}
    unrelated = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "other"}}
    write("deepseek", [started, question, unrelated])
    assert d._read_task(path)[1]["state"] == "NeedsApproval"
    answer = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "question"}}
    write("deepseek", [started, question, unrelated, answer])
    assert d._read_task(path)[1]["state"] == "Working"
    write("deepseek", [started, {"type": "event_msg", "payload": {"type": "task_complete"}}])
    assert d._read_task(path)[1]["state"] == "Done"
    import run
    agent = run._new_agent("Codex")
    with patch.object(run.mibe, "CODEX_SESSIONS_DIR", Path(folder)):
        run._seed_codex(agent)
        assert not agent["tasks"]  # DeepSeek 不能在 Codex 历史任务里重复显示
        write("openai", [started])
        run._seed_codex(agent)
        assert "Test" in agent["tasks"]  # 原 Codex 来源继续正常显示
with patch.object(d, "_usage_cache", {"checked": 0, "value": {"old": True}}), \
        patch.object(d, "_fetch_usage", side_effect=ValueError):
    assert d.read_usage() is None
print("PASS: balances, currency isolation, zero/missing data, official key selection, task ownership, approval matching, stale clearing")
