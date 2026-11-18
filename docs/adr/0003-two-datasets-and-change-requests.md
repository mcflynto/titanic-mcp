# Read-only reference data and an approved writable copy

Status: accepted (2026-11-18)

## Context

OP-01 requires write access in version 1.0, with a separate role, a separate writable database and an approval step.

## Decision

The original SQLite file stays read-only in the image (`reference`). A PostgreSQL copy (`working`) is writable only through change requests. A second agent with the role `approver` must approve each change. The server applies it in the approval transaction and detects conflicts with a copy of the old row.

## Consequences

Free SQL (`run_sql`) is available only on `reference`. SQLite is not used for writes, because several replicas cannot write to one SQLite file safely.
