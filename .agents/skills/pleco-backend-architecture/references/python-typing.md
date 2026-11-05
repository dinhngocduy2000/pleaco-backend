## Python Typing Requirements

Treat type annotations as **required**, not optional, for every new or modified function,
method, class, and field, including async functions, helpers, constructors, and test helpers:

- Annotate **every parameter** (except Python's conventional `self` and `cls`) and **every return
  type**. Use `-> None` when a function returns nothing, including `__init__`. Explicitly annotate
  `*args` and `**kwargs` when they are needed.
- Give class attributes and instance attributes concrete annotations. For SQLAlchemy 2.x models,
  follow the project's declarative pattern and use `Mapped[T]` for mapped columns and relationships.
  For Pydantic models, declare typed fields with the correct optionality and collection types.
- Use the **most accurate useful type** rather than `Any`, `object`, or an untyped container.
  Prefer domain enums, UUIDs, DTOs/Pydantic models, SQLAlchemy model types, `TypedDict`,
  `Protocol`, or precise standard-library types where appropriate. Do not introduce abstractions
  solely to make a type annotation look sophisticated.
- Specify collection element types (`list[Robot]`, `dict[str, int]`) and absence explicitly
  (`Robot | None`). Do not use `Optional`/`None` unless `None` is a real possible value.
- **Never use `Any` to bypass typing** or suppress errors. If an external library genuinely lacks
  usable type information, isolate the untyped boundary, validate/narrow its values, and document
  the reason for any narrowly scoped exception. Do not propagate `Any` into application APIs.
- Keep function signatures, inferred behavior, and actual returns consistent. Do not add false
  annotations or use unchecked casts just to satisfy a type checker.
- Preserve existing public API, payload, and database contracts when adding annotations. New
  annotations must not silently change runtime behavior or introduce unrelated refactors.
- Before finishing, review all added/changed signatures and fields for missing annotations, bare
  containers, or unjustified `Any`; run the repository's configured type checker if one exists.

Examples:

```python
from datetime import datetime, timedelta
from uuid import UUID

# Avoid
async def get_robot(robot_id: Any) -> Any: ...

class RobotPosition:
    x: Any
    y: Any

# Prefer (Robot and the repository function are existing project types)
async def get_robot(robot_id: UUID) -> Robot | None: ...

class RobotPosition:
    x: float
    y: float

def is_robot_stale(
    last_seen_at: datetime | None,
    now: datetime,
    timeout: timedelta,
) -> bool:
    return last_seen_at is None or now - last_seen_at > timeout
```
