# How_To_Become_Warren_Buffett

> 미국 소형주 보도자료를 **AI 두 단계(Jev → Claude)** 가 읽고, 호재라고 판단하면 **분할 매수 · 분할 매도**로 자동 매매하는 실험.
> 판단 근거는 생성형 AI 가 한국어 리포트로 남기고, 결과는 **KNIME** 으로 채점 · 분석한다.
> 강원대학교 AI융합학과 「오픈소스프로그래밍」 기말 프로젝트 (2026-2).

⚠️ 투자 권유가 아니다. 소형주 뉴스 매매는 손실 가능성이 크고, 이 프로젝트의 목적은 수익이 아니라 시스템 구현과 검증이다.

## 무엇을 하나

```mermaid
flowchart LR
    N["📰 RTPR 실시간 보도자료"] --> U["🔎 종목 범위<br/>시총 $5M~$300M"]
    U --> J["🧮 Jev<br/>5단계 분류 · 쓰레기 컷"]
    J -- "작은/큰 호재" --> C["🧠 Claude Opus 5.5<br/>최종 판단 + 매수 근거 리포트"]
    C -- BUY --> T["💸 토스증권<br/>분할 매수 → 분할 매도"]
    T --> D["🔔 Discord 알림 · 09:00 일일 보고서"]
    T --> K["📊 KNIME 채점 · 규칙 학습"]
```

| 역할 | 도구 | 이유 |
|---|---|---|
| 1단계 분류 | **Jev** (TypeSafe 결정 모델 · OpenRouter) | 글을 쓰지 않고 선택지별 확률만 준다 → 빠르고 매우 싸다. 악재 · 중립을 걸러 비싼 모델 호출을 줄인다 |
| 2단계 판단 | **Claude Opus 5.5** (OpenRouter) | 본문을 읽고 BUY/PASS 를 정하고, 사는 경우 **한국어 매수 근거 리포트를 생성**한다 |
| 매매 | 토스증권 Open API | 프리 · 정규 · 애프터마켓 지정가 주문 |
| 분석 | **KNIME** | AI 판단이 실제 주가와 맞았는지 혼동행렬로 채점, 의사결정나무로 매수 규칙 학습 |

## 매매 규칙 (요약 · 전체는 [`config/settings.yaml`](config/settings.yaml), [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md))

- 분할매수: 예산 50% → 10분 안에 +3% 오르면 나머지 (물타기 없음)
- 분할매도: −5% 손절 · +10% 절반 익절 · 나머지는 +20% 또는 최고가 대비 −5% · 30분 시간 청산 · 애프터마켓 끝 전 전량
- 안전장치: 한 번에 한 종목 · 하루 손실 −$15 면 그날 매수 중단 · 계좌 $60 미만이면 정지

## 폴더

```
config/settings.yaml      매매 설정 (비밀값 아님)
src/hbwb/
  news/rtpr.py            RTPR WebSocket 알림 · 기사 본문
  universe.py             종목 범위 (시가총액)
  ai/jev.py · claude.py   1단계 · 2단계 AI (OpenRouter)
  broker/toss.py          토스 Open API (시세 · 계좌 · 주문)
  strategy/rules.py       분할매수 · 분할매도 규칙 (순수 함수 · 실거래와 시뮬레이션이 같이 씀)
  strategy/backtest.py    1분봉 시뮬레이션 (KNIME 정답 라벨)
  engine/runner.py        전체 흐름 · 보유 관리
  report/daily.py         09:00 일일 보고서
scripts/                  run_live · check_setup · daily_report · build_dataset · install_launchd
knime/                    KNIME 워크플로우 (.knwf) 와 노드 설정 가이드
tests/                    pytest (네트워크 차단 · 키 유출 검사 포함)
docs/                     설계 · 결정 기록 · 보안 · 인수인계
```

## 실행 (맥)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env          # 키는 편집기로 직접 입력
brew install pre-commit gitleaks && pre-commit install

.venv/bin/python -m pytest -q                   # 시험 (네트워크 안 씀)
.venv/bin/python scripts/check_setup.py --discord   # 키 · 토스 계좌 · AI · Discord 점검 (주문 없음)
caffeinate -i .venv/bin/python scripts/run_live.py --mode shadow   # 주문 없이 전 과정
# 실거래: settings.yaml 의 mode.live_enabled: true 로 바꾼 뒤
caffeinate -i .venv/bin/python scripts/run_live.py --mode live
bash scripts/install_launchd.sh                 # 매일 09:00 일일 보고서
```

실거래 전 확인: 토스 앱에서 **달러로 환전** (Open API 는 자동 환전하지 않는다) · 토스 허용 IP 에 이 맥의 공인 IP 등록 · 맥이 잠들지 않게.

## KNIME

과거 기사(2026-05 ~ 10)에 같은 AI · 같은 매매 규칙을 적용한 데이터셋을 만들어 KNIME 으로 분석한다.

```bash
.venv/bin/python scripts/build_dataset.py --stage prices   # 토스 1분봉 · 일봉 · 종목정보
.venv/bin/python scripts/build_dataset.py --estimate       # AI 비용 추정
.venv/bin/python scripts/build_dataset.py --stage jev
.venv/bin/python scripts/build_dataset.py --stage claude --claude-max 300
.venv/bin/python scripts/build_dataset.py --stage build    # → data/knime/news_dataset.csv
```

노드 구성은 [`knime/README.md`](knime/README.md).

## 보안

API 키는 `.env` 에만 있고 저장소에는 들어가지 않는다. `.gitignore` · pre-commit(gitleaks) · 시험 · GitHub Actions 이력 검사 · 로그 마스킹의 다섯 겹으로 막는다. 자세한 내용은 [`docs/SECURITY.md`](docs/SECURITY.md).

## 데이터 출처와 한계

- 뉴스: RTPR (원문 · 과거 자료는 약관상 저장소에 포함하지 않는다)
- 시세 · 종목정보: 토스증권 Open API (미국 시세는 일부 거래소 기준 · NBBO 아님)
- 과거 데이터셋의 시가총액은 현재 발행주식수로 근사한다 (그 사이 분할 · 증자가 있으면 오차)
- 1분봉 시뮬레이션은 한 봉 안에서 손절이 먼저 닿았다고 보는 보수적 가정을 쓴다

## 라이선스

MIT
