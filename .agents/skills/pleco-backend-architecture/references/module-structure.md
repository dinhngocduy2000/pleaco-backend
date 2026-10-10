<!-- Pleco-specific reference for the pleco-backend-architecture Codex skill. -->

> **Scope:** Pleco backend feature structure and file placement.  
> **Purpose:** Define the project's feature-based Router, Handler, Service, Repository, Models, and
> transport-adapter organization while preserving dependency direction and ownership boundaries.  
> **Usage:** Project-specific reference documentation. Skill activation, precedence, and cross-skill
> behavior live in `../SKILL.md`.

# Pleco Backend Module Structure

## Architectural Style

Pleco preserves the existing feature-based layered architecture for normal HTTP application flows:

```text
Router
  -> Handler
    -> Service
      -> Repository
        -> SQLAlchemy Model / PostgreSQL
```

Pleco also has asynchronous/realtime entry and exit points:

```text
MQTT/RabbitMQ Consumer
        |
        v
      Service
        |
        +--> Repository -> Model / PostgreSQL
        |
        +--> Redis / Realtime Publisher -> WebSocket clients
```

and outbound robot commands:

```text
Handler / Application Adapter
        |
        v
      Service
        |
        +--> Repository
        |
        +--> Command Publisher -> RabbitMQ / MQTT -> Robot
```

These diagrams define responsibility and dependency direction, not mandatory folder names.

## Preserve Existing Folder Structure

The user has established that Pleco uses the same backend folder structure and skill layout as the
previous project.

Router, Handler, Service, and Repository folders remain organized by feature.

Conceptually:

```text
app/
├── <router-folder>/
│   ├── robot.py
│   ├── map.py
│   ├── cleaning_task.py
│   └── <feature>.py
│
├── <handler-folder>/
│   ├── robot.py
│   ├── map.py
│   ├── cleaning_task.py
│   └── <feature>.py
│
├── <service-folder>/
│   ├── robot.py
│   ├── map.py
│   ├── cleaning_task.py
│   └── <feature>.py
│
├── <repository-folder>/
│   ├── robot.py
│   ├── map.py
│   ├── cleaning_task.py
│   └── <feature>.py
│
└── models/
    ├── tenant.py
    ├── robot.py
    ├── map.py
    ├── cleaning_task.py
    └── <table-or-domain-model>.py
```

These names are examples of Pleco features, not instructions to create these exact files.

Use the repository's actual existing directory names and naming convention. Do not create new
plural/singular folder variants from this reference.

## Transport and Realtime Code Placement

Pleco requires code for concerns that do not naturally belong to Router/Handler/Repository layers,
including:

- RabbitMQ consumers
- RabbitMQ publishers
- MQTT integration
- telemetry decoding
- command publishing
- Redis pub/sub
- WebSocket connection/fan-out
- simulator communication
- scheduled/internal workers as later features require them

This reference intentionally does not invent canonical top-level folder names for these concerns.

Before adding such code:

1. inspect the repository for an established messaging/infrastructure/realtime location
2. reuse that location and naming convention
3. keep transport code thin
4. delegate reusable business behavior to Services
5. keep persistence in Repositories

If no convention exists and the task requires introducing one, make it an explicit architecture
decision rather than silently copying a generic skill's folder structure.

## File Placement Rules

### Router file

A feature Router file owns route declaration and API metadata.

Examples:

- path
- HTTP method
- endpoint/Handler method binding
- response model declaration
- HTTP success status
- summary/description
- documented response examples/statuses
- router registration metadata

A Router should not contain:

- business logic
- database access
- SQLAlchemy queries
- broker connection logic
- telemetry processing
- persistence logic

### Handler file

A feature Handler file owns HTTP-facing orchestration.

Examples:

- `Request`
- parsed query/path/body inputs
- `Depends(...)`
- authentication dependencies
- tenant/user request context
- cookies/redirects when used
- HTTP-specific response formatting
- endpoint logging
- tracing/application context creation
- exception-to-HTTP mapping
- calling one or more Services

A Handler should be thin.

It should not contain:

- complex business rules
- robot/task/mapping state machines
- direct database queries
- SQLAlchemy persistence logic
- RabbitMQ/MQTT implementation details

### Service file

A feature Service file owns reusable business/application logic.

Examples:

- domain/business rules
- business validation beyond transport parsing
- tenant/resource ownership checks
- RBAC/resource-scope decisions
- robot state transitions
- mapping-session workflows
- cleaning-task state transitions
- command lifecycle and safety rules
- telemetry ordering/idempotency decisions
- incident/recovery workflows
- operation sequencing
- coordinating one or more Repositories
- coordinating other Services
- invoking existing infrastructure gateways/publishers

A Service should not own:

- FastAPI route declarations
- raw `Request`/cookie/redirect concerns
- HTTP response formatting
- direct SQL/ORM queries
- broker connection/channel lifecycle
- WebSocket connection registry internals

### Repository file

A feature Repository file owns data access.

Examples:

- SQLAlchemy `select`, `insert`, `update`, `delete`
- ORM persistence
- tenant-scoped filtering
- database filtering/sorting/pagination
- loading entities and relationships
- query composition
- persistence-specific locking
- transaction operations according to the existing session pattern

A Repository should not own:

- HTTP concerns
- FastAPI dependencies
- response formatting
- RabbitMQ acknowledgement/requeue
- MQTT topic handling
- Redis/WebSocket fan-out
- high-level business workflow
- command/task/robot state-machine decisions

### Models folder

The Models folder owns database table/ORM schema definitions.

A model can define:

- `__tablename__`
- mapped columns
- SQL/PostgreSQL types
- primary keys
- unique/index/nullability constraints
- server/default/update values
- SQLAlchemy relationships

Models define persistence structure. They should not become API Handlers, Services, message
consumers, or Repositories.

### Messaging Consumer / Adapter

Use the repository's existing messaging location.

Consumer/adapter code owns:

- subscription/binding setup according to existing abstractions
- message decode/deserialization
- transport-level validation
- message metadata extraction
- ack/reject/requeue mechanics
- invoking Services

It should not own direct database queries or reusable domain rules.

### Publisher / Gateway

Use the repository's existing infrastructure/messaging location.

Publisher/gateway code owns:

- transport serialization
- routing/topic/exchange selection according to established topology
- broker publish mechanics
- transport-specific timeout/error handling

It should not decide whether a command is authorized, whether a robot is in a valid state, or what a
task transition means. Those decisions belong in Services.

### WebSocket / Realtime Adapter

Use the repository's existing realtime location.

It may own:

- connection lifecycle
- connection authentication plumbing
- room/subscription organization
- heartbeat
- disconnect cleanup
- Redis subscription/fan-out
- client serialization

It should delegate domain operations to Services and should not query SQLAlchemy directly.

## Dependency Direction

Allowed normal HTTP direction:

```text
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
Model / Database
```

Allowed event entry direction:

```text
Consumer / Transport Adapter
  |
  v
Service
  |
  v
Repository
  |
  v
Model / Database
```

Allowed outbound infrastructure dependency:

```text
Service
  |
  v
Existing Publisher / Gateway Interface
  |
  v
RabbitMQ / MQTT / Redis infrastructure
```

Avoid reverse dependencies:

```text
Repository -> Service
Service    -> Handler
Handler    -> Router
Model      -> Repository
Publisher  -> Service business decisions
```

Avoid bypasses:

```text
Router   -> Repository          # avoid
Handler  -> Repository          # avoid direct DB access
Consumer -> Repository          # avoid direct DB access
Router   -> Service             # avoid for standard HTTP feature flow
Service  -> raw HTTP Request    # avoid
Service  -> broker connection   # avoid
```

A Service may coordinate multiple Repositories and infrastructure gateways when a business operation
spans multiple concerns.

A Handler may coordinate more than one Service when HTTP orchestration genuinely requires it, but
prefer reusable workflow inside a Service.

## Pleco Feature Domains

As features are implemented, likely modules include:

- tenant/auth/RBAC
- robot management
- map management
- manual map creation
- Teach/Taylor mapping
- environment obstacles/zones/charging stations
- cleaning tasks
- trajectory generation/validation
- telemetry
- robot commands
- realtime monitoring
- incidents/recovery
- fleets
- scheduling
- history/playback
- analytics
- maintenance
- multi-robot coordination

Do not create all of these eagerly. Add only the modules required by the current implementation.

## Adding a New HTTP Feature

For a normal data-backed HTTP feature, evaluate/create:

```text
<feature> Router
    |
<feature> Handler
    |
<feature> Service
    |
<feature> Repository
    |
existing/new Model(s)
```

Not every change requires a new Model.

Not every feature requires every file if it genuinely has no corresponding responsibility, but do
not collapse layers merely because the initial implementation is small.

## Adding a New Robot/Event Feature

For a robot-originated or broker-originated feature, evaluate:

```text
existing Consumer / Adapter
    |
<feature> Service
    |
<feature> Repository
    |
existing/new Model(s)
```

plus, when live client updates are required:

```text
<feature> Service
    |
existing Redis / Realtime Publisher
    |
WebSocket delivery
```

Do not make the consumer itself the business layer.

## Adding a New Command Feature

Evaluate:

```text
Router/Handler
    |
<feature> Service
    |            \
    v             v
Repository      existing Command Publisher
                  |
                  v
             RabbitMQ / MQTT
```

Acknowledgement/execution events re-enter through the existing consumer/adapter path and delegate to
a Service.

## Shared Concerns Not Defined Here

This reference does not define canonical locations for:

- Pydantic request/response schemas
- transport event schemas
- query DTOs
- enums
- constants
- middleware
- authentication implementation
- dependencies
- exceptions
- logging infrastructure
- tracing/context types
- configuration/settings
- Redis client abstractions
- broker connection factories
- WebSocket managers
- utilities
- shared response types

Inspect and preserve the current Pleco repository convention.

Do not create new top-level folders solely from generic FastAPI, RabbitMQ, MQTT, or WebSocket
examples.

## Environment Files

Do not inspect protected environment files to decide code placement or configuration.

Use source code, templates, documented environment-variable names, and sanitized configuration.
The root `AGENTS.md` security rule remains authoritative.
