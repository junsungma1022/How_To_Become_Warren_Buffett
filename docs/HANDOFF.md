# 인수인계 (HANDOFF) — 다음 세션이 먼저 읽는 파일

갱신: 2026-11-23 · 발표 2026-12-01 (개인)

## 지금 상태
- 코드 1차 완성: 뉴스(RTPR) → 종목 범위 → Jev → Claude → 5분 안 +5% 신호 → 3칸 매수 → 이전 프로그램 사다리 매도 → Discord → 일일 보고서 · KNIME 데이터셋 스크립트
- 2026-11-19: 매수 · 매도를 이전 프로그램 규칙으로 교체 (D-9) · 안전장치 % 기준 (D-10) · 24/7 무인 운영 보강 (D-11)
- 2026-11-23 (사용자 승인): 하루 경계 KST 10:00 (D-12) · 애프터 끝 ET 19:50 보정 (D-13) · 시작 때 계좌 대조 (D-14) · 매수 상한 +10% 임시 (D-15)
- 시험 pytest (네트워크 차단) 통과. **실제 API 와는 아직 한 번도 붙여 보지 않았다** (키는 사람 맥 `.env` 에만 · Claude 는 열지 않음)
- 실거래 아직 안 함. 기본은 shadow. **엔진을 한 번도 켜지 않았다** (data/ · logs/ 없음 · 토스 키 아직 안 넣음 — 2026-11-23)

## 사람이 할 일 (순서대로)
1. `.env` 채우기 · `pre-commit install` · 토스 USD 환전 · 허용 IP 등록 · OpenRouter 월 한도
2. `scripts/check_setup.py --discord` 모두 ✅ · '애프터 끝: 장 캘린더 … → 엔진이 쓰는 값 …' 줄을 확인 (D-13 · 19:50 ET 이어야 함)
3. `run_live.py --mode shadow` 로 하루 미국 장 시간 돌려 보기 → 기사 처리 · Discord 확인
4. `build_dataset.py` 단계별 실행 (가격 단계도 토스 키 필요) → KNIME 워크플로우 A · B 만들기 → `.knwf` 커밋 → B 결정트리로 매수 상한 다시 정하기 (D-15)
5. `install_launchd.sh --engine shadow` 로 하루 → 문제없으면 `settings.yaml` 의 `mode.live_enabled: true` → `install_launchd.sh --engine live` (가능한 거래일 최대)
6. 끝나면 `daily_report.py --history` 로 전체 결과

## 확인 안 된 것 (실제로 돌려 봐야 안다)
- 토스 장 캘린더의 애프터 끝이 19:50 인지 20:00 인지 (D-13 · 둘 다 맞게 동작하지만 기록으로 확인)
- 토스 `/api/v1/holdings` 의 `marketCountry` 값 표기 (D-14 · 국내 주식은 KR/KRW 로 보고 건너뜀 · 다르면 #critical 로 드러난다)
- 토스 장외 세션 주문이 `timeInForce=DAY` 지정가로 접수되는지 (사용자는 앱 · API 장외 거래 확인했다고 함 · 문서는 DAY 를 '정규장까지 유효'로 설명) → 첫 실거래를 작은 수량으로 보고 `order_final` 이벤트 확인
- 미국 주식 가격 정정 시 새 주문의 수량이 '남은 수량'인지
- shadow 모드도 토스 키가 있어야 돈다 (종목 범위 · 시세 · 장 캘린더를 토스에서 받는다)
- Jev `typesafe/jev-1.13` 이 OpenRouter 에서 그대로 응답하는지 (check_setup 이 확인)
- RTPR WebSocket 이 지금 키로 붙는지 (구독 상태)

## 파일 지도
`docs/ARCHITECTURE.md` 설계 · `docs/DECISIONS.md` 결정 · `docs/SECURITY.md` 보안 · `knime/README.md` KNIME
