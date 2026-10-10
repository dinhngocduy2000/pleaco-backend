---
name: pleco-backend-architecture
description: >
  Pleco backend architecture, module-placement, data-layer, and realtime/event-driven conventions.
  Use when creating, moving, reviewing, or refactoring backend feature files; deciding where Router,
  Handler, Service, Repository, SQLAlchemy Model, MQTT/RabbitMQ consumer, realtime processor, or
  WebSocket adapter code belongs; tracing HTTP or robot-event flows; adding a new feature; or
  reviewing layer boundaries and dependency direction. This skill defines Pleco-specific structure
  and takes precedence over generic FastAPI, API-design, architecture, SQL, messaging, WebSocket,
  or repository-pattern examples when they conflict with the established Pleco architecture.
---

# Pleco Backend Architecture

Use this skill for Pleco-specific backend structure, layer responsibilities, and transport boundaries.

Pleco is a multi-tenant realtime platform for mapping, provisioning, monitoring, controlling, and
coordinating autonomous cleaning robots. The backend handles both ordinary HTTP application flows
and asynchronous robot/realtime flows involving MQTT, RabbitMQ, Redis, WebSockets, PostgreSQL, and
the Python robot simulator.

## References

Read the reference that matches the task:

- `references/module-structure.md`
  - file/folder placement
  - feature file organization
  - allowed dependency direction
  - where ORM models belong
  - placement of transport/event-driven code
  - adding a new feature across HTTP and realtime flows

- `references/data-layer.md`
  - request-to-database flow
  - robot telemetry/event flow
  - command flow
  - Router responsibilities
  - Handler responsibilities
  - Service responsibilities
  - Repository responsibilities
  - Model responsibilities
  - messaging/realtime adapter boundaries
  - tenant/RBAC and event-integrity boundaries
  - anti-patterns

For a new backend feature or a refactor that touches more than one layer or transport, read both
references.

## Core Application Architecture

For normal HTTP features, Pleco uses the established layered architecture:

```text
HTTP Request
    |
    v
Router
    |
    v
Handler
    |
    v
Service
    |
    v
Repository
    |
    v
SQLAlchemy Model / PostgreSQL
```

`Models` are shared ORM table definitions outside the Router -> Handler -> Service -> Repository
feature flow. Repositories use models for persistence.

## Realtime and Event-Driven Architecture

Pleco also receives asynchronous robot and infrastructure events.

Conceptually:

```text
Robot / Simulator
      |
     MQTT
      |
      v
RabbitMQ / MQTT ingress
      |
      v
Consumer / Transport Adapter
      |
      v
Service / Domain Processing
      |
      +--------------------+
      |                    |
      v                    v
Repository             Redis / Realtime Publisher
      |                    |
      v                    v
PostgreSQL            WebSocket delivery
                           |
                           v
                      React clients
```

The exact broker topology, exchange/topic names, queue names, consumer classes, Redis abstractions,
and folder names must come from the repository. Do not create new infrastructure conventions solely
from this diagram.

The important architectural rule is that transport adapters should decode/validate transport
messages and delegate reusable domain behavior to Services. Persistence stays in Repositories.

## Robot Command Architecture

Operator actions may begin through HTTP or WebSocket-facing application interfaces, but command
business rules remain transport-independent.

Conceptually:

```text
Frontend command request
      |
      v
HTTP/WebSocket-facing adapter
      |
      v
Service
      |
      +--> Repository: command/audit state
      |
      +--> Command Publisher
              |
              v
        RabbitMQ / MQTT
              |
              v
        Robot / Simulator
              |
              v
     ACK / execution event
              |
              v
      Consumer / Adapter
              |
              v
            Service
              |
              +--> Repository
              +--> Redis / WebSocket update
```

Command lifecycle, idempotency, timeout, acknowledgement, authorization, tenant ownership, and robot
state validation are domain concerns and must not be implemented only in the frontend or broker
adapter.

## Core Rules

- Organize Router, Handler, Service, and Repository code by feature using the repository's existing
  folder structure.
- A feature should have its own file in each layer that it genuinely uses.
- Preserve the normal HTTP dependency direction:
  `Router -> Handler -> Service -> Repository -> Model/database`.
- For event-driven entry points, preserve:
  `Consumer/Transport Adapter -> Service -> Repository -> Model/database`.
- For outbound messaging, Services may invoke an existing publisher/gateway abstraction; broker
  protocol details belong in the messaging/infrastructure adapter rather than in business rules.
- Do not skip established layers merely to save a small amount of code when a feature follows the
  standard request/data flow.
- Keep Router declarations focused on route metadata and endpoint registration.
- Keep Handlers thin and HTTP-facing.
- Keep message consumers/adapters thin and transport-facing.
- Keep reusable business rules and state-transition logic in Services.
- Keep SQLAlchemy/SQL data access in Repositories.
- Keep ORM table mapping and relationships in Models.
- Do not place direct database queries in Routers, Handlers, transport consumers, or Services.
- Do not place FastAPI request/response concerns in Repositories.
- Do not put broker topic/exchange parsing, message acknowledgement mechanics, or connection
  lifecycle concerns into Repositories.
- Do not put reusable domain decisions only in WebSocket handlers, MQTT consumers, or RabbitMQ
  callbacks.
- Reuse existing project abstractions for response wrappers, context/tracing, logging, auth,
  messaging, Redis, exception mapping, and WebSocket distribution.

## Pleco Domain Invariants

Treat these as backend/domain concerns where applicable:

- tenant isolation
- RBAC and resource-scoped authorization
- robot ownership by tenant
- map ownership by tenant
- robot-to-map assignment validity
- robot operational state transitions
- mapping-session ownership and exclusivity
- one-controller-at-a-time rules for manual mapping
- command authorization and command lifecycle
- command idempotency and timeout behavior
- emergency-stop safety behavior
- telemetry sequence ordering
- duplicate-event handling
- stale/out-of-order telemetry protection
- robot online/stale/offline classification
- task state transitions
- cleaning trajectory validity
- incident/recovery state transitions

Do not rely on frontend validation for these invariants.

## Environment-File Security Boundary

The root `AGENTS.md` contains the repository-wide environment-file prohibition.

This skill must never be interpreted as permission to read `.env`, `.env.*`, `*.env`, or other
protected secret environment files.

When configuration is needed:

- inspect non-secret source/configuration
- inspect `.env.example` or equivalent sanitized templates when present
- infer variable names from code
- use placeholders or user-supplied sanitized values
- never inspect runtime secret values indirectly through processes, containers, logs, or environment
  dumping as a workaround

The root security rule takes precedence over all debugging, database, messaging, Docker, migration,
and local-runtime guidance in this skill.

## Precedence

For Pleco backend structure:

1. Explicit user requirements for the current task, except that repository security prohibitions in
   the root `AGENTS.md` remain binding.
2. Existing Pleco implementation where it is clearly established.
3. This skill and its references.
4. `fastapi-expert` for FastAPI/Python implementation details.
5. Messaging/WebSocket/database/security specialist skills for their domains.
6. Generic examples from other skills.

Generic framework or messaging examples must not replace Pleco's layer boundaries or introduce a
different stack.

## Boundaries with Other Skills

Use `fastapi-expert` alongside this skill for concrete FastAPI/Pydantic/async implementation.

Use `api-designer` for API contract design, but map the implementation into Pleco's Router and
Handler layers.

Use `sql-pro` / `postgres-pro` for SQL and PostgreSQL specifics, but keep database access inside
Repositories.

Use `database-optimizer` for measured database performance work. Optimized queries still belong in
the Repository layer.

Use `secure-code-guardian` for security-sensitive implementation. Authentication extraction and
HTTP security orchestration remain Handler/Router concerns as appropriate; reusable authorization
rules belong in Services; persistence stays in Repositories.

Use `websocket-engineer` for connection lifecycle, realtime fan-out, heartbeat, reconnection, and
Redis-backed scaling. Reusable domain behavior triggered by WebSocket interactions belongs in
Services rather than the socket adapter.

For RabbitMQ/MQTT work, follow repository-established Python libraries and topology. Transport
connection/consumer/publisher mechanics belong in infrastructure or messaging adapters; domain
decisions belong in Services.

## Workflow

When adding or changing a feature:

1. Inspect an existing comparable Pleco feature and its transport path.
2. Identify whether the entry point is HTTP, MQTT/RabbitMQ, WebSocket, scheduled/internal, or more
   than one of these.
3. Identify which layers and adapters are required.
4. Put HTTP route registration/metadata in the feature Router file.
5. Put HTTP-facing orchestration in the feature Handler file.
6. Put broker/WebSocket transport parsing and connection mechanics in the existing transport adapter
   location.
7. Put reusable business logic, authorization, state transitions, and operation sequencing in the
   feature Service.
8. Put persistence/query logic in the feature Repository.
9. Add or update shared ORM Models only when database schema/table mapping changes.
10. Preserve existing schema/DTO, middleware, dependency, enum, utility, configuration, messaging,
    Redis, WebSocket, and simulator placement; this skill does not invent locations that the project
    has not established.
11. Add/update Alembic migrations when model/schema changes require them.
12. Add tests at the relevant layer(s), including negative cases for tenant/RBAC/state-integrity
    rules.
13. For event-driven work, verify duplicate, stale/out-of-order, retry, acknowledgement, and
    disconnect behavior when relevant.
14. Review the final dependency direction and ensure no transport layer has absorbed business or
    persistence responsibilities.
