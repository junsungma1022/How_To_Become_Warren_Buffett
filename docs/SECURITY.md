# 보안 — API 키를 절대 유출하지 않기

이 저장소는 나중에 **Public** 으로 바꾼다. 그래서 처음부터 "키가 저장소에 들어갈 길이 없게" 만든다.

## 1. 비밀값은 어디에 있나

| 비밀값 | 위치 | 비고 |
|---|---|---|
| RTPR 키 · OpenRouter 키 · 토스 client id/secret · Discord 웹훅 5개 | 맥의 `.env` 한 파일 (`chmod 600`) | 이름 목록은 `.env.example` (값 없음) |
| 토스 계좌번호 | **어디에도 저장하지 않음** | 코드가 `GET /api/v1/accounts` 로 `accountSeq` 를 실행 중에만 받아 메모리에 둔다 |
| 토스 접근 토큰 | 메모리에만 | 파일 · 로그에 쓰지 않는다 |

## 2. 다섯 겹 방어

1. **`.gitignore`**: `.env`, `.env.*` (단 `.env.example` 은 허용), `data/`, `logs/`, `*.jsonl`, 키 파일 모양(`*.pem`, `*.key`, `secrets.*` …)
2. **pre-commit + gitleaks**: 커밋할 때마다 키 모양을 찾아 걸리면 커밋을 막는다
3. **시험 `tests/test_leak_guard.py`**: 커밋될 파일에 키 모양이 있거나 `.env`/`data/` 가 들어가면 실패
4. **GitHub Actions `secret-scan`**: push 마다 커밋 **이력 전체**를 gitleaks 로 검사
5. **코드 안의 `scrub`**: 로그 · 예외 문구 · Discord 알림에 쓰기 전에 알려진 키 값과 키 모양(OpenRouter `sk-or-v1-…`, Discord 웹훅 URL, JWT, `apiKey=`)을 `***` 로 바꾼다

## 3. 사람이 할 일 (처음 한 번)

```bash
cp .env.example .env && chmod 600 .env      # 값은 편집기로 직접 입력 (채팅에 붙여넣지 않기)
brew install pre-commit gitleaks
pre-commit install
```

- OpenRouter 키 설정에서 **월 사용 한도**를 건다 (버그로 호출이 폭주해도 그 이상 결제되지 않음)
- 토스 WTS → 설정 → Open API → 허용 IP 에 이 맥의 공인 IP 만 등록
- **Public 으로 바꾸는 즉시** GitHub Settings → Code security 에서 *Secret scanning* 과 *Push protection* 을 켠다
- Public 전환 전에 한 번 더: `gitleaks git --log-opts="--all" .` 로 이력 전체 검사

## 4. 만약 키가 새면

1. 그 서비스에서 **키를 즉시 폐기 · 재발급** (저장소에서 지우는 것으로는 부족하다 — 이력과 포크에 남는다)
2. `.env` 를 새 키로 바꾼다
3. 토스는 허용 IP 목록도 확인, Discord 는 웹훅을 삭제 후 새로 만든다

## 5. Claude(작업 도우미)의 규칙

- `.env` 를 열거나 값을 읽지 않는다. 키 이름은 `.env.example` 로만 안다
- 외부 API 호출은 사람의 맥에서만 한다
