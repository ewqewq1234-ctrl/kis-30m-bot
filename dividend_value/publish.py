"""Publish complete snapshots separately from partial screening checkpoints."""
from collections.abc import Mapping
from collections import Counter
from datetime import datetime
import argparse
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
        'source_run_id': os.environ.get('GITHUB_RUN_ID'),
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


def promote_existing_snapshot(input_path, now, source_sha, source_run_id=None):
    """Promote an existing verified PASS snapshot for a one-day Friday recovery."""
    snapshot = json.loads(Path(input_path).read_text(encoding='utf-8'))
    candidates = snapshot.get('candidates')
    if not isinstance(candidates, list) or not snapshot.get('one_off_partial') is True:
        raise ValueError('existing snapshot is not a verified partial PASS snapshot')
    if snapshot.get('universe_verified') is not True or snapshot.get('unprocessed') != 0:
        raise ValueError('existing snapshot does not satisfy partial recovery integrity')
    promoted = dict(snapshot)
    promoted.update({
        'promotion_date': now.date().isoformat(),
        'promotion_source_sha': source_sha,
        'recovery_allowed_date': now.date().isoformat(),
        'recovery_source_scan_date': snapshot.get('scan_date'),
        'source_run_id': source_run_id or os.environ.get('GITHUB_RUN_ID'),
        'publication_status': 'PARTIAL_CHECKPOINT',
        'published_at': now.isoformat(),
        'candidates': [r for r in candidates if r.get('status') == 'PASS'],
    })
    return promoted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--promote-existing', metavar='PATH',
                        help='Promote an existing verified partial PASS snapshot without re-screening')
    args = parser.parse_args()
    now = datetime.now(ZoneInfo('Asia/Seoul'))
    sha = os.environ.get('GITHUB_SHA')
    if not sha:
        print('Signal publication safely skipped: GITHUB_SHA is missing')
        return 0
    if args.promote_existing:
        try:
            snapshot = promote_existing_snapshot(args.promote_existing, now, sha, os.environ.get('GITHUB_RUN_ID'))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f'Signal publication safely skipped: invalid existing snapshot: {exc}')
            return 1
        output = Path('signal-publish/signals.json')
        output.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(output, snapshot)
        verified = json.loads(output.read_text(encoding='utf-8'))
        if verified != snapshot:
            raise RuntimeError('promoted snapshot verification failed')
        print('Existing verified PASS snapshot promoted as one-day Friday recovery.')
        return 0
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
