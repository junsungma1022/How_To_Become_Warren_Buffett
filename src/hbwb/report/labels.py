"""기록 코드 → 사람이 읽는 한국어 (대시보드 · 실시간 보기 · 보고서가 같이 쓴다)."""

from __future__ import annotations

REASONS = {
    "universe": "종목 범위 밖",
    "jev_error": "Jev 호출 실패",
    "jev_filtered": "Jev: 호재 아님",
    "stale_alert": "늦은 알림 (2분 초과)",
    "claude_error": "Claude 호출 실패",
    "claude_pass": "Claude 패스",
    "no_trigger_5m": "5분 안에 +5% 없음",
    "over_cap_5m": "+10% 위라서 추격 안 함",
    "entry_not_filled": "매수 미체결",
    "filled": "매수 체결",
    "already_holding": "이미 보유 중",
}
RISK = {
    "slots_full": "3칸 모두 사용 중",
    "daily_loss_limit": "하루 손실 한도",
    "consecutive_losses": "3연패",
    "ticker_already_traded_today": "오늘 이미 거래한 종목",
}
UNIVERSE = {
    "market_cap_out": "시총 범위 밖",
    "unclassified:no_stock_info": "토스 종목 정보 없음",
    "unclassified:no_prev_close": "전일 종가 없음",
    "unclassified:no_shares": "발행 주식 수 없음",
    "not_common_share": "보통주 아님",
}
EXITS = {
    "stop_loss": "손절 −7%",
    "session_close": "장 마감 정리",
    "time_stop_15m": "시간 손절 (15분 +5% 미만)",
    "time_stop_5m": "시간 손절 (5분 +2% 미만)",
    "tp1": "익절 +5%",
    "tp2": "익절 +10%",
    "tp3": "익절 +15%",
    "trailing_stop": "추적 손절",
}
JEV = {
    "big_negative": "큰 악재", "small_negative": "작은 악재", "neutral": "중립",
    "small_positive": "작은 호재", "big_positive": "큰 호재",
}


def reason_ko(code: str | None) -> str:
    code = code or ""
    if code in REASONS:
        return REASONS[code]
    if code.startswith("risk:"):
        inner = code[5:]
        if inner.startswith("halted:"):
            return "정지: " + inner[7:]
        return RISK.get(inner, inner)
    if code.startswith("session_closed:"):
        return "거래 세션 밖"
    if code.startswith("article_http_"):
        return "본문 못 받음 (" + code[13:] + ")"
    if code.startswith("too_expensive"):
        return "1주 가격이 칸보다 큼"
    return code


def universe_ko(code: str | None) -> str:
    code = code or ""
    if code.startswith("security_type:"):
        return "보통주 아님 (" + code[14:] + ")"
    if code.startswith("status:"):
        return "거래 상태 " + code[7:]
    if code.startswith("market:"):
        return "거래소 밖 (" + code[7:] + ")"
    return UNIVERSE.get(code, code)


def exits_ko(codes: str | None) -> str:
    return " → ".join(EXITS.get(c, c) for c in (codes or "").split("|") if c)
