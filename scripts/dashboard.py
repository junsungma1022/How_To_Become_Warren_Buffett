#!/usr/bin/env python3
"""로컬 대시보드 — 엔진 상태를 브라우저에서 본다 (D-17 · 평가표 [4] UI).

    .venv/bin/python scripts/dashboard.py              # http://127.0.0.1:8765 (이 맥에서만 열린다)
    .venv/bin/python scripts/dashboard.py --port 9000

- 읽기만 한다 (data/state · data/ledger · logs). 주문 · 설정을 바꾸는 기능은 없다.
- 127.0.0.1 에만 열린다 → 같은 와이파이의 다른 기기에서도 안 보인다.
- OpenRouter 키는 서버 안에서만 쓴다 (`GET /api/v1/key` 로 사용액 · 남은 한도). 브라우저로 키를 보내지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hbwb.clock import now_ms  # noqa: E402
from hbwb.config import ROOT, load_dotenv_file, load_secrets, load_settings, scrub  # noqa: E402
from hbwb.report.live_state import build_state  # noqa: E402

KEY_TTL_S = 120


class KeyInfo:
    """OpenRouter 키 사용액 (2분 캐시 · 실패해도 대시보드는 돈다)."""

    def __init__(self, key: str | None):
        self.key, self.at, self.val, self._lock = key, 0.0, None, threading.Lock()

    def get(self) -> dict | None:
        if not self.key:
            return None
        with self._lock:
            if time.time() - self.at < KEY_TTL_S:
                return self.val
            self.at = time.time()
            try:
                req = urllib.request.Request("https://openrouter.ai/api/v1/key",
                                             headers={"Authorization": f"Bearer {self.key}"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    d = json.loads(r.read().decode("utf-8")).get("data") or {}
                self.val = {k: d.get(k) for k in ("usage", "usage_daily", "limit", "limit_remaining")}
            except Exception as e:  # noqa: BLE001
                self.val = {"error": scrub(f"{type(e).__name__}: {e}", [self.key])[:120]}
            return self.val


def make_handler(cfg: dict, keyinfo: KeyInfo, root: Path = ROOT):
    class H(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/state"):
                try:
                    st = build_state(root, cfg, now_ms(), keyinfo.get())
                    self._send(200, json.dumps(st, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                except Exception as e:  # noqa: BLE001
                    self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"[:200]}).encode(), "application/json")
            elif self.path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *a):  # 접속 기록은 화면에 찍지 않는다
            pass
    return H


PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HBWB 대시보드</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e4e3de;--good:#1a7f4b;--bad:#c0392b;--acc:#2f5fd0;--warn:#b26a00}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--card:#21211e;--ink:#ecebe6;--mute:#9b9a94;--line:#34332f;--good:#4cc38a;--bad:#ff6b5e;--acc:#7da2ff;--warn:#f0a63a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif}
header{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:16px 24px;border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0 12px 0 0}h2{font-size:14px;margin:0 0 10px;color:var(--mute);font-weight:600}
.pill{padding:3px 10px;border-radius:999px;border:1px solid var(--line);font-size:12px}
.ok{color:var(--good);border-color:var(--good)}.ng{color:var(--bad);border-color:var(--bad)}.wa{color:var(--warn);border-color:var(--warn)}
main{padding:20px 24px;display:grid;gap:16px;grid-template-columns:repeat(12,1fr)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0}
.c3{grid-column:span 3}.c4{grid-column:span 4}.c6{grid-column:span 6}.c8{grid-column:span 8}.c12{grid-column:span 12}
@media (max-width:900px){.c3,.c4,.c6,.c8{grid-column:span 12}}
.big{font-size:26px;font-weight:700}.sub{color:var(--mute);font-size:12px}
.pos{color:var(--good)}.neg{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mute);font-weight:600}td.t{white-space:normal;max-width:420px}tr.buy td{background:rgba(47,95,208,.08)}
.bar{display:flex;align-items:center;gap:8px;margin:4px 0}.bar span:first-child{width:170px;flex:none;color:var(--mute)}
.bar i{display:block;height:14px;background:var(--acc);border-radius:3px;min-width:2px}.bar b{font-weight:600}
.empty{color:var(--mute);padding:8px 0}
</style></head><body>
<header><h1>How_To_Become_Warren_Buffett</h1><span id="mode" class="pill"></span><span id="alive" class="pill"></span>
<span id="rtpr" class="pill"></span><span id="halt" class="pill" hidden></span><span class="sub" id="now"></span></header>
<main>
<section class="card c3"><h2>가상 계좌 합계</h2><div class="big" id="eq">-</div><div class="sub" id="eqsub"></div></section>
<section class="card c3"><h2>현금</h2><div class="big" id="cash">-</div><div class="sub" id="cashsub"></div></section>
<section class="card c3"><h2>누적 실현 손익</h2><div class="big" id="real">-</div><div class="sub" id="realsub"></div></section>
<section class="card c3"><h2>AI 비용 (OpenRouter)</h2><div class="big" id="ai">-</div><div class="sub" id="aisub"></div></section>
<section class="card c6"><h2 id="funnelh">오늘 깔때기</h2><div id="funnel"></div></section>
<section class="card c6"><h2>걸러진 이유 (오늘)</h2><div id="reasons"></div></section>
<section class="card c12"><h2>보유 종목 (3칸)</h2><div id="pos"></div></section>
<section class="card c12"><h2>최근 기사 (새것부터 40건)</h2><div id="arts"></div></section>
<section class="card c12"><h2>거래 기록</h2><div id="trades"></div></section>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const usd=v=>v==null?'-':(v<0?'-$':'$')+Math.abs(v).toFixed(2);
const sgn=v=>v==null?'-':(v>=0?'+':'')+v.toFixed(2);
const cls=v=>v==null?'':(v>=0?'pos':'neg');
function table(cols,rows,rowcls){if(!rows.length)return '<div class="empty">아직 없음</div>';
 return '<table><tr>'+cols.map(c=>'<th>'+c[0]+'</th>').join('')+'</tr>'+rows.map(r=>'<tr class="'+(rowcls?rowcls(r):'')+'">'+cols.map(c=>'<td'+(c[2]?' class="'+c[2]+'"':'')+'>'+c[1](r)+'</td>').join('')+'</tr>').join('')+'</table>';}
function bars(items){const m=Math.max(1,...items.map(x=>x[1]));return items.length?items.map(x=>'<div class="bar"><span>'+esc(x[0])+'</span><i style="width:'+(x[1]/m*60)+'%"></i><b>'+x[1]+'</b></div>').join(''):'<div class="empty">아직 없음</div>';}
async function tick(){
 let s;try{const r=await fetch('/api/state',{cache:'no-store'});s=await r.json();if(s.error)throw new Error(s.error);}catch(e){$('alive').textContent='대시보드 읽기 실패: '+e.message;$('alive').className='pill ng';return;}
 const e=s.engine,a=s.account,ai=s.ai;
 $('mode').textContent=e.mode==='paper'?'paper (가상 계좌)':'live (실계좌)';$('mode').className='pill '+(e.mode==='paper'?'':'wa');
 $('alive').textContent=e.alive?'엔진 동작 중 · '+e.heartbeat_age_s+'초 전 기록':(e.heartbeat_age_s==null?'엔진 기록 없음':'엔진 멈춤? '+Math.round(e.heartbeat_age_s/60)+'분째 기록 없음');
 $('alive').className='pill '+(e.alive?'ok':'ng');
 $('rtpr').textContent=e.last_alert_age_s==null?'RTPR 알림 없음':'마지막 기사 '+(e.last_alert_age_s<120?e.last_alert_age_s+'초':Math.round(e.last_alert_age_s/60)+'분')+' 전';
 $('halt').hidden=!e.halted;$('halt').textContent='신규 매수 정지: '+(e.halted||'');$('halt').className='pill ng';
 $('now').textContent='지금 '+s.now+' KST · 5초마다 갱신';
 $('eq').textContent=usd(a.equity);$('eq').className='big '+cls(a.ret_pct);
 $('eqsub').textContent='시작 '+usd(a.start)+' 대비 '+(a.ret_pct==null?'-':sgn(a.ret_pct)+'%')+' · '+a.source;
 $('cash').textContent=usd(a.cash);$('cashsub').textContent='보유 '+s.positions.length+'종목 · 칸 '+s.positions.length+'/3';
 $('real').textContent=usd(a.realized);$('real').className='big '+cls(a.realized);
 $('realsub').textContent='거래 '+a.trades+'건 · 승 '+a.wins+(a.trades?' ('+Math.round(100*a.wins/a.trades)+'%)':'');
 $('ai').textContent=usd(ai.today)+' 오늘';
 let k=ai.key,ks='';if(k&&!k.error){ks=' · OpenRouter 오늘(UTC) '+usd(k.usage_daily)+(k.limit_remaining!=null?' · 키 남은 한도 '+usd(k.limit_remaining):'');}
 $('aisub').textContent='누적 '+usd(ai.total)+(ai.per_day_est!=null?' · 하루 예상 '+usd(ai.per_day_est)+' (최근 '+ai.basis_h+'시간 기준)':' · 하루 예상은 1시간 뒤부터')+ks+' · Claude '+ai.claude_calls_today+'회';
 $('funnelh').textContent='오늘 깔때기 ('+e.window_start+' KST 부터)';$('funnel').innerHTML=bars(s.funnel);
 $('reasons').innerHTML=bars(s.reasons)+(s.universe.length?'<h2 style="margin-top:12px">종목 범위 밖 세부</h2>'+bars(s.universe):'');
 $('pos').innerHTML=table([['종목',r=>'<b>'+esc(r.symbol)+'</b>'],['칸',r=>r.slot],['수량',r=>r.qty],['평균가',r=>'$'+r.avg],['현재가',r=>r.mark?'$'+r.mark:'-'],
   ['손익',r=>r.pnl_pct==null?'-':'<span class="'+cls(r.pnl_pct)+'">'+sgn(r.pnl_pct)+'%</span>'],['최고',r=>r.peak_pct==null?'-':sgn(r.peak_pct)+'%'],
   ['익절',r=>r.tp_done+'/3'+(r.trailing?' · 추적 중':'')],['매수 시각',r=>r.since]],s.positions);
 $('arts').innerHTML=table([['시각',r=>r.time],['종목',r=>'<b>'+esc(r.ticker)+'</b>'],['제목',r=>esc(r.title),'t'],['Jev',r=>esc(r.jev)],['Claude',r=>esc(r.claude)],
   ['상승',r=>r.rise?esc(r.rise)+'%':''],['결과',r=>esc(r.result)]],s.articles,r=>r.buy?'buy':'');
 $('trades').innerHTML=table([['종목',r=>'<b>'+esc(r.ticker)+'</b>'],['칸',r=>r.slot],['매수',r=>r.entry],['매도',r=>r.exit],['보유',r=>r.hold_min+'분'],
   ['수익률',r=>'<span class="'+cls(r.ret_pct)+'">'+sgn(r.ret_pct)+'%</span>'],['손익',r=>'<span class="'+cls(r.pnl)+'">'+usd(r.pnl)+'</span>'],['매도 이유',r=>esc(r.why)]],s.trades);
}
tick();setInterval(tick,5000);
</script></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-openrouter", action="store_true", help="OpenRouter 사용액 조회 끄기")
    ap.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)   # 시험 · 화면 확인용
    args = ap.parse_args()
    load_dotenv_file()
    cfg = load_settings()
    key = None if args.no_openrouter else (load_secrets().get("openrouter_key", required=False) or None)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(cfg, KeyInfo(key), args.root))
    print(f"대시보드: http://127.0.0.1:{args.port}  (Ctrl+C 로 끈다 · 엔진에는 영향 없음)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
