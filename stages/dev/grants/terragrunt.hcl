include "root" {
  path = find_in_parent_folders("root.hcl")
}

# The outputs of base are used below. A plan of this unit is only valid for the state of base it
# was made against, so a merge that changes both units is refused at prod-plan (R52).
dependency "base" {
  config_path = "../base"

  mock_outputs = {
    catalog    = "mock-catalog"
    generation = "mock-generation"
  }
  mock_outputs_allowed_terraform_commands = ["init", "validate", "plan", "show"]
  mock_outputs_merge_strategy_with_state  = "shallow"
}

inputs = {
  tag = trimspace(file("${get_repo_root()}/e2e-grants-tag.txt"))
  # Shaped like a databricks_grants resource so the plan JSON carries the same kind of data.
  grants = {
    catalog         = dependency.base.outputs.catalog
    base_generation = dependency.base.outputs.generation
    principal       = "data-engineers"
    privileges      = ["USE_CATALOG", "SELECT"]
  }
}
