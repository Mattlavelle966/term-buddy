#!/usr/bin/env bash
set -euo pipefail

PREFIX="${PREFIX:-$HOME/.local}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    printf 'term-buddy requires Python 3.10 or newer.\n' >&2
    exit 1
fi
if ! command -v tmux >/dev/null 2>&1; then
    printf 'warning: tmux is not installed; install it before running term-buddy.\n' >&2
fi

INSTALL_ROOT="$PREFIX/lib/term-buddy"
install -d "$INSTALL_ROOT" "$PREFIX/bin"
install -d "$INSTALL_ROOT/term_buddy"
cp -R "$SCRIPT_DIR/term_buddy/." "$INSTALL_ROOT/term_buddy/"
install -d "$INSTALL_ROOT/assets"
cp -R "$SCRIPT_DIR/assets/." "$INSTALL_ROOT/assets/"
printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    "export PYTHONPATH=\"$INSTALL_ROOT\${PYTHONPATH:+:\${PYTHONPATH}}\"" \
    "exec \"$PYTHON_BIN\" -m term_buddy \"\$@\"" \
    > "$PREFIX/bin/term-buddy"
chmod 0755 "$PREFIX/bin/term-buddy"
printf 'Installed term-buddy. Ensure %s/bin is in PATH.\n' "$PREFIX"
