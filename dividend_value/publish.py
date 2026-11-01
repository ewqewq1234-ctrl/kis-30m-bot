"""Build a Tuesday signal snapshot, never account data or credentials."""
from datetime import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
from .data import atomic_json


def main():
    root = Path('results/dividend-value')
    meta = json.loads((root/'run_metadata.json').read_text(encoding='utf-8'))
    rows = json.loads((root/'screening_details.json').read_text(encoding='utf-8'))
    now = datetime.now(ZoneInfo('Asia/Seoul'))
    if now.weekday() != 1 or meta['partial'] or meta.get('blocker'):
        raise SystemExit('No complete Tuesday scan; signal publication blocked')
    atomic_json('signal-publish/signals.json', {'as_of':meta['as_of'],
        'source_sha':os.environ['GITHUB_SHA'], 'scan_date':now.date().isoformat(),
        'partial':False,'candidates':[r for r in rows if r['status']=='PASS'],
        'review_alerts':{r['code']: [k for k in ('no_30pct_cut','audit','capital_not_impaired')
            if r.get('checks',{}).get(k) is False] for r in rows
            if any(r.get('checks',{}).get(k) is False for k in ('no_30pct_cut','audit','capital_not_impaired'))},
        'review_count':meta['counts']['REVIEW']})


if __name__ == '__main__': main()
