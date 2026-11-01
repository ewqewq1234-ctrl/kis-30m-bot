"""Credential/API connectivity check only. No ranking, account or order calls."""
from .data import PublicData

api = PublicData('.screen-cache')
quote = api.kis('/uapi/domestic-stock/v1/quotations/inquire-price',
                {'FID_COND_MRKT_DIV_CODE':'J','FID_INPUT_ISCD':'005930'})
print('KIS official price response verified:', bool(quote.get('output', {}).get('stck_prpr')))
company = api.dart('company.json', {'corp_code':'00126380'})
if company.get('status') != '000' or company.get('stock_code') != '005930':
    raise SystemExit('DART company response could not be verified')
print('OpenDART official company response verified: True; no screening or orders performed')
