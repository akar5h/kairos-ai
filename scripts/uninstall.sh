#!/usr/bin/env bash
# uninstall.sh — Remove Kairos hook entries from ~/.claude/settings.json (F1.3).
#
# Removes ONLY the kairos-added hook entries (matched by hooks/kairos_hook.py path).
# Leaves all other hooks, env keys, and settings untouched.
# Idempotent: safe to run multiple times.
#
# Usage:
#   bash scripts/uninstall.sh [--restore-backup]
#
# Options:
#   --restore-backup   Restore from the most recent kairos backup instead of
#                      doing a surgical removal.
#
# Note: this does NOT remove the env keys added by install.sh.  Those are
# harmless without the hooks and avoiding removal prevents breaking any other
# tools that may rely on those env settings.  Remove manually if desired.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK_SCRIPT="${REPO_DIR}/hooks/kairos_hook.py"
SETTINGS_FILE="${HOME}/.claude/settings.json"

# ── Preflight ─────────────────────────────────────────────────────────────────

if ! command -v jq &>/dev/null; then
    echo "ERROR: jq is required but not installed." >&2
    exit 1
fi

if [[ ! -f "${SETTINGS_FILE}" ]]; then
    echo "No settings.json found at ${SETTINGS_FILE} — nothing to do."
    exit 0
fi

# ── --restore-backup mode ─────────────────────────────────────────────────────

RESTORE_BACKUP=false
for arg in "$@"; do
    if [[ "${arg}" == "--restore-backup" ]]; then
        RESTORE_BACKUP=true
    fi
done

if [[ "${RESTORE_BACKUP}" == "true" ]]; then
    # Find the most recent kairos backup.
    LATEST_BACKUP=$(ls -t "${HOME}/.claude/settings.json.kairos-bak."* 2>/dev/null | head -1 || true)
    if [[ -z "${LATEST_BACKUP}" ]]; then
        echo "ERROR: No kairos backup found in ${HOME}/.claude/. Run without --restore-backup for surgical removal." >&2
        exit 1
    fi
    cp "${LATEST_BACKUP}" "${SETTINGS_FILE}"
    echo "Restored ${SETTINGS_FILE} from ${LATEST_BACKUP}"
    echo "Kairos uninstall complete (backup restore)."
    exit 0
fi

# ── Surgical removal ──────────────────────────────────────────────────────────
# For each event type in .hooks, filter out any hook entry whose .command
# contains the kairos_hook.py path.  Leave everything else untouched.

UPDATED=$(jq \
    --arg hook_script "${HOOK_SCRIPT}" \
    '
    if .hooks then
        .hooks = (
            .hooks |
            to_entries |
            map(
                .value = [
                    .value[] |
                    .hooks = [
                        .hooks[]? |
                        select(.command | test($hook_script; "g") | not)
                    ] |
                    # Remove the outer event-group if its hooks array is now empty.
                    select(.hooks | length > 0)
                ] |
                # Remove the event key entirely if its array is now empty.
                select(.value | length > 0)
            ) |
            from_entries
        )
    else
        .
    end
    ' \
    "${SETTINGS_FILE}")

TMP_SETTINGS="${SETTINGS_FILE}.kairos-uninstall-tmp.$$"
echo "${UPDATED}" > "${TMP_SETTINGS}"
mv "${TMP_SETTINGS}" "${SETTINGS_FILE}"

echo "Kairos hook entries removed from ${SETTINGS_FILE}."
echo ""
echo "Note: env keys added by install.sh (OTEL_*, CLAUDE_CODE_*) were left in"
echo "place. Remove them manually from ${SETTINGS_FILE} if desired."
echo ""
echo "Start a new Claude Code session to apply changes."
