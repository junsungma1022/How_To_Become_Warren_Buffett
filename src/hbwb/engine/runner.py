"""매매 엔진 — 기사 → 종목 범위 → Jev(최소 컷) → Claude(메인 판단 · 리포트) → 분할 매수 → 분할 매도.

한 번에 한 종목만 보유한다. 모든 판단은 events-*.jsonl 과 articles.csv / trades.csv 에 남는다.
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
from ..clock import KST, fmt_kst, iso_utc, ms_to, now_ms
from ..config import ROOT, Secrets
from ..eventlog import EventLog
from ..ledger import articles as articles_ledger
from ..ledger import trades as trades_ledger
from ..news.rtpr import Alert, RtprClient, RtprFatal
from ..notify.discord import Discord
from ..strategy.risk import RiskBook
from ..strategy.rules import Fill, Position, entry2_qty, entry2_triggered, exit_decision, plan_entry1, update_peak
from ..universe import Universe
from . import sessions as S
from .executor import LiveExecutor, ShadowExecutor


def report_day_key(ms: int) -> str:
    """KST 09:00 ~ 다음날 09:00 을 하루로 (일일 보고서와 같은 경계)."""
    return (ms_to(ms, KST) - dt.timedelta(hours=9)).strftime("%Y-%m-%d")


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
        self.pos: Position | None = None
        self.pos_meta: dict = {}
        self.windows: list[tuple[str, int, int]] = []
        self.fee_rate = cfg["fees"]["commission_rate_default"]
        self.seen: set[str] = set()
        self.last_alert_ms = now_ms()
        self._entry_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(4)
        self._stop = asyncio.Event()
        self.state_path = self.data_dir / "state" / "position.json"
        self.executor = None
        self._bg: set[asyncio.Task] = set()     # 기사 처리 작업 (참조를 들고 있어야 GC 로 사라지지 않는다)

    # ── 알림 ────────────────────────────────────────────────────────────────
    async def notify(self, channel: str, text: str, key: str | None = None, every_s: float = 0) -> None:
        try:
            await self.discord.send(channel, text, key, every_s)
        except Exception as e:  # noqa: BLE001 - 알림 실패로 매매가 멈추면 안 된다
            self.ev.write("notify_exception", err=str(e)[:200])

    # ── 시작 점검 ────────────────────────────────────────────────────────────
    async def preflight(self) -> None:
        bp = await self.toss.buying_power_usd()
        rate = await self.toss.commission_rate_us()
        if rate is not None:
            self.fee_rate = rate
        await self.refresh_calendar()
        self.ev.write("preflight", mode=self.mode, buying_power_usd=bp, fee_rate=self.fee_rate,
                      sessions=[(n, fmt_kst(a), fmt_kst(b)) for n, a, b in self.windows])
        if bp < 1:
            await self.notify("critical", f"🔴 토스 USD 매수가능금액 ${bp:.2f} — 앱에서 달러로 환전해 둘 것 (Open API 는 자동 환전 안 함)")
        self.executor = (LiveExecutor(self.toss, self.cfg, self.ev.write, self.notify, self.fee_rate) if self.mode == "live"
                         else ShadowExecutor(self.cfg, self.ev.write, self.fee_rate, self.toss.quote))
        await self.restore_state()
        await self.notify("warnings", f"▶️ 시작 ({self.mode}) · USD 매수가능 ${bp:.2f} · 수수료율 {self.fee_rate:.4%} · "
                                      f"예산 ${self.cfg['capital']['budget_usd']:.0f}")

    async def refresh_calendar(self) -> None:
        cal = await self.toss.us_calendar()
        self.windows = S.windows_from_calendar(cal)

    # ── 상태 저장 (재시작 대비) ──────────────────────────────────────────────
    def save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if self.pos is None or self.pos.closed:
            if self.state_path.exists():
                self.state_path.unlink()
            return
        d = {"pos": {**asdict(self.pos), "fills": [asdict(f) for f in self.pos.fills]}, "meta": self.pos_meta}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    async def restore_state(self) -> None:
        if not self.state_path.exists():
            return
        d = json.loads(self.state_path.read_text(encoding="utf-8"))
        p = d["pos"]
        pos = Position(p["symbol"], p["budget"], [Fill(**f) for f in p["fills"]], p["first_fill_ms"], p["peak"],
                       p["tp1_done"], p["entry2_done"])
        held = pos.qty
        if self.mode == "live":
            held = int(await self.toss.sellable_qty(pos.symbol))
        if held <= 0:
            self.ev.write("restore_skip", symbol=pos.symbol, why="보유 수량 0")
            self.state_path.unlink()
            return
        if held != pos.qty:
            self.risk.halted = f"restore_mismatch {pos.symbol} 기록 {pos.qty} vs 계좌 {held}"
            await self.notify("critical", f"🔴 재시작: {pos.symbol} 기록 {pos.qty}주 ≠ 계좌 {held}주 — 신규 매수 멈춤 · 수동 확인")
        self.pos, self.pos_meta = pos, d.get("meta") or {}
        self.ev.write("restore", symbol=pos.symbol, qty=pos.qty)
        await self.notify("warnings", f"↩️ 재시작: {pos.symbol} {pos.qty}주 보유 상태를 이어서 관리")

    # ── 세션 ────────────────────────────────────────────────────────────────
    def session_now(self, t: int | None = None) -> str | None:
        w = S.current(self.windows, t or now_ms())
        return w[0] if w else None

    def tradeable_now(self) -> bool:
        return S.allowed(self.session_now(), self.cfg)

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
        block = self.risk.block_reason(a.ticker, now_ms(), None, self.pos is not None)
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

        result = await self.enter(a, art.title, uc, jr, cr)
        done("buy" if result == "filled" else "skip", result)

    # ── 매수 ────────────────────────────────────────────────────────────────
    async def equity(self) -> tuple[float, float]:
        cash = await self.toss.buying_power_usd() if self.mode == "live" else self.cfg["capital"]["budget_usd"] + self.risk.realized_today
        mv = 0.0
        if self.pos and self.pos.qty > 0:
            p = (await self.toss.last_prices([self.pos.symbol])).get(self.pos.symbol) or (self.pos.avg_price or 0)
            mv = p * self.pos.qty
        return cash, cash + mv

    async def enter(self, a: Alert, title: str, uc, jr, cr) -> str:
        async with self._entry_lock:
            cash, eq = await self.equity()
            block = self.risk.block_reason(a.ticker, now_ms(), eq, self.pos is not None)
            if block:
                if block.startswith("halted"):
                    await self.notify("critical", f"🔴 시스템 정지: {block} — 신규 매수 안 함")
                return f"risk:{block}"
            budget = min(self.cfg["capital"]["budget_usd"], cash)
            q = await self.toss.quote(a.ticker)
            if not q.ask:
                return "no_ask"
            q1, total, limit = plan_entry1(budget, q.ask, self.cfg, self.fee_rate)
            if q1 <= 0:
                return f"too_expensive(ask {q.ask})"
            fill = await self.executor.buy(a.ticker, q1, limit, "entry1")
            if not fill:
                return "entry1_not_filled"
            self.pos = Position(a.ticker, budget)
            self.pos.add(fill)
            self.pos.peak = fill.price
            if total == 1:
                self.pos.entry2_done = True     # 1주뿐이면 분할 없음
            self.pos_meta = {"trade_id": uuid.uuid4().hex[:12], "article_id": a.article_id, "title": title,
                             "jev": jev_mod.KO.get(jr.choice or "", ""), "claude_grade": cr.impact_grade,
                             "claude_conf": cr.confidence, "cash_left": budget - fill.qty * fill.price - fill.fee}
            self.risk.record_open()
            self.save_state()
            mcap = f"${uc.market_cap / 1e6:.0f}M" if uc.market_cap else "?"
            await self.notify("entries",
                              f"🟢 **매수 1차** {a.ticker} {fill.qty}주 @ ${fill.price:.4g} (시총 {mcap})\n"
                              f"> {title[:200]}\nJev: {self.pos_meta['jev']} · Claude: {cr.impact_grade}/5 "
                              f"(확신 {cr.confidence}) · {cr.catalyst_type}\n**매수 근거 리포트**\n{cr.report_ko}")
            return "filled"

    # ── 보유 관리 (가격 확인 → 2차 매수 · 분할 매도) ────────────────────────
    async def manage_once(self) -> None:
        pos = self.pos
        if pos is None:
            return
        sym, t = pos.symbol, now_ms()
        if S.current(self.windows, t) is None:
            # 장이 모두 닫힌 시간에 보유 중 — 정상이라면 애프터마켓 끝 전에 정리됐어야 한다. 주문은 거부되므로 내지 않는다
            await self.notify("critical", f"🔴 장이 닫힌 시간에 {sym} {pos.qty}주 보유 중 — 다음 세션 시작 때 정리 시도 · 수동 확인 권장",
                              key="hold_closed", every_s=1800)
            return
        last = (await self.toss.last_prices([sym])).get(sym)
        if not last:
            return
        update_peak(pos, last)
        if entry2_triggered(pos, last, t, self.cfg):
            pos.entry2_done = True
            q = await self.toss.quote(sym)
            if q.ask:
                q2, lim2 = entry2_qty(pos, q.ask, self.pos_meta.get("cash_left", 0.0), self.cfg, self.fee_rate)
                if q2 > 0:
                    f2 = await self.executor.buy(sym, q2, lim2, "entry2")
                    if f2:
                        pos.add(f2)
                        self.pos_meta["cash_left"] -= f2.qty * f2.price + f2.fee
                        await self.notify("entries", f"🟢 **매수 2차** {sym} {f2.qty}주 @ ${f2.price:.4g} (1차 대비 상승 확인)")
            self.save_state()
        end = S.trading_block_end(self.windows, t, self.cfg)
        if end is None:  # 허용 세션 밖 (데이마켓 등) — 정리 시각을 넘겼다
            end = t
        ex = exit_decision(pos, last, t, end, self.cfg)
        if ex is None:
            return
        fill = await self.executor.sell(sym, ex.qty, ex.reason, lambda: self.toss.quote(sym),
                                        lambda: self._last(sym))
        if fill:
            pos.add(fill)
            pnl_part = fill.qty * (fill.price - (pos.avg_price or 0)) - fill.fee
            await self.notify("exits", f"🔵 **매도** {sym} {fill.qty}주 @ ${fill.price:.4g} · {ex.reason} · 이번 손익 ${pnl_part:+.2f}")
        self.save_state()
        if pos.closed:
            await self.close_trade()

    async def _last(self, sym: str) -> float | None:
        return (await self.toss.last_prices([sym])).get(sym)

    async def close_trade(self) -> None:
        pos, m = self.pos, self.pos_meta
        buys = [f for f in pos.fills if f.side == "BUY"]
        sells = [f for f in pos.fills if f.side == "SELL"]
        e1 = next((f for f in buys if f.reason == "entry1"), None)
        e2 = next((f for f in buys if f.reason == "entry2"), None)
        sold = sum(f.qty for f in sells)
        pnl = pos.realized_pnl()
        row = {"trade_id": m.get("trade_id"), "article_id": m.get("article_id"), "ticker": pos.symbol, "mode": self.mode,
               "entry1_utc": iso_utc(e1.t_ms) if e1 else "", "entry1_qty": e1.qty if e1 else 0,
               "entry1_price": e1.price if e1 else None, "entry2_utc": iso_utc(e2.t_ms) if e2 else "",
               "entry2_qty": e2.qty if e2 else 0, "entry2_price": e2.price if e2 else None,
               "avg_price": pos.avg_price, "bought_qty": pos.bought_qty, "exit_utc": iso_utc(sells[-1].t_ms) if sells else "",
               "exit_reasons": "|".join(f.reason for f in sells), "sold_qty": sold,
               "avg_exit_price": (sum(f.qty * f.price for f in sells) / sold) if sold else None,
               "fees_usd": pos.fees, "pnl_usd": pnl, "ret_pct": 100 * pnl / pos.cost if pos.cost else None,
               "hold_min": (sells[-1].t_ms - pos.first_fill_ms) / 60000 if sells and pos.first_fill_ms else None,
               "peak_price": pos.peak, "jev_choice": m.get("jev"), "claude_grade": m.get("claude_grade"),
               "claude_confidence": m.get("claude_conf")}
        self.trades.append(row)
        self.risk.record_close(pos.symbol, pnl, now_ms())
        self.ev.write("trade_closed", **row)
        await self.notify("exits", f"✅ **거래 종료** {pos.symbol} · 손익 ${pnl:+.2f} ({row['ret_pct']:+.2f}%) · "
                                   f"{row['exit_reasons']} · 보유 {row['hold_min']:.1f}분")
        self.pos, self.pos_meta = None, {}
        self.save_state()
        if self.mode == "live":
            held = await self.toss.sellable_qty(row["ticker"])
            if held > 0:
                await self.notify("critical", f"🔴 {row['ticker']} 청산했다고 기록했지만 계좌에 {held}주 남음 — 수동 확인")

    # ── 루프들 ──────────────────────────────────────────────────────────────
    async def news_loop(self) -> None:
        def on_event(kind: str, fields: dict) -> None:
            self.ev.write(kind, **fields)
        try:
            async for a in self.rtpr.alerts(on_event):
                self.last_alert_ms = now_ms()
                self.ev.write("alert", ticker=a.ticker, article=a.article_id, age_s=a.age_s)
                task = asyncio.create_task(self.handle_alert(a))
                self._bg.add(task)
                task.add_done_callback(self._bg.discard)
        except RtprFatal as e:
            await self.notify("critical", f"🔴 {e} — 뉴스 수신 중단, 엔진을 멈춘다")
            self._stop.set()

    async def position_loop(self) -> None:
        interval = self.cfg["exit"]["poll_interval_s"]
        while not self._stop.is_set():
            try:
                await self.manage_once()
            except TossError as e:
                self.ev.write("manage_error", status=e.status, code=e.code, msg=e.message)
                if e.code in {"ip-not-allowed", "forbidden", "account-restricted"}:
                    await self.notify("critical", f"🔴 토스 {e.code} — 허용 IP · 계좌 상태 확인 (보유 관리 불가)", key="toss_fatal", every_s=300)
                await asyncio.sleep(2)
            except Exception as e:  # noqa: BLE001
                self.ev.write("manage_error", err=f"{type(e).__name__}: {e}"[:300])
                await self.notify("warnings", f"⚠️ 보유 관리 오류 {type(e).__name__}", key="mg_err", every_s=300)
                await asyncio.sleep(2)
            await asyncio.sleep(interval)

    async def housekeeping_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(60)
            t = now_ms()
            self.risk.roll_day(report_day_key(t))
            try:
                if t % (30 * 60_000) < 60_000:
                    await self.refresh_calendar()
            except Exception as e:  # noqa: BLE001
                self.ev.write("calendar_error", err=str(e)[:200])
            if self.session_now() in {"pre", "regular", "after"} and t - self.last_alert_ms > 30 * 60_000:
                await self.notify("warnings", "⚠️ 미국 장 시간인데 RTPR 알림 30분째 0건 — 연결 · 'All articles' 규칙 확인",
                                  key="quiet", every_s=1800)

    async def run(self) -> None:
        await self.preflight()
        self.risk.roll_day(report_day_key(now_ms()))
        tasks = [asyncio.create_task(c) for c in (self.news_loop(), self.position_loop(), self.housekeeping_loop())]
        await self._stop.wait()
        for t in tasks:
            t.cancel()

    def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        if self.pos is not None:
            self.save_state()
            await self.notify("critical", f"🔴 엔진 종료 — {self.pos.symbol} {self.pos.qty}주 보유 중 (다시 켜면 이어서 관리)")
        for c in (self.toss, self.ai, self.rtpr, self.discord):
            try:
                await c.aclose()
            except Exception:  # noqa: BLE001
                pass
