#!/usr/bin/env bash
# install.sh — Wire ALL future Claude Code sessions into Kairos (F1.3).
#
# MANUAL TEST PROCEDURE (run once to verify idempotency):
#   1. Create a temp home:
#        TMPDIR=$(mktemp -d) && export HOME=$TMPDIR
#        mkdir -p "$HOME/.claude"
#        echo '{"env":{"EXISTING_KEY":"existing_val"},"hooks":{"PostToolUse":[]}}' \
#             > "$HOME/.claude/settings.json"
#   2. Run install once:
#        bash /path/to/kairos/scripts/install.sh
#   3. Verify output: one kairos hook entry per event, EXISTING_KEY preserved.
#   4. Run install a second time:
#        bash /path/to/kairos/scripts/install.sh
#   5. Verify: exactly ONE kairos hook entry per event (no duplicates), same env keys.
#
# AUTOMATED TEST: tests/test_install_sh.py shells out via subprocess and verifies
# idempotency, env-key preservation, and hook-entry counts against a seeded tmp HOME.
# It also runs `bash -n` syntax check on both scripts.
#
# Usage:
#   bash scripts/install.sh [OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318]
#
# Options (positional env overrides):
#   OTEL_EXPORTER_OTLP_ENDPOINT  — OTLP collector endpoint (default http://localhost:4318)
#
# What this modifies:
#   ~/.claude/settings.json — env keys (additive) + hooks stanzas (append, idempotent).
#
# How to uninstall:
#   bash scripts/uninstall.sh
#
# Security note:
#   OTEL_* env vars are NOT propagated to hook subprocesses by Claude Code.
#   The hook (hooks/kairos_hook.py) reads only KAIROS_SPOOL_DIR / KAIROS_HOOK_DISABLED.
#   The DSN (for the uploader) is supplied separately — not stored in settings.json.

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────────

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK_SCRIPT="${REPO_DIR}/hooks/kairos_hook.py"

# Resolve the python interpreter: prefer the repo's venv, fall back to system python3.
if [[ -x "${REPO_DIR}/.venv/bin/python" ]]; then
    PYTHON_BIN="${REPO_DIR}/.venv/bin/python"
elif command -v python3 &>/dev/null; then
    PYTHON_BIN="$(command -v python3)"
else
    echo "ERROR: python3 not found. Install Python 3 and retry." >&2
    exit 1
fi

HOOK_COMMAND="${PYTHON_BIN} ${HOOK_SCRIPT}"

# Allow caller to override the OTLP endpoint.
OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://localhost:4318}"

SETTINGS_FILE="${HOME}/.claude/settings.json"
TIMESTAMP="$(date +%Y%m%d%H%M%S)"
BACKUP_FILE="${HOME}/.claude/settings.json.kairos-bak.${TIMESTAMP}"

# ── Preflight ─────────────────────────────────────────────────────────────────

if ! command -v jq &>/dev/null; then
    echo "ERROR: jq is required but not installed." >&2
    echo "  macOS:  brew install jq" >&2
    echo "  Debian: apt-get install jq" >&2
    exit 1
fi

if [[ ! -f "${HOOK_SCRIPT}" ]]; then
    echo "ERROR: hook script not found at ${HOOK_SCRIPT}" >&2
    exit 1
fi

# ── Ensure settings.json exists ───────────────────────────────────────────────

mkdir -p "${HOME}/.claude"
if [[ ! -f "${SETTINGS_FILE}" ]]; then
    echo '{}' > "${SETTINGS_FILE}"
    echo "Created ${SETTINGS_FILE}"
fi

# ── Backup ────────────────────────────────────────────────────────────────────

cp "${SETTINGS_FILE}" "${BACKUP_FILE}"
echo "Backed up settings.json → ${BACKUP_FILE}"

# ── Idempotency check ─────────────────────────────────────────────────────────
# Detect whether this exact hook command is already present anywhere in the hooks.
# If so, skip the merge (already installed).

ALREADY_INSTALLED=false
if jq -e --arg cmd "${HOOK_COMMAND}" '
    .hooks // {} |
    to_entries[] |
    .value[] |
    .hooks[]? |
    select(.command == $cmd)
' "${SETTINGS_FILE}" &>/dev/null; then
    ALREADY_INSTALLED=true
fi

# ── Build the new-hooks fragment ──────────────────────────────────────────────
# Each event type gets one kairos hook entry.  We use async=true on PostToolUse /
# PostToolUseFailure so the hook never blocks the user's loop.

read -r -d '' KAIROS_HOOKS_FRAGMENT <<EOF || true
{
  "PostToolUse": [
    {
      "matcher": "",
      "hooks": [
        {
          "type": "command",
          "command": "${HOOK_COMMAND}",
          "async": true
        }
      ]
    }
  ],
  "PostToolUseFailure": [
    {
      "matcher": "",
      "hooks": [
        {
          "type": "command",
          "command": "${HOOK_COMMAND}",
          "async": true
        }
      ]
    }
  ],
  "SessionStart": [
    {
      "matcher": "",
      "hooks": [
        {
          "type": "command",
          "command": "${HOOK_COMMAND}"
        }
      ]
    }
  ],
  "SessionEnd": [
    {
      "matcher": "",
      "hooks": [
        {
          "type": "command",
          "command": "${HOOK_COMMAND}"
        }
      ]
    }
  ]
}
EOF

# ── Merge env keys ────────────────────────────────────────────────────────────
# Additive: never remove existing keys, only add/update kairos-required ones.

NEW_ENV=$(jq -n \
    --arg endpoint "${OTLP_ENDPOINT}" \
    '{
        CLAUDE_CODE_ENABLE_TELEMETRY:        "1",
        OTEL_LOGS_EXPORTER:                  "otlp",
        OTEL_METRICS_EXPORTER:               "otlp",
        OTEL_EXPORTER_OTLP_PROTOCOL:         "http/protobuf",
        OTEL_EXPORTER_OTLP_ENDPOINT:         $endpoint,
        OTEL_LOG_TOOL_DETAILS:               "1",
        CLAUDE_CODE_ENHANCED_TELEMETRY_BETA: "1",
        OTEL_TRACES_EXPORTER:                "otlp",
        OTEL_LOG_TOOL_CONTENT:               "1"
    }')

# ── Apply merge ───────────────────────────────────────────────────────────────

if [[ "${ALREADY_INSTALLED}" == "true" ]]; then
    echo ""
    echo "Kairos hooks are already present in ${SETTINGS_FILE}."
    echo "Skipping hook merge (idempotent). Env keys will still be ensured."
    UPDATED=$(jq \
        --argjson new_env "${NEW_ENV}" \
        '.env = ($new_env + (.env // {}))' \
        "${SETTINGS_FILE}")
else
    # Merge env (new keys take precedence so we can update endpoints, but existing
    # non-kairos keys are preserved via the + merge order).
    # For hooks: for each event type, APPEND kairos entries to any existing arrays.
    UPDATED=$(jq \
        --argjson new_env "${NEW_ENV}" \
        --argjson new_hooks "${KAIROS_HOOKS_FRAGMENT}" \
        '
        # Merge env: existing keys win over new defaults (reverse order).
        .env = ($new_env + (.env // {})) |
        # Merge hooks: append kairos entries to existing arrays per event type.
        .hooks = (
            (.hooks // {}) as $existing |
            $new_hooks |
            to_entries |
            reduce .[] as $entry (
                $existing;
                .[$entry.key] = ((.[$entry.key] // []) + $entry.value)
            )
        )
        ' \
        "${SETTINGS_FILE}")
fi

# Write atomically via a temp file.
TMP_SETTINGS="${SETTINGS_FILE}.kairos-tmp.$$"
echo "${UPDATED}" > "${TMP_SETTINGS}"
mv "${TMP_SETTINGS}" "${SETTINGS_FILE}"

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "Kairos install complete."
echo ""
echo "  Settings file : ${SETTINGS_FILE}"
echo "  Backup        : ${BACKUP_FILE}"
echo "  Hook command  : ${HOOK_COMMAND}"
echo "  OTLP endpoint : ${OTLP_ENDPOINT}"
echo ""
if [[ "${ALREADY_INSTALLED}" == "true" ]]; then
    echo "  Status: hooks already present — no duplicate entries added."
else
    echo "  Status: hooks added for PostToolUse, PostToolUseFailure, SessionStart, SessionEnd."
fi
echo ""
echo "To uninstall: bash ${REPO_DIR}/scripts/uninstall.sh"
echo ""
echo "Start a new Claude Code session to activate."
