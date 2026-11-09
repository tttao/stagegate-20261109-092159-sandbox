#!/usr/bin/env bash
# Verify the approved plan binding, then apply each unit in dependency order.
# Env: SG_ENV SG_UNITS_ROOT SG_BINDING_MODE SG_PLAN_ARTIFACT SG_MANIFEST_PATH SG_ENCRYPT
#      SG_KEY_VAULT_KEY_ID [SG_ENGINE] [SG_SHA|GITHUB_SHA] [SG_LOCK_TIMEOUT]
#      [SG_BREAKGLASS_INCIDENT] [SG_ONLY_UNITS]
# Writes $RUNNER_TEMP/stagegate/<env>/units.json ({unit: {status, converged}}) once the first
# apply has started, even when a unit fails or does not converge, so the ledger step can record
# the outcome. Engine output goes to $RUNNER_TEMP/stagegate/<env>/logs, never to the job log.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/stagegate-lib.sh
. "$here/../lib/stagegate-lib.sh"

env="${SG_ENV:?SG_ENV is required}"
root="${SG_UNITS_ROOT:?SG_UNITS_ROOT is required}"
mode="${SG_BINDING_MODE:?SG_BINDING_MODE is required}"
sha="${SG_SHA:-${GITHUB_SHA:-}}"
[ -n "$sha" ] || sg_die "no commit SHA (SG_SHA or GITHUB_SHA)"
sg_check_env "$env"
sg_init_engine
sg_init_lock_timeout
sg_check_mode "$env" "$mode" "${SG_ENCRYPT:-true}" "${SG_ENGINE:-opentofu}"
# R71: break-glass always applies the whole prod tree; only same-job (dev/prp) may narrow the scope.
if [ -n "${SG_ONLY_UNITS:-}" ] && [ "$mode" != same-job ]; then
  sg_die "only-units is only allowed with binding-mode same-job, not $mode"
fi
sg_setup_encryption "$here/../plan/encryption.tofu.tmpl"

base="$(cd "$root/$env" 2>/dev/null && pwd)" || sg_die "units directory not found: $root/$env"
work="$(sg_workdir "$env")"
SG_LOGS="$work/logs"
mkdir -p "$work" "$SG_LOGS"
units_json="$work/units.json"
rm -f "$units_json"

# The plans to apply: the downloaded artifact (it may hold plans/ or the files directly), the
# fresh re-plan (replan), or this job's own plans (same-job, breakglass).
plans=""
case "$mode" in
  encrypted_plan)
    art="${SG_PLAN_ARTIFACT:?plan-artifact is required for binding-mode encrypted_plan}"
    plans="$art"
    [ ! -d "$art/plans" ] || plans="$art/plans"
    plans="$(cd "$plans" 2>/dev/null && pwd)" || sg_die "plan artifact directory not found: $art"
    ;;
  replan) plans="$work/replan/plans" ;;
  same-job | breakglass)
    plans="$(cd "$work/plans" 2>/dev/null && pwd)" || sg_die "no plans from this job in $work/plans"
    ;;
esac

mapfile -t units < <(sg_units "$base")
# SG_ONLY_UNITS (same-job only): apply only the units that were planned with the same list.
sg_filter_units

# check_binding_exit RC: stop the run on any verification failure; never apply.
check_binding_exit() {
  case "$1" in
    0) ;;
    3) sg_die "approved plan no longer matches: stop, re-plan and re-approve (nothing was applied)" ;;
    *) sg_die "plan verification failed with exit code $1 (nothing was applied)" ;;
  esac
}

case "$mode" in
  encrypted_plan)
    manifest="${SG_MANIFEST_PATH:?manifest-path is required for binding-mode encrypted_plan}"
    # Init every unit so the provider lock files exist, then collect them.
    rm -rf "$work/locks"
    mkdir -p "$work/locks"
    for rel in "${units[@]}"; do
      name="$(sg_unit_name "$rel")"
      sg_step "$name" init "$base/$rel" init -input=false || sg_die "init of $name failed"
      sg_copy_lock "$base/$rel" "$work/locks/$name.lock.hcl"
    done
    rc=0
    stagegate binding verify --manifest "$manifest" --sha "$sha" \
      --engine-version "$SG_ENGINE_VERSION" --plans "$plans" --locks "$work/locks" || rc=$?
    check_binding_exit "$rc"
    ;;
  replan)
    manifest="${SG_MANIFEST_PATH:?manifest-path is required for binding-mode replan}"
    rm -rf "$work/replan"
    mkdir -p "$work/replan/plans" "$work/replan/json"
    for rel in "${units[@]}"; do
      name="$(sg_unit_name "$rel")"
      sg_step "$name" replan "$base/$rel" plan -input=false "$SG_LOCK_TIMEOUT_FLAG" \
        -out="$work/replan/plans/$name.tfplan" || sg_die "re-plan of $name failed"
      sg_step_json "$name" replan-show "$base/$rel" "$work/replan/json/$name.json" \
        "$work/replan/plans/$name.tfplan" || sg_die "reading the re-plan of $name failed"
    done
    rc=0
    stagegate binding verify-replan --manifest "$manifest" --sha "$sha" \
      --engine-version "$SG_ENGINE_VERSION" --plan-json "$work/replan/json" || rc=$?
    check_binding_exit "$rc"
    ;;
esac

# Apply in dependency order (same-job and breakglass: plan, apply, converge per unit). Each applied unit is re-planned straight away (converge check:
# exit 0 means the unit matches the code). A failed or non-converging unit stops the run and
# every later unit is skipped.
declare -A status converged
for rel in "${units[@]}"; do
  name="$(sg_unit_name "$rel")"
  status[$name]=skipped
  converged[$name]=false
done
stop=false
for rel in "${units[@]}"; do
  [ "$stop" = false ] || continue
  name="$(sg_unit_name "$rel")"
  dir="$base/$rel"
  planfile="$plans/$name.tfplan"
  # R52: in same-job and breakglass the plan.sh plans are only a preview. A dependent unit was
  # planned against the upstream's outputs from before its apply, so the plan applied here is made
  # right now, after every earlier unit was applied and converged.
  if [ "$mode" = same-job ] || [ "$mode" = breakglass ]; then
    rm -f "$planfile"
    if ! sg_step "$name" replan "$dir" plan -input=false "$SG_LOCK_TIMEOUT_FLAG" -out="$planfile"; then
      status[$name]=failed
      stop=true
      continue
    fi
    # R53: in break-glass the policy pack checked the preview, so the plan that is applied must
    # propose the same changes ((address, actions) set). Same-job dev/prp has no policy gate.
    if [ "$mode" = breakglass ]; then
      fresh="$work/fresh-$name.json"
      same=false
      if sg_step_json "$name" fresh-show "$dir" "$fresh" "$planfile" && [ -f "$work/json/$name.json" ]; then
        same="$(jq -n --slurpfile a "$work/json/$name.json" --slurpfile b "$fresh" '
          def changes: [.resource_changes[]? | select(.change.actions != ["no-op"] and
            .change.actions != ["read"]) | {address, actions: .change.actions}] | sort;
          ($a[0] | changes) == ($b[0] | changes)')" || same=false
      fi
      rm -f "$fresh"
      if [ "$same" != true ]; then
        echo "stagegate: $name: plan changed since the policy check; re-run break-glass" >&2
        status[$name]=failed
        stop=true
        continue
      fi
    fi
  fi
  if [ ! -f "$planfile" ]; then
    echo "stagegate: no plan file for unit $name" >&2
    status[$name]=failed
    stop=true
    continue
  fi
  changes=1
  tmpjson="$work/show-$name.json"
  if sg_step_json "$name" apply-show "$dir" "$tmpjson" "$planfile"; then
    changes="$(jq '[.resource_changes[]? | select(.change.actions != ["no-op"] and
      .change.actions != ["read"])] | length' "$tmpjson")" || changes=1
  fi
  rm -f "$tmpjson"
  if ! sg_step "$name" apply "$dir" apply -input=false "$SG_LOCK_TIMEOUT_FLAG" "$planfile"; then
    status[$name]=failed
    stop=true
    continue
  fi
  if [ "$changes" -eq 0 ]; then status[$name]=no_changes; else status[$name]=applied; fi
  rc=0
  sg_step "$name" converge "$dir" plan -input=false "$SG_LOCK_TIMEOUT_FLAG" -detailed-exitcode || rc=$?
  if [ "$rc" -eq 0 ]; then
    converged[$name]=true
  else
    echo "stagegate: unit $name did not converge (plan exit $rc); later units are skipped" >&2
    stop=true
  fi
done

flat=()
bad=false
for rel in "${units[@]}"; do
  name="$(sg_unit_name "$rel")"
  flat+=("$name" "${status[$name]}" "${converged[$name]}")
  case "${status[$name]}" in applied | no_changes) [ "${converged[$name]}" = true ] || bad=true ;; *) bad=true ;; esac
done
jq -n '$ARGS.positional as $p | [range(0; ($p | length); 3)]
  | map({($p[.]): {status: $p[. + 1], converged: ($p[. + 2] == "true")}}) | add' \
  --args "${flat[@]}" >"$units_json.tmp" || sg_die "cannot write units.json"
mv "$units_json.tmp" "$units_json"
sg_output units-json "$units_json"

[ "$bad" = false ] || sg_die "a unit failed, did not converge or was skipped; see units.json"
