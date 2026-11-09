include "root" {
  path = find_in_parent_folders("root.hcl")
}

inputs = {
  # Changing this file changes base only (a resource input, no output): the ordinary commit.
  marker = trimspace(file("${get_repo_root()}/e2e-marker.txt"))
  # Changing this file changes an output of base that grants reads (the cascade scenario).
  generation = trimspace(file("${get_repo_root()}/e2e-generation.txt"))
}
