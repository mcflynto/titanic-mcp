# Runbook: TmcpErrorBudgetBurn

**Meaning:** The error budget of the 99.5 % availability SLO burns 14 times faster than permitted.

**Procedure:**

1. Do the steps of TmcpHighErrorRate.
2. If the cause is a release, do a rollback in 5 minutes or less (DEP-04).
3. Write an incident report after the recovery.
