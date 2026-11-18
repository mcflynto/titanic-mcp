# SQL validation with a syntax tree

Status: accepted (2026-11-18)

## Context

Agents can generate SQL. Text patterns cannot detect all dangerous statements.

## Decision

sqlglot parses the SQL. The guard permits one SELECT, an allow-list of functions and the tables of the agent. It replaces restricted tables with filtered subqueries and sets a LIMIT. The connection is also read-only (`mode=ro`, `immutable=1`, `query_only`).

## Consequences

Some valid SQLite functions are not permitted. The allow-list can grow when agent teams need more functions.
