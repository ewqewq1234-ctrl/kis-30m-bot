"""2026-11-10 ONLY: revalidate last completed scan's PASS symbols before publishing.

Never submits, simulates, or imports brokerage order actions.
Published signals are marked as incomplete and individually verified.
"""
from datetime import date, datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .data import PublicData, atomic_json
from .screen import months_ago

TARGET_DATE = date(2026, 11, 10)
EXPECTED_AS_OF = '2026-11-09'
SOURCE_RUN = '42746704303'


def build_once(root=Path('results/dividend-value'), *, now=None, api=None):
    now = now or datetime.now(ZoneInfo('Asia/Seoul'))
    if now.date() != TARGET_DATE or not (9 <= now.hour < 15):
        raise SystemExit('One-off dividend signal publication only during 2026-11-10 KST 09:00-15:00')
    meta = json.loads((root / 'run_metadata.json').read_text(encoding='utf-8'))
    rows = json.loads((root / 'screening_details.json').read_text(encoding='utf-8'))
    if (meta.get('as_of') != EXPECTED_AS_OF
            or not str(meta.get('executed_kst', '')).startswith('2026-11-10')
            or meta.get('universe_verified') is not True
            or meta.get('unprocessed') != 0
            or meta.get('query_failed') != 12
            or meta.get('blocker') or meta.get('counts', {}).get('PASS') != 23
            or len(rows) != meta.get('universe_total')):
        raise SystemExit('One-off source artifact did not match the known completed-universe scan')
    sha = os.environ.get('GITHUB_SHA')
    if not sha or len(sha) != 40 or not all(c in '0123456789abcdef' for c in sha.lower()):
        raise SystemExit('Missing immutable GitHub source SHA')

    api = api or PublicData('.screen-cache')
    current = api.market_master()
    indexed = {r['code']: r for r in current}
    if len(indexed) < 3000:
        raise SystemExit('Current official KIS market master incomplete')
    cutoff = months_ago(date.fromisoformat(EXPECTED_AS_OF), 7)
    candidates = []
    excluded = {}
    for row in rows:
        if row.get('status') != 'PASS':
            continue
        code = str(row.get('code', ''))
        m = indexed.get(code)
        if not m or m.get('listing_date_verified') is not True:
            excluded[code] = 'UNVERIFIED:listing_date_or_master'
            continue
        listed = date.fromisoformat(m['listing_date'])
        if (listed > cutoff or m.get('risk_verified') is not True
                or m.get('risk_flagged') or m.get('exclusion')
                or row.get('query_complete') is not True
                or row.get('query_failed')
                or row.get('risk_verified') is not True
                or row.get('risk_flagged') is True
                or row.get('exclusion')):
            excluded[code] = 'IPO_AGE_OR_RISK_NOT_ELIGIBLE'
            continue
        candidates.append({**row, 'listing_date': m['listing_date'],
                           'listing_date_verified': True})
    if not candidates:
        raise SystemExit('No fully checked and 7-calendar-month-old PASS symbols')
    snapshot = {
        'as_of': EXPECTED_AS_OF, 'scan_date': TARGET_DATE.isoformat(),
        'source_sha': sha, 'source_run_id': SOURCE_RUN,
        'partial': True, 'one_off_partial': True,
        'universe_verified': True, 'unprocessed': 0,
        'query_failed': meta['query_failed'],
        'blocker': None, 'candidates': candidates,
        'excluded_by_age_or_risk': excluded,
        'review_count': meta.get('counts', {}).get('REVIEW', 0),
        'review_alerts': {
            r['code']: [k for k in ('no_30pct_cut', 'capital_not_impaired')
                        if r.get('checks', {}).get(k) is False]
            for r in rows
            if any(r.get('checks', {}).get(k) is False
                   for k in ('no_30pct_cut', 'capital_not_impaired'))
        },
    }
    return snapshot


def main():
    snapshot = build_once()
    atomic_json('signal-publish/signals.json', snapshot)
    print(json.dumps({'one_off': True, 'age_verified_PASS':len(snapshot['candidates']),
                      'excluded_age_or_risk':len(snapshot['excluded_by_age_or_risk']),
                      'other_query_failures':snapshot['query_failed'],
                      'orders_submitted':0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
