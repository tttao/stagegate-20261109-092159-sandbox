package stagegate.databricks

# SG-DBX-003: public network access on a Databricks workspace.
# Stage comes from `--data` (data.stagegate.stage); missing data means "".
# Shared by every rule file in this package.

default stage := ""

stage := data.stagegate.stage

default flags := []

flags := data.stagegate.flags

public_workspace_violations contains msg if {
	some rc in input.resource_changes
	rc.type == "azurerm_databricks_workspace"
	rc.change.after.public_network_access_enabled == true
	msg := sprintf("SG-DBX-003: %s enables public network access", [rc.address])
}

deny contains msg if {
	stage == "prod"
	some msg in public_workspace_violations
}

warn contains msg if {
	stage != "prod"
	some msg in public_workspace_violations
}
