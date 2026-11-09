#!/usr/bin/env bash
# Plan every unit of an environment in dependency order, bind the plans, set outputs.
# Env: SG_ENV SG_UNITS_ROOT SG_BINDING_MODE SG_ENCRYPT SG_KEY_VAULT_KEY_ID [SG_ENGINE]
#      [SG_SHA|GITHUB_SHA] [SG_BREAKGLASS_INCIDENT] [SG_ONLY_UNITS] [SG_LOCK] [SG_DRIFT]
#      [SG_LOCK_TIMEOUT] [SG_EXPECT_SHA] [SG_ALLOW_CASCADE]
# Writes under $RUNNER_TEMP/stagegate/<env>/: plans/<unit>.tfplan, json/<unit>.json,
# locks/<unit>.lock.hcl, changeset.json, summary.md (the rendered change set, no values), manifest.json and logs/ (engine output). The workflow decides what
# leaves the job. With encryption off and a binding-mode other than same-job or breakglass, the plan files are
# deleted after the manifest is made: a replan run never needs them.
# SG_DRIFT=true (binding-mode drift): plan -detailed-exitcode per unit, exit 2 (changes) is not a
# failure, exit-codes.json maps unit to plan exit code; plan files are deleted, never applied.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/stagegate-lib.sh
. "$here/../lib/stagegate-lib.sh"

env="${SG_ENV:?SG_ENV is required}"
root="${SG_UNITS_ROOT:?SG_UNITS_ROOT is required}"
mode="${SG_BINDING_MODE:-encrypted_plan}"
sha="${SG_SHA:-${GITHUB_SHA:-}}"
[ -n "$sha" ] || sg_die "no commit SHA (SG_SHA or GITHUB_SHA)"
sg_check_env "$env"
sg_init_engine
sg_init_lock_timeout
sg_check_mode "$env" "$mode" "${SG_ENCRYPT:-true}" "${SG_ENGINE:-opentofu}" "${SG_DRIFT:-false}"
# R71: break-glass always plans the whole prod tree (unchanged units are no-ops).
if [ -n "${SG_ONLY_UNITS:-}" ] && [ "$mode" = breakglass ]; then
  sg_die "only-units is not allowed with binding-mode breakglass: break-glass plans every unit"
fi
sg_setup_encryption "$here/encryption.tofu.tmpl"

# SG_EXPECT_SHA=true: the working tree must be at SG_SHA (the nightly drift check plans the live SHA).
case "${SG_EXPECT_SHA:-false}" in
  true)
    [ -n "${SG_SHA:-}" ] || sg_die "expect-sha needs SG_SHA"
    head="$(git rev-parse HEAD)" || sg_die "expect-sha: cannot read HEAD"
    [ "$head" = "$SG_SHA" ] || sg_die "expect-sha: the checked-out commit is not $SG_SHA"
    ;;
  false) ;;
  *) sg_die "expect-sha must be true or false" ;;
esac

base="$(cd "$root/$env" 2>/dev/null && pwd)" || sg_die "units directory not found: $root/$env"
work="$(sg_workdir "$env")"
SG_LOGS="$work/logs"
rm -rf "$work"
mkdir -p "$work/plans" "$work/json" "$work/locks" "$SG_LOGS"

# SG_LOCK=false plans with -lock=false: a read-only identity cannot take the state lock.
lockflag=()
case "${SG_LOCK:-true}" in
  true) lockflag=("$SG_LOCK_TIMEOUT_FLAG") ;;
  false) lockflag=(-lock=false) ;;
  *) sg_die "lock must be true or false" ;;
esac

driftflag=()
[ "${SG_DRIFT:-false}" != true ] || driftflag=(-detailed-exitcode)

mapfile -t units < <(sg_units "$base")
# SG_ONLY_UNITS: comma list of unit names to plan (empty = all); the dependency order is kept.
sg_filter_units
exit_codes=()
for rel in "${units[@]}"; do
  name="$(sg_unit_name "$rel")"
  dir="$base/$rel"
  prc=0
  sg_step "$name" plan "$dir" plan -input=false "${lockflag[@]}" "${driftflag[@]}" \
    -out="$work/plans/$name.tfplan" || prc=$?
  if [ "${SG_DRIFT:-false}" = true ] && [ "$prc" -eq 2 ]; then
    : # drift found: not a failure
  elif [ "$prc" -ne 0 ]; then
    sg_die "planning $name failed"
  fi
  exit_codes+=("$(jq -n --arg u "$name" --argjson c "$prc" '{($u): $c}')")
  sg_step_json "$name" show "$dir" "$work/json/$name.json" "$work/plans/$name.tfplan" ||
    sg_die "reading the plan of $name failed"
  sg_copy_lock "$dir" "$work/locks/$name.lock.hcl"
  if sg_lock_is_sentinel "$work/locks/$name.lock.hcl" &&
    [ "$(jq '[(.configuration.provider_config // {})[]
      | select((.full_name // "") | contains("/builtin/") | not)] | length' \
      "$work/json/$name.json")" -gt 0 ]; then
    sg_die "unit $name uses providers but has no .terraform.lock.hcl"
  fi
done

if [ "${SG_DRIFT:-false}" = true ]; then
  printf '%s\n' "${exit_codes[@]}" | jq -s 'add' >"$work/exit-codes.json"
  sg_output exit-codes-path "$work/exit-codes.json"
fi

# R52: a saved plan (encrypted_plan, replan) of a unit that reads another unit's outputs is only valid
# if that upstream does not change in the same run: refuse, unless allow-cascade says otherwise.
if [ "$mode" = encrypted_plan ] || [ "$mode" = replan ]; then
  case "${SG_ALLOW_CASCADE:-false}" in
    true) echo "stagegate: allow-cascade is true: cascading changes are not refused (unsafe)" >&2 ;;
    false)
      sg_edges "$base" "$work/edges.json"
      stagegate cascade check --edges "$work/edges.json" --plan-json "$work/json" ||
        sg_die "refusing the plan (cascading change); set allow_cascade only if you accept the risk"
      ;;
    *) sg_die "allow-cascade must be true or false" ;;
  esac
fi

stagegate binding make --sha "$sha" --env "$env" \
  --engine "${SG_ENGINE:-opentofu}" --engine-version "$SG_ENGINE_VERSION" \
  --plans "$work/plans" --locks "$work/locks" --plan-json "$work/json" \
  --out "$work/manifest.json"

stagegate changeset --plan-json "$work/json" --sha "$sha" --env "$env" \
  --out "$work/changeset.json" --summary "$work/summary.md"
[ -z "${GITHUB_STEP_SUMMARY:-}" ] || cat "$work/summary.md" >>"$GITHUB_STEP_SUMMARY"
if [ "$(jq '.items | length' "$work/changeset.json")" -gt 0 ]; then has=true; else has=false; fi

sg_output has-changes "$has"
sg_output manifest-path "$work/manifest.json"
sg_output changeset-path "$work/changeset.json"
sg_output json-dir "$work/json"
sg_output summary-path "$work/summary.md"
if [ "${SG_ENCRYPT:-true}" = false ] && [ "$mode" != same-job ] && [ "$mode" != breakglass ]; then
  rm -rf "$work/plans"
else
  sg_output plan-dir "$work/plans"
fi
