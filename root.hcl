# SANDBOX ONLY. This fixture keeps Terraform state in plain files that state-sync.sh moves to the
# `stagegate-state` branch of the sandbox repository, because the e2e runs need state to survive
# between jobs and the sandbox has no cloud storage. Never copy this to a real repository.
locals {
  state_dir = get_env("STAGEGATE_E2E_STATE_DIR", "${get_repo_root()}/.stagegate-e2e-state")
  sync      = "${get_repo_root()}/scripts/state-sync.sh"
  # stages/<env>/<unit>
  env  = basename(dirname(get_terragrunt_dir()))
  unit = basename(get_terragrunt_dir())
}

remote_state {
  backend = "local"
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }
  config = {
    path = "${local.state_dir}/${local.env}/${local.unit}.tfstate"
  }
}

terraform {
  before_hook "pull_state" {
    commands = ["init", "plan", "apply"]
    execute  = [local.sync, "pull", local.state_dir]
  }

  after_hook "push_state" {
    commands     = ["apply"]
    execute      = [local.sync, "push", local.state_dir, "${local.env}/${local.unit}.tfstate"]
    run_on_error = true
  }
}
