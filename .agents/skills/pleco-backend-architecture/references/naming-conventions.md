## Naming and Readability

Apply these rules to all new or modified Python code while this skill is active:

- Use plain, familiar English. Prefer the shortest name that clearly explains what a value or
  operation represents; do not shorten names so far that their meaning becomes unclear.
- Give variables and parameters descriptive **noun** names (`robot_id`, `battery_level`,
  `last_seen_at`); give functions descriptive **verb** names (`get_robot`, `assign_robot_to_map`,
  `is_robot_online`). Boolean names should read as a yes/no question (`is_active`, `has_permission`,
  `can_start_task`).
- Follow Python conventions: `snake_case` for variables, functions, methods, and modules;
  `PascalCase` for classes; `UPPER_SNAKE_CASE` for constants.
- Use established Pleco domain terms consistently. Reuse existing names for group/tenant, robot,
  map, task, command, session, and event concepts instead of inventing synonyms. Preserve existing
  database columns, API fields, event payload keys, public interfaces, and other contracts unless
  changing them is explicitly part of the task.
- Avoid cryptic abbreviations, unexplained acronyms, generic names (`data`, `item`, `obj`,
  `process_data`, `handle_stuff`), and unnecessarily long or clever names when a specific, simpler
  term works. Standard technical abbreviations (`id`, `uuid`, `mqtt`, `http`) are fine when clear.
  Short names like `x` and `y` are fine for coordinates; single-letter counters are fine in tiny
  local scopes.
- Prefer clarity over saving characters or maximizing detail. A reader should understand a name
  without tracing several layers of code. Do not rename unrelated existing code just to meet these
  rules; apply them to the code you touch.

Examples:

```python
# Avoid
async def execute_robot_operational_status_reconciliation_procedure(rbt, payload_data): ...

# Prefer (with annotations appropriate to the real domain model)
async def update_robot_status(robot_id: UUID, status: RobotStatus) -> None: ...
```
