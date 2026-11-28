# 인수인계 (HANDOFF) — 다음 세션이 먼저 읽는 파일

갱신: 2026-11-28 · 발표 2026-12-01 (개인) · 남은 장: 11/30 (월) KST 18:00 ~ 12/1 09:50

## 지금 상태
- 코드 1차 완성: 뉴스(RTPR) → 종목 범위 → Jev → Claude → 5분 안 +5% 신호 → 3칸 매수 → 이전 프로그램 사다리 매도 → Discord → 일일 보고서 · KNIME 데이터셋 스크립트
- 2026-11-19: 매수 · 매도를 이전 프로그램 규칙으로 교체 (D-9) · 안전장치 % 기준 (D-10) · 24/7 무인 운영 보강 (D-11)
- 2026-11-23 (사용자 승인): 하루 경계 KST 10:00 (D-12) · 애프터 끝 ET 19:50 보정 (D-13) · 시작 때 계좌 대조 (D-14) · 매수 상한 +10% 임시 (D-15)
- 시험 pytest (네트워크 차단) 통과. **실제 API 와는 아직 한 번도 붙여 보지 않았다** (키는 사람 맥 `.env` 에만 · Claude 는 열지 않음)
- 2026-11-25 (사용자): 지금은 실계좌 대신 **paper 가상 계좌 $1000** 으로 돌린다 (D-16 · 손익이 다음 날 · 재시작에도 이어짐). 사용자: 토스 허용 IP 등록 · RTPR 키 입력함
- 2026-11-25 18:14 KST: **paper 엔진 상시 가동 시작** (launchd · RTPR 연결 · 알림 들어옴). check_setup 전부 ✅ (USD $0 은 live 전용 경고)
- 토스 장 캘린더 애프터 끝 = ET 19:50 그대로 (D-13 보정 안 걸림 · 확인 완료) · 11/27 조기마감 정규 KST 03:00 · 애프터 06:50
- 대시보드 · 실시간 보기 · 한 줄 명령 `scripts/hbwb.sh` (D-17)
- 첫 밤 실측 (11-25 18:12 ~ 11-26 09:00): 기사 975 → 시총 범위 통과 138 → Jev 호재 33 → Claude 33 모두 PASS → 매수 0 · Claude 1회 $0.017 (Opus) · AI $0.58
- 2026-11-26 (사용자 · D-19): 시가총액 제한 없앰 · Claude Opus 5.5 → Sonnet 5.5 ($5 크레딧) · 다음 장 11/27 KST 18:00 전에 `hbwb restart`
- KNIME 가이드를 강의자료(12 · 13주차) 설정값 그대로 고침 (knime/README.md)
- 두 장 (11/25 · 11/27) 실측: Claude 67 회 모두 PASS → 매수 0 · 매도 0 (D-20). `replay_live.py` 로 다시 돌려 봄: Claude PASS 67 중 5분 안 +5~10% 신호 11 · 샀다면 3 승 8 패 · 평균 −0.79% (PASS 가 대체로 맞았다) · Jev 가 걸러낸 569 중 신호 4 (4 승 · 표본 작음)
- 2026-11-28 (사용자 승인 · D-21): Claude 에 '발행 전 등락' 과 '발행 뒤 움직임' 을 따로 준다 · 판단 규칙 = 등급 ≥ `claude.buy_min_grade` (4) 이고 희석 · 홍보성 · 발행 전 급등이 아니면 BUY · 같은 보도자료 중복은 AI 를 한 번만
- **엔진은 11-28 15:3x 에 사용자가 `hbwb stop` 으로 꺼 둠 → 월요일 18:00 KST 전에 `hbwb start` 필수**
- 2026-11-28 (D-22): KNIME 워크플로우 `knime/KNIME_project` 완성 (CSV Reader → Row Filter → Scorer · Column Filter → Partitioner → Decision Tree → Scorer · Scatter Plot) · 매수 상한 +10% → **+7%** (+7 ~ 10% 구간 29건 중 1승)
- 데이터셋 Claude 단계: 50건 중 33건만 (OpenRouter 신규 계정 분당 한도 429) · BUY 2 (둘 다 WIN · 표본 2)
- 실거래 아직 안 함

## 사람이 할 일 (순서대로)
1. `.env` 채우기 · `pre-commit install` · 토스 USD 환전 · 허용 IP 등록 · OpenRouter 월 한도
2. `scripts/check_setup.py --discord` 모두 ✅ · '애프터 끝: 장 캘린더 … → 엔진이 쓰는 값 …' 줄을 확인 (D-13 · 19:50 ET 이어야 함)
3. RTPR 대시보드 Alerts(`rtpr.io/wire/rules`)에서 **All articles** 스위치 켜기 → `run_live.py --mode paper` 로 미국 장 시간 돌려 보기 → 기사 처리 · Discord 확인
4. `build_dataset.py` 단계별 실행 (가격 단계도 토스 키 필요) → KNIME 워크플로우 A · B 만들기 → `.knwf` 커밋 → B 결정트리로 매수 상한 다시 정하기 (D-15)
5. `install_launchd.sh --engine paper` 로 상시 (사용자 결정: 당분간 paper) → 문제없으면 `settings.yaml` 의 `mode.live_enabled: true` → `install_launchd.sh --engine live` (가능한 거래일 최대)
6. 끝나면 `daily_report.py --history` 로 전체 결과

## 확인 안 된 것 (실제로 돌려 봐야 안다)
- 토스 장 캘린더의 애프터 끝이 19:50 인지 20:00 인지 (D-13 · 둘 다 맞게 동작하지만 기록으로 확인)
- 토스 `/api/v1/holdings` 의 `marketCountry` 값 표기 (D-14 · 국내 주식은 KR/KRW 로 보고 건너뜀 · 다르면 #critical 로 드러난다)
- 토스 장외 세션 주문이 `timeInForce=DAY` 지정가로 접수되는지 (사용자는 앱 · API 장외 거래 확인했다고 함 · 문서는 DAY 를 '정규장까지 유효'로 설명) → 첫 실거래를 작은 수량으로 보고 `order_final` 이벤트 확인
- 미국 주식 가격 정정 시 새 주문의 수량이 '남은 수량'인지
- paper 모드도 토스 키가 있어야 돈다 (종목 범위 · 시세 · 장 캘린더를 토스에서 받는다)
- Jev `typesafe/jev-1.13` 이 OpenRouter 에서 그대로 응답하는지 (check_setup 이 확인)
- RTPR WebSocket 이 지금 키로 붙는지 (구독 상태)

## 파일 지도
`docs/ARCHITECTURE.md` 설계 · `docs/DECISIONS.md` 결정 · `docs/SECURITY.md` 보안 · `knime/README.md` KNIME
