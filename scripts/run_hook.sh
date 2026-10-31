#!/bin/sh
# POSIX shell launcher for Downshift lifecycle hooks (US-2.1 / Issue #10).
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET_ARG="${1:-}"
shift 2>/dev/null || true

fail_open() {
    case "$TARGET_ARG" in
        *pre_tool_use*) printf '{"decision":"allow"}\n' ;;
        *) printf '{}\n' ;;
    esac
    exit 0
}

if [ -z "$TARGET_ARG" ]; then
    fail_open
fi

if [ -f "$TARGET_ARG" ]; then
    RESOLVED_SCRIPT="$TARGET_ARG"
else
    BASE_NAME=$(basename -- "$TARGET_ARG")
    RESOLVED_SCRIPT="$SCRIPT_DIR/$BASE_NAME"
fi

if [ ! -f "$RESOLVED_SCRIPT" ]; then
    fail_open
fi

if command -v uv >/dev/null 2>&1; then
    exec uv run --no-project python "$RESOLVED_SCRIPT" "$@"
fi
if [ -x "$HOME/.local/bin/uv" ]; then
    exec "$HOME/.local/bin/uv" run --no-project python "$RESOLVED_SCRIPT" "$@"
fi
if [ -x "$HOME/.cargo/bin/uv" ]; then
    exec "$HOME/.cargo/bin/uv" run --no-project python "$RESOLVED_SCRIPT" "$@"
fi
if command -v python3 >/dev/null 2>&1; then
    exec python3 "$RESOLVED_SCRIPT" "$@"
fi
if command -v python >/dev/null 2>&1; then
    exec python "$RESOLVED_SCRIPT" "$@"
fi

fail_open

