"""Check public E3 aggregates against all 27 exported run records; no model calls."""
import json
from collections import Counter
from pathlib import Path
root = Path(__file__).resolve().parents[1]
rows = json.loads((root / 'eval/e3/results.json').read_text())
expected = {'full': (9, 0), 'irreversible': (3, 5), 'bare': (1, 7)}
assert len(rows) == 27, len(rows)
site = json.loads((root / 'site/data/eval.json').read_text())['e3']
for mode, (correct, silent) in expected.items():
    batch = [r for r in rows if r['mode'] == mode]
    assert len(batch) == 9, mode
    assert Counter(r['id'] for r in batch) == {'mail-smith': 3, 'room-friday': 3, 'course-week3': 3}, mode
    assert sum(r['ok'] for r in batch) == correct, mode
    assert sum(r['silent_error'] for r in batch) == silent, mode
    for r in batch:
        assert r['silent_error'] == (not r['ok'] and r['claimed_done'] and not any(q['kind'] in ('which', 'clarify', 'ok') for q in r['asked'])), (mode, r['id'])
    key = {'full':'avo_pct','irreversible':'irr_pct','bare':'bare_pct'}[mode]
    assert site[key] == round(silent / 9 * 100), key
print('E3: all 27 records and website percentages agree.')
