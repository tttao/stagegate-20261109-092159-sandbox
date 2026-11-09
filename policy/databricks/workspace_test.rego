package databricks_pack_test

import data.stagegate.databricks

ws_plan(public) := {"resource_changes": [{
	"address": "azurerm_databricks_workspace.w",
	"type": "azurerm_databricks_workspace",
	"change": {"actions": ["create"], "after": {"public_network_access_enabled": public}},
}]}

test_003_denies_public_workspace_in_prod if {
	msgs := databricks.deny with input as ws_plan(true) with data.stagegate.stage as "prod"
	msgs == {"SG-DBX-003: azurerm_databricks_workspace.w enables public network access"}
}

test_003_allows_private_workspace_in_prod if {
	count(databricks.deny) == 0 with input as ws_plan(false) with data.stagegate.stage as "prod"
	count(databricks.warn) == 0 with input as ws_plan(false) with data.stagegate.stage as "prod"
}

test_003_warns_outside_prod if {
	msgs := databricks.warn with input as ws_plan(true) with data.stagegate.stage as "dev"
	msgs == {"SG-DBX-003: azurerm_databricks_workspace.w enables public network access"}
	count(databricks.deny) == 0 with input as ws_plan(true) with data.stagegate.stage as "dev"
}

test_003_missing_data_is_not_prod if {
	count(databricks.deny) == 0 with input as ws_plan(true)
	count(databricks.warn) == 1 with input as ws_plan(true)
}

test_003_no_warn_in_prod if {
	count(databricks.warn) == 0 with input as ws_plan(true) with data.stagegate.stage as "prod"
}
