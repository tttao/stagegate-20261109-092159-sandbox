# stagegate ephemeral sandbox

Created by `python -m e2e.ephemeral up` from the stagegate repository, for trying out
and demonstrating stagegate. SANDBOX ONLY: Terraform state lives on the
`stagegate-state` branch and the prod plan is encrypted with a passphrase instead of a
Key Vault key.
Delete it with `python -m e2e.ephemeral down --repo <owner>/<name>`.
