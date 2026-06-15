#!/usr/bin/env bash
# Run AI Majiacheng daily briefing locally with Ollama, then publish the docs.

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/Users/mar/.local/bin:$PATH"
export TZ="Asia/Bangkok"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

cd "$PROJECT_DIR"

exec >> "$LOG_DIR/daily-run.log" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Starting AI Majiacheng daily run"

if ! curl -fsS http://localhost:11434/v1/models >/dev/null; then
  echo "Ollama OpenAI-compatible endpoint is not reachable. Trying to start ollama serve..."
  nohup ollama serve > "$LOG_DIR/ollama.log" 2>&1 &
  sleep 5
fi

curl -fsS http://localhost:11434/v1/models >/dev/null

if ! ollama list | awk '{print $1}' | grep -qx "gemma4:12b"; then
  echo "Missing Ollama model gemma4:12b"
  exit 1
fi

TARGET_DATE="$(
  python3 - <<'PY'
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

tz = ZoneInfo("Asia/Bangkok")
print((datetime.now(tz).date() - timedelta(days=1)).isoformat())
PY
)"

echo "Generating briefing for $TARGET_DATE Asia/Bangkok"

uv sync --quiet
uv run horizon --date "$TARGET_DATE" --timezone Asia/Bangkok

git add docs/_posts docs/index.md docs/_config.yml docs/CNAME data/config.json src/main.py src/orchestrator.py scripts/local-daily-run.sh launchd/com.majiacheng.ai-horizon.plist .gitignore 2>/dev/null || true

if ! git diff --cached --quiet; then
  git commit -m "Daily AI briefing: $TARGET_DATE"
  git push origin main
else
  echo "No main-branch changes to commit."
fi

echo "Publishing docs to gh-pages"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

if git fetch origin gh-pages:refs/heads/gh-pages 2>/dev/null; then
  git worktree add "$TMPDIR" gh-pages
else
  git worktree add --detach "$TMPDIR" HEAD
  cd "$TMPDIR"
  git checkout --orphan gh-pages
  git rm -rf . >/dev/null 2>&1 || true
  cd "$PROJECT_DIR"
fi

cd "$TMPDIR"
git rm -r . >/dev/null 2>&1 || true
cp -R "$PROJECT_DIR/docs/." "$TMPDIR/"
git add -A
if ! git diff --cached --quiet; then
  git commit -m "Publish AI briefing: $TARGET_DATE"
  git push origin gh-pages
else
  echo "No gh-pages changes to publish."
fi

cd "$PROJECT_DIR"
git worktree remove "$TMPDIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Done"
