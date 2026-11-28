# KNIME 워크플로우 가이드 (수업 12 · 13주차 방식 그대로)

> **실제로 만든 것 (2026-11-28 · KNIME 5.12.1 · D-22)**: `knime/KNIME_project/` (워크스페이스 폴더 그대로 · Reset 상태 · KNIME 에서 File → Import 또는 워크스페이스에 복사해서 연다)
> `CSV Reader(news_dataset.csv) → Row Filter(triggered = yes · 843행) ─┬─ Scorer ①(label_buy vs jev_pred)`
> `                                                                    ├─ Column Filter(15열) → Table Partitioner(70% · random · seed 0) → Decision Tree Learner(Gini · No pruning · 50) → Predictor → Scorer ②`
> `                                                                    └─ Scatter Plot(x sim_entry_rise_pct · y sim_ret_pct)`
> 결과와 그로 바꾼 설정은 `docs/DECISIONS.md` D-22. 아래 A · B · C 는 처음 계획 (시간 때문에 위 한 워크플로우로 줄였다 · 5.12 에는 Color Manager 가 안 보여 색 구분은 생략).

KNIME 은 코드가 아니라 **화면(GUI)에서 노드를 끌어다 놓고 → 연결하고 → Configure → Execute** 하는 도구다 (12주차 16쪽 '작업 루틴').
그래서 이 워크플로우들은 사람이 KNIME 화면에서 직접 만든다. Python 은 KNIME 이 읽을 CSV 만 만든다.
아래 노드 · 설정값은 **강의자료에 나온 것만** 쓴다. 수업과 다른 값을 쓰는 곳은 이유를 적었다.

| 입력 파일 (data/ 아래 · 저장소에 없음) | 만드는 방법 | 한 줄 = |
|---|---|---|
| `data/knime/news_dataset.csv` | `python scripts/build_dataset.py --stage build` | 과거 기사 하나 (2026-05 ~ 10 · 기사 원문 · 제목 없음) |
| `data/ledger/articles.csv` | 엔진이 자동 기록 | 실시간으로 받은 기사 하나 (제목 있음) |
| `data/ledger/trades.csv` | 엔진이 자동 기록 | 거래 하나 |

## 0. 준비 (수업 그대로)

1. **Workflow group** 을 하나 만든다: 이름 `HBWB` (수업의 `DS_class` 와 같은 역할 · 12주차 19 ~ 20쪽)
2. 그 아래에 **Workflow** 3개: `A_jev_claude_scoring` · `B_buy_rule_learning` · `C_live_review`
3. Modern UI 라면 **Preferences → KNIME Modern UI → "All nodes"** 를 켠다. 안 켜면 Decision Tree 같은 노드가 검색에 안 나온다 (13주차 7 ~ 8쪽)
4. 파일 읽기 노드: 13주차 화면처럼 **CSV Reader**, 또는 12주차처럼 **File Reader** (둘 다 수업에 나옴 · Browse 로 CSV 선택)
5. 모든 노드는 수업 순서대로: 검색 → 끌어다 놓기 → 연결 → 더블클릭(Configure) → Execute → 우클릭으로 결과 보기
   (노드 신호등: 빨강 = 설정 안 됨 · 노랑 = 설정됨 · 초록 = 실행됨)

## 워크플로우 A — `A_jev_claude_scoring`: AI 판단 채점 (12주차 + 13주차 Scorer)

질문: **"매수 신호(5분 안 +5%)가 난 기사 중, Jev 가 호재라고 한 것 / Claude 가 BUY 라고 한 것은 실제로 더 많이 이겼나?"**

```
CSV Reader ─ Row Filter(triggered = yes) ─┬─ Bar Chart (jev_choice 별 건수)
                                           ├─ Color Manager(label_win) ─ Scatter Plot (x jev_expected_grade, y sim_ret_pct)
                                           ├─ Scorer ①  (label_buy  vs  jev_pred)
                                           └─ Row Filter(claude_pred 있는 행) ─ Scorer ②  (label_buy  vs  claude_pred)
```

| 노드 | 설정 (수업 화면의 칸 이름) |
|---|---|
| CSV Reader | `news_dataset.csv` |
| Row Filter | Column value matching · Column to test `triggered` · **use pattern matching** `yes` (12주차 31쪽과 같은 방식) |
| Bar Chart | Category column `jev_choice` · Aggregation method **Occurrence count** · Report on missing values **해제** · General Plot Options 에 축 이름 (12주차 41 ~ 42쪽) |
| Color Manager | Select one column `label_win` · Palettes **Set 1** (12주차 51쪽) |
| Scatter Plot | x `jev_expected_grade` · y `sim_ret_pct` · Report on missing values 해제 · General Plot Options **Show color legend 체크** (12주차 52 ~ 53쪽) |
| Scorer ① | First column `label_buy` · Second column `jev_pred` (13주차 39쪽) |
| Row Filter | `claude_pred` 가 빈칸이 아닌 행만 (Claude 는 비용 때문에 50건만 불렀다 · 나머지는 빈칸) |
| Scorer ② | First column `label_buy` · Second column `claude_pred` |

- `label_buy` 는 정답(`label_win`)을 예측 열과 같은 값 이름(`BUY` / `SKIP`)으로 바꾼 열이다 — Scorer 는 두 열의 값 이름이 같아야 비교한다
- **읽는 법 (13주차 26 ~ 28쪽)**: 혼동행렬에서 "BUY 라고 한 것 중 실제 WIN 비율" = **Precision**. 이 전략의 핵심 숫자 (산 것 중 몇 %가 돈을 벌었나)
- 기준선 = `triggered = yes` 전체의 WIN 비율 (AI 없이 +5% 신호만으로 샀을 때). AI 의 Precision 이 이보다 높아야 AI 를 쓴 의미가 있다
- WIN 이 LOSS 보다 훨씬 적으면 Accuracy 가 부풀려진다 (13주차 27쪽 '불균형 라벨') → Precision 을 본다

## 워크플로우 B — `B_buy_rule_learning`: 매수 규칙 학습 (13주차 그대로)

질문: **"AI 점수 + 기사 · 가격 정보로 이길 거래를 맞힐 수 있나? 어떤 규칙인가?"**

```
CSV Reader ─ Row Filter(triggered = yes) ─ Column Filter ─ Partitioning
   ├─ First partition (70%) ─ Decision Tree Learner ─────────┐
   │                          Random Forest Learner ─────────┤ (모델)
   │                          Logistic Regression Learner ───┤
   └─ Second partition (30%) ─────────────────────── 각 Predictor (2번째 입력) ─ Scorer
```

| 노드 | 설정 |
|---|---|
| Column Filter | Manual Selection 으로 **아래 '넣을 열' 만 Include** (12주차 27쪽) |
| Partitioning | Relative[%] **70** · **Draw randomly** · Use random seed 체크 **0** (13주차 31쪽) |
| Decision Tree Learner | Class column `label_win` · Quality measure **Gini index** · Pruning method **No pruning** · Min number records per node **50** (13주차 12 · 35쪽) |
| Decision Tree Predictor | 1번째 입력 = Learner 의 모델 · 2번째 입력 = Second partition (13주차 35쪽) |
| Random Forest Learner | Target column `label_win` · Split criterion **Information Gain Ratio** · Limit number of levels **10** · Minimum node size **1** · Number of models **100** · Use static random seed **0** (13주차 18쪽) |
| Logistic Regression Learner | Target column `label_win` (13주차 23쪽 · 수업은 '구축해 보세요' 만 · 설정값 없음) |
| Random Forest / Logistic Regression **Predictor** | Decision Tree Predictor 와 같은 방식으로 연결 (수업은 Decision Tree 로만 보여 줌) |
| Scorer (모델마다 하나) | First column `label_win` · Second column `Prediction (label_win)` (13주차 39쪽) → Accuracy statistics · Confusion matrix |

**넣을 열** (매수 순간에 알 수 있는 값만): `session`, `published_et_hour`, `log10_market_cap`, `change_vs_prev_close_pct`,
`sim_entry_rise_pct`, `ret_15m_before_pct`, `volume_15m_before`, `jev_expected_grade`, `jev_confidence`, `jev_p_*` 5개, `title_len`, `label_win`

**빼야 하는 열** (미래 정보 · 정답 복사본): `sim_entry_rise_pct` 를 뺀 나머지 `sim_*`, `day_high_vs_prev_close_pct`, `label_surge30`, `label_buy`, `claude_*`

**수업 연습문제처럼 비교하기** (13주차 42 ~ 43쪽): 같은 데이터에서 Decision Tree 를
① Gini · No pruning ② Gain ratio · No pruning ③ Gain ratio · MDL 로 바꿔 Accuracy · Precision 을 표로 비교한다.
데이터가 적어서 (신호 난 기사 수백 건) Min number records per node 50 이면 나무가 거의 안 갈라질 수 있다 —
그때는 수업 연습문제 값인 **5** 로도 돌려 본다.

**결과를 설계에 반영하기 (D-15 매수 상한)**: Decision Tree **View** 에서 맨 위 갈림 조건에 `sim_entry_rise_pct > x` 가 나오면
그 x 를 `config/settings.yaml` `entry.trigger_max_rise_pct` 로 바꾸고 `docs/DECISIONS.md` 에 새 D-번호로 남긴다 → 발표에서 "KNIME 결과로 설계를 바꿨다".
(보조 그림: Scatter Plot x `sim_entry_rise_pct` · y `sim_ret_pct` · Color Manager `label_win`)

## 워크플로우 C — `C_live_review`: paper 엔진 결과 보기 (발표용 · 12주차 시각화만)

```
CSV Reader(trades.csv) ─ Color Manager(exit_reasons) ─ Scatter Plot (x hold_min, y ret_pct)
                       └ Bar Chart (Category exit_reasons · Aggregation Sum · pnl_usd)
CSV Reader(articles.csv) ─ Bar Chart (Category action_reason · Occurrence count = 깔때기)
```

## Git 에 올리기

- 실행 결과에는 데이터가 들어 있다 (`articles.csv` 에는 RTPR 기사 제목). **올리기 전에 워크플로우를 Reset** 한다 (노드가 모두 노랑)
- 클래식 UI: 탐색기에서 워크플로우 우클릭 → **Export KNIME Workflow** → `knime/<이름>.knwf`
- Modern UI 에서 Export 메뉴가 안 보이면: `knime-workspace` 안의 워크플로우 폴더를 그대로 `knime/` 에 복사해서 커밋한다 (Claude 가 대신 할 수 있다)
