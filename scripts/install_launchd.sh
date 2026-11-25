#!/usr/bin/env bash
# 맥 launchd 로 24/7 무인 운영을 설치한다. 맥 시계 = KST 기준.
#
#   bash scripts/install_launchd.sh                  # 10:00 일일 보고서 + 10분마다 감시 (엔진은 손으로 켤 때)
#   bash scripts/install_launchd.sh --engine paper   # + 엔진 상시 실행 (가상 계좌 · 주문 없음)
#   bash scripts/install_launchd.sh --engine live    # + 엔진 상시 실행 (실제 주문 · settings.yaml live_enabled: true 필요)
#   bash scripts/install_launchd.sh --stop-engine    # 엔진만 내린다 (보고서 · 감시는 그대로)
#   bash scripts/install_launchd.sh --status         # 지금 상태
#   bash scripts/install_launchd.sh --remove         # 전부 제거
#
# 엔진 서비스: 로그인하면 시작 · 오류로 죽으면 60초 뒤 다시 켬 · 키 · 구독 · 허용 IP 문제(종료 코드 0)는 다시 켜지 않음
# 잠자기 방지: 엔진을 켜 두는 동안 caffeinate 를 같이 띄운다 (전원 연결 상태에서 잠들지 않게).
#   ⚠️ 노트북 뚜껑을 닫으면 caffeinate 로도 잠든다 (외부 모니터 · 전원 연결 클램쉘 모드는 예외) → 뚜껑은 열어 둔다.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${REPO}/.venv/bin/python"
AGENTS="${HOME}/Library/LaunchAgents"
UID_="$(id -u)"
L_ENGINE="com.hbwb.engine"; L_AWAKE="com.hbwb.awake"; L_REPORT="com.hbwb.daily-report"; L_WATCH="com.hbwb.watchdog"

plist() {  # 이름 · <dict> 안에 들어갈 나머지 XML
  local label="$1" body="$2"
  cat > "${AGENTS}/${label}.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${label}</string>
  <key>WorkingDirectory</key><string>${REPO}</string>
  <key>StandardOutPath</key><string>${REPO}/logs/${label#com.hbwb.}.out.log</string>
  <key>StandardErrorPath</key><string>${REPO}/logs/${label#com.hbwb.}.err.log</string>
${body}
</dict>
</plist>
EOF
}

load()   { launchctl bootout "gui/${UID_}/$1" 2>/dev/null || true; launchctl bootstrap "gui/${UID_}" "${AGENTS}/$1.plist"; }
unload() { launchctl bootout "gui/${UID_}/$1" 2>/dev/null || true; rm -f "${AGENTS}/$1.plist"; }

status() {
  for l in "${L_ENGINE}" "${L_AWAKE}" "${L_REPORT}" "${L_WATCH}"; do
    if launchctl print "gui/${UID_}/${l}" >/dev/null 2>&1; then
      st="$(launchctl print "gui/${UID_}/${l}" | awk -F'= ' '/state =/{print $2; exit}')"
      ex="$(launchctl print "gui/${UID_}/${l}" | awk -F'= ' '/last exit code =/{print $2; exit}')"
      echo "  ${l}: 설치됨 · 상태 ${st:-?} · 마지막 종료 코드 ${ex:-없음}"
    else
      echo "  ${l}: 없음"
    fi
  done
  [[ -f "${REPO}/data/state/heartbeat.json" ]] && echo "  heartbeat: $(cat "${REPO}/data/state/heartbeat.json" | head -c 200)"
  return 0
}

case "${1:-}" in
  --remove)      for l in "${L_ENGINE}" "${L_AWAKE}" "${L_REPORT}" "${L_WATCH}"; do unload "$l"; done; echo "전부 제거"; exit 0 ;;
  --stop-engine) unload "${L_ENGINE}"; unload "${L_AWAKE}"; echo "엔진 내림 (보고서 · 감시는 그대로)"; exit 0 ;;
  --status)      status; exit 0 ;;
esac

[[ -x "${PY}" ]] || { echo "가상환경이 없다: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }
mkdir -p "${AGENTS}" "${REPO}/logs"
# 일일 보고서 시각 = settings.yaml report.daily_time_kst 의 시 (맥 시간대가 KST 라고 본다)
REPORT_HOUR=$(sed -n 's/^ *daily_time_kst: *"\{0,1\}\([0-9][0-9]\):00.*/\1/p' "${REPO}/config/settings.yaml" | head -1)
REPORT_HOUR=$((10#${REPORT_HOUR:-10}))

plist "${L_REPORT}" "  <key>ProgramArguments</key>
  <array><string>${PY}</string><string>${REPO}/scripts/daily_report.py</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>${REPORT_HOUR}</integer><key>Minute</key><integer>0</integer></dict>"
load "${L_REPORT}"

plist "${L_WATCH}" "  <key>ProgramArguments</key>
  <array><string>${PY}</string><string>${REPO}/scripts/watchdog.py</string></array>
  <key>StartInterval</key><integer>600</integer>"
load "${L_WATCH}"
echo "설치: 일일 보고서 (매일 ${REPORT_HOUR}:00 KST) · 감시 (10분마다)"

if [[ "${1:-}" == "--engine" ]]; then
  MODE="${2:-}"
  [[ "${MODE}" == "shadow" ]] && MODE=paper
  [[ "${MODE}" == "paper" || "${MODE}" == "live" ]] || { echo "--engine paper 또는 --engine live"; exit 1; }
  plist "${L_ENGINE}" "  <key>ProgramArguments</key>
  <array><string>${PY}</string><string>${REPO}/scripts/run_live.py</string><string>--mode</string><string>${MODE}</string><string>--quiet</string><string>--no-delay</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>60</integer>"
  plist "${L_AWAKE}" "  <key>ProgramArguments</key>
  <array><string>/usr/bin/caffeinate</string><string>-i</string><string>-s</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>"
  load "${L_AWAKE}"
  load "${L_ENGINE}"
  echo "설치: 엔진 (${MODE}) 상시 실행 · 잠자기 방지"
fi
status
