package databricks_pack_test

import data.stagegate.databricks

grants_plan(grant) := {"resource_changes": [{
	"address": "databricks_grants.x",
	"type": "databricks_grants",
	"change": {"actions": ["update"], "after": {"grant": grant}},
}]}

single_grant_plan(after) := {"resource_changes": [{
	"address": "databricks_grant.x",
	"type": "databricks_grant",
	"change": {"actions": ["create"], "after": after},
}]}

permissions_plan(after) := {"resource_changes": [{
	"address": "databricks_permissions.job",
	"type": "databricks_permissions",
	"change": {"actions": ["create"], "after": after},
}]}

test_001_denies_all_privileges_to_account_users if {
	msgs := databricks.deny with input as grants_plan([{"principal": "account users", "privileges": ["USE_CATALOG", "ALL_PRIVILEGES"]}])
	msgs == {"SG-DBX-001: databricks_grants.x grants ALL_PRIVILEGES to \"account users\""}
}

test_001_denies_single_grant_resource if {
	msgs := databricks.deny with input as single_grant_plan({"principal": "account users", "privileges": ["ALL_PRIVILEGES"]})
	msgs == {"SG-DBX-001: databricks_grant.x grants ALL_PRIVILEGES to \"account users\""}
}

test_001_allows_other_principal if {
	count(databricks.deny) == 0 with input as grants_plan([{"principal": "data-team", "privileges": ["ALL_PRIVILEGES"]}])
}

test_001_allows_account_users_with_limited_privileges if {
	count(databricks.deny) == 0 with input as grants_plan([{"principal": "account users", "privileges": ["USE_CATALOG"]}])
}

test_001_allows_single_grant_limited if {
	count(databricks.deny) == 0 with input as single_grant_plan({"principal": "account users", "privileges": ["SELECT"]})
}

test_006_denies_job_without_manager if {
	msgs := databricks.deny with input as permissions_plan({"job_id": "123", "access_control": [{"group_name": "g", "permission_level": "CAN_VIEW"}]})
	msgs == {"SG-DBX-006: databricks_permissions.job leaves its job without a CAN_MANAGE holder"}
}

test_006_denies_job_without_access_control if {
	count(databricks.deny) == 1 with input as permissions_plan({"job_id": "123"})
}

test_006_allows_can_manage if {
	count(databricks.deny) == 0 with input as permissions_plan({"job_id": "123", "access_control": [{"group_name": "g", "permission_level": "CAN_MANAGE"}]})
}

test_006_allows_is_owner if {
	count(databricks.deny) == 0 with input as permissions_plan({"job_id": "123", "access_control": [{"user_name": "u", "permission_level": "IS_OWNER"}]})
}

test_006_ignores_non_job_permissions if {
	count(databricks.deny) == 0 with input as permissions_plan({"job_id": null, "cluster_id": "c", "access_control": []})
}

test_006_denies_unknown_job_id_without_manager if {
	plan := {"resource_changes": [{
		"address": "databricks_permissions.job",
		"type": "databricks_permissions",
		"change": {
			"actions": ["create"],
			"after": {"access_control": [{"group_name": "g", "permission_level": "CAN_VIEW"}]},
			"after_unknown": {"job_id": true},
		},
	}]}
	count(databricks.deny) == 1 with input as plan
}

test_006_allows_unknown_job_id_with_manager if {
	plan := {"resource_changes": [{
		"address": "databricks_permissions.job",
		"type": "databricks_permissions",
		"change": {
			"actions": ["create"],
			"after": {"access_control": [{"group_name": "g", "permission_level": "CAN_MANAGE"}]},
			"after_unknown": {"job_id": true},
		},
	}]}
	count(databricks.deny) == 0 with input as plan
}

test_006_ignores_cluster_permissions if {
	plan := {"resource_changes": [{
		"address": "databricks_permissions.cluster",
		"type": "databricks_permissions",
		"change": {
			"actions": ["create"],
			"after": {"cluster_id": "c", "access_control": [{"group_name": "g", "permission_level": "CAN_VIEW"}]},
			"after_unknown": {"id": true},
		},
	}]}
	count(databricks.deny) == 0 with input as plan
}
