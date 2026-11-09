#!/usr/bin/env bash
# Shared helpers for the stagegate composite-action scripts. Source this file; do not run it.
# Everything here reads its configuration from environment variables set by action.yml.
#
# Engine output (init, plan, show, apply, converge) can contain resource values, so it never
# reaches the job log: it goes to $work/logs/<unit>.<step>.log inside $RUNNER_TEMP, which no
# action uploads. The job log only gets the unit, the step and its exit code.

NO_LOCK_SENTINEL='# stagegate: no providers'

sg_die() {
  echo "stagegate: $*" >&2
  exit 1
}

# sg_check_mirror MIRROR: empty, or an https URL without a trailing slash (tool-mirror, R66).
sg_check_mirror() {
  [ -z "$1" ] && return 0
  [[ "$1" =~ ^https://[^/[:space:]?#]+(/[^[:space:]?#]*[^/[:space:]?#])?$ ]] ||
    sg_die "tool-mirror must be an https URL without a trailing slash, got: $(printf '%q' "$1")"
}

# sg_mirror_url MIRROR URL: print URL with its https://github.com prefix replaced by MIRROR (no
# mirror, no change). Checksums are verified by the caller either way.
sg_mirror_url() {
  if [ -n "$1" ]; then
    echo "$1${2#https://github.com}"
  else
    echo "$2"
  fi
}

# sg_output NAME VALUE: set a step output (stdout when not running in Actions).
sg_output() {
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "$1=$2" >>"$GITHUB_OUTPUT"
  else
    echo "output $1=$2"
  fi
}

# sg_init_engine: validate SG_ENGINE once and set SG_TF_BIN and SG_ENGINE_VERSION. Call it in
# the main shell (not inside $(...)) so a failure really stops the script.
sg_init_engine() {
  case "${SG_ENGINE:-opentofu}" in
    opentofu) SG_TF_BIN=tofu ;;
    terraform) SG_TF_BIN=terraform ;;
    *) sg_die "engine must be opentofu or terraform, got: ${SG_ENGINE}" ;;
  esac
  SG_ENGINE_VERSION="$("$SG_TF_BIN" version -json | jq -er '.terraform_version')" ||
    sg_die "cannot read the $SG_TF_BIN version"
  [ -n "$SG_ENGINE_VERSION" ] || sg_die "empty $SG_TF_BIN version"
}

# sg_init_lock_timeout: validate SG_LOCK_TIMEOUT (default 10m) and set SG_LOCK_TIMEOUT_FLAG for every
# engine command that takes the state lock, so a run waits for a drift plan (R42) instead of failing
# on the blob lease. Plans run with -lock=false must not use it.
sg_init_lock_timeout() {
  local t="${SG_LOCK_TIMEOUT:-10m}"
  [[ "$t" =~ ^[0-9]+[sm]$ ]] || sg_die "lock-timeout must look like 30s or 10m, got: $t"
  SG_LOCK_TIMEOUT_FLAG="-lock-timeout=$t"
}

# sg_workdir ENV: $RUNNER_TEMP/stagegate/<env>.
sg_workdir() {
  [ -n "${RUNNER_TEMP:-}" ] || sg_die "RUNNER_TEMP is not set"
  echo "$RUNNER_TEMP/stagegate/$1"
}

# sg_check_env ENV: environment names become paths, so keep them tame.
sg_check_env() {
  [[ "$1" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || sg_die "invalid env name: $1"
}

# sg_check_incident VALUE: an incident reference is ABC-123 or INC123, nothing else (no spaces or
# newlines). The C locale keeps [A-Z] to ASCII capitals.
sg_check_incident() {
  local LC_ALL=C
  [[ "$1" =~ ^[A-Z]+-[0-9]+$ ]] || [[ "$1" =~ ^INC[0-9]+$ ]] ||
    sg_die "binding-mode breakglass needs SG_BREAKGLASS_INCIDENT like ABC-123 or INC123"
}

# sg_check_mode ENV MODE ENCRYPT ENGINE [DRIFT]: the one place that allows or refuses a combination.
#   same-job       only for dev and prp (plan and apply in one job, no verification).
#   breakglass     only for prod, and only with SG_BREAKGLASS_INCIDENT set to an incident reference
#                  (ABC-123 or INC123): plan and apply in one job like same-job, for the documented
#                  emergency path (iac-breakglass.yml, R44).
#   encrypted_plan needs encryption on (the approved plan files travel between jobs).
#   replan         may run unencrypted: the plan files are deleted after planning.
#   drift          nightly drift plans (DRIFT=true only, and then only this mode): never applied,
#                  plan files deleted once the JSON exists, so encrypt=false is required.
#   terraform cannot encrypt plans, so it needs encrypt=false (and so replan).
sg_check_mode() {
  local env="$1" mode="$2" encrypt="$3" engine="$4" drift="${5:-false}"
  case "$drift" in true | false) ;; *) sg_die "drift must be true or false, got: $drift" ;; esac
  if [ "$drift" = true ]; then
    [ "$mode" = drift ] || sg_die "drift: true needs binding-mode drift, got: $mode"
    [ "$encrypt" = false ] || sg_die "binding-mode drift requires encrypt=false (plans are deleted, never applied)"
  fi
  case "$encrypt" in true | false) ;; *) sg_die "encrypt must be true or false, got: $encrypt" ;; esac
  case "$mode" in
    same-job)
      [ "$env" = dev ] || [ "$env" = prp ] ||
        sg_die "binding-mode same-job is only allowed for dev and prp, not $env"
      ;;
    breakglass)
      [ "$env" = prod ] || sg_die "binding-mode breakglass is only allowed for prod, not $env"
      sg_check_incident "${SG_BREAKGLASS_INCIDENT:-}"
      ;;
    encrypted_plan)
      [ "$encrypt" = true ] || sg_die "binding-mode encrypted_plan requires encrypt=true"
      ;;
    replan) ;;
    drift)
      [ "$drift" = true ] || sg_die "binding-mode drift is only allowed with drift: true"
      ;;
    *) sg_die "binding-mode must be same-job, breakglass, encrypted_plan, replan or drift, got: $mode" ;;
  esac
  if [ "$engine" = terraform ] && [ "$encrypt" = true ]; then
    sg_die "encrypt=true needs engine opentofu; terraform cannot encrypt plans"
  fi
}

# sg_setup_encryption TEMPLATE: export TF_ENCRYPTION when SG_ENCRYPT=true, or fail closed.
# The key source is the Azure Key Vault key (SG_KEY_VAULT_KEY_ID) or, in tests only,
# STAGEGATE_TEST_PASSPHRASE; having both is ambiguous and an error. The key URL must not carry
# a key version (a pinned version would break key rotation) and must be a vault.azure.net
# host (managed HSM keys are not supported yet).
sg_setup_encryption() {
  local tmpl="$1"
  unset TF_ENCRYPTION
  [ "${SG_ENCRYPT:-true}" = true ] || return 0
  if [ -n "${STAGEGATE_TEST_PASSPHRASE:-}" ] && [ -n "${SG_KEY_VAULT_KEY_ID:-}" ]; then
    sg_die "both STAGEGATE_TEST_PASSPHRASE and key-vault-key-id are set; use exactly one"
  fi
  if [ -n "${STAGEGATE_TEST_PASSPHRASE:-}" ]; then
    [[ "$STAGEGATE_TEST_PASSPHRASE" =~ ^[A-Za-z0-9_.-]{16,}$ ]] ||
      sg_die "STAGEGATE_TEST_PASSPHRASE must be at least 16 chars of [A-Za-z0-9_.-]"
    TF_ENCRYPTION="key_provider \"pbkdf2\" \"stagegate\" {
  passphrase = \"${STAGEGATE_TEST_PASSPHRASE}\"
}

method \"aes_gcm\" \"stagegate\" {
  keys = key_provider.pbkdf2.stagegate
}

plan {
  method   = method.aes_gcm.stagegate
  enforced = true
}"
  elif [ -n "${SG_KEY_VAULT_KEY_ID:-}" ]; then
    local re='^(https://[A-Za-z0-9-]+\.vault\.azure\.net)/keys/([A-Za-z0-9-]+)/?$'
    [[ "$SG_KEY_VAULT_KEY_ID" =~ $re ]] ||
      sg_die "key-vault-key-id must look like https://<vault>.vault.azure.net/keys/<name> (no key version, no managed HSM)"
    local body
    body="$(cat "$tmpl")" || sg_die "cannot read encryption template: $tmpl"
    body="${body//@VAULT_URI@/${BASH_REMATCH[1]}}"
    TF_ENCRYPTION="${body//@KEY_NAME@/${BASH_REMATCH[2]}}"
  else
    sg_die "encrypt=true needs the key-vault-key-id input (or STAGEGATE_TEST_PASSPHRASE in tests)"
  fi
  export TF_ENCRYPTION
}

# sg_units BASE: print unit paths relative to BASE, dependencies first. Names must be unique
# once / becomes __.
sg_units() {
  local base="$1" out rel name
  local -A seen=()
  [ -d "$base" ] || sg_die "units directory not found: $base"
  out="$(terragrunt find --dag --no-color --working-dir "$base")" || sg_die "unit discovery failed"
  [ -n "$out" ] || sg_die "no units found under $base"
  while IFS= read -r rel; do
    name="$(sg_unit_name "$rel")"
    [ -z "${seen[$name]:-}" ] || sg_die "duplicate unit name: $name"
    seen[$name]=1
  done <<<"$out"
  printf '%s\n' "$out"
}

# sg_unit_name REL: the unit's name is its relative path with / replaced by __.
sg_unit_name() {
  echo "${1//\//__}"
}

# sg_filter_units: narrow the global array `units` to the names in SG_ONLY_UNITS (comma list, names
# as sg_unit_name makes them; empty keeps every unit). The dependency order is kept. An unknown
# name fails, so a typo can never silently shrink the scope.
sg_filter_units() {
  [ -n "${SG_ONLY_UNITS:-}" ] || return 0
  local -a wanted filtered=()
  local -A known=() keep=()
  local rel want
  IFS=, read -r -a wanted <<<"$SG_ONLY_UNITS"
  for rel in "${units[@]}"; do known[$(sg_unit_name "$rel")]=1; done
  for want in "${wanted[@]}"; do
    [ -n "$want" ] || continue
    [ -n "${known[$want]:-}" ] || sg_die "only-units: unknown unit: $want"
    keep[$want]=1
  done
  [ "${#keep[@]}" -gt 0 ] || sg_die "only-units names no unit"
  for rel in "${units[@]}"; do
    [ -z "${keep[$(sg_unit_name "$rel")]:-}" ] || filtered+=("$rel")
  done
  units=("${filtered[@]}")
}

# sg_step UNIT STEP DIR TF_ARGS...: run an engine command in a unit, engine output to the log.
# Prints only the outcome; returns the engine's exit code.
sg_step() {
  local unit="$1" step="$2" dir="$3" rc=0
  shift 3
  mkdir -p "$SG_LOGS"
  local log="$SG_LOGS/$unit.$step.log"
  TG_TF_PATH="$SG_TF_BIN" terragrunt run --non-interactive --no-color --working-dir "$dir" -- \
    "$@" >"$log" 2>&1 || rc=$?
  sg_step_report "$unit" "$step" "$rc" "$log"
  return "$rc"
}

# sg_step_json UNIT STEP DIR OUTFILE PLANFILE: `show -json`; the JSON goes to OUTFILE, engine
# stderr to the log.
sg_step_json() {
  local unit="$1" step="$2" dir="$3" out="$4" planfile="$5" rc=0
  mkdir -p "$SG_LOGS"
  local log="$SG_LOGS/$unit.$step.log"
  TG_TF_PATH="$SG_TF_BIN" terragrunt run --non-interactive --no-color --tf-forward-stdout \
    --working-dir "$dir" -- show -json "$planfile" >"$out" 2>"$log" || rc=$?
  sg_step_report "$unit" "$step" "$rc" "$log"
  return "$rc"
}

sg_step_report() {
  if [ "$3" -eq 0 ]; then
    echo "stagegate: $1 $2: ok"
  elif [ "$3" -eq 2 ] && [ "$2" = plan ] && [ "${SG_DRIFT:-false}" = true ]; then
    echo "stagegate: $1 $2: ok (changes present, exit 2)"
  else
    echo "stagegate: $1 $2: failed (exit $3); engine output is in $4 (job temp dir, never uploaded)" >&2
  fi
}

# sg_copy_lock DIR DEST: copy the unit's .terraform.lock.hcl. A unit without one is recorded
# with a sentinel so that "no providers" is distinguishable from a missing lock file.
sg_copy_lock() {
  if [ -f "$1/.terraform.lock.hcl" ]; then
    cp "$1/.terraform.lock.hcl" "$2"
  else
    echo "$NO_LOCK_SENTINEL" >"$2"
  fi
}

# sg_lock_is_sentinel FILE
sg_lock_is_sentinel() {
  [ "$(cat "$1")" = "$NO_LOCK_SENTINEL" ]
}

# sg_edges BASE OUTFILE: write {unit: [units whose outputs it reads]} as JSON. Only `dependency`
# blocks count (outputs flow through them); `dependencies` only orders units. Terragrunt renders an
# interpolated config_path (${get_terragrunt_dir()}/../base) as an absolute path, a plain one as
# written (relative to the unit). Fails closed: an upstream outside BASE or that is not a
# discovered unit is an error, never silently dropped.
sg_edges() {
  local base="$1" out="$2" rel name dir cfg path up
  local -a ups all
  local -A known=()
  local rbase
  rbase="$(realpath "$base")"
  mapfile -t all < <(sg_units "$base")
  for rel in "${all[@]}"; do known[$rel]=1; done
  : >"$out.lines"
  for rel in "${units[@]}"; do
    name="$(sg_unit_name "$rel")"
    dir="$base/$rel"
    cfg="$SG_LOGS/$name.render.json"
    TG_TF_PATH="$SG_TF_BIN" terragrunt render --json --non-interactive --no-color \
      --no-dependency-outputs --working-dir "$dir" >"$cfg" 2>"$SG_LOGS/$name.render.log" ||
      sg_die "cannot read the dependencies of $name"
    ups=()
    while IFS= read -r path; do
      [ -n "$path" ] || continue
      case "$path" in
        /*) up="$(realpath -m "$path")" ;;
        *) up="$(realpath -m "$(realpath "$dir")/$path")" ;;
      esac
      case "$up" in
        "$rbase"/*) up="${up#"$rbase"/}" ;;
        *) sg_die "$name depends on a unit outside $base; cannot check for cascading changes" ;;
      esac
      [ -n "${known[$up]:-}" ] ||
        sg_die "$name depends on $up, which is not a discovered unit; cannot check for cascading changes"
      ups+=("$(sg_unit_name "$up")")
    done < <(jq -r '(.dependency // {}) | to_entries[] | .value.config_path // empty' "$cfg")
    jq -n --arg u "$name" '{($u): $ARGS.positional}' --args "${ups[@]}" >>"$out.lines"
  done
  jq -s 'add' "$out.lines" >"$out" || sg_die "cannot write the dependency graph"
  rm -f "$out.lines"
}
