"""Issue labels shared by the guard, the issue commands and the drift check."""

STOP_LABEL = "stagegate:stop-the-line"
APPLY_FAILED_LABEL = "stagegate:apply-failed"  # lets nightly drift skip an env with a failed apply
DRIFT_LABEL = "stagegate:drift"
POST_APPROVAL_LABEL = "stagegate:post-approval"  # break-glass use awaiting after-the-fact approval
