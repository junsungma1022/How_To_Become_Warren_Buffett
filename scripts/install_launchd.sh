#!/usr/bin/env bash
# 매일 09:00 (맥 시계 = KST) 에 daily_report.py 를 돌리는 launchd 작업을 설치한다.
#   bash scripts/install_launchd.sh            # 설치
#   bash scripts/install_launchd.sh --remove   # 제거
# 맥이 09:00 에 잠들어 있으면 깨어난 직후 한 번 실행된다 (launchd StartCalendarInterval 동작).
set -euo pipefail

LABEL="com.hbwb.daily-report"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${REPO}/.venv/bin/python"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

if [[ "${1:-}" == "--remove" ]]; then
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  rm -f "${PLIST}"
  echo "제거: ${PLIST}"
  exit 0
fi

[[ -x "${PY}" ]] || { echo "가상환경이 없다: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }
mkdir -p "${HOME}/Library/LaunchAgents" "${REPO}/logs"

cat > "${PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>${PY}</string><string>${REPO}/scripts/daily_report.py</string></array>
  <key>WorkingDirectory</key><string>${REPO}</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>${REPO}/logs/daily_report.out.log</string>
  <key>StandardErrorPath</key><string>${REPO}/logs/daily_report.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${PLIST}"
echo "설치: ${PLIST} (매일 09:00) · 지금 시험: launchctl kickstart gui/$(id -u)/${LABEL}"
