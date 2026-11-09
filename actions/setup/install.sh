#!/usr/bin/env bash
# Install pinned OpenTofu and Terragrunt (SHA-256 verified before use) and the stagegate CLI.
# Env: SG_TOOL_MIRROR (optional https base replacing https://github.com for the downloads)
#      SG_TOFU_VERSION SG_TOFU_SHA256 SG_TERRAGRUNT_VERSION SG_TERRAGRUNT_SHA256
#      GITHUB_ACTION_PATH (stagegate is installed from the repo this action lives in)
# Needs uv on PATH (the action installs it with astral-sh/setup-uv). Linux amd64 only.
set -euo pipefail

: "${SG_TOFU_VERSION:?}" "${SG_TOFU_SHA256:?}" "${SG_TERRAGRUNT_VERSION:?}" "${SG_TERRAGRUNT_SHA256:?}"
[[ "$SG_TOFU_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "bad tofu-version: $SG_TOFU_VERSION" >&2; exit 1; }
[[ "$SG_TERRAGRUNT_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "bad terragrunt-version: $SG_TERRAGRUNT_VERSION" >&2; exit 1; }
# shellcheck source=../lib/stagegate-lib.sh
. "${GITHUB_ACTION_PATH:?GITHUB_ACTION_PATH is not set}/../lib/stagegate-lib.sh"
mirror="${SG_TOOL_MIRROR:-}"
sg_check_mirror "$mirror"
[ "$(uname -s)-$(uname -m)" = Linux-x86_64 ] || { echo "only linux amd64 runners are supported" >&2; exit 1; }

tools="${RUNNER_TEMP:?RUNNER_TEMP is not set}/stagegate-tools"
bin="$tools/bin"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$bin"

# fetch URL SHA256 FILE: download and verify before anything is used.
fetch() {
  # GitHub release downloads return 504 for a minute at a time; ride that out (about 90s).
  curl -fsSL --retry 6 --retry-delay 15 --connect-timeout 30 -o "$tmp/$3" "$(sg_mirror_url "$mirror" "$1")"
  echo "$2  $tmp/$3" | sha256sum --check --quiet - || { echo "checksum mismatch: $3" >&2; exit 1; }
}

fetch "https://github.com/opentofu/opentofu/releases/download/v${SG_TOFU_VERSION}/tofu_${SG_TOFU_VERSION}_linux_amd64.tar.gz" \
  "$SG_TOFU_SHA256" tofu.tgz
tar -xzf "$tmp/tofu.tgz" -C "$tmp" tofu
install -m 0755 "$tmp/tofu" "$bin/tofu"

fetch "https://github.com/gruntwork-io/terragrunt/releases/download/v${SG_TERRAGRUNT_VERSION}/terragrunt_linux_amd64" \
  "$SG_TERRAGRUNT_SHA256" terragrunt
install -m 0755 "$tmp/terragrunt" "$bin/terragrunt"

# stagegate goes into its own venv with the `azure` extra (the Azure Blob ledger store needs it).
# Every dependency is pinned by version AND hash from the committed uv.lock (--require-hashes);
# the local project itself is installed without resolving anything further (--no-deps).
src="$(cd "${GITHUB_ACTION_PATH:?GITHUB_ACTION_PATH is not set}/../.." && pwd)"
venv="$tools/venv"
(cd "$src" && uv export --frozen --no-dev --extra azure --no-emit-project -o "$tmp/constraints.txt" >/dev/null)
uv venv --quiet --clear "$venv"
VIRTUAL_ENV="$venv" uv pip install --quiet --require-hashes -r "$tmp/constraints.txt"
VIRTUAL_ENV="$venv" uv pip install --quiet --no-deps "$src[azure]"
ln -sf "$venv/bin/stagegate" "$bin/stagegate"

if [ -n "${GITHUB_PATH:-}" ]; then
  echo "$bin" >>"$GITHUB_PATH"
fi
echo "installed tofu ${SG_TOFU_VERSION}, terragrunt ${SG_TERRAGRUNT_VERSION}, stagegate into $bin"
