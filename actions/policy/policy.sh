#!/usr/bin/env bash
# Install conftest (SHA-256 verified) and run the Databricks Rego pack over <plan-json>/*.json
# with a data file naming the stage. The pack lives in this repo (policy/databricks).
# Env: SG_TOOL_MIRROR (optional) SG_PLAN_JSON SG_STAGE SG_CONFTEST_VERSION SG_CONFTEST_SHA256 (RUNNER_TEMP)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/stagegate-lib.sh
. "$here/../lib/stagegate-lib.sh"

dir="${SG_PLAN_JSON:?SG_PLAN_JSON is required}"
stage="${SG_STAGE:?SG_STAGE is required}"
version="${SG_CONFTEST_VERSION:?}"
sha="${SG_CONFTEST_SHA256:?}"
sg_check_env "$stage"
mirror="${SG_TOOL_MIRROR:-}"
sg_check_mirror "$mirror"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || sg_die "invalid conftest version: $version"
[ -d "$dir" ] || sg_die "plan JSON directory not found: $dir"
[ -n "${RUNNER_TEMP:-}" ] || sg_die "RUNNER_TEMP is not set"

tmp="$(mktemp -d "$RUNNER_TEMP/conftest.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL --retry 6 --retry-delay 15 --connect-timeout 30 -o "$tmp/conftest.tgz" "$(sg_mirror_url "$mirror" \
  "https://github.com/open-policy-agent/conftest/releases/download/v${version}/conftest_${version}_Linux_x86_64.tar.gz")"
echo "$sha  $tmp/conftest.tgz" | sha256sum --check --quiet - || sg_die "conftest checksum mismatch"
tar -xzf "$tmp/conftest.tgz" -C "$tmp" conftest

jq -n --arg stage "$stage" '{stagegate: {stage: $stage, flags: []}}' >"$tmp/data.json"
shopt -s nullglob
files=("$dir"/*.json)
[ "${#files[@]}" -gt 0 ] || sg_die "no plan JSON files in $dir"

"$tmp/conftest" test --no-color --policy "$here/../../policy/databricks" \
  --namespace stagegate.databricks --data "$tmp/data.json" "${files[@]}"
