# Pleco Backend — Agent Instructions

These are repository-wide invariants. Use relevant `.agents/skills/` for specialist guidance; do not copy whole skill manuals into this file.

## 1. Security: never inspect environment secrets

- **Never read, print, search, parse, copy, source, decode, or otherwise inspect** `.env`, `.env.*`, `*.env`, or equivalent secret-bearing environment/configuration files. This includes indirect access through shell/Python/Node, subprocesses, Docker/container/process inspection, `env`/`printenv`, `/proc/*/environ`, or sub-agents.
- Do not expose secrets in responses, logs, test output, patches, or generated artifacts. Do not use a debugging request as permission to inspect protected values.
- You may check whether a file exists, inspect **sanitized** `.env.example` / `.env.sample` templates or non-secret source/configuration, and infer **variable names** (not values).
- When a task needs a protected value, use a placeholder or mock; otherwise ask the user for narrowly scoped, redacted diagnostics. Services may consume environment configuration during normal execution, but the agent must not retrieve it for inspection.

## 2. Project and source of truth

- Backend: Python, FastAPI, Pydantic v2, SQLAlchemy 2 / asyncpg, PostgreSQL, Alembic, Redis, RabbitMQ, MQTT, WebSockets, and Docker Compose.
- Current API entry point: `app.cmd.main:app`. Exact versions, services, commands, and file layout come from **non-secret repository files** (`requirements*.txt`, Compose, source, migrations, tests), not this document.
- Follow this order when guidance conflicts: **explicit task requirements → actual repository code/configuration and established contracts → these repository rules → Pleco architecture skill → other relevant skills → generic examples**. The environment-secret prohibition above remains in effect for every task.
- Do not silently replace established technologies, response formats, folder conventions, or working behavior with patterns from generic skills. Explicit architecture migrations are a different kind of task.

## 3. Skill router

Route by **task intent**, not by keywords alone. For non-trivial work, select the most relevant **primary** skill below, then add supporting skills only when the task actually crosses their boundaries. Read the selected `.agents/skills/<skill-name>/SKILL.md` before applying it; never load every skill by default.

| If the task involves... | Primary skill | Add when needed |
| --- | --- | --- |
| Pleco layer ownership, feature placement, HTTP/event flow, MQTT/RabbitMQ consumers, robot command or telemetry architecture | `pleco-backend-architecture` | `fastapi-expert` for Python implementation; `websocket-engineer` for realtime transport |
| FastAPI endpoints, handlers, Pydantic v2, dependencies, async SQLAlchemy integration, or API tests | `fastapi-expert` | `pleco-backend-architecture` for new feature boundaries; `secure-code-guardian` for auth |
| REST resources, HTTP semantics, pagination, versioning, response/error contracts, OpenAPI design | `api-designer` | `fastapi-expert` when implementing the contract |
| Major architecture decisions, technology trade-offs, scaling strategy, ADRs, or failure-mode design | `architecture-designer` | `pleco-backend-architecture` to respect existing Pleco boundaries |
| Python docstrings, API documentation, developer guides, or documentation-focused changes | `code-documenter` | Domain-specific skill for the feature being documented |
| SQL query construction, joins, CTEs, window functions, or general relational query design | `sql-pro` | `postgres-pro` for PostgreSQL-specific behavior |
| PostgreSQL schema/database behavior, extensions, JSONB, administration, migrations, or database internals | `postgres-pro` | `sql-pro` for complex SQL; `fastapi-expert` for ORM/application integration |
| Slow queries, indexes, query plans, locks, contention, or measured database tuning | `database-optimizer` | `postgres-pro` for PostgreSQL-specific diagnosis |
| Implementing authentication, RBAC, JWT handling, validation, secret handling, or other security controls | `secure-code-guardian` | `fastapi-expert` for API code; `security-reviewer` for a requested audit |
| Security audits, vulnerability reviews, dependency scans, threat findings, or remediation reviews | `security-reviewer` | `secure-code-guardian` when implementing a fix |
| WebSocket connections, rooms, presence, heartbeat, reconnection, Redis pub/sub, or realtime fan-out | `websocket-engineer` | `fastapi-expert` for FastAPI endpoints; `pleco-backend-architecture` for event boundaries |

**Multi-skill routing examples:**

- New robot telemetry ingestion: `pleco-backend-architecture` → `fastapi-expert`; add `websocket-engineer` if modifying delivery to clients.
- New authenticated API endpoint: `fastapi-expert` + `secure-code-guardian`; add `api-designer` only if defining/changing the contract.
- Slow PostgreSQL query: `database-optimizer` + `postgres-pro`; add `sql-pro` if rewriting SQL.
- Robot command security review: `security-reviewer` + `pleco-backend-architecture`; add `secure-code-guardian` when fixing code.

If no specialist skill matches a routine task, follow repository conventions without forcing a skill. Existing Pleco code and these repository rules take precedence over generic 
skill examples. Do not adopt another framework, ORM, database, or transport solely because an example uses it.


## 4. Code boundaries

Standard HTTP flow:

```text
Router → Handler → Service → Repository → SQLAlchemy Model / PostgreSQL
```

- **Router:** route declarations and metadata. **Handler:** HTTP orchestration, request/auth context, error/response mapping. **Service:** business rules and workflows. **Repository:** persistence queries. **Model:** table mappings.
- Keep dependencies one-way. No database queries in routers/handlers/services, no HTTP concerns in repositories. Preserve existing conventions for schemas, dependencies, configuration, and shared utilities.
- Event consumers, broker adapters, and WebSocket handlers have separate lifecycles; do not force them into the HTTP chain. Keep transport concerns out of repositories and delegate durable writes through established service/repository boundaries.
- For file placement, boundaries, and complete data flows, consult `.agents/skills/pleco-backend-architecture/SKILL.md` (or the installed skill by that name).

## 5. Pleco domain and messaging invariants

- Enforce **group/tenant isolation, RBAC, and resource-level permissions in the backend** for maps, robots, tasks, commands, and related resources. Never rely only on frontend checks.
- Treat MQTT telemetry and robot acknowledgements as untrusted input; validate ownership, schema, and state transitions. Handle duplicates, stale/out-of-order messages, disconnects, timeouts, and retries where relevant.
- A broker publish/ACK **does not** mean the robot executed the command. Keep command dispatch, acknowledgement, execution, failure, rejection, and timeout states distinct according to the implemented state machine.
- Safety-sensitive movement commands require appropriate authorization, expiry/deadman behavior, and explicit failure handling. Avoid stale commands executing after reconnection.
- Keep current robot identity stable (`robot_id` UUID); network IP is mutable metadata, not robot identity.
- Preserve established map-boundary, obstacle/no-go-region, and docking-station separation rather than merging distinct entities.
- Do not hold request-scoped database sessions across long-running broker/WebSocket loops. Preserve cancellation, reconnect, lifecycle, and cleanup behavior.

Reference flows (verify actual implementation before changing it):

```text
Robot → MQTT → ingestion/RabbitMQ → event processor → PostgreSQL/Redis → WebSocket → UI
UI → FastAPI command/API validation → RabbitMQ/MQTT → Robot → ACK/execution event → backend state update
```

## 6. Code Review Rules

When reviewing code changes, prioritize:

- Group isolation and backend authorization.
- Robot UUID identity and data consistency.
- PostGIS geometry and workspace constraints.
- MQTT, API, and Socket.IO compatibility.
- Duplicate, stale, and out-of-order telemetry.
- Robot command acknowledgement and idempotency.
- Emergency-stop and safety behavior.

Report actionable defects with:
- Severity (Critical, High, Medium, Low).
- File path and line number.
- Impact and supporting evidence.
- Recommended fix when applicable.

Prioritize correctness, security, and reliability.
Avoid stylistic comments unless they cause real issues.
Do not report speculative defects without evidence.

## 7. Changes and verification

1. Inspect the target code, nearby patterns, relevant tests, and **non-secret** configuration; identify applicable skills.
2. Make the smallest coherent change. Preserve established async/session, API, logging, and dependency conventions; avoid new packages or unrelated refactors without need.
3. For schema changes, add/review Alembic migrations (including downgrade) and test on a non-production database. Never run destructive production operations without explicit authorization.
4. Run the narrowest meaningful checks: targeted pytest; API/auth negative cases; migration or SQL checks; Compose validation; broker/telemetry duplicate, ordering, ACK, timeout, and reconnect checks when relevant.
5. Review the diff. Report what changed, tests actually run, failures/untested paths, and important operational or security implications. Never claim unrun tests passed; do not weaken tests to hide failures.

For commands, service names, pinned dependency versions, and runtime configuration, inspect the repository rather than treating examples in an instruction file as authoritative.
