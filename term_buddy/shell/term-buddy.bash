# shellcheck shell=bash
# Loaded only inside the shell pane created by term-buddy.

__term_buddy_emit_prompt() {
    local status=$?
    local hist_num command
    hist_num="${HISTCMD:-0}"
    if [[ "$hist_num" != "${__TERM_BUDDY_LAST_HIST:-$hist_num}" ]]; then
        command="$(builtin history 1)"
        if [[ "$command" =~ ^[[:space:]]*[0-9]+[[:space:]]+(.*)$ ]]; then
            command="${BASH_REMATCH[1]}"
        fi
        "$TERM_BUDDY_LAUNCHER" emit command_finished \
            --events "$TERM_BUDDY_EVENTS" --session "$TERM_BUDDY_SESSION" \
            --status "$status" --cwd "$PWD" --command "$command" >/dev/null 2>&1 &
    fi
    __TERM_BUDDY_LAST_HIST="$hist_num"
    return "$status"
}

__term_buddy_complete() {
    local suffix
    suffix="$("$TERM_BUDDY_LAUNCHER" suggest --events "$TERM_BUDDY_EVENTS" --buffer "$READLINE_LINE" 2>/dev/null)"
    if [[ -n "$suffix" ]]; then
        READLINE_LINE="${READLINE_LINE}${suffix}"
        READLINE_POINT=${#READLINE_LINE}
    fi
}

buddy() {
    if (($# == 0)); then
        printf 'usage: buddy <question>\n' >&2
        return 2
    fi
    "$TERM_BUDDY_LAUNCHER" emit question --events "$TERM_BUDDY_EVENTS" \
        --session "$TERM_BUDDY_SESSION" --cwd "$PWD" --message "$*"
}

bind -x '"\e[Z":__term_buddy_complete' 2>/dev/null || true

if [[ -n "${PROMPT_COMMAND:-}" ]]; then
    PROMPT_COMMAND="__term_buddy_emit_prompt;${PROMPT_COMMAND}"
else
    PROMPT_COMMAND="__term_buddy_emit_prompt"
fi

"$TERM_BUDDY_LAUNCHER" emit shell_ready --events "$TERM_BUDDY_EVENTS" \
    --session "$TERM_BUDDY_SESSION" --cwd "$PWD" >/dev/null 2>&1 &
