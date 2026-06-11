#!/usr/bin/env bash
# Unprivileged update CLI for the ruby-updated root handler.
#
# Usage: ruby-update.sh check            queue a tag check, watch status
#        ruby-update.sh apply [ref]      queue an update (default: newest tag)
#        ruby-update.sh rollback [ref]   queue a rollback (default: previous)
#        ruby-update.sh status           print the current status snapshot
#        ruby-update.sh log              tail the phase log (last 40 lines)
#
# Requests are JSON files written atomically (mktemp + mv) into the queue
# dir; a systemd .path unit wakes /usr/local/sbin/ruby-updated to consume
# them, so no root is needed here. check/apply/rollback then tail
# status.jsonl until a terminal phase (done/error/rolled-back/busy).
set -u

DIR="${RUBYHUD_UPDATE_DIR:-/run/ruby-update}"
QUEUE="$DIR/queue"
STATUS_JSON="$DIR/status.json"
STATUS_LOG="$DIR/status.jsonl"
WATCH_TIMEOUT_S=1200   # 20 min: deps install on a slow SD card takes a while

usage() {
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
}

enqueue() {
    # enqueue <cmd> [ref]: atomic queue write (mktemp in-dir, then mv).
    local cmd="$1" ref="${2:-}" tmp req
    if [ ! -d "$QUEUE" ] || [ ! -w "$QUEUE" ]; then
        echo "ERROR: queue dir $QUEUE missing or not writable" >&2
        echo "       (is ruby-updated installed? see deploy/install.sh)" >&2
        return 1
    fi
    tmp="$(mktemp "$QUEUE/.req.XXXXXX")" || return 1
    if [ -n "$ref" ]; then
        printf '{"cmd":"%s","ref":"%s"}\n' "$cmd" "$ref" >"$tmp"
    else
        printf '{"cmd":"%s"}\n' "$cmd" >"$tmp"
    fi
    req="$QUEUE/$(date +%s)-$$.req"
    mv "$tmp" "$req" || return 1
    echo "queued: $cmd${ref:+ $ref}"
}

phase_of() {
    printf '%s' "$1" \
        | sed -n 's/.*"phase"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
}

watch() {
    # Print status.jsonl lines appended after this call until a terminal
    # phase; exit 0 only on done.
    local seen=0 deadline n new line phase
    if [ -f "$STATUS_LOG" ]; then
        seen="$(wc -l <"$STATUS_LOG" | tr -d '[:space:]')"
    fi
    deadline=$(( $(date +%s) + WATCH_TIMEOUT_S ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if [ -f "$STATUS_LOG" ]; then
            n="$(wc -l <"$STATUS_LOG" | tr -d '[:space:]')"
            if [ "$n" -gt "$seen" ]; then
                new="$(tail -n "+$((seen + 1))" "$STATUS_LOG")"
                seen="$n"
                while IFS= read -r line; do
                    [ -n "$line" ] || continue
                    echo "$line"
                    phase="$(phase_of "$line")"
                    case "$phase" in
                        done)
                            echo "RESULT: done"
                            return 0 ;;
                        error|busy|rolled-back*)
                            echo "RESULT: $phase"
                            return 1 ;;
                    esac
                done <<<"$new"
            fi
        fi
        sleep 1
    done
    echo "ERROR: no terminal phase after ${WATCH_TIMEOUT_S}s" >&2
    return 1
}

show_status() {
    if [ -f "$STATUS_JSON" ]; then
        cat "$STATUS_JSON"
        echo
    else
        echo "no status yet ($STATUS_JSON missing)"
    fi
}

show_log() {
    if [ -f "$STATUS_LOG" ]; then
        tail -40 "$STATUS_LOG"
    else
        echo "no log yet ($STATUS_LOG missing)"
    fi
}

cmd="${1:-}"
case "$cmd" in
    check|apply|rollback)
        ref="${2:-}"
        if [ "$cmd" = "check" ] && [ -n "$ref" ]; then
            echo "ERROR: check takes no ref" >&2
            exit 2
        fi
        if [ -n "$ref" ] && ! printf '%s' "$ref" \
                | grep -Eq '^v[0-9][0-9.]*$'; then
            echo "ERROR: ref must look like v3.1.0 (got: $ref)" >&2
            exit 2
        fi
        enqueue "$cmd" "$ref" || exit 1
        watch
        ;;
    status)
        show_status
        ;;
    log)
        show_log
        ;;
    ""|-h|--help|help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
