# Runbook: TmcpAuthFailureSpike

**Meaning:** More than 20 failed authentications per minute for 5 minutes. This can be an attack or a wrong client configuration.

**Procedure:**

1. Look at the panel 'Rejected SQL and failed authentication'. The label `reason` shows the cause.
2. `expired`: a client does not refresh its tokens. `wrong_issuer_or_audience`: a client uses the wrong IdP or audience.
3. `invalid` or `unknown_key` in large numbers: tell the security team. Find the client IPs in the access logs.
