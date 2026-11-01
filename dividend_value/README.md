# 배당·가치 조건검색 (주문 비활성)

포트폴리오 목표는 `portfolio.json`의 공격형 BAA 60%, 배당·가치 40%입니다.
현재 40%는 예약 예산이며 종목을 자동 매수하지 않습니다. 최초 매수 참고 예산은
**40% 전략자산의 3%**, 즉 계좌 총자산의 1.2%입니다. BAA의 96% 현금 버퍼와
하락 추세에서 절반으로 줄이는 기존 규칙은 유지하므로 실제 투자비중이 항상
60:40이 되지는 않습니다. 스윙 전략은 삭제되었고 기존 보유는 자동 매도하지 않습니다.

## 실행

GitHub 저장소 Settings → Secrets and variables → Actions에 아래 세 Secret을
등록합니다. 이미 등록된 KIS 키는 다시 등록할 필요가 없습니다.

- `KIS_APP_KEY`
- `KIS_APP_SECRET`
- `DART_API_KEY`

계좌번호나 MCP/별도 서버는 필요하지 않습니다. 키는 로그·아티팩트·캐시에 저장하지
않습니다. KIS 토큰은 프로세스 메모리에만 보관합니다. REST 요청 허용목록은
시세·공시·종목마스터와 토큰 발급뿐이며 계좌조회·주문 엔드포인트는 차단합니다.

병합 후 Actions → **Dividend value read-only screen** → Run workflow에서 실행합니다.
`max_symbols=0`은 전체, 양수는 분할 검색입니다. 같은 기준일·설정·입력이라면
체크포인트를 복원해 나머지를 조회하며 실패 종목은 재시도합니다. 주간 검색은
일요일 00:00 UTC = 09:00 KST입니다. 지연 실행 시 실제 최근 완료 거래일을 다시
계산합니다. 과거 시점 검색은 현재 종목마스터/주당지표를 사용하면 부정확하므로 거부합니다.

병합 전에는 기존 **Offline Ubuntu regression** 워크플로를 이 변경 브랜치로
수동 실행하고 `run_dividend_screen=true`로 지정할 수 있습니다. 이 경로는
동일한 읽기 전용 검색 워크플로를 호출합니다. 실거래 워크플로를 진단용으로 실행하지 마세요.

로컬 실행(Python 3.11 이상):

```sh
pip install -r requirements.txt
python -m unittest -v test_dividend_value
python -m dividend_value.screen --max-symbols 100
# 총자산을 입력하면 40% 전략의 첫 진입 참고 수량도 계산
python -m dividend_value.screen --account-nav 10000000
# 기존 주식 수량은 선택 입력 JSON {"005930": 3}; 기본 0주
python -m dividend_value.screen --holdings local-holdings.json
```

기존 수량 입력 파일은 저장소에 커밋하거나 업로드하지 마세요. 수량은 매수 가능 참고량
계산에만 사용하고 원본 계좌 정보는 아티팩트에 포함하지 않습니다.

## 조건과 판정

모든 수치는 `config.json`에서 수정합니다. 완화된 임시 통과 조건은 없습니다.

| 항목 | 조건 |
|---|---|
| 배당 이력 | 최근 완료 5개 회계연도 현금 보통배당 **또는** 최근 확정 보통배당/종가 ≥8% |
| 순이익 | 최근 5개년 각각 양수 |
| ROE | 최근 3개년 평균 ≥5%; 동일 귀속 순이익/연초·연말 평균 귀속자본 |
| 재무상태 | 최신 공개 부채/총자본 ≤100%, 총자본 양수, 자본금 미달 아님 |
| 영업현금흐름 | 최근 3개년 각각 양수 |
| 배당성향 | 3년 현금배당총액 합/동일 귀속 순이익 합 ≤70% |
| 배당 삭감 | 최근 보통배당이 전년 대비 30% 이상 감소하면 탈락 (정확히 30%도 탈락) |
| 배당률 | D=min(최근 보통배당, 최근 3년 중앙값); D/종가 ≥4% |
| 가치 | 0<PER≤10, 0<PBR≤1; 주식 종류·지표 기간을 검증한 EPS/BPS 사용 |
| 유동성 | 완료된 최근 20거래일 거래대금 평균 ≥1억원 |
| 매수상한 참고 | min(D/0.04, EPS×10, BPS×1.0) |

`PASS`: 필수 조건 모두 검증 완료. `FAIL`: 필수 조건 탈락 확인.
`REVIEW`: 자료·기간·귀속·우선주 구분 등 미검증. `EXCLUDED`: 금융주,
ETF·ETN·SPAC·리츠 등 대상 외 증권. 금융업은 DART 기업개황 업종코드 64/65/66으로
별도 분류하며 최종 통과 수에 포함하지 않습니다. 확인하지 못한 업종/증권종류는 REVIEW입니다.

연결 재무제표의 지배기업 귀속 순이익/자본을 우선합니다. OpenDART가 명시적으로
연결 자료 없음(013)을 반환한 연도만 별도 기준을 시도합니다. 다른 기준이 연도 간
섞이면 REVIEW입니다. 표준 계정ID를 확인하지 못하거나 서로 다른 값이 중복되면
숫자를 추정하지 않습니다. 금액은 KRW, 비율은 소수입니다. 누락=null/빈칸,
확인된 0=0, 비적용은 not_applicable로 구별합니다.

현재가의 PER/PBR은 우선 선별용 참고 지표이며 기간이 확인되지 않으면 확정 탈락이나
통과 근거로 쓰지 않습니다. 참고 지표가 상한을 넘는 종목은 REVIEW에 남기고 비싼
공시 조회를 생략합니다. 지표 누락 종목은 공시 보완 조회를 계속합니다. TTM 계산은
직전 연간+당기 누적-전년 동기 누적만 허용하며 동일 기간·단위·범위 확인이 필요합니다.
분기 3개월 이익을 네 배 하지 않습니다. 현재 TTM **주당**지표의 완전한 자동 구성은
미구현이므로 검증된 연간 EPS/BPS는 `basis=annual`로 표시합니다.

## 공시 증거와 검토 파일

OpenDART `alotMatter`는 배당 관련 행을 제공하지만 특별배당 여부, 분할 전후 주당
수치, 주식 종류별 지표까지 자동으로 확정해 주지 않습니다. 금액 급증을 특별배당으로
추정하지 않습니다. 원시 배당 행과 감사의견은 `screening_details.json`에 보존합니다.

`evidence.json`의 `stocks`에 종목코드별 검토를 기록합니다. 출처는 공식 DART/KIND/KIS
문서 URL만 허용합니다. `as_of`, `published_at`, `checked_at`, `reviewed_by`, `reason`,
`sources`를 필수로 요구하며 기준일 이후 공개 자료는 거부합니다. 검토 파일은 실제
공시를 읽고 작성해야 하며 아래 형태를 그대로 복사해 검증 완료로 취급하면 안 됩니다.

```json
{
  "schema_version": 1,
  "stocks": {
    "종목코드": {
      "as_of": "YYYY-MM-DD",
      "published_at": "YYYY-MM-DD",
      "checked_at": "YYYY-MM-DD",
      "reviewed_by": "검토자",
      "reason": "주식종류별 보통·특별배당, 확정일 및 분할 조정 확인 근거",
      "sources": ["https://dart.fss.or.kr/dsaf001/main.do?rcpNo=실제접수번호"],
      "fields": {
        "annual": [{
          "year": 2025,
          "share_class": "common",
          "ordinary_dps": 500,
          "total_taxable_annual_dps": 600,
          "special_dividend_verified": true,
          "split_adjustment_verified": true,
          "dividend_final": true,
          "cash_dividends_parent": 1000000000,
          "payout_scope_verified": true
        }],
        "valuation": {
          "basis": "annual",
          "period_end": "2025-12-31",
          "price_date": "YYYY-MM-DD",
          "share_class": "common",
          "eps": 1500,
          "bps": 12000,
          "basis_verified": true
        }
      }
    }
  }
}
```

매년 동일한 종류의 배당 증거가 필요합니다. 과거 재무제표가 없는 연도를 검토 파일로
만들거나 순이익·귀속 기준을 덮어쓸 수 없습니다. 우선주는 해당 종류의 배당·EPS·BPS
확인 전까지 REVIEW입니다. 12월 외 결산법인과 비표준 계정 매핑도 자동 지원이 미완료라
REVIEW로 남습니다. 감사의견 API의 당기 구분이 명확한 경우에만 자동 판정하고, 최신
재무상태·감사 범위가 불명확하면 검토가 필요합니다.

## 세금 수량 (참고)

일반 과세계좌의 소득세 14% 가정이며 지방소득세 15.4%를 사용하지 않습니다.
`최대 총수량 = ceil(1000 / (지급분별 과세배당×0.14)) - 1`로 **엄격한 미만**을 계산하고
기존 보유를 차감합니다. 특별배당도 과세분에는 포함합니다. 지급분이 불명확하면
검증된 연간 과세배당을 한 번에 수령하는 보수적 참고값으로 표시합니다. 임의의 분기
4등분은 하지 않습니다. 계산 불가라도 종목 적격 판정은 별도로 유지합니다.

[소득세법 제86조](https://law.go.kr/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1021862057)의
소액부징수 조문을 확인했으나, 증권사의 지급분 합산·원단위 절사와 개별 계좌 적용은
검증하지 못했습니다. 출력은 항상 추정이며 세금 0원을 보장하지 않습니다.

## 결과와 재개

아티팩트: `summary.html`, `candidates.csv`, `review_required.csv`,
`screening_all.csv`, `financial_excluded.csv`, `screening_details.json`,
`run_metadata.json`, `execution.log`. HTML에 전체/대상/조회 완료/미처리/오류 수,
각 판정 수, 중복 제거 PASS 기업 수, 세금 한도 아래 1주 가능한 PASS 수와 기준일을
표시합니다. 최대 30종목은 향후 보유 규칙이며 검색 결과는 30개에서 자르지 않습니다.

응답 타임아웃 10/40초, 읽기 요청 최대 3회, 최소 요청 간격 0.6초를 적용합니다.
과거 재무 캐시는 기업별 최신 정기공시 목록 지문으로 정정 공시 시 무효화합니다.
체크포인트는 종목별 성공 결과를 원자 저장하며 실패 종목은 다음 실행에서 다시 조회합니다.
실행 시작 시 45일 초과 파일과 512MiB 초과 오래된 캐시를 정리합니다. 캐시는 사라져도
재구축할 수 있습니다. 조회 완료 전 중단되거나 배치 제한이 있으면 항상 부분 검색입니다.
GitHub Actions 캐시 복원 실패/만료 시 이전 체크포인트의 지속성은 보장되지 않습니다.
API 호출량·다운로드량·캐시 크기·시간은 결과에 기록합니다.

## 현재 검증 범위와 미구현

공식 종목 마스터를 실제 다운로드하여 4,413건의 필드 구조·증권그룹·우선주 구분을
확인했습니다(2026-11-01 KST). **이는 배당 조건 전체 검색이나 PASS 수가 아닙니다.**
로컬 환경에는 KIS/OpenDART 키가 없어 인증 시세·재무 응답은 미검증입니다.
2026-11-01 20:49 KST의 [실제 읽기 전용 Actions 진단](https://github.com/ewqewq1234-ctrl/kis-30m-bot/actions/runs/41303592026)에서
KIS 키는 등록되어 있고 **DART_API_KEY만 누락**된 것을 확인했습니다. 이 실행은
외부 API 호출 전에 종료되었고 실제 검색 수/최종 통과 수는 확인되지 않았습니다.
[8개 결과 파일 아티팩트](https://github.com/ewqewq1234-ctrl/kis-30m-bot/actions/runs/41303592026/artifacts/13082192313)가 저장됐습니다.
키가 없는 실행은 조회 수 0,
기준일 미확정, 부분 검색 및 명확한 blocker를 남깁니다.

자동매매, 실시간 추적매도 감시·상태 구조, 주문 후 비중 검증, 매도 다음 거래일 재선정,
기업행사/특별배당/우선주 주당지표의 완전 자동 판독은 구현하지 않았습니다.
향후 3% 최초매수·4% 기업한도·30종목·업종20%·현금10%·추가매수 금지·거래대금1%·
15%에서 추적 활성화·고점대비5% 매도·고정 익절 없음·20거래일 재매수 대기는
`config.json`에 비활성 설정으로 보관합니다. 예약 실행만으로 실시간 감시를 보장하지 않습니다.

## 공식 출처 및 이용 범위 (2026-11-01 확인)

- [KIS 종목마스터와 공식 파서](https://github.com/koreainvestment/open-trading-api/tree/main/stocks_info)
- [KIS 현재가](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_price/inquire_price.py)
- [KIS 기간별 일봉](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_itemchartprice/inquire_daily_itemchartprice.py)
- [OpenDART 전체 재무제표](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019020)
- [OpenDART 배당](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2019005)
- [OpenDART 감사의견](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020009)
- [OpenDART 이용약관](https://opendart.fss.or.kr/intro/terms.do)

OpenDART 약관의 호출량 제한·인증키 비공유 및 자료 정확성 범위를 확인했습니다.
개인용 검색 산출물을 GitHub Actions 아티팩트로 보관하며 시세 재배포 서버를 제공하지
않습니다. KIS 계정별 시세 이용/재배포 권한은 계정 약정에 따라 별도 확인이 필요하며,
이 구현이 공개 재배포 허가를 의미하지 않습니다. 키나 계좌정보를 결과에 포함하지 않습니다.
