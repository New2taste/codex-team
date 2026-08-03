#!/bin/sh
# Emit only a runtime identity allowlist from one uniquely matched rollout.
# Never echo arguments, file names, or jq input: rollout files may contain
# prompts, environment values, configuration, and token data.
set -eu

fail() {
    printf '%s\n' 'runtime inspection failed' >&2
    exit 2
}

[ "$#" -eq 3 ] || fail
[ "$1" = '--sessions-dir' ] || fail
sessions_dir=$2
thread_id=$3

case "$sessions_dir" in
    /*) ;;
    *) fail ;;
esac

[ -d "$sessions_dir" ] || fail
printf '%s' "$thread_id" | grep -Eq '^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$' || fail
command -v jq >/dev/null 2>&1 || fail

# A newline in a candidate name makes this conservatively reject rather than
# risk treating an attacker-controlled path as a single match.
# Enumerate every matching directory entry before deciding whether it is a
# regular file.  ``find -type f`` alone would silently ignore a same-suffix
# symlink and could therefore turn an ambiguous rollout set into one match.
matches=$(find "$sessions_dir" -name "*$thread_id" -print 2>/dev/null) || fail
[ -n "$matches" ] || fail
match_count=$(printf '%s\n' "$matches" | awk 'END { print NR }')
[ "$match_count" -eq 1 ] || fail
[ ! -L "$matches" ] || fail
[ -f "$matches" ] || fail

umask 077
temporary=$(mktemp "${TMPDIR:-/tmp}/ai-workflow-runtime.XXXXXX") || fail
cleanup() {
    rm -f "$temporary"
}
trap cleanup EXIT HUP INT TERM

# ``field`` collects every duplicate occurrence recursively.  Each requested
# field must occur with exactly one nonempty string value; duplicate values are
# allowed only when they are identical.  jq diagnostics are discarded so a
# malformed rollout cannot reflect sensitive input to stderr.
jq -ce --arg requested_thread "$thread_id" '
    def field($name):
      [.. | objects | select(has($name)) | .[$name]] as $values
      | if ($values | length) == 0
           or any($values[]; type != "string" or length == 0)
        then error("invalid runtime rollout")
        else ($values | unique)
             | if length == 1 then .[0] else error("invalid runtime rollout") end
        end;
    def nullable_agent_type:
      [.. | objects | select(has("agent_type")) | .agent_type] as $values
      | if ($values | length) == 0
           or any($values[]; (type != "string" and type != "null") or (type == "string" and length == 0))
        then error("invalid runtime rollout")
        else ($values | unique)
             | if length == 1 then .[0] else error("invalid runtime rollout") end
        end;
    field("thread_id") as $thread_id
    | if $thread_id != $requested_thread then error("invalid runtime rollout") else . end
    | {
        thread_id: $thread_id,
        agent_type: nullable_agent_type,
        model: field("model"),
        reasoning_effort: field("reasoning_effort"),
        sandbox_policy: field("sandbox_policy"),
        permission_profile: field("permission_profile"),
        cwd: field("cwd")
      }
' "$matches" >"$temporary" 2>/dev/null || fail

# Check the emitted object itself before it reaches stdout.  This is a second
# allowlist boundary should the filter above ever be changed.
jq -e '
    type == "object"
    and (keys | sort == ["agent_type", "cwd", "model", "permission_profile", "reasoning_effort", "sandbox_policy", "thread_id"])
    and (.agent_type == null or (.agent_type | type == "string" and length > 0))
    and all(del(.agent_type)[]; type == "string" and length > 0)
' "$temporary" >/dev/null 2>&1 || fail

cat "$temporary"
