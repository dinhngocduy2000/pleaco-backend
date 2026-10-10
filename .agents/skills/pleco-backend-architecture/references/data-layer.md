<!-- Pleco-specific reference for the pleco-backend-architecture Codex skill. -->

> **Scope:** Pleco backend request orchestration, robot-event processing, business logic, realtime
> distribution, and persistence boundaries.  
> **Purpose:** Define the responsibilities of Router, Handler, Service, Repository, SQLAlchemy Model,
> and transport/realtime adapter code and prevent responsibility leakage across layers.  
> **Usage:** Project-specific reference documentation. Skill activation, precedence, and
> file-placement rules live in `../SKILL.md`.

# Pleco Backend Data Layer and Event Flow

## 1. Normal HTTP End-to-End Flow

For a normal HTTP feature:

```text
HTTP Request
    |
    v
Router
    |
    | binds route to Handler endpoint
    v
Handler
    |
    | authenticates/extracts request context
    | invokes business operation
    v
Service
    |
    | applies domain rules and coordinates persistence/infrastructure
    v
Repository
    |
    | executes SQLAlchemy/ORM data access
    v
SQLAlchemy Model(s)
    |
    v
PostgreSQL
```

Return flow:

```text
PostgreSQL / ORM result
    |
    v
Repository
    |
    v
Service
    |
    v
Handler
    |
    | wraps/formats HTTP-facing response
    v
Client
```

## 2. Robot Telemetry / Mapping Event Flow

Pleco receives robot telemetry asynchronously. A typical conceptual flow is:

```text
Robot / Python Simulator
      |
     MQTT
      |
      v
RabbitMQ / MQTT ingress
      |
      v
Consumer / Transport Adapter
      |
      | decode + transport validation
      | attach trace/message context
      v
Service
      |
      | tenant/robot/session validation
      | ordering/idempotency/state rules
      | domain processing
      +----------------------+
      |                      |
      v                      v
Repository              Realtime Publisher
      |                      |
      v                      v
PostgreSQL                 Redis
                              |
                              v
                        WebSocket nodes
                              |
                              v
                         React clients
```

This flow applies to telemetry such as:

- robot position
- heading/orientation
- speed/velocity
- battery
- connectivity/liveness signals
- task state/progress
- mapping coordinates
- mapping sequence numbers
- distance travelled
- command acknowledgements/execution events
- incidents/faults

Not every telemetry event must be persisted or published identically. Preserve the repository's
actual persistence and caching policy.

## 3. Robot Command Flow

A command may originate from an HTTP endpoint or another authorized application-facing transport.

```text
Operator request
      |
      v
Router/Handler or existing application adapter
      |
      v
Service
      |
      | authorize tenant/user/resource
      | validate robot state
      | create command identity/state
      +-------------------------+
      |                         |
      v                         v
Repository                Command Publisher
      |                         |
      v                         v
PostgreSQL               RabbitMQ / MQTT
                                |
                                v
                         Robot / Simulator
                                |
                         ACK / execution event
                                |
                                v
                      Consumer / Transport Adapter
                                |
                                v
                              Service
                                |
                      +---------+---------+
                      |                   |
                      v                   v
                  Repository       Redis/WebSocket
```

The Service owns command-domain decisions. The publisher owns broker/protocol mechanics.

## Router Layer

### Responsibility

The Router declares HTTP API routes and metadata and binds them to Handler endpoints.

### Router owns

- route path
- endpoint binding
- HTTP methods
- response model declaration
- declared success status
- endpoint summary/description
- documented response metadata/examples
- router registration metadata

### Router does not own

- business rules
- SQL/ORM queries
- database session operations
- telemetry processing
- RabbitMQ/MQTT connection logic
- Redis pub/sub mechanics
- complex authentication/authorization orchestration
- logging/tracing workflow for the endpoint
- response-building logic that belongs in Handler

## Handler Layer

### Responsibility

The Handler is the HTTP-facing orchestration layer between Routers and Services.

It handles HTTP-specific concerns, prepares application context, delegates business work to a
Service, and formats the HTTP-facing result.

### Handler owns

- FastAPI request inputs
- query/path/body dependency-bound inputs
- authentication dependencies
- request-derived tenant/user context
- cookies/redirects when the project uses them
- API response formatting/wrapping
- endpoint logging
- trace/context creation and propagation
- exception-to-HTTP mapping through the existing mechanism
- invoking one or more Services

### Handler should be thin

If a decision would still be required when the same use case is triggered by MQTT, RabbitMQ,
WebSocket, a scheduler, or a test harness, that decision belongs in the Service.

### Handler does not own

- SQLAlchemy queries
- direct database sessions/transactions
- repository query construction
- reusable business rules
- robot state-transition rules
- telemetry ordering/idempotency logic
- broker publishing internals

## Transport Consumer / Messaging Adapter

### Responsibility

A consumer or messaging adapter is the transport-facing entry point for asynchronous events.

It is analogous to a Handler at the transport boundary, but it is not an HTTP Handler.

### Consumer/adapter owns

- broker subscription/consumer mechanics
- topic/routing-key/queue binding according to existing topology
- message decoding/deserialization
- transport-level schema validation
- extracting message metadata
- acknowledgement/reject/requeue mechanics
- transport retry/error hooks where established
- trace/message context creation
- invoking the appropriate Service

### Consumer/adapter does not own

- SQLAlchemy queries
- reusable business/domain decisions
- tenant authorization policy beyond transport identity extraction
- robot/task/mapping state-machine logic
- deduplication policy when it is a domain invariant
- persistence transaction logic
- WebSocket presentation behavior

A consumer should be thin. It converts a transport event into an application/domain operation.

## Realtime / WebSocket Adapter

### Responsibility

WebSocket-facing code owns connection and delivery concerns.

### WebSocket/realtime adapter owns

- connection acceptance
- authentication handshake according to the existing design
- connection registry/room/subscription mechanics
- disconnect cleanup
- heartbeat/liveness behavior
- serialization for clients
- Redis pub/sub subscription and fan-out mechanics when applicable

### WebSocket/realtime adapter does not own

- durable business state
- direct SQLAlchemy queries
- task/robot/mapping business decisions
- tenant isolation rules that exist only in the client
- command state-machine logic

When a WebSocket interaction requests a domain operation, delegate the reusable behavior to a
Service.

## Service Layer

### Responsibility

The Service hosts Pleco business logic and reusable application workflows.

### Service owns

- domain/business rules
- operation-level validation
- tenant/resource ownership checks
- reusable authorization rules
- robot state transitions
- task state transitions
- mapping-session state transitions
- one-controller-at-a-time rules
- command lifecycle decisions
- command timeout/idempotency behavior
- telemetry ordering and stale-event rejection policy
- duplicate-event policy
- connectivity classification rules
- incident/recovery workflows
- cleaning trajectory business validation
- sequencing a use case
- coordinating Repositories
- invoking existing publisher/cache/realtime gateway abstractions when the use case requires them
- coordinating other Services where appropriate
- propagating application context according to existing conventions

### Service does not own

- FastAPI route registration
- `Request`-specific behavior
- cookies/redirects
- HTTP response envelopes/status formatting
- RabbitMQ connection/channel setup
- MQTT client lifecycle
- WebSocket connection registry internals
- SQLAlchemy query expressions
- direct ORM persistence

## Repository Layer

### Responsibility

The Repository owns SQL/database access through SQLAlchemy/ORM and is called by Services.

### Repository owns

- SQLAlchemy ORM queries
- `select`, `insert`, `update`, `delete`
- persistence filtering
- joins/relationship loading
- sorting
- data-layer pagination implementation
- database-specific query composition
- persistence operations using the existing session/transaction abstraction
- locking/concurrency query details when needed for correctness

### Repository API design

Repository methods should expose persistence operations in terms useful to Services without leaking
HTTP or broker concepts.

Conceptual examples:

```python
await robot_repository.get_by_id(robot_id)
await robot_repository.get_for_tenant(robot_id, tenant_id)
await mapping_repository.append_sample(...)
await task_repository.update_state(...)
await command_repository.get_by_command_id(command_id)
```

These are conceptual only. Use existing naming/session patterns.

### Repository does not own

- FastAPI `Request`
- `Depends`
- cookies/redirects
- HTTP response wrappers/status decisions
- authentication middleware
- RabbitMQ acknowledgements
- MQTT topic parsing
- WebSocket fan-out
- high-level domain workflow
- frontend presentation logic

## Models

### Responsibility

Models define database table schemas through SQLAlchemy ORM mappings.

Likely Pleco model areas may include entities such as tenants, users/roles, maps, robots, mapping
sessions/samples, cleaning tasks, trajectories, commands, incidents, fleets, and schedules as those
features are implemented.

Models may contain:

- `__tablename__`
- mapped columns
- SQL/PostgreSQL types
- primary keys
- unique/index/nullability constraints
- server/default/update values
- SQLAlchemy relationships

Do not move feature workflow or state-transition orchestration into model definitions merely to
avoid a Service.

## Tenant Isolation and RBAC

Pleco is multi-tenant. Tenant/resource boundaries are backend invariants.

At minimum:

- never trust a tenant/resource identifier merely because the frontend supplied it
- scope persistence access to the authenticated tenant according to existing project conventions
- prevent cross-tenant reads/writes
- enforce permission checks server-side
- apply resource-scoped permissions where the feature supports them
- verify that referenced robots/maps/tasks/fleets belong to the authorized scope
- do not rely only on UI hiding for access control

Authentication extraction is transport-facing. Reusable authorization decisions belong in the
Service. Database lookups required for authorization belong in Repositories called by the Service.

## Telemetry Ordering and Idempotency

Robot telemetry and acknowledgements may be duplicated, delayed, or arrive out of order.

When the event contract includes identifiers such as:

```text
message_id
command_id
sequence_number
timestamp
session_id
robot_id
tenant_id
```

preserve and use the repository's established ordering/idempotency strategy.

Do not let stale events overwrite newer robot state merely because they arrived later.

Do not implement deduplication only in a WebSocket client.

## Mapping Sessions

For Teach/Taylor Mode, business rules may include:

- robot belongs to current tenant
- robot is eligible for manual mapping
- only one active controller/session where required
- mapping session transitions are valid
- raw samples remain distinct from the final generated boundary
- sequence/order integrity is preserved
- interrupted sessions stop or release control safely according to the current design
- finishing/cancelling updates robot/session state consistently

These rules belong primarily in Services; persistence belongs in Repositories; transport commands
belong in publisher adapters.

Use whichever user-facing term (Teach Mode or Taylor Mode) the current Pleco repository has
standardized on.

## Commands and Safety

Robot commands may include movement, pause, resume, stop, emergency stop, return-to-dock, and other
operations as the product evolves.

Keep these concerns separate:

- Handler/transport adapter: receive authenticated request/event
- Service: authorize, validate robot/task state, create command state, apply safety/idempotency rules
- Repository: persist command/state/audit data
- Publisher: serialize and publish through RabbitMQ/MQTT
- Consumer: receive ACK/execution event and delegate
- Realtime adapter: notify clients

A successful broker publish is not equivalent to robot execution. Preserve command lifecycle states
when the implementation supports them.

## Redis and Realtime State

Redis may be used for caching, realtime distribution, presence/liveness, or transient state according
to the repository's actual implementation.

Do not automatically make Redis the source of truth for durable business state.

When correctness requires persistence across restart, use the established durable storage strategy.

## Context, Logging, and Tracing

Preserve existing application context/tracing patterns.

For distributed flows, propagate available identifiers such as:

- trace/correlation ID
- message/event ID
- command ID
- tenant ID
- robot ID
- task ID
- mapping session ID

Do not log secrets, credentials, tokens, protected environment values, or unnecessarily sensitive
payload data.

## Exception and Failure Handling

Use the project's existing exception/error mechanism.

Different boundaries may map failures differently:

- Handler -> HTTP response/error mapping
- RabbitMQ/MQTT consumer -> ack/reject/requeue/DLQ behavior according to topology
- WebSocket adapter -> connection/message-level error handling
- Service -> domain/application errors
- Repository -> persistence errors

Do not convert every failure into a generic HTTP exception inside Services because Services may be
used by non-HTTP entry points.

## Transaction Boundaries

Preserve the project's current transaction/session convention.

For multi-step state changes—especially command creation, mapping transitions, task transitions, and
incident workflows—ensure the established transaction mechanism preserves required atomicity.

Do not invent a new unit-of-work architecture without an explicit architectural task.

## Environment-File Constraint

Never read protected environment files to discover database URLs, broker credentials, Redis
passwords, MQTT credentials, JWT secrets, or other runtime values.

Use non-secret templates/source/configuration and variable names instead. The root `AGENTS.md`
security rule is authoritative.

## Layer Boundary Checklist

Before finishing a change, verify:

- [ ] Router contains route declaration/metadata, not business logic.
- [ ] Handler handles HTTP orchestration and remains thin.
- [ ] Consumer/transport adapters handle transport mechanics and remain thin.
- [ ] WebSocket/realtime adapters handle connection/fan-out mechanics, not durable business logic.
- [ ] Handler/consumer/realtime adapters do not query the database directly.
- [ ] Service contains reusable business rules and workflow.
- [ ] Tenant isolation and RBAC are enforced server-side.
- [ ] Robot/task/mapping/command state transitions live in the domain/service layer.
- [ ] Telemetry duplicate/stale/out-of-order behavior is handled deliberately.
- [ ] Service does not contain raw SQLAlchemy query construction.
- [ ] Repository owns ORM/database queries.
- [ ] Repository does not depend on HTTP, broker, or WebSocket concerns.
- [ ] Models define ORM table structure and relationships.
- [ ] HTTP direction remains Router -> Handler -> Service -> Repository -> Model/DB.
- [ ] Event direction remains Consumer/Adapter -> Service -> Repository -> Model/DB.
- [ ] Outbound broker mechanics are isolated behind the existing publisher/gateway abstraction.
- [ ] Existing context/logging/auth/exception abstractions are reused.
- [ ] Database schema changes include the appropriate Alembic migration.
- [ ] Protected environment files were not read or indirectly inspected.
