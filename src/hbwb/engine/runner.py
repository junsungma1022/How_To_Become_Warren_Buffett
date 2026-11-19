"""매매 엔진 — 기사 → 종목 범위 → Jev(최소 컷) → Claude(메인 판단 · 리포트) → 5분 안 +5% 신호 → 칸 매수 → 이전 프로그램 매도.

자금은 3칸 (칸 하나 = 자금의 1/3 · 한 종목). 3칸이 모두 보유 · 대기 중이면 새로 사지 않는다.
모든 판단은 events-*.jsonl 과 articles.csv / trades.csv 에 남는다.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from dataclasses import asdict

from ..ai import claude as claude_mod
from ..ai import jev as jev_mod
from ..ai.openrouter import OpenRouter, OpenRouterError
from ..broker.toss import TossClient, TossError
from ..clock import ET, KST, fmt_kst, iso_utc, ms_to, now_ms, parse_iso_ms
from ..config import ROOT, Secrets
from ..eventlog import EventLog
from ..ledger import articles as articles_ledger
from ..ledger import trades as trades_ledger
from ..news.rtpr import Alert, RtprClient, RtprFatal
from ..notify.discord import Discord
from ..strategy.risk import RiskBook
from ..strategy.rules import (Fill, Position, apply_exit, entry_signal, exit_decision, plan_entry, ref_from_bars,
                              slot_budget, update_peak)
from ..universe import Universe
from . import sessions as S
from .executor import LiveExecutor, ShadowExecutor


class EngineFatal(RuntimeError):
    """사람이 고쳐야 하는 문제 (키 오류 · 구독 끝 · 설정). 자동 재시작해도 소용없으므로 정상 종료(0)로 끝낸다."""


HEARTBEAT_S = 300
CALENDAR_REFRESH_S = 1800
TOSS_FATAL_CODES = {"invalid-client", "invalid_client", "unauthorized_client", "ip-not-allowed", "account-choice"}


def report_day_key(ms: int) -> str:
    """KST 09:00 ~ 다음날 09:00 을 하루로 (일일 보고서와 같은 경계)."""
    return (ms_to(ms, KST) - dt.timedelta(hours=9)).strftime("%Y-%m-%d")


def us_day(ms: int) -> str:
    return ms_to(ms, ET).strftime("%Y-%m-%d")


class Engine:
    def __init__(self, cfg: dict, secrets: Secrets, mode: str):
        if mode not in {"shadow", "live"}:
            raise ValueError(mode)
        if mode == "live" and not cfg["mode"]["live_enabled"]:
            raise SystemExit("live 모드는 settings.yaml 의 mode.live_enabled: true 도 필요하다 (이중 확인)")
        self.cfg, self.secrets, self.mode = cfg, secrets, mode
        self.data_dir = ROOT / cfg["paths"]["data_dir"]
        self.ev = EventLog(ROOT / cfg["paths"]["log_dir"], secrets.all_values())
        self.toss = TossClient(secrets.get("toss_client_id"), secrets.get("toss_client_secret"), allow_orders=(mode == "live"))
        self.ai = OpenRouter(secrets.get("openrouter_key"))
        self.rtpr = RtprClient(secrets.get("rtpr_key"))
        self.discord = Discord(secrets, self.ev.write, prefix=f"HBWB·{mode}")
        self.universe = Universe(self.toss, cfg)
        self.articles = articles_ledger(self.data_dir)
        self.trades = trades_ledger(self.data_dir)
        self.risk = RiskBook(cfg)
        self.positions: dict[str, Position] = {}
        self.meta: dict[str, dict] = {}
        self.pending: dict[str, int] = {}          # 신호를 기다리는 종목 → 잡아 둔 칸
        self.windows: list[tuple[str, int, int]] = []
        self.fee_rate = cfg["fees"]["commission_rate_default"]
        self.seen: set[str] = set()
        self.last_alert_ms = now_ms()
        self.shadow_realized = 0.0
        self._entry_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(6)
        self._stop = asyncio.Event()
        self._bg: set[asyncio.Task] = set()
        self._busy: set[str] = set()
        self.state_path = self.data_dir / "state" / "positions.json"
        self.heartbeat_path = self.data_dir / "state" / "heartbeat.json"
        self._cal_ms = 0
        self._hb_ms = 0
        self.started_ms = now_ms()
        self.executor = None

    # ── 알림 ────────────────────────────────────────────────────────────────
    async def notify(self, channel: str, text: str, key: str | None = None, every_s: float = 0) -> None:
        try:
            await self.discord.send(channel, text, key, every_s)
        except Exception as e:  # noqa: BLE001 - 알림 실패로 매매가 멈추면 안 된다
            self.ev.write("notify_exception", err=str(e)[:200])

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ── 시작 점검 ────────────────────────────────────────────────────────────
    async def preflight(self) -> None:
        bp = await self.toss.buying_power_usd() if self.mode == "live" else float(self.cfg["capital"]["budget_usd"])
        if self.mode == "live":
            rate = await self.toss.commission_rate_us()
            if rate is not None:
                self.fee_rate = rate
        await self.refresh_calendar()
        self.ev.write("preflight", mode=self.mode, buying_power_usd=bp, fee_rate=self.fee_rate,
                      sessions=[(n, fmt_kst(a), fmt_kst(b)) for n, a, b in self.windows])
        if self.mode == "live" and bp < 1:
            await self.notify("critical", f"🔴 토스 USD 매수가능금액 ${bp:.2f} — 앱에서 달러로 환전해 둘 것 (Open API 는 자동 환전 안 함)")
        self.executor = (LiveExecutor(self.toss, self.cfg, self.ev.write, self.notify, self.fee_rate) if self.mode == "live"
                         else ShadowExecutor(self.cfg, self.ev.write, self.fee_rate, self.toss.quote))
        await self.restore_state()
        _, eq = await self.equity()
        self.risk.roll_day(report_day_key(now_ms()), eq)
        # 재시작해도 안전장치가 풀리지 않게: 오늘 실현손익 · 연패 · 오늘 산 종목을 거래 기록에서 복원
        self.risk.rebuild(self.trades.rows(), report_day_key, us_day, parse_iso_ms,
                          [(sym, us_day(p.entry_ms)) for sym, p in self.positions.items() if p.entry_ms])
        if self.mode == "shadow":
            self.shadow_realized = sum(float(r.get("pnl_usd") or 0) for r in self.trades.rows() if r.get("mode") == "shadow")
        self.seen.update(r["article_id"] for r in self.articles.rows() if r.get("article_id"))
        self.ev.write("risk_restored", realized_today=self.risk.realized_today, consecutive_losses=self.risk.consecutive_losses,
                      traded_today=sorted(f"{a}@{b}" for a, b in self.risk.traded), seen=len(self.seen))
        c = self.cfg["capital"]
        await self.notify("warnings", f"▶️ 시작 ({self.mode}) · USD 현금 ${bp:.2f} · 수수료율 {self.fee_rate:.4%} · "
                                      f"예산 ${c['budget_usd']:.0f} ÷ {c['slots']}칸")

    async def refresh_calendar(self) -> None:
        self.windows = S.windows_from_calendar(await self.toss.us_calendar())
        self._cal_ms = now_ms()

    # ── 상태 저장 (재시작 대비) ──────────────────────────────────────────────
    def save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        live = {s: p for s, p in self.positions.items() if not p.closed}
        if not live:
            if self.state_path.exists():
                self.state_path.unlink()
            return
        d = {s: {"pos": {**asdict(p), "fills": [asdict(f) for f in p.fills]}, "meta": self.meta.get(s, {})}
             for s, p in live.items()}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    async def restore_state(self) -> None:
        if not self.state_path.exists():
            return
        d = json.loads(self.state_path.read_text(encoding="utf-8"))
        for sym, rec in d.items():
            p = rec["pos"]
            pos = Position(p["symbol"], p["slot"], [Fill(**f) for f in p["fills"]], p["entry_ms"], p["peak"],
                           list(p["tp_done"]), p["trailing"])
            held = int(await self.toss.sellable_qty(sym)) if self.mode == "live" else pos.qty
            if held <= 0:
                self.ev.write("restore_skip", symbol=sym, why="보유 수량 0")
                continue
            if held != pos.qty:
                self.risk.halted = f"restore_mismatch {sym} 기록 {pos.qty} vs 계좌 {held}"
                await self.notify("critical", f"🔴 재시작: {sym} 기록 {pos.qty}주 ≠ 계좌 {held}주 — 신규 매수 멈춤 · 수동 확인")
            self.positions[sym], self.meta[sym] = pos, rec.get("meta") or {}
            self.ev.write("restore", symbol=sym, qty=pos.qty, slot=pos.slot)
            await self.notify("warnings", f"↩️ 재시작: {sym} {pos.qty}주 (칸 {pos.slot + 1}) 이어서 관리")
        self.save_state()

    # ── 세션 · 칸 ────────────────────────────────────────────────────────────
    def session_now(self, t: int | None = None) -> str | None:
        w = S.current(self.windows, t or now_ms())
        return w[0] if w else None

    def tradeable_now(self) -> bool:
        return S.allowed(self.session_now(), self.cfg)

    def slots_used(self) -> int:
        return len([p for p in self.positions.values() if not p.closed]) + len(self.pending)

    def free_slot(self) -> int | None:
        used = {p.slot for p in self.positions.values() if not p.closed} | set(self.pending.values())
        return next((i for i in range(int(self.cfg["capital"]["slots"])) if i not in used), None)

    async def equity(self) -> tuple[float, float]:
        """(현금, 현금 + 보유 평가)."""
        open_pos = [p for p in self.positions.values() if p.qty > 0]
        if self.mode == "live":
            cash = await self.toss.buying_power_usd()
        else:
            spent = sum(p.cost + sum(f.fee for f in p.fills if f.side == "BUY") for p in open_pos)
            received = sum(sum(f.qty * f.price - f.fee for f in p.fills if f.side == "SELL") for p in open_pos)
            cash = float(self.cfg["capital"]["budget_usd"]) + self.shadow_realized - spent + received
        mv = 0.0
        if open_pos:
            px = await self.toss.last_prices([p.symbol for p in open_pos])
            mv = sum((px.get(p.symbol) or p.avg_price or 0) * p.qty for p in open_pos)
        return cash, cash + mv

    # ── 기사 처리 ────────────────────────────────────────────────────────────
    async def handle_alert(self, a: Alert) -> None:
        if a.article_id in self.seen:
            return
        self.seen.add(a.article_id)
        async with self._sem:
            try:
                await self._pipeline(a)
            except Exception as e:  # noqa: BLE001 - 한 기사 오류가 엔진을 멈추면 안 된다
                self.ev.write("pipeline_error", ticker=a.ticker, article=a.article_id, err=f"{type(e).__name__}: {e}"[:300])
                await self.notify("warnings", f"⚠️ 기사 처리 오류 {a.ticker}: {type(e).__name__}", key="pipe_err", every_s=300)

    def _pre_block(self, sym: str) -> str | None:
        """Claude 를 부르기 전에 (비용 절약) 볼 수 있는 막힘 — 자산 하한은 매수 직전에 본다."""
        if sym in self.positions or sym in self.pending:
            return "already_holding"
        return self.risk.block_reason(sym, us_day(now_ms()), None, self.slots_used())

    async def _pipeline(self, a: Alert) -> None:
        row: dict = {"article_id": a.article_id, "ticker": a.ticker, "published_utc": iso_utc(a.published_ms),
                     "received_utc": iso_utc(a.received_ms), "age_s": a.age_s, "session": self.session_now(a.received_ms),
                     "mode": self.mode, "ai_cost_usd": 0.0, "jev_pass": False, "claude_called": False}

        def done(action: str, reason: str) -> None:
            row.update(action=action, action_reason=reason)
            self.articles.append(row)
            self.ev.write("article", **{k: row.get(k) for k in ("article_id", "ticker", "action", "action_reason",
                                                                  "jev_choice", "claude_decision", "universe_reason")})

        uc = await self.universe.check(a.ticker, a.received_ms)
        row.update(universe_ok=uc.ok, universe_reason=uc.reason, market_cap_usd=uc.market_cap, prev_close=uc.prev_close)
        if not uc.ok:
            return done("skip", "universe")

        art = await self.rtpr.article(a.raw_url)
        row.update(title=art.title, exchange=art.exchange)
        if art.status != 200 or not art.body:
            return done("skip", f"article_http_{art.status}")
        stale = a.age_s is not None and a.age_s > self.cfg["news"]["max_alert_age_s"]

        try:
            jr = await jev_mod.classify(self.ai, self.cfg["jev"]["model"],
                                        jev_mod.build_state(a.ticker, art.title, art.body, uc.market_cap, self.cfg["news"]["body_max_chars"]))
        except OpenRouterError as e:
            self.ev.write("jev_error", ticker=a.ticker, status=e.status, err=str(e)[:200])
            await self.notify("warnings", f"⚠️ Jev 호출 실패 ({e.status})", key="jev_err", every_s=600)
            return done("skip", "jev_error")
        row.update(jev_choice=jev_mod.KO.get(jr.choice or "", "refusal" if jr.refusal else ""), jev_grade=jr.grade,
                   jev_expected_grade=jr.expected_grade, jev_confidence=jr.confidence,
                   ai_cost_usd=row["ai_cost_usd"] + jr.usage.cost_usd,
                   **{f"jev_p_{k}": jr.probabilities.get(k) for k in jev_mod.CLASSES})
        passed = jr.passes(self.cfg["jev"]["pass_choices"])
        row["jev_pass"] = passed
        if not passed:
            return done("skip", "jev_filtered")
        if stale:
            return done("skip", "stale_alert")
        if not self.tradeable_now():
            return done("skip", f"session_closed:{self.session_now()}")
        block = self._pre_block(a.ticker)
        if block:
            return done("skip", f"risk:{block}")

        prices = await self.toss.last_prices([a.ticker])
        last = prices.get(a.ticker)
        chg = round(100 * (last / uc.prev_close - 1), 2) if last and uc.prev_close else None
        row.update(last_price=last, change_vs_prev_close_pct=chg)
        ctx = {"market_cap_usd": round(uc.market_cap) if uc.market_cap else None, "session": self.session_now(),
               "minutes_since_publish": round((now_ms() - (a.published_ms or a.received_ms)) / 60000, 1),
               "prev_close": uc.prev_close, "last_price": last, "change_vs_prev_close_pct": chg}
        row["claude_called"] = True
        try:
            cr = await claude_mod.judge(self.ai, self.cfg, a.ticker, art.title, art.body, ctx, with_report=True)
        except OpenRouterError as e:
            self.ev.write("claude_error", ticker=a.ticker, status=e.status, err=str(e)[:200])
            await self.notify("warnings", f"⚠️ Claude 호출 실패 ({e.status}) — {a.ticker} 매수 안 함", key="cl_err", every_s=300)
            return done("skip", "claude_error")
        row.update(claude_decision=cr.decision, claude_grade=cr.impact_grade, claude_confidence=cr.confidence,
                   claude_catalyst=cr.catalyst_type, claude_parse_error=cr.parse_error,
                   ai_cost_usd=row["ai_cost_usd"] + cr.usage.cost_usd)
        self.ev.write("claude", ticker=a.ticker, decision=cr.decision, grade=cr.impact_grade, conf=cr.confidence,
                      catalyst=cr.catalyst_type, facts=cr.key_facts, risks=cr.risks, report=cr.report_ko, parse_error=cr.parse_error)
        if cr.parse_error:
            await self.notify("warnings", f"⚠️ Claude 응답 형식 오류 ({cr.parse_error}) — {a.ticker} 매수 안 함", key="cl_parse", every_s=300)
        if not cr.buy:
            return done("skip", "claude_pass")

        result = await self.watch_and_enter(a, art.title, uc, jr, cr, row)
        done("buy" if result == "filled" else "skip", result)

    # ── 매수: 5분 안 +5% 신호 → 칸 금액만큼 한 번에 ──────────────────────────
    async def reference_price(self, sym: str, published_ms: int) -> tuple[float | None, str]:
        try:
            bars = await self.toss.candles(sym, "1m", count=40)
            ref = ref_from_bars(bars, published_ms, self.cfg)
            if ref:
                return ref, "prev_bar_close"
            trades = await self.toss.recent_trades(sym, 50)
            after = [p for t, p in trades if t >= published_ms]
            if after:
                return after[0], "first_trade_after"
        except TossError as e:
            self.ev.write("ref_error", symbol=sym, code=e.code)
        return None, "none"

    async def watch_and_enter(self, a: Alert, title: str, uc, jr, cr, row: dict) -> str:
        sym, pub = a.ticker, a.published_ms or a.received_ms
        async with self._entry_lock:
            block = self._pre_block(sym)
            slot = self.free_slot()
            if block or slot is None:
                return f"risk:{block or 'slots_full'}"
            self.pending[sym] = slot
        try:
            ref, src = await self.reference_price(sym, pub)
            row.update(ref_price=ref, ref_source=src)
            poll = self.cfg["entry"]["poll_interval_s"]
            while True:
                t = now_ms()
                last = (await self.toss.last_prices([sym])).get(sym)
                if ref is None and last:
                    ref, src = last, "watch_start"
                    row.update(ref_price=ref, ref_source=src)
                sig = entry_signal(ref, last or 0.0, t, pub, self.cfg) if last else (
                    "expired" if t > pub + self.cfg["entry"]["trigger_window_min"] * 60_000 else None)
                if sig == "expired":
                    return "no_trigger_5m"
                if sig == "signal":
                    break
                await asyncio.sleep(poll)
            row.update(signal_utc=iso_utc(t), signal_price=last, signal_rise_pct=round(100 * (last / ref - 1), 2))
            async with self._entry_lock:
                cash, eq = await self.equity()
                self.pending.pop(sym, None)
                block = self.risk.block_reason(sym, us_day(t), eq, self.slots_used())
                if block:
                    if block.startswith("halted"):
                        await self.notify("critical", f"🔴 시스템 정지: {block} — 신규 매수 안 함")
                    return f"risk:{block}"
                su = slot_budget(eq, cash, self.cfg)
                qty, limit = plan_entry(su, last, self.cfg, self.fee_rate)
                if qty <= 0:
                    return f"too_expensive(price {last}, slot ${su:.2f})"
                self.pending[sym] = slot          # 주문 중에도 칸을 잡아 둔다
                fill = await self.executor.buy(sym, qty, limit, "entry")
                self.pending.pop(sym, None)
                if not fill:
                    return "entry_not_filled"
                pos = Position(sym, slot)
                pos.add(fill)
                self.positions[sym] = pos
                self.meta[sym] = {"trade_id": uuid.uuid4().hex[:12], "article_id": a.article_id, "title": title,
                                  "published_ms": pub, "ref_price": ref, "slot_usd": su,
                                  "jev": jev_mod.KO.get(jr.choice or "", ""), "claude_grade": cr.impact_grade,
                                  "claude_conf": cr.confidence}
                self.risk.record_open(sym, us_day(t))
                self.save_state()
            mcap = f"${uc.market_cap / 1e6:.0f}M" if uc.market_cap else "?"
            await self.notify("entries",
                              f"🟢 **매수** {sym} {fill.qty}주 @ ${fill.price:.4g} · 칸 {slot + 1}/{self.cfg['capital']['slots']} "
                              f"(${su:.0f}) · 시총 {mcap}\n신호: 발행 뒤 {(t - pub) / 60000:.1f}분 · 기준가 대비 "
                              f"{row['signal_rise_pct']:+.1f}%\n> {title[:200]}\nJev: {self.meta[sym]['jev']} · Claude: "
                              f"{cr.impact_grade}/5 (확신 {cr.confidence}) · {cr.catalyst_type}\n**매수 근거 리포트**\n{cr.report_ko}")
            return "filled"
        finally:
            self.pending.pop(sym, None)

    # ── 보유 관리 (종목마다 따로 · 이전 프로그램 매도 규칙) ──────────────────
    async def manage_one(self, sym: str) -> None:
        pos = self.positions.get(sym)
        if pos is None or pos.closed:
            return
        t = now_ms()
        if S.current(self.windows, t) is None:
            await self.notify("critical", f"🔴 장이 닫힌 시간에 {sym} {pos.qty}주 보유 중 — 다음 세션에서 정리 시도 · 수동 확인 권장",
                              key=f"hold_closed_{sym}", every_s=1800)
            return
        last = (await self.toss.last_prices([sym])).get(sym)
        if not last:
            return
        update_peak(pos, last)
        end = S.trading_block_end(self.windows, t, self.cfg)
        ex = exit_decision(pos, last, t, end if end is not None else t, self.cfg)
        if ex is None:
            return
        fill = await self.executor.sell(sym, ex.qty, ex.reason, lambda: self.toss.quote(sym), lambda: self._last(sym))
        if fill:
            pos.add(fill)
            apply_exit(pos, ex)
            part = fill.qty * (fill.price - (pos.avg_price or 0)) - fill.fee
            await self.notify("exits", f"🔵 **매도** {sym} {fill.qty}주 @ ${fill.price:.4g} · {ex.reason} · "
                                       f"손익 {pos.pnl_pct(fill.price):+.1f}% · 이번 ${part:+.2f} · 남은 {pos.qty}주")
        self.save_state()
        if pos.closed:
            await self.close_trade(sym)

    async def _last(self, sym: str) -> float | None:
        return (await self.toss.last_prices([sym])).get(sym)

    async def close_trade(self, sym: str) -> None:
        pos, m = self.positions[sym], self.meta.get(sym, {})
        buys = [f for f in pos.fills if f.side == "BUY"]
        sells = [f for f in pos.fills if f.side == "SELL"]
        sold = sum(f.qty for f in sells)
        pnl = pos.realized_pnl()
        row = {"trade_id": m.get("trade_id"), "article_id": m.get("article_id"), "ticker": sym, "mode": self.mode,
               "slot": pos.slot + 1, "published_utc": iso_utc(m.get("published_ms")), "ref_price": m.get("ref_price"),
               "entry_utc": iso_utc(buys[0].t_ms) if buys else "", "entry_qty": pos.orig_qty, "entry_price": pos.avg_price,
               "slot_usd": m.get("slot_usd"), "exit_utc": iso_utc(sells[-1].t_ms) if sells else "",
               "exit_reasons": "|".join(f.reason for f in sells), "sold_qty": sold,
               "avg_exit_price": (sum(f.qty * f.price for f in sells) / sold) if sold else None,
               "fees_usd": pos.fees, "pnl_usd": pnl, "ret_pct": 100 * pnl / pos.cost if pos.cost else None,
               "hold_min": (sells[-1].t_ms - pos.entry_ms) / 60000 if sells and pos.entry_ms else None,
               "peak_price": pos.peak, "peak_pct": pos.pnl_pct(pos.peak), "jev_choice": m.get("jev"),
               "claude_grade": m.get("claude_grade"), "claude_confidence": m.get("claude_conf")}
        self.trades.append(row)
        self.risk.record_close(pnl)
        self.shadow_realized += pnl
        self.ev.write("trade_closed", **row)
        await self.notify("exits", f"✅ **거래 종료** {sym} (칸 {pos.slot + 1}) · 손익 ${pnl:+.2f} ({row['ret_pct']:+.2f}%) · "
                                   f"{row['exit_reasons']} · 보유 {row['hold_min']:.1f}분")
        del self.positions[sym]
        self.meta.pop(sym, None)
        self.save_state()
        if self.mode == "live":
            held = await self.toss.sellable_qty(sym)
            if held > 0:
                await self.notify("critical", f"🔴 {sym} 청산했다고 기록했지만 계좌에 {held}주 남음 — 수동 확인")
        r = self.cfg["risk"]
        if self.risk.consecutive_losses >= r["max_consecutive_losses"]:
            await self.notify("warnings", f"⏸️ {self.risk.consecutive_losses}연패 — 오늘(KST 09:00 까지) 신규 매수 중단")
        elif self.risk.daily_loss_pct() <= -abs(r["daily_loss_limit_pct"]):
            await self.notify("warnings", f"⏸️ 오늘 실현손실 {self.risk.daily_loss_pct():.1f}% — 오늘 신규 매수 중단")

    async def _manage_guarded(self, sym: str) -> None:
        try:
            await self.manage_one(sym)
        except TossError as e:
            self.ev.write("manage_error", symbol=sym, status=e.status, code=e.code, msg=e.message)
            if e.code in {"ip-not-allowed", "forbidden", "account-restricted"}:
                await self.notify("critical", f"🔴 토스 {e.code} — 허용 IP · 계좌 상태 확인 (보유 관리 불가)", key="toss_fatal", every_s=300)
            await asyncio.sleep(2)
        except Exception as e:  # noqa: BLE001
            self.ev.write("manage_error", symbol=sym, err=f"{type(e).__name__}: {e}"[:300])
            await self.notify("warnings", f"⚠️ 보유 관리 오류 {sym} {type(e).__name__}", key="mg_err", every_s=300)
            await asyncio.sleep(2)
        finally:
            self._busy.discard(sym)

    # ── 루프들 ──────────────────────────────────────────────────────────────
    async def news_loop(self) -> None:
        def on_event(kind: str, fields: dict) -> None:
            self.ev.write(kind, **fields)
        try:
            async for a in self.rtpr.alerts(on_event):
                self.last_alert_ms = now_ms()
                self.ev.write("alert", ticker=a.ticker, article=a.article_id, age_s=a.age_s)
                self._spawn(self.handle_alert(a))
        except RtprFatal as e:
            await self.notify("critical", f"🔴 {e} — 뉴스 수신 중단, 엔진을 멈춘다 (자동 재시작 안 함 · 키 · 구독 확인)")
            raise EngineFatal(str(e)) from None

    async def position_loop(self) -> None:
        """종목마다 따로 관리한다 — 한 종목의 느린 매도가 다른 종목 손절을 막지 않게."""
        interval = self.cfg["exit"]["poll_interval_s"]
        while not self._stop.is_set():
            for sym in list(self.positions):
                if sym not in self._busy:
                    self._busy.add(sym)
                    self._spawn(self._manage_guarded(sym))
            await asyncio.sleep(interval)

    def write_heartbeat(self) -> None:
        """엔진이 살아 있다는 표시 — 일일 보고서가 읽어 '엔진 멈춤'을 알린다."""
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        hb = {"t": now_ms(), "mode": self.mode, "started_ms": self.started_ms, "pid_note": "run_live",
              "positions": {s: p.qty for s, p in self.positions.items()}, "pending": sorted(self.pending),
              "last_alert_ms": self.last_alert_ms, "halted": self.risk.halted}
        tmp = self.heartbeat_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(hb), encoding="utf-8")
        tmp.replace(self.heartbeat_path)

    async def housekeeping_loop(self) -> None:
        while not self._stop.is_set():
            t = now_ms()
            try:
                if report_day_key(t) != self.risk.day_key:          # KST 09:00 — 하루 손익 · 연패 리셋
                    prev = self.risk.day_key
                    _, eq = await self.equity()
                    self.risk.roll_day(report_day_key(t), eq)
                    self.ev.write("day_roll", prev=prev, new=self.risk.day_key, start_equity=eq)
                if t - self._cal_ms >= CALENDAR_REFRESH_S * 1000:
                    await self.refresh_calendar()
            except Exception as e:  # noqa: BLE001 - 다음 바퀴에 다시
                self.ev.write("housekeeping_error", err=str(e)[:200])
            if t - self._hb_ms >= HEARTBEAT_S * 1000:
                self._hb_ms = t
                self.write_heartbeat()
                self.ev.write("heartbeat", positions=len(self.positions), pending=len(self.pending), bg_tasks=len(self._bg))
            if self.session_now() in {"pre", "regular", "after"} and t - self.last_alert_ms > 30 * 60_000:
                await self.notify("warnings", "⚠️ 미국 장 시간인데 RTPR 알림 30분째 0건 — 연결 · 'All articles' 규칙 확인",
                                  key="quiet", every_s=1800)
            await asyncio.sleep(30)

    async def start_with_retry(self) -> None:
        """시작 점검이 네트워크 문제로 실패하면 기다렸다 다시 (맥이 막 깨어났을 때 등). 키 · IP 문제는 EngineFatal."""
        wait = 10
        while not self._stop.is_set():
            try:
                await self.preflight()
                return
            except TossError as e:
                if e.status in (401, 403) or e.code in TOSS_FATAL_CODES:
                    await self.notify("critical", f"🔴 시작 실패: 토스 {e.status} {e.code} — 키 · 허용 IP · 계좌 확인 (자동 재시작 안 함)")
                    raise EngineFatal(f"toss {e.status} {e.code}") from None
                err = f"토스 {e.status} {e.code}"
            except Exception as e:  # noqa: BLE001 - 네트워크 등 일시적 문제
                err = f"{type(e).__name__}: {str(e)[:120]}"
            self.ev.write("start_retry", err=err, wait_s=wait)
            await self.notify("warnings", f"⚠️ 시작 점검 실패 ({err}) — {wait}초 뒤 다시", key="start_retry", every_s=600)
            await asyncio.sleep(wait)
            wait = min(300, wait * 2)

    async def run(self) -> None:
        """세 루프 중 하나라도 끝나면(오류 · 버그) 엔진 전체를 멈춘다 → launchd 가 다시 켠다 (반쪽짜리로 계속 돌지 않게)."""
        await self.start_with_retry()
        if self._stop.is_set():
            return
        loops = {asyncio.create_task(c, name=n) for n, c in (("news", self.news_loop()), ("positions", self.position_loop()),
                                                           ("housekeeping", self.housekeeping_loop()))}
        stopper = asyncio.create_task(self._stop.wait(), name="stop")
        done, _ = await asyncio.wait(loops | {stopper}, return_when=asyncio.FIRST_COMPLETED)
        crashed = [t for t in done if t is not stopper]
        for t in loops | {stopper}:
            t.cancel()
        if crashed and not self._stop.is_set():
            t = crashed[0]
            exc = t.exception() if not t.cancelled() else None
            self.ev.write("loop_died", loop=t.get_name(), err=repr(exc)[:300])
            if isinstance(exc, EngineFatal):
                raise exc
            await self.notify("critical", f"🔴 엔진 루프 '{t.get_name()}' 가 멈췄다 ({type(exc).__name__ if exc else '끝남'}) — 엔진을 다시 시작한다")
            raise RuntimeError(f"loop {t.get_name()} died: {exc!r}")

    def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        open_pos = [p for p in self.positions.values() if p.qty > 0]
        if open_pos:
            self.save_state()
            held = ", ".join(f"{p.symbol} {p.qty}주" for p in open_pos)
            await self.notify("critical", f"🔴 엔진 종료 — 보유 중: {held} (다시 켜면 이어서 관리)")
        for c in (self.toss, self.ai, self.rtpr, self.discord):
            try:
                await c.aclose()
            except Exception:  # noqa: BLE001
                pass
