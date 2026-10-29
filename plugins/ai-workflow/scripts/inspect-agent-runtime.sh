#!/bin/sh
# Emit only a runtime identity allowlist from one uniquely matched rollout.
# Never echo arguments, file names, or jq input: rollout files may contain
# prompts, environment values, configuration, and token data.
set -eu

fail() {
    printf '%s\n' 'runtime inspection failed' >&2
    exit 2
}

[ "$#" -eq 3 ] || [ "$#" -eq 5 ] || fail
[ "$1" = '--sessions-dir' ] || fail
sessions_dir=$2
thread_id=$3
native_agent_id=null
if [ "$#" -eq 5 ]; then
    [ "$4" = '--native-agent-id' ] || fail
    printf '%s' "$5" | grep -Eq '^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$' || fail
    native_agent_id=$5
fi

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
matches=$(
    find "$sessions_dir" \
        \( -name "*$thread_id" -o -name "*$thread_id.jsonl" \) \
        -print 2>/dev/null
) || fail
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

# Parse the complete JSONL stream as one array.  Real Codex rollouts spread
# identity facts across records, unlike the legacy one-object test fixture.
# Diagnostics are discarded so malformed input cannot reflect secrets.
jq -cse --arg requested_thread "$thread_id" --argjson issued_native_agent_id "$(printf '%s' "$native_agent_id" | jq -R 'if . == "null" then null else . end')" '
    . as $records
    | def one_string($values):
        $values
        | if length == 0
             or any(.[]; type != "string" or length == 0)
          then error("invalid runtime rollout")
          else unique
               | if length == 1 then .[0] else error("invalid runtime rollout") end
          end;
      # Model messages/tool schemas are not runtime authority. Current rollouts
      # carry identity in these typed records; untyped legacy fixtures retain
      # their original object contract.
      def identity_objects:
        $records[]
        | if (.type == "session_meta" or .type == "turn_context")
             and (.payload | type == "object")
          then .payload | .. | objects
          elif (has("type") | not)
          then .. | objects
          else empty
          end;
      def field($name):
        one_string([
          identity_objects
          | select(has($name))
          | .[$name]
        ]);
      def rollout_thread:
        one_string(
          [
            identity_objects
            | select(has("thread_id"))
            | .thread_id
          ]
          +
          [
            $records[]
            | select(.type == "session_meta" and (.payload | type == "object"))
            | .payload
            | .id?, .session_id?
            | select(. != null)
          ]
        );
      def nullable_agent_type:
        [
          identity_objects
          | select(has("agent_type"))
          | .agent_type
        ] as $values
        | if ($values | length) == 0
          then null
          elif any(
            $values[];
            (type != "string" and type != "null")
            or (type == "string" and length == 0)
          )
          then error("invalid runtime rollout")
          else ($values | unique)
               | if length == 1 then .[0] else error("invalid runtime rollout") end
          end;
      def runtime_cwd:
        one_string([
          $records[]
          | if (.type == "session_meta" or .type == "turn_context")
               and (.payload | type == "object")
               and (.payload | has("cwd"))
            then .payload.cwd
            elif (has("type") | not)
            then [.. | objects | select(has("cwd")) | .cwd][]
            else empty
            end
        ]);
      # Recent Codex versions store the canonical effort as payload.effort.
      # Only a trusted turn_context record may supply this alias. Include all
      # canonical observations too so dual fields or turns cannot disagree.
      def runtime_effort:
        one_string(
          [
            identity_objects
            | select(has("reasoning_effort"))
            | .reasoning_effort
          ]
          +
          [
            $records[]
            | select(.type == "turn_context" and (.payload | type == "object"))
            | .payload
            | select(has("effort"))
            | .effort
          ]
        );
      def sandbox_policy:
        [
          identity_objects
          | select(has("sandbox_policy"))
          | .sandbox_policy
          | if type == "string" and length > 0
            then .
            elif type == "object"
                 and (keys == ["type"])
                 and .type == "read-only"
            then "read-only"
            elif type == "object"
                 and (keys == ["exclude_slash_tmp", "exclude_tmpdir_env_var", "network_access", "type"])
                 and .type == "workspace-write"
                 and .network_access == false
                 and .exclude_tmpdir_env_var == false
                 and .exclude_slash_tmp == false
            then "workspace-write"
            else error("invalid runtime rollout")
            end
        ]
        | one_string(.);
      def managed_workspace_profile($cwd):
        (keys == ["file_system", "network", "type"])
        and .type == "managed"
        and .network == "restricted"
        and (.file_system | type == "object")
        and (.file_system | keys == ["entries", "type"])
        and .file_system.type == "restricted"
        and (.file_system.entries | type == "array")
        and ((.file_system.entries | sort_by(.path | tojson)) == ([
          {path: {type: "special", value: {kind: "root"}}, access: "read"},
          {path: {type: "path", path: $cwd}, access: "write"},
          {path: {type: "special", value: {kind: "slash_tmp"}}, access: "write"},
          {path: {type: "special", value: {kind: "tmpdir"}}, access: "write"},
          ([".git", ".agents", ".codex", ".aws"][] |
            {path: {type: "path", path: ($cwd + "/" + .)}, access: "read", missing_path_behavior: "skip"})
        ] | sort_by(.path | tojson)));
      def permission_profile:
        runtime_cwd as $cwd |
        [
          identity_objects
          | select(has("permission_profile"))
          | .permission_profile
          | if type == "string" and length > 0
            then .
            elif type == "object"
                 and (keys == ["file_system", "network", "type"])
                 and .type == "managed"
                 and .network == "restricted"
                 and (.file_system | type == "object")
                 and (.file_system | keys == ["entries", "type"])
                 and .file_system.type == "restricted"
                 and (.file_system.entries | type == "array" and length > 0)
                 and all(
                   .file_system.entries[];
                   type == "object"
                   and (keys == ["access", "path"])
                   and .access == "read"
                   and (.path | type == "object")
                   and (.path | keys == ["type", "value"])
                   and .path.type == "special"
                   and (.path.value | type == "object")
                   and (.path.value | keys == ["kind"])
                   and .path.value.kind == "root"
                 )
            then "read-only"
            elif type == "object" and managed_workspace_profile($cwd)
            then "workspace-write"
            else error("invalid runtime rollout")
            end
        ]
        | one_string(.);
    rollout_thread as $thread_id
    | if $thread_id != $requested_thread then error("invalid runtime rollout") else . end
    | (if $issued_native_agent_id == null
       then null
       else field("native_agent_id") as $rollout_agent_id
            | if $rollout_agent_id == $issued_native_agent_id
              then $rollout_agent_id
              else error("invalid runtime rollout")
              end
       end) as $native_agent_id
    | {
        thread_id: $thread_id,
        native_agent_id: $native_agent_id,
        agent_type: nullable_agent_type,
        model: field("model"),
        reasoning_effort: runtime_effort,
        sandbox_policy: sandbox_policy,
        permission_profile: permission_profile,
        cwd: runtime_cwd
      }
' "$matches" >"$temporary" 2>/dev/null || fail

# Check the emitted object itself before it reaches stdout.  This is a second
# allowlist boundary should the filter above ever be changed.
jq -e '
    type == "object"
    and (keys | sort == ["agent_type", "cwd", "model", "native_agent_id", "permission_profile", "reasoning_effort", "sandbox_policy", "thread_id"])
    and (.agent_type == null or (.agent_type | type == "string" and length > 0))
    and (.native_agent_id == null or (.native_agent_id | type == "string" and length > 0))
    and all(del(.agent_type, .native_agent_id)[]; type == "string" and length > 0)
' "$temporary" >/dev/null 2>&1 || fail

cat "$temporary"
