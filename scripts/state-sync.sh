#!/usr/bin/env bash
# SANDBOX ONLY: persist Terraform local-backend state between workflow jobs on the
# `stagegate-state` branch of the sandbox repository. Never use this for real state.
#
#   state-sync.sh pull <state-dir>                   restore every state file of the branch
#   state-sync.sh push <state-dir> <rel/path.tfstate> commit one state file to the branch
#
# The token (contents write on the sandbox repository) arrives as STAGEGATE_EXTRA_SECRET, which the
# stagegate workflows export to the plan and apply steps from their `extra-env-secret` secret.
# On GitHub Actions a missing token is an error (state would silently not persist), except for
# pull_request events: PR plans never hold a write token (R39), so they run against empty state and
# neither read nor write the branch. Outside Actions (a local trial) a missing token or origin just
# skips the sync.
set -euo pipefail

branch="${STAGEGATE_E2E_STATE_BRANCH:-stagegate-state}"
cmd="${1:?usage: state-sync.sh pull|push <state-dir> [file]}"
dir="${2:?state dir}"
token="${STAGEGATE_EXTRA_SECRET:-}"

case "${GITHUB_EVENT_NAME:-}" in
  pull_request | pull_request_target)
    echo "state-sync: pull request run, no state sync (plans use empty state)" >&2
    exit 0
    ;;
esac
if [ "${GITHUB_ACTIONS:-}" = true ]; then
  [ -n "$token" ] || { echo "state-sync: STAGEGATE_EXTRA_SECRET is not set; refusing to run without persisted state" >&2; exit 1; }
else
  git rev-parse --git-dir >/dev/null 2>&1 || { echo "state-sync: not a git checkout; skipped" >&2; exit 0; }
  git remote get-url origin >/dev/null 2>&1 || { echo "state-sync: no origin remote; skipped" >&2; exit 0; }
  [ -n "$token" ] || { echo "state-sync: no token; skipped" >&2; exit 0; }
fi

# The credential goes through git's environment configuration, never through the command line.
auth="$(printf 'x-access-token:%s' "$token" | base64 -w0)"
g() {
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraheader GIT_CONFIG_VALUE_0="Authorization: basic $auth" \
    git "$@"
}
top="$(git rev-parse --show-toplevel)"
rel="$(realpath --relative-to="$top" "$dir")"
# Terragrunt runs hooks inside .terragrunt-cache; git resolves pathspecs against the current directory.
cd "$top"

remote_has_branch() { [ -n "$(g ls-remote --heads origin "refs/heads/$branch")" ]; }

case "$cmd" in
  pull)
    remote_has_branch || exit 0
    g fetch --quiet --depth=1 origin "+refs/heads/$branch:refs/stagegate-e2e/state"
    mkdir -p "$dir"
    # nothing saved yet for this unit: start empty; a saved state that cannot be restored fails
    git cat-file -e "refs/stagegate-e2e/state:$rel" 2>/dev/null || exit 0
    git archive refs/stagegate-e2e/state -- "$rel" | tar -x -C "$top" || {
      echo "state-sync: restoring $rel failed" >&2
      exit 1
    }
    ;;
  push)
    file="${3:?file}"
    [ -f "$dir/$file" ] || exit 0
    for _ in 1 2 3 4 5; do
      parent=""
      if remote_has_branch; then
        g fetch --quiet --depth=1 origin "+refs/heads/$branch:refs/stagegate-e2e/state"
        parent="refs/stagegate-e2e/state"
      fi
      idx="$(mktemp -u)"
      [ -z "$parent" ] || GIT_INDEX_FILE="$idx" git read-tree "$parent"
      oid="$(git hash-object -w "$dir/$file")"
      GIT_INDEX_FILE="$idx" git update-index --add --cacheinfo "100644,$oid,$rel/$file"
      tree="$(GIT_INDEX_FILE="$idx" git write-tree)"
      rm -f "$idx"
      commit="$(GIT_AUTHOR_NAME=stagegate-e2e GIT_AUTHOR_EMAIL=e2e@users.noreply.github.com \
        GIT_COMMITTER_NAME=stagegate-e2e GIT_COMMITTER_EMAIL=e2e@users.noreply.github.com \
        git commit-tree "$tree" ${parent:+-p "$parent"} -m "state: $file")"
      if g push --quiet origin "$commit:refs/heads/$branch"; then exit 0; fi
      sleep 2
    done
    echo "state-sync: push failed after 5 attempts" >&2
    exit 1
    ;;
  *) echo "state-sync: unknown command $cmd" >&2; exit 2 ;;
esac
