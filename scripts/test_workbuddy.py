import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'bridge'))
import workbuddy as w

fixture = {'data': {'Packages': [
    {'CycleTotalCapacity': '1000', 'CycleRemainCapacity': '750'},
    {'CycleTotalCapacity': 100, 'CycleRemainCapacity': 25},
]}}
u = w._parse_usage(fixture)
assert u['primary']['remaining'] == 70
assert u['secondary'] == {'credits': 775, 'total': 1100}
assert w._parse_usage({'data': {'Packages': [
    {'CycleTotalCapacity': 100, 'CycleRemainCapacity': 0},
]}})['primary']['remaining'] == 0
for payload in ({'data': {'Packages': []}}, {'data': {'Packages': [{'CycleTotalCapacity': 100}]}}):
    try:
        w._parse_usage(payload)
        raise AssertionError('Invalid payload accepted')
    except ValueError:
        pass
assert w._parse_usage({'data': {'limitNum': -1, 'credit': 10}}, True)['primary']['unlimited']
with tempfile.TemporaryDirectory() as folder:
    database = Path(folder) / 'workbuddy.db'
    db = sqlite3.connect(database)
    db.execute('CREATE TABLE sessions(cwd, status, updated_at, deleted_at)')
    db.executemany('INSERT INTO sessions VALUES (?, ?, ?, ?)', [
        ('/tmp/A', 'Running', 5000, None), ('/tmp/A', 'Completed', 4000, None),
        ('/tmp/B', 'Completed', 3000, None), ('/tmp/C', 'Failed', 2000, None),
        ('/tmp/hidden', 'Running', 6000, 1000),
    ])
    db.commit()
    db.close()
    with patch.object(w, 'DATABASE', database):
        tasks = w.read_tasks()
        assert list(tasks) == ['A', 'B', 'C']
        assert [t['state'] for t in tasks.values()] == ['Working', 'Done', 'Stopped']
        assert tasks['A']['updated_at'] == 5
with patch.object(w, '_cache', {'checked': 0, 'value': {'old': 1}}), \
        patch.object(w, '_fetch_usage', side_effect=ValueError):
    assert w.read_usage() is None
print('PASS: credits, zero balance, missing data, unlimited, task deduplication, deleted tasks, state mapping, stale clearing')
