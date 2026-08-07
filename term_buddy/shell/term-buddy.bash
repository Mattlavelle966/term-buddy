# shellcheck shell=bash
# Loaded only inside the shell pane created by term-buddy.

__term_buddy_emit_prompt() {
    local status=$?
    local command_file command
    command_file="${TERM_BUDDY_EVENTS%/*}/pending-command"
    if [[ -s "$command_file" ]]; then
        command="$(<"$command_file")"
        : > "$command_file"
        "$TERM_BUDDY_LAUNCHER" emit command_finished \
            --events "$TERM_BUDDY_EVENTS" --session "$TERM_BUDDY_SESSION" \
            --pane "${TMUX_PANE:-}" \
            --status "$status" --cwd "$PWD" --command "$command" >/dev/null 2>&1
    fi
    return "$status"
}

__term_buddy_mark_command() {
    local command command_file
    command="$(builtin history 1)"
    if [[ "$command" =~ ^[[:space:]]*[0-9]+[[:space:]]+(.*)$ ]]; then
        command="${BASH_REMATCH[1]}"
    fi
    command_file="${TERM_BUDDY_EVENTS%/*}/pending-command"
    printf '%s' "$command" > "$command_file"
    chmod 0600 "$command_file" 2>/dev/null || true
}

__term_buddy_complete() {
    local suffix
    if [[ ! -f "${TERM_BUDDY_EVENTS%/*}/autocomplete.enabled" ]]; then
        printf '\a' >&2
        return 0
    fi
    printf '\r\033[2KBuddy: completing with AI...' >&2
    suffix="$("$TERM_BUDDY_LAUNCHER" suggest --events "$TERM_BUDDY_EVENTS" --buffer "$READLINE_LINE" 2>/dev/null)"
    printf '\r\033[2K' >&2
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
        --session "$TERM_BUDDY_SESSION" --pane "${TMUX_PANE:-}" --cwd "$PWD" --message "$*"
}

# Keep the binding installed so the Buddy pane can toggle it live. The function
# checks a private per-session flag before invoking the model.
bind -x '"\e[Z":__term_buddy_complete' 2>/dev/null || true

# PS0 is expanded once after Bash accepts a non-empty command and before executing
# it. Unlike HISTCMD, this still fires when HISTCONTROL removes duplicate entries.
PS0='$(__term_buddy_mark_command)'"${PS0:-}"

if [[ -n "${PROMPT_COMMAND:-}" ]]; then
    PROMPT_COMMAND="__term_buddy_emit_prompt;${PROMPT_COMMAND}"
else
    PROMPT_COMMAND="__term_buddy_emit_prompt"
fi

"$TERM_BUDDY_LAUNCHER" emit shell_ready --events "$TERM_BUDDY_EVENTS" \
    --session "$TERM_BUDDY_SESSION" --pane "${TMUX_PANE:-}" --cwd "$PWD" >/dev/null 2>&1
