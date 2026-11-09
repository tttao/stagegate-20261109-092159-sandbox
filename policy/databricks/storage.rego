package stagegate.databricks

# SG-DBX-002: catalog not ISOLATED (deny in prod, warn elsewhere).
# SG-DBX-004: skip_validation on external locations and storage credentials.
# SG-DBX-005: metastore deletion or forget without the allow-metastore-destroy flag.

catalog_violations contains msg if {
	some rc in input.resource_changes
	rc.type == "databricks_catalog"
	writes(rc)
	object.get(rc.change.after, "isolation_mode", null) != "ISOLATED"
	msg := sprintf("SG-DBX-002: %s is not ISOLATED", [rc.address])
}

deny contains msg if {
	stage == "prod"
	some msg in catalog_violations
}

warn contains msg if {
	stage != "prod"
	some msg in catalog_violations
}

warn contains msg if {
	some rc in input.resource_changes
	rc.type in {"databricks_external_location", "databricks_storage_credential"}
	rc.change.after.skip_validation == true
	msg := sprintf("SG-DBX-004: %s sets skip_validation", [rc.address])
}

deny contains msg if {
	not "allow-metastore-destroy" in flags
	some rc in input.resource_changes
	startswith(rc.type, "databricks_metastore")
	some action in rc.change.actions
	action in {"delete", "forget"}
	msg := sprintf("SG-DBX-005: %s is deleted or forgotten without the allow-metastore-destroy flag", [rc.address])
}
