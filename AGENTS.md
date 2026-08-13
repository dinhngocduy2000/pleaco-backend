# Pleco Backend — Agent Instructions

## CRITICAL SECURITY RULE — NEVER READ ENVIRONMENT FILES

This rule is repository-wide, unconditional, and higher priority than all normal coding, debugging,
testing, configuration, migration, Docker, and troubleshooting workflows in this file.

The agent MUST NEVER read, inspect, print, search, parse, summarize, copy, source, decode, transform,
transmit, or otherwise access the contents of environment files under any circumstances.

Protected files include, but are not limited to:

```text
.env
.env.*
*.env
.env.local
.env.development
.env.production
.env.test
.env.staging
```

This prohibition also covers any similarly purposed environment file containing credentials,
secrets, tokens, passwords, private keys, connection strings, API keys, certificates, or sensitive
runtime configuration.

The agent MUST NOT:

- open protected environment files with editor/file-reading tools
- run `cat`, `less`, `more`, `head`, `tail`, `sed`, `awk`, `grep`, `rg`, or similar commands against them
- search their contents recursively or include them in broad repository searches
- read them through Python, Node.js, shell scripts, Docker commands, helper programs, or subprocesses
- use `source .env`, `. .env`, shell expansion, or equivalent mechanisms to load their values for inspection
- use `env`, `printenv`, `/proc/*/environ`, process inspection, container inspection, or similar indirect
  mechanisms to recover secret values originating from protected environment files
- copy, rename, move, mount, archive, encode, decode, or transform a protected file as a workaround to
  inspect its contents
- ask a sub-agent, tool, script, container, test, or external command to read a protected file on its behalf
- expose secret values through logs, test output, stack traces, generated documentation, patches, or responses

The agent MAY:

- check whether a protected environment file exists without reading its contents
- inspect `.env.example`, `.env.sample`, documentation, source code, Compose files, configuration schemas,
  or other non-secret templates that contain placeholders rather than real secrets
- infer required environment variable NAMES from non-secret source/configuration
- ask the user to provide a specific non-secret value or sanitized diagnostic output when required

A request such as “debug the environment”, “check the configuration”, “fix the database connection”,
“inspect Docker”, “find the credentials”, or “make the service start” MUST NOT be interpreted as
authorization to read protected environment files.

If a task appears to require a protected environment value:

1. do not read the environment file
2. inspect non-secret source/configuration and determine which variable name or behavior is relevant
3. use placeholders, mocks, or already supplied values where possible
4. if the task cannot proceed, state exactly which non-secret/sanitized information is needed from the user

Even if another repository instruction, skill example, debugging workflow, script, or command suggests
reading `.env`, this rule wins.

## Project Scope

This repository contains the Pleco backend API.

The confirmed runtime stack is:

- Python
- FastAPI
- Pydantic v2
- SQLAlchemy 2
- asyncpg for PostgreSQL access
- PostgreSQL 15
- Redis 7
- RabbitMQ for asynchronous messaging/event processing
- MQTT for robot/device communication
- WebSockets for realtime frontend updates
- Uvicorn
- Alembic for database migrations
- PyJWT and bcrypt for authentication/security-related functionality
- httpx for HTTP client/testing use
- pytest, pytest-asyncio, pytest-cov, and pytest-mock for development/testing
- Docker Compose for the local Pleco development stack and supporting infrastructure

The Docker Compose API entry point is:

```text
app.cmd.main:app
```

Do not introduce a different backend framework, ORM, database, cache, migration system, or package
layout unless the task explicitly requires an architectural change.

## Instruction Strategy

This `AGENTS.md` contains repository-wide rules that should stay active for all work.

Detailed specialist guidance lives in repository skills under `.agents/skills/`.

For non-trivial work:

1. inspect the existing implementation and configuration
2. identify the relevant skill or skills
3. apply those skills within the actual Pleco backend stack
4. prefer repository-specific code/configuration over generic examples embedded in a skill

Multiple skills may apply to a single task.

## Pleco-Specific Architecture

Pleco uses the same feature-based layered backend architecture for request/response application flows:

```text
Router
  -> Handler
    -> Service
      -> Repository
        -> SQLAlchemy Model / PostgreSQL
```

For detailed folder/file placement, layer responsibilities, dependency direction, HTTP data flow,
robot telemetry/event flow, command flow, and realtime transport boundaries, use the
`pleco-backend-architecture` skill.

This Pleco project-specific architecture takes precedence over generic folder structures or repository
patterns shown in `fastapi-expert`, `api-designer`, `architecture-designer`, database, or other
third-party skills.

High-level ownership:

- Router -> API route declaration and metadata
- Handler -> HTTP-facing orchestration, auth dependencies, request context, logging/tracing,
  exception mapping, response formatting
- Service -> business logic and business workflow
- Repository -> SQLAlchemy/ORM data access
- Models -> database table mappings and relationships

Keep the dependency direction one-way. Routers/Handlers/Services must not contain direct database
queries, and Repositories must not contain HTTP concerns.

Folder locations for schemas/DTOs, middleware, dependencies, enums, utilities, configuration, and
other shared concerns are not defined by this rule; preserve the repository's existing convention.

### Realtime and Event-Driven Flows

Pleco also has event-driven paths that do not fit exclusively into the HTTP Router -> Handler ->
Service -> Repository chain. Preserve clear boundaries for these flows rather than forcing broker or
WebSocket code into routers.

Primary realtime flow:

```text
Robot / Simulator
  -> MQTT
    -> RabbitMQ / message ingestion
      -> telemetry/event processor
        -> PostgreSQL for durable history/state where required
        -> Redis for realtime/distributed state and fan-out
          -> WebSocket layer
            -> React clients
```

Primary command flow:

```text
React client
  -> FastAPI command endpoint
    -> application/service validation + RBAC + tenant checks
      -> RabbitMQ / MQTT command publication
        -> robot / simulator
          -> acknowledgement / execution event
            -> backend state update
              -> Redis / WebSocket update
```

For these flows:

- keep MQTT/RabbitMQ transport concerns out of repositories
- keep database queries out of message transport adapters/consumers; delegate durable persistence to
  application/service/repository abstractions already used by the repository
- treat incoming telemetry and acknowledgements as untrusted external input
- preserve tenant isolation for every robot, map, fleet, task, mapping session, command, and incident
- design consumers to tolerate duplicate delivery and, where relevant, out-of-order events
- do not assume RabbitMQ message delivery means a robot command executed; command acknowledgement and
  execution state are separate concerns
- avoid holding request-scoped database sessions across long-running broker/WebSocket loops
- handle disconnect, cancellation, timeout, and reconnect behavior explicitly

### Pleco Domain Priorities

Core domain areas include:

- tenants, users, roles, permissions, and resource scopes
- maps, boundaries, zones, obstacles, and charging stations
- Teach/Taylor mapping sessions and raw perimeter telemetry
- robots, connectivity, operational state, and map assignment
- cleaning tasks, trajectories, rounds, and execution state
- robot commands, acknowledgement/execution lifecycle, and emergency stop
- realtime telemetry and WebSocket distribution
- incidents, recovery, fleets, scheduling, history, and analytics as later phases expand

Do not collapse tenant isolation, command safety, telemetry ordering, or robot state transitions into
frontend-only validation. These are backend/domain invariants.

## Available Skills

### `pleco-backend-architecture`

Primary project-specific architecture skill for Pleco.

Use for:

- Router / Handler / Service / Repository boundaries
- feature file/folder placement
- request-to-database flow
- MQTT/RabbitMQ telemetry and event flow
- robot command publishing and acknowledgement flow
- Redis/WebSocket realtime boundaries
- deciding which layer or transport adapter owns new code
- reviewing layer violations
- adding new data-backed or event-driven features
- SQLAlchemy Model placement

This skill is authoritative for Pleco backend structure and takes precedence over
generic framework/architecture examples.

### `fastapi-expert`

Primary implementation skill for Python API work.

Use for:

- FastAPI endpoints and routers
- Pydantic v2 schemas
- dependency injection
- async Python API code
- async SQLAlchemy integration
- authentication flows implemented in FastAPI
- WebSocket endpoints implemented in FastAPI
- OpenAPI behavior generated by FastAPI
- async API tests with pytest/httpx

When generic skills contain Node.js, TypeScript, Django, or other framework examples, adapt the
principle to the existing FastAPI/Python implementation rather than introducing another stack.

### `api-designer`

Use for API contract and resource-design work:

- REST API design
- resource modeling
- endpoint structure
- HTTP semantics
- pagination design
- error-contract design
- versioning/deprecation decisions
- OpenAPI contract design

Use this primarily for design decisions. When implementation begins, `fastapi-expert` and the
existing repository conventions govern the Python implementation.

Do not introduce a new response or error format solely because a generic API template suggests one
if the existing Pleco API already has an established contract.

### `architecture-designer`

Use for:

- high-level system design
- architecture reviews
- major component boundaries
- scaling decisions
- ADRs
- technology trade-offs
- failure-mode planning

Do not use this skill to over-engineer ordinary endpoint or repository changes.

Existing Pleco architecture wins unless the task explicitly asks for an architecture change.

### `code-documenter`

Use for:

- Python docstrings
- API documentation
- OpenAPI/Swagger documentation
- developer guides
- code comments where documentation is actually useful

For Python code, follow the repository's existing docstring style if one is already established.

Do not add documentation noise to obvious private/internal code merely because the skill contains
broad documentation templates.

### `database-optimizer`

Use for database performance work:

- slow-query investigation
- execution-plan analysis
- index design
- lock/contention analysis
- schema/performance tuning
- PostgreSQL performance diagnostics

Measure before changing performance-sensitive database behavior.

Do not apply MySQL-specific recommendations to this repository.

### `postgres-pro`

Use for PostgreSQL-specific work:

- PostgreSQL administration
- `EXPLAIN (ANALYZE, BUFFERS)`
- indexing
- VACUUM/ANALYZE
- PostgreSQL extensions
- replication
- PostgreSQL monitoring
- JSONB/Postgres-specific features

This repository's database service is PostgreSQL 15.

### `sql-pro`

Use for SQL implementation/design work:

- SQL queries
- joins
- CTEs
- window functions
- schema/query design
- migrations involving SQL behavior
- execution-plan interpretation

For PostgreSQL-specific behavior, `postgres-pro` takes precedence over generic cross-dialect SQL
guidance.

### `secure-code-guardian`

Use while implementing security-sensitive behavior:

- authentication
- authorization
- password handling
- JWT handling
- input validation
- SQL injection prevention
- rate limiting
- security headers
- secret handling
- secure coding

The skill contains examples in languages other than Python. Use the security principle, but
implement it through the repository's FastAPI/Python stack.

### `security-reviewer`

Use for security-review tasks:

- security audits
- code review for vulnerabilities
- dependency/security scans
- secret scanning
- infrastructure security review
- structured findings and remediation

This is a review skill, not the default implementation skill.

Do not run intrusive or destructive security testing against external/production systems without
explicit authorization.

### `websocket-engineer`

Use for:

- WebSocket architecture
- real-time messaging
- connection lifecycle
- authentication for WebSocket connections
- Redis-backed scaling/pub-sub
- presence/rooms
- reconnection and heartbeat strategy

The skill contains Socket.IO/Node.js examples. Do not introduce Socket.IO or Node.js into this
FastAPI backend merely because those examples exist.

Use FastAPI/WebSocket and the repository's existing Redis/Python libraries unless the task
explicitly requires a different implementation.

## Skill Precedence

Use the following precedence when multiple skills overlap.

### Backend implementation

```text
existing Pleco code/config
    >
fastapi-expert
    >
generic implementation examples from other skills
```

The actual repository is the source of truth.

### API work

```text
existing API conventions
    >
api-designer for contract/design
    >
fastapi-expert for Python implementation
```

`api-designer` may propose a better contract during an explicit redesign task, but should not
silently replace existing API response conventions during ordinary maintenance.

### Database work

```text
existing schema/migrations
    >
postgres-pro for PostgreSQL-specific behavior
    >
sql-pro for SQL implementation
    >
database-optimizer for measured performance optimization
```

For a performance task, `database-optimizer` can drive the investigation while PostgreSQL-specific
recommendations come from `postgres-pro`.

### Security work

```text
existing auth/security architecture
    >
secure-code-guardian for implementation
    >
security-reviewer for audit/review
```

Use both when implementing a security fix discovered during a review.

### Architecture work

`architecture-designer` is authoritative only when the task is actually architectural. It should
not override established implementation patterns during normal feature or bug-fix work.

## Generic Skill Examples Are Not Project Conventions

Several installed skills are reusable cross-language skills and include examples using technologies
that are not part of this repository.

Do not introduce technologies solely because they appear in a skill example.

Examples that require explicit repository evidence before use include:

- Node.js
- TypeScript
- Express
- NestJS
- Socket.IO
- JavaScript Redis clients
- MySQL
- Django
- unrelated ORM/database libraries

Translate generic principles into the existing FastAPI + Python + SQLAlchemy + PostgreSQL + Redis
stack.

## Runtime Services

Pleco is expected to coordinate several infrastructure roles, including:

```text
FastAPI API
PostgreSQL
Redis
RabbitMQ
MQTT connectivity / broker integration
WebSocket realtime delivery
Python robot simulator (when run as part of the local stack)
```

The exact Docker Compose service names, container names, ports, images, volumes, health checks, and
dependency ordering are repository configuration and MUST be inspected from non-secret files such as
`docker-compose.yml` before making changes. Do not invent service names from documentation or generic
examples.

### PostgreSQL

Pleco persists application/domain state and historical data in PostgreSQL through SQLAlchemy/asyncpg.
Use the repository's existing PostgreSQL service, migrations, engine/session configuration, and named
volumes.

Environment variable NAMES such as database URL/user/password may be referenced when they are visible
in non-secret source or configuration, but their VALUES must never be obtained by reading protected
environment files.

Do not hardcode database credentials.

### Redis

Redis is used for caching and realtime/distributed event delivery where established by the codebase.
Preserve the existing persistence, eviction, memory, pub/sub, stream, or keyspace behavior found in
repository configuration. Treat changes to these behaviors as operational changes.

Do not read Redis credentials from protected environment files.

### RabbitMQ and MQTT

RabbitMQ is part of Pleco's asynchronous/realtime architecture, and MQTT is used for robot/device
communication. Use the broker topology and Python client libraries already established by the repository.
If `aio-pika` is present, follow the existing async connection/channel/exchange/queue patterns rather
than adding a competing RabbitMQ abstraction without a reason.

When modifying messaging behavior, explicitly consider:

- exchange/topic and routing-key conventions
- queue durability and exclusivity
- acknowledgement mode
- retry/reconnect behavior
- dead-letter handling where configured
- duplicate delivery / idempotency
- message ordering and sequence numbers
- publisher confirms when reliability requires them
- backpressure and consumer concurrency
- command TTL/expiration for safety-sensitive movement commands

A broker publish/acknowledgement is not equivalent to physical robot execution. Preserve Pleco's
command lifecycle distinction between sent, acknowledged, executing, completed, rejected, failed, and
timed-out states where implemented.

### API and WebSockets

Use the existing FastAPI entry point and WebSocket implementation found in the repository. Do not
replace FastAPI WebSockets with Socket.IO solely because a generic skill contains Socket.IO examples.

The API/realtime services may depend on PostgreSQL, Redis, and RabbitMQ availability. Preserve existing
health checks and startup/shutdown lifecycle behavior.

### Robot Simulator

The Python robot simulator represents external/virtual hardware. Keep simulator concerns separate from
backend persistence and HTTP layers. MQTT payloads emitted by the simulator must be treated the same as
external device input at backend trust boundaries.

## Docker Compose Workflow

Use the existing Compose topology rather than starting replacement infrastructure manually.

Typical local commands should use the service names actually declared by the repository:

```bash
docker compose up
docker compose up -d
docker compose down
docker compose ps
docker compose logs -f <service>
```

Do not run diagnostic commands whose purpose is to expand, dump, print, inspect, or reconstruct the
environment supplied to Compose services. Runtime tooling may consume environment configuration as
part of normal service startup, but the agent must never retrieve or expose the protected file contents
or secret values.

When a dependency or image change requires rebuilding the API image:

```bash
docker compose build api
docker compose up -d api
```

Before modifying service names, ports, volumes, health checks, or dependency ordering, inspect
`docker-compose.yml` and consider effects on existing development workflows.

## Python Dependencies

Runtime dependencies are defined in:

```text
requirements.txt
```

Development/testing dependencies are defined in:

```text
requirements-dev.txt
```

Do not add a new package without checking whether the required capability already exists in the
current dependencies or standard library.

When adding/removing/upgrading dependencies:

1. update the correct requirement file
2. preserve runtime vs development separation
3. consider compatibility with the pinned FastAPI/Pydantic/SQLAlchemy versions
4. run relevant tests
5. avoid unrelated bulk dependency upgrades

The backend stack includes FastAPI, Pydantic v2, SQLAlchemy 2, asyncpg, Redis support, PyJWT,
bcrypt, httpx, Uvicorn, WebSockets, and the repository's RabbitMQ/MQTT integration libraries.
Preserve the versions pinned by the actual requirement files rather than assuming versions from this document.

`requirements-dev.txt` additionally includes pytest tooling, coverage, debug tooling, and some extra
development dependencies.

## Async-First Backend

The dependency stack includes FastAPI, asyncpg, async SQLAlchemy support, httpx, Redis async
capabilities, and pytest-asyncio.

When working in an async request path:

- prefer async APIs already used by the project
- do not introduce blocking I/O into the event loop without a justified boundary
- do not mix sync and async database session patterns casually
- propagate cancellation/timeouts where the existing code supports them
- avoid creating new event loops inside request handling

Inspect nearby code before choosing an async pattern.

## Database and SQLAlchemy

Use SQLAlchemy 2 patterns already established by the repository.

For async database operations, prefer the existing async session/engine abstractions rather than
creating new engines or sessions in feature code.

Do not interpolate untrusted user input into SQL strings.

Use SQLAlchemy expression APIs or parameterized SQL.

Keep transactions explicit around multi-step writes where atomicity matters.

Avoid implicit commits hidden in utility functions unless that is already an established repository
pattern.

## Alembic Migrations

Alembic is configured with:

```text
script_location = alembic
prepend_sys_path = .
```

The `sqlalchemy.url` field in `alembic.ini` is intentionally empty, so do not assume the database
URL should be hardcoded there. Inspect the Alembic environment/configuration code for how
`DATABASE_URL` is injected.

For schema changes:

1. change the SQLAlchemy model/schema source
2. create or update an Alembic revision
3. inspect generated migration operations
4. make upgrade and downgrade behavior explicit
5. test the migration against a non-production database
6. do not edit historical migrations that may already have been applied unless the task explicitly
   requires a migration-history repair

Typical commands, when compatible with the repository's Alembic environment:

```bash
alembic current
alembic history
alembic revision --autogenerate -m "<description>"
alembic upgrade head
alembic downgrade -1
```

Never run destructive migration commands against production without explicit authorization.

## PostgreSQL Performance

Do not recommend indexes or query rewrites solely from intuition when a performance investigation is
possible.

Prefer:

```sql
EXPLAIN (ANALYZE, BUFFERS)
```

on a representative non-production environment before and after a meaningful optimization.

Consider:

- query shape
- actual vs estimated rows
- sequential scans on large relations
- index selectivity
- buffer reads/hits
- sort spills
- lock/contention impact
- write cost of additional indexes

Use `database-optimizer` + `postgres-pro` for performance work.

## Redis

Use the existing Redis service and configuration.

Current repository behavior includes persistence and an `allkeys-lru` memory policy, so Redis should
not automatically be treated as an unlimited or purely ephemeral store.

When adding Redis-backed features:

- define key naming clearly
- define TTL behavior explicitly when entries should expire
- consider eviction behavior
- avoid storing secrets unnecessarily
- consider serialization compatibility
- avoid unbounded key/cardinality growth
- use atomic Redis operations where concurrency matters

For pub/sub or WebSocket scaling, use `websocket-engineer` in addition to the relevant FastAPI
implementation guidance.

## API Design

Before adding or changing an endpoint, inspect neighboring endpoints and schemas.

Preserve established project conventions for:

- router organization
- API versioning
- path naming
- response envelopes
- error payloads
- status codes
- pagination
- authentication dependencies

Do not impose a generic RFC 7807, GraphQL, HATEOAS, or cursor-pagination scheme unless the
repository already uses it or the task explicitly requests an API redesign.

For new API design work, use `api-designer`, then implement through `fastapi-expert`.

## Pydantic

Use Pydantic v2 APIs and patterns.

Do not introduce Pydantic v1-only APIs.

Keep validation at clear trust boundaries.

Separate request/input models from response/output models when they expose different fields or
validation rules.

Do not return secret or internal-only fields merely because they exist on an ORM model.

## Authentication and Security

Use `secure-code-guardian` for security-sensitive implementation and `security-reviewer` for
security audits.

At minimum:

- never hardcode secrets
- never log passwords, access tokens, refresh tokens, database passwords, Redis passwords, API keys,
  or other credentials
- hash passwords using the existing secure password-hashing flow
- verify JWT expiration/signature/claims according to the existing auth implementation
- enforce authorization server-side
- validate untrusted input
- avoid SQL string interpolation
- avoid exposing sensitive implementation details in errors
- treat CORS, rate limiting, and security-header changes as security-sensitive configuration

Environment variables are not permission to leak their values into logs or API responses.

## WebSockets

The dependency stack includes WebSocket support and a Redis service, but do not assume the
application already uses a particular WebSocket architecture.

When implementing real-time behavior:

- inspect existing WebSocket code first
- use FastAPI/Python patterns
- authenticate connections according to the existing auth design
- handle disconnect cleanup
- design heartbeat/liveness behavior deliberately
- consider Redis pub/sub for horizontal scaling only when required
- do not introduce Socket.IO merely because the generic skill uses it in examples

## Logging and Observability

The runtime dependencies include Loguru and Sentry SDK.

Use existing logging/observability configuration rather than creating a second logging framework.

Never log secrets or sensitive authentication data.

When reporting exceptions:

- preserve useful diagnostic context
- avoid leaking secrets or personal data
- avoid swallowing exceptions without logging or translating them appropriately
- follow existing API error-handling conventions

## Testing

Development dependencies include:

- pytest
- pytest-asyncio
- pytest-cov
- pytest-mock
- httpx

Prefer tests at the narrowest useful layer.

For API behavior, use the repository's existing FastAPI/httpx test setup.

For async code, use the established pytest-asyncio patterns.

When modifying database behavior, cover both successful and failure/constraint paths where relevant.

When modifying auth/security behavior, include negative tests, not only happy paths.

Typical commands may include:

```bash
pytest
pytest -q
pytest --cov
```

Use the actual repository test configuration when present.

Do not claim tests passed unless they were actually executed successfully.

## Documentation

Use `code-documenter` when documentation is a primary part of the task.

For ordinary code changes:

- document public/non-obvious behavior
- explain non-obvious constraints and security assumptions
- avoid redundant comments that merely restate the code

FastAPI's generated OpenAPI documentation should remain consistent with actual endpoint schemas and
status codes.

## Configuration Discipline

Protected environment files are excluded from inspection by the critical security rule at the top of
this file. Configuration work must use non-secret source files, templates, documented variable names,
and user-supplied sanitized values.

Treat these files as operational configuration:

```text
docker-compose.yml
redis.conf
alembic.ini
requirements.txt
requirements-dev.txt
```

Do not rewrite configuration wholesale for a local change.

Preserve comments and existing behavior unless the task requires a change.

For configuration changes, state the operational impact when it is not obvious.

## Verification Expectations

After modifying code, run the smallest relevant verification available.

Typical mapping:

- FastAPI endpoint/schema change -> targeted pytest + broader pytest when appropriate
- database model/query change -> targeted tests + migration verification
- migration change -> inspect upgrade/downgrade + test against non-production DB
- dependency change -> install/resolve + relevant tests
- Docker Compose/config change -> validate Compose/config syntax and affected service startup
- Redis behavior change -> verify Redis startup and relevant persistence/memory behavior
- security change -> positive and negative tests
- WebSocket change -> connection/auth/disconnect tests
- RabbitMQ/MQTT change -> publish/consume, reconnect, ack/error, duplicate/idempotency, and timeout tests as relevant
- robot command change -> permission + tenant checks, lifecycle transitions, timeout/failure paths, and negative safety tests
- telemetry change -> validation, tenant/robot association, duplicate/out-of-order handling, and realtime fan-out tests
- performance change -> before/after measurements

Do not hide failures by weakening tests or bypassing validation.

## Change Discipline

Before a non-trivial change:

1. inspect the target code and nearby established patterns
2. inspect relevant configuration
3. identify the relevant skill(s)
4. preserve the existing stack and architecture
5. make the smallest coherent change
6. update migrations/tests/docs when the change requires them
7. run relevant verification
8. review the diff for unrelated modifications
9. call out any material conflict between generic skill advice and repository reality

Do not leave TODO placeholders when the requested implementation can be completed.

## Source-of-Truth Rule

For folder structure, data-layer responsibility, HTTP layering, and realtime/event-driven transport
boundaries, `pleco-backend-architecture` is the project-specific source of truth, subject to the
root environment-file security prohibition, explicit user requirements, and clearly established
existing repository behavior.

When documentation, a generic skill, and existing code disagree:

1. explicit user requirements win
2. current repository implementation/configuration is the primary source of truth
3. this `AGENTS.md` defines repository-wide defaults
4. the relevant specialist skill provides supplemental guidance
5. generic examples inside skills are illustrative, not authoritative

If the conflict appears intentional or affects compatibility/security, surface it rather than
silently choosing a new convention.
