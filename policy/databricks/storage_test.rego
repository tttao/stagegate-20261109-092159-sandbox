package databricks_pack_test

import data.stagegate.databricks

catalog_plan(actions, after) := {"resource_changes": [{
	"address": "databricks_catalog.c",
	"type": "databricks_catalog",
	"change": {"actions": actions, "after": after},
}]}

skip_plan(type, skip) := {"resource_changes": [{
	"address": sprintf("%s.s", [type]),
	"type": type,
	"change": {"actions": ["create"], "after": {"skip_validation": skip}},
}]}

metastore_plan(type, actions) := {"resource_changes": [{
	"address": sprintf("%s.m", [type]),
	"type": type,
	"change": {"actions": actions, "after": null},
}]}

test_002_denies_non_isolated_catalog_in_prod if {
	msgs := databricks.deny with input as catalog_plan(["create"], {"isolation_mode": "OPEN"}) with data.stagegate.stage as "prod"
	msgs == {"SG-DBX-002: databricks_catalog.c is not ISOLATED"}
}

test_002_denies_missing_isolation_mode_in_prod if {
	count(databricks.deny) == 1 with input as catalog_plan(["update"], {"name": "c"}) with data.stagegate.stage as "prod"
}

test_002_allows_isolated_catalog_in_prod if {
	count(databricks.deny) == 0 with input as catalog_plan(["create"], {"isolation_mode": "ISOLATED"}) with data.stagegate.stage as "prod"
	count(databricks.warn) == 0 with input as catalog_plan(["create"], {"isolation_mode": "ISOLATED"}) with data.stagegate.stage as "prod"
}

test_002_ignores_deleted_catalog if {
	count(databricks.deny) == 0 with input as catalog_plan(["delete"], null) with data.stagegate.stage as "prod"
}

test_002_is_scoped_to_stage if {
	count(databricks.deny) == 0 with input as catalog_plan(["create"], {"isolation_mode": "OPEN"}) with data.stagegate.stage as "dev"
	count(databricks.warn) == 1 with input as catalog_plan(["create"], {"isolation_mode": "OPEN"}) with data.stagegate.stage as "dev"
	count(databricks.warn) == 0 with input as catalog_plan(["create"], {"isolation_mode": "OPEN"}) with data.stagegate.stage as "prod"
}

test_004_warns_on_skip_validation_external_location if {
	msgs := databricks.warn with input as skip_plan("databricks_external_location", true)
	msgs == {"SG-DBX-004: databricks_external_location.s sets skip_validation"}
}

test_004_warns_on_skip_validation_storage_credential if {
	count(databricks.warn) == 1 with input as skip_plan("databricks_storage_credential", true)
}

test_004_no_warn_without_skip_validation if {
	count(databricks.warn) == 0 with input as skip_plan("databricks_external_location", false)
}

test_004_is_a_warning_not_a_deny if {
	count(databricks.deny) == 0 with input as skip_plan("databricks_external_location", true)
}

test_005_denies_metastore_delete if {
	msgs := databricks.deny with input as metastore_plan("databricks_metastore", ["delete"])
	msgs == {"SG-DBX-005: databricks_metastore.m is deleted or forgotten without the allow-metastore-destroy flag"}
}

test_005_denies_metastore_replace_and_prefixed_types if {
	count(databricks.deny) == 1 with input as metastore_plan("databricks_metastore_assignment", ["delete", "create"])
}

test_005_allows_metastore_update if {
	count(databricks.deny) == 0 with input as metastore_plan("databricks_metastore", ["update"])
}

test_005_flag_allows_delete if {
	count(databricks.deny) == 0 with input as metastore_plan("databricks_metastore", ["delete"]) with data.stagegate.flags as ["allow-metastore-destroy"]
	count(databricks.deny) == 1 with input as metastore_plan("databricks_metastore", ["delete"]) with data.stagegate.flags as ["other"]
}

test_005_denies_metastore_forget if {
	count(databricks.deny) == 1 with input as metastore_plan("databricks_metastore", ["forget"])
	count(databricks.deny) == 0 with input as metastore_plan("databricks_metastore", ["forget"]) with data.stagegate.flags as ["allow-metastore-destroy"]
}
