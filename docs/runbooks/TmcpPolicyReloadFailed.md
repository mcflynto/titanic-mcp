# Runbook: TmcpPolicyReloadFailed

**Meaning:** A new policy file is not valid. The pods keep the old policy (PERM-02).

**Procedure:**

1. Read the log event `policy_reload_failed`. It contains the validation error.
2. Correct the policy file and deploy it again. Run `make test` before: the tests validate the policy.
