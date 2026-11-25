#!/usr/bin/env bash
# 자주 쓰는 명령을 한 곳에 (D-17). 어디서든 `hbwb 명령` 으로 쓰려면 한 번만:
#   echo "alias hbwb='bash $HOME/Projects/How_To_Become_Warren_Buffett/scripts/hbwb.sh'" >> ~/.zshrc && source ~/.zshrc
#
#   hbwb start     엔진 켜기 (paper · 24/7 · 10:00 보고서 · 감시 · 잠자기 방지)
#   hbwb stop      전부 끄기 (기록은 남는다 · 다시 start 하면 이어진다)
#   hbwb restart   끄고 다시 켜기
#   hbwb status    상태 (+ 가상 계좌)
#   hbwb dash      대시보드 켜고 브라우저 열기 (http://127.0.0.1:8765)
#   hbwb dash-stop 대시보드 끄기
#   hbwb watch     실시간 한 줄 보기 (Ctrl+C 로 끔 · 엔진은 계속)
#   hbwb report    지금까지 오늘 요약 (Discord 로 안 보냄)
#   hbwb history   날짜별 전체 결과
#   hbwb check     연결 점검 (check_setup --discord)
#   hbwb errors    엔진 오류 로그 끝 40줄
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO}"
PY="${REPO}/.venv/bin/python"
PORT="${HBWB_DASH_PORT:-8765}"

dash_running() { lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; }

case "${1:-help}" in
  start)     bash scripts/install_launchd.sh --engine paper ;;
  stop)      bash scripts/install_launchd.sh --remove ;;
  restart)   bash scripts/install_launchd.sh --remove; sleep 2; bash scripts/install_launchd.sh --engine paper ;;
  status)    bash scripts/install_launchd.sh --status ;;
  dash)
    mkdir -p logs
    if ! dash_running; then
      nohup "${PY}" scripts/dashboard.py --port "${PORT}" > logs/dashboard.log 2>&1 &
      for _ in 1 2 3 4 5 6 7 8 9 10; do dash_running && break; sleep 0.3; done
    fi
    dash_running && { echo "대시보드: http://127.0.0.1:${PORT} (끄기: hbwb dash-stop)"; open "http://127.0.0.1:${PORT}"; } \
                 || { echo "대시보드가 안 켜졌다 — logs/dashboard.log 확인"; tail -n 20 logs/dashboard.log; exit 1; } ;;
  dash-stop) pkill -f "scripts/dashboard.py" && echo "대시보드 끔" || echo "켜져 있지 않다" ;;
  watch)     exec "${PY}" scripts/watch.py ;;
  report)    exec "${PY}" scripts/daily_report.py --dry-run --current ;;
  history)   exec "${PY}" scripts/daily_report.py --dry-run --history ;;
  check)     exec "${PY}" scripts/check_setup.py --discord ;;
  errors)    tail -n 40 logs/engine.err.log 2>/dev/null || echo "오류 로그 없음" ;;
  *)         sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
esac
