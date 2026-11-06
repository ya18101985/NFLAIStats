#!/bin/bash
# Keep the Bet Tracker artifact current during a game day without an interactive Claude session.
# Rebuilds every 2 minutes from first kickoff (every 15 before), and only when the page's data
# changed does it ask a small headless Claude session (Haiku) to republish it. One session is
# reused all night (--resume), so the artifact is read once and each publish costs very little.
# Stops when every game is final.
#
# Usage: scripts/dash_loop.sh 2026-11-05 2026-11-06T01:15:00Z [artifact_url]
set -u
cd "$(dirname "$0")/.."
DAY=$1
KICKOFF=$(date -j -u -f "%Y-%m-%dT%H:%M:%SZ" "$2" +%s)
URL=${3:-https://claude.ai/artifact/3zjxWGVFxLY2cP3PmH7tNo}
LOG=logs/dash_$DAY.log
SID=$(uuidgen | tr 'A-Z' 'a-z')
# newest Claude Code (the VS Code extension's copy is usually newer than ~/.local/bin/claude)
CLAUDE=$(ls -d ~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null | sort -V | tail -1)
CLAUDE=${CLAUDE:-claude}
last=""
first=1

publish() {
  if [ $first = 1 ]; then
    "$CLAUDE" -p --model haiku --session-id "$SID" --allowedTools=Artifact \
      "Use only the Artifact tool. First call it with action read and url $URL (just to register the live version; ignore its content). Then publish file_path dashboard/index.html with url $URL (action publish). The local file is a full rebuild meant to replace the live page, so no merge is needed. Reply only OK or the error." >>"$LOG" 2>&1
    first=0
  else
    "$CLAUDE" -p --model haiku --resume "$SID" --allowedTools=Artifact \
      "Publish dashboard/index.html to $URL again, same as before. If refused because of a newer version, call action read on $URL, then publish again. Reply only OK or the error." >>"$LOG" 2>&1
  fi
}

while true; do
  out=$(.venv/bin/python scripts/dashboard.py "$DAY" 2>>"$LOG")
  echo "$(date '+%H:%M:%S') $out" >>"$LOG"
  sig=$(sed -E 's/"(generatedAt|updatedAt)": ?"[^"]*"//g' dashboard/index.html | md5)
  if [ "$sig" != "$last" ]; then
    publish && last=$sig
  fi
  case "$out" in *"games post"*) echo "final; stopping" >>"$LOG"; exit 0 ;; esac
  if [ "$(date +%s)" -lt "$KICKOFF" ]; then sleep 900; else sleep 120; fi
done
