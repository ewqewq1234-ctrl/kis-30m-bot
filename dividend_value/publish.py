"""Publish complete snapshots separately from partial screening checkpoints."""
from collections.abc import Mapping
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .data import atomic_json


def publication_block_reason(meta, rows, now):
    """Return a reason to skip complete publication; REVIEW rows are not blockers."""
    if not isinstance(meta, Mapping) or not isinstance(rows, list):
        return 'invalid scan metadata or result rows'
    counts = meta.get('counts')
    if not isinstance(counts, Mapping):
        return 'missing result counts'
    required_counts = ('PASS', 'FAIL', 'REVIEW', 'EXCLUDED')
    if any(not isinstance(counts.get(key), int) or counts[key] < 0 for key in required_counts):
        return 'invalid result counts'
    universe_total = meta.get('universe_total')
    if not isinstance(universe_total, int) or universe_total <= 0:
        return 'universe total is missing or invalid'
    if len(rows) != universe_total or sum(counts[k] for k in required_counts) != universe_total:
        return 'scan does not cover the verified universe'
    row_counts = Counter(r.get('status') for r in rows if isinstance(r, Mapping))
    if len(row_counts) == 0 or any(row_counts.get(k, 0) != counts[k] for k in required_counts):
        return 'row statuses do not match verified result counts'
    if sum(row_counts.values()) != len(rows):
        return 'one or more result rows have an invalid status'
    if meta.get('universe_verified') is not True:
        return 'market universe was not verified'
    if meta.get('partial') is not False:
        return 'scan is marked partial'
    # REVIEW is allowed at universe level, but those rows are never candidates.
    # The only incomplete-validation flag permitted is the presence of REVIEW rows.
    if meta.get('validation_incomplete') is not (counts['REVIEW'] > 0):
        return 'validation-incomplete flag does not match REVIEW count'
    if meta.get('blocker'):
        return 'scan has a blocking error'
    if meta.get('unprocessed') != 0:
        return 'unprocessed securities remain'
    if meta.get('query_failed') != 0:
        return 'official data requests failed'
    if not meta.get('as_of'):
        return 'market data cutoff is missing'
    if not os.environ.get('GITHUB_SHA'):
        return 'GITHUB_SHA is missing'
    return None


def build_snapshot(meta, rows, now, source_sha):
    counts = meta['counts']
    return {
        'scan_date': now.date().isoformat(),
        'as_of': meta['as_of'],
        'source_sha': source_sha,
        'partial': bool(meta['partial']),
        'universe_verified': meta['universe_verified'],
        'universe_total': meta['universe_total'],
        'processed_total': len(rows),
        'unprocessed': meta['unprocessed'],
        'query_failed': meta['query_failed'],
        'validation_incomplete': bool(counts['REVIEW']),
        'counts': counts,
        'candidates': [r for r in rows if r.get('status') == 'PASS'],
        'review_alerts': {
            r['code']: r.get('reasons', [])
            for r in rows
            if r.get('status') == 'REVIEW' and r.get('code')
        },
        'publication_status': 'COMPLETE' if not meta['partial'] else 'PARTIAL_CHECKPOINT',
        'published_at': now.isoformat(),
    }


def main():
    root = Path('results/dividend-value')
    meta = json.loads((root / 'run_metadata.json').read_text(encoding='utf-8'))
    rows = json.loads((root / 'screening_details.json').read_text(encoding='utf-8'))
    now = datetime.now(ZoneInfo('Asia/Seoul'))
    sha = os.environ.get('GITHUB_SHA')
    if not sha:
        print('Signal publication safely skipped: GITHUB_SHA is missing')
        return 0
    snapshot = build_snapshot(meta, rows, now, sha)
    partial_path = Path('signal-publish/partial-signals.json')
    if meta.get('partial') is True:
        partial_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(partial_path, snapshot)
        print('Verified PASS partial checkpoint preserved separately.')
        return 0
    reason = publication_block_reason(meta, rows, now)
    if reason:
        print(f'Signal publication safely skipped: {reason}')
        return 0

    atomic_json('signal-publish/signals.json', snapshot)
    print('Verified complete signal snapshot created.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
