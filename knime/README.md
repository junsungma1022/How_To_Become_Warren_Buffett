# KNIME 워크플로우 가이드

수업(12주차 AutoML 1 · 13주차 AutoML 2)에서 배운 노드만으로 만든다. 입력은 Python 이 만든 CSV 다.

| 입력 파일 (data/ 아래 · 저장소에 없음) | 만드는 방법 | 한 줄 = |
|---|---|---|
| `data/knime/news_dataset.csv` | `python scripts/build_dataset.py --stage build` | 과거 기사 하나 (2026-05 ~ 10) |
| `data/ledger/articles.csv` | 실거래 엔진이 자동 기록 | 실시간으로 받은 기사 하나 |
| `data/ledger/trades.csv` | 실거래 엔진이 자동 기록 | 거래 하나 |

완성한 워크플로우는 **File → Export KNIME Workflow** 로 `knime/*.knwf` 에 저장해 커밋한다 (실행 데이터는 빼고 — Export 창에서 "Reset workflow(s) before export" 체크).

---

## 워크플로우 A — `A_jev_claude_scoring`: AI 분류 채점

질문: **"Jev 가 호재라고 한 것 / Claude 가 BUY 라고 한 것 중 실제로 이긴 비율은?"**

```
File Reader ─ Row Filter(tradeable = yes) ─┬─ Bar Chart (jev_choice 별 건수)
                                            ├─ Color Manager(label_win) ─ Scatter Plot (x jev_expected_grade, y sim_ret_pct)
                                            ├─ Scorer ①  (label_buy  vs  jev_pred)
                                            └─ Scorer ②  (label_buy  vs  claude_pred)
```

1. **File Reader**: `news_dataset.csv` (첫 줄 열 이름 체크)
2. **Row Filter**: `tradeable` = `yes` (살 수 없었던 기사는 채점 대상이 아니다)
3. **Bar Chart**: Category column `jev_choice`, Aggregation = Occurrence count → 5단계 분포
4. **Color Manager**: `label_win` (WIN 초록 · LOSS 빨강) → **Scatter Plot**: X `jev_expected_grade`, Y `sim_ret_pct`
5. **Scorer ①**: First column `label_buy`, Second column `jev_pred` → Jev 단독의 혼동행렬
   (`label_buy` 는 `label_win` 을 예측 열과 같은 값 이름 `BUY/SKIP` 으로 바꾼 열이라 Scorer 에 바로 넣을 수 있다)
6. **Scorer ②**: First column `label_buy`, Second column `claude_pred` → Jev + Claude 파이프라인 전체의 혼동행렬
   (Jev 에서 걸러진 기사는 `claude_pred` = SKIP. Jev 는 통과했는데 비용 상한 때문에 Claude 를 안 부른 기사는 빈칸이므로
   Scorer ② 앞에 **Row Filter**(`claude_pred` 가 빈칸이 아닌 행)를 둔다)

**정밀도 읽는 법**: 혼동행렬에서 "BUY 라고 한 것 중 WIN" 의 비율 = Precision. 이 전략의 핵심 숫자다 (산 것 중 몇 %가 돈을 벌었나).
기준선은 `tradeable = yes` 전체의 WIN 비율 (아무거나 샀을 때). AI 가 기준선보다 높아야 의미가 있다.

## 워크플로우 B — `B_buy_rule_learning`: 매수 규칙 학습 (13주차 그대로)

질문: **"AI 점수 + 기사 · 가격 정보로 이길 거래를 맞힐 수 있나? 어떤 규칙인가?"**

```
File Reader ─ Row Filter(tradeable = yes) ─ Column Filter ─ Partitioning(70/30, seed 0)
    ├─(70%)─ Decision Tree Learner ──┐
    │        Random Forest Learner ─┤
    │        Logistic Regression Learner ─┤
    └─(30%)────────────────────────── 각 Predictor ─ Scorer (label_win vs Prediction(label_win))
```

- **Column Filter** (입력으로 쓸 열만): `session`, `published_et_hour`, `log10_market_cap`, `change_vs_prev_close_pct`,
  `ret_15m_before_pct`, `volume_15m_before`, `jev_expected_grade`, `jev_confidence`, `jev_p_*` 5개, `title_len`, `label_win`
  (`label_buy` · `jev_pred` · `claude_*` 는 빼거나 비교 실험용으로만)
  - **넣으면 안 되는 열** (미래 정보 · 정답 복사본): `sim_*`, `day_high_vs_prev_close_pct`, `label_surge30`, `label_buy`
- **Partitioning**: Relative 70%, Draw randomly, Use random seed `0` (수업 설정)
- **Decision Tree Learner**: Class column `label_win`, Quality measure Gini index, Pruning MDL, Min records per node 10
  → **View** 로 나무를 열어 맨 위 갈림 조건을 발표 자료에 쓴다 (예: "jev_expected_grade > 4.2 이고 장 시작 30분 안")
- **Random Forest Learner**: Target `label_win`, Information Gain Ratio, Limit levels 10, Min node size 1, Models 100, seed 0
- **Logistic Regression Learner**: Target `label_win`
- **Scorer**: 세 모델의 Accuracy · Precision 비교 → 가장 나은 규칙을 `config/settings.yaml` 에 반영할지 결정
  (반영하면 `docs/DECISIONS.md` 에 근거와 함께 기록 → 발표에서 "KNIME 결과로 설계를 바꿨다")

주의: WIN 이 LOSS 보다 훨씬 적으면 Accuracy 가 과대평가된다 (수업 13주차 '불균형 라벨'). Precision 을 본다.

## 워크플로우 C — `C_live_review`: 실거래 결과 보기 (발표용)

```
File Reader(trades.csv) ─ Color Manager(exit_reasons) ─ Scatter Plot (x hold_min, y ret_pct)
                        └ Bar Chart (exit_reasons 별 pnl_usd 합계)
File Reader(articles.csv) ─ Bar Chart (action_reason 별 건수 = 깔때기)
```
