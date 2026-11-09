package stagegate.databricks

# SG-DBX-001: ALL_PRIVILEGES granted to the "account users" principal.
# SG-DBX-006: a job left without a CAN_MANAGE (or IS_OWNER) holder.

writes(rc) if {
	some a in rc.change.actions
	a in {"create", "update"}
}

# databricks_grants carries a grant[] list; databricks_grant is one entry.
grant_entries(rc) := rc.change.after.grant if rc.type == "databricks_grants"

grant_entries(rc) := [rc.change.after] if rc.type == "databricks_grant"

deny contains msg if {
	some rc in input.resource_changes
	writes(rc)
	some g in grant_entries(rc)
	g.principal == "account users"
	"ALL_PRIVILEGES" in g.privileges
	msg := sprintf("SG-DBX-001: %s grants ALL_PRIVILEGES to \"account users\"", [rc.address])
}

manager_levels := {"CAN_MANAGE", "IS_OWNER"}

deny contains msg if {
	some rc in input.resource_changes
	rc.type == "databricks_permissions"
	writes(rc)
	job_permission(rc)
	acl := object.get(rc.change.after, "access_control", [])
	not has_manager(acl)
	msg := sprintf("SG-DBX-006: %s leaves its job without a CAN_MANAGE holder", [rc.address])
}

has_manager(acl) if {
	some entry in acl
	entry.permission_level in manager_levels
}

# A job permission has a known job_id, or one unknown until apply (for example
# job_id = databricks_job.x.id in the same plan): then after omits it and
# after_unknown.job_id is true.
job_permission(rc) if object.get(rc.change.after, "job_id", null) != null

job_permission(rc) if object.get(object.get(rc.change, "after_unknown", {}), "job_id", false) == true
