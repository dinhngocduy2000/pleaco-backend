# Mock Device Heartbeat and MQTT Status Events

The mock device publishes connection, operational-status, and heartbeat events to Mosquitto.
The backend validates each MQTT message. Only `robot.online` and `robot.offline` are forwarded
through RabbitMQ; heartbeats and `robot.status.updated` go directly to the status service.

**Storage policy:** Redis stores the frequently changing `last_seen_at` heartbeat timestamp.
PostgreSQL stores durable robot metadata, connectivity state, status-event ordering, and a
`last_seen_at` snapshot on connectivity transitions. Heartbeats never update PostgreSQL.
Only accepted connectivity transitions produce a Socket.IO `robot.status` event.

## MQTT topic

Every event uses the robot-specific status topic:

```text
pleco/robots/{ROBOT_ID}/status
```

The device publishes with QoS 1 and does not retain messages. The backend subscribes to
`pleco/robots/+/status`.

## Events

### `robot.online`

Published after the MQTT connection is established.

```json
{
  "message_id": "a-unique-uuid",
  "robot_id": "the-robot-uuid",
  "ip_address": "192.168.10.24",
  "event": "robot.online",
  "sequence_number": 1001,
  "timestamp": "2026-10-14T12:00:00+00:00",
  "data": {
    "connection_status": "ONLINE"
  }
}
```

The backend validates that the `robot_id` matches the ID in the MQTT topic and that the robot is
registered. The current Mosquitto configuration allows anonymous publishing, so per-robot
publisher authentication requires a separate broker/ACL change. After status sequence and
deduplication checks, an accepted transition to `ONLINE`:

1. Updates the persisted connectivity status in PostgreSQL.
2. Initializes or refreshes Redis `last_seen_at` using **backend receive time**.
3. Emits the appropriate Socket.IO status-change event.

An already-applied or stale status event must not trigger another database write or Socket.IO event.

### `robot.heartbeat`

Published every 10 seconds while the MQTT client is connected.

```json
{
  "message_id": "a-unique-uuid",
  "robot_id": "the-robot-uuid",
  "event": "robot.heartbeat",
  "timestamp": "2026-10-14T12:00:10+00:00"
}
```

Heartbeats have their own message ID and do not include or consume the online/offline status
sequence number. The backend accepts heartbeats only for known robots currently
marked online, deduplicates each message ID in Redis, and refreshes Redis `last_seen_at` using
backend receive time.

**A normal heartbeat must not:**

- Update the robot row or `last_seen_at` in PostgreSQL.
- Change connectivity status or operational status.
- Emit a status-change Socket.IO event.

In particular, the device-provided `timestamp` is informational: it is not used as the authoritative
`last_seen_at`, because simulator clocks may differ from the backend clock.

### `robot.offline`

Published during graceful shutdown. The same offline payload is configured as the MQTT Last Will,
which the broker publishes if it detects an unexpected disconnection.

```json
{
  "message_id": "a-unique-uuid",
  "robot_id": "the-robot-uuid",
  "ip_address": "192.168.10.24",
  "event": "robot.offline",
  "sequence_number": 1002,
  "timestamp": "2026-10-14T12:05:00+00:00",
  "data": {
    "connection_status": "OFFLINE"
  }
}
```

The Last Will is configured before the device connects. Heartbeats do not advance the status
sequence, so the Will's sequence remains newer than the online event for that connection. The
backend continues to apply its existing sequence checks to online/offline status events.

On an accepted transition to `OFFLINE`, the backend:

1. Updates persisted connectivity status in PostgreSQL.
2. Copies the latest Redis heartbeat timestamp into PostgreSQL before clearing live state.
   If that value is missing, PostgreSQL keeps its existing `last_seen_at`.
3. Marks Redis connectivity state as offline and emits a Socket.IO status-change event.

An offline event from an older connection session must not overwrite a newer accepted online
connection. The existing sequence checks must cover reconnect and delayed-LWT scenarios; a
session/connection generation identifier is a recommended future hardening measure.

`robot.status.updated` goes directly from MQTT ingestion to the status service. An operational
update that overtakes a queued online event is ignored while the persisted robot is offline;
the device must send another update after it is online.

## Redis heartbeat storage

Recommended live key format:

```text
Key:   robot:{robot_id}:last_seen_at
Value: backend receive timestamp (UTC ISO 8601)
TTL:   60 seconds (configurable)
```

Every **accepted** heartbeat sets the new timestamp and refreshes the TTL. Using a TTL bounds the
lifetime of stale heartbeat data; it does **not** itself mark the robot offline.

Example using async Redis:

```python
from datetime import datetime, timezone

HEARTBEAT_REDIS_TTL_SECONDS = 60

async def refresh_last_seen(redis, robot_id: str) -> None:
    received_at = datetime.now(timezone.utc).isoformat()
    await redis.set(
        f"robot:{robot_id}:last_seen_at",
        received_at,
        ex=HEARTBEAT_REDIS_TTL_SECONDS,
    )
```

Run this only after robot identity, online state and message deduplication checks. The above is an
illustrative storage operation; concurrent status transitions must be coordinated so a delayed
heartbeat cannot revive an offline session's live state.

The bot list API obtains robot data from PostgreSQL and batches Redis freshness reads for online
robots. An online robot with a missing or expired Redis value returns `last_seen_at: null`;
an offline robot returns its durable PostgreSQL snapshot. A missing Redis value does not
independently prove an MQTT disconnection.

## PostgreSQL persistence policy

| Incoming event | Redis write | PostgreSQL write | Socket.IO status event |
| --- | --- | --- | --- |
| First accepted `robot.online` transition | Initialize `last_seen_at`; set live status | Persist `ONLINE` | Yes |
| Additional newer `robot.online` with no change | Update status projection only | Persist sequence/message ID only | No |
| Accepted `robot.heartbeat` | Refresh `last_seen_at` and TTL | **No** | **No** |
| Accepted `robot.offline` transition | Clear live heartbeat; set status projection | Persist `OFFLINE` and last-known heartbeat | Yes |
| Accepted `robot.status.updated` | Update status projection; leave heartbeat untouched | Persist operational status and ordering only | No |
| Duplicate or rejected event | No freshness refresh | No | No |

The `robot.status` Socket.IO event keeps the existing payload: `robot_id`, `ip_address`,
`connection_status`, `operational_status`, and `last_seen_at`. After an accepted `ONLINE` or
`OFFLINE` transition, the backend sends it to the robot's group room and, when the robot has an
assigned map, that map room. A socket in both rooms receives one copy. Room IDs come from the
persisted robot record. Heartbeats, operational-only changes, and IP-only changes do not emit it.

PostgreSQL may also store `last_connected_at`, `last_disconnected_at`, and a snapshot of the last
known heartbeat at the time of a status transition. These are durable historical fields, not values
to rewrite every 10 seconds.

If exact recent `last_seen_at` must survive Redis data loss, a **future optional** job can checkpoint
it to PostgreSQL periodically (for example, every 10 minutes), rather than on every heartbeat.
This checkpoint is not part of the initial implementation.

## Timing and offline detection

- Heartbeat interval: **10 seconds**.
- MQTT keepalive: **30 seconds**.
- Redis heartbeat freshness TTL: **60 seconds**, configurable.
- Unexpected disconnect handling: Mosquitto publishes the configured Last Will after it detects
  the disconnected client. MQTT keepalive detection is not an exact 30-second deadline.
- **Current design:** No separate backend timeout job for missed heartbeats. MQTT online/offline
  events determine connectivity status; heartbeats only refresh freshness.
- A missing/expired Redis heartbeat key must **not** automatically change PostgreSQL status to
  `OFFLINE` under this design.

**Known limitation:** a connected MQTT client whose application logic stops sending heartbeats
may remain marked `ONLINE` until the broker detects a lost connection or another valid offline
event arrives. A future `STALE` state and timeout-monitor worker can address this separately.
Also, after Redis data loss/restart, missing live heartbeat values require reconciliation from
fresh connectivity evidence rather than treating all robots as offline.

## Backend event flow

```text
Python Mock Device
    |
    | robot.online / robot.heartbeat / robot.offline
    v
Mosquitto (MQTT)
    |
    v
MQTT ingestion
    |-- online/offline --> RabbitMQ --> connectivity consumer --> status service
    |-- heartbeat ---------------------------------------------> status service
    |-- status.updated ----------------------------------------> status service
                                                               |--> Redis: live heartbeat freshness
                                                               |--> PostgreSQL: status and transition snapshot
                                                               +--> Socket.IO: connectivity changes to group and assigned map
```

The consumer should use an idempotent, ordered status-transition path. In a multi-worker
implementation, avoid a separate unsafe read-then-write race when accepting reconnect/offline
events. Redis connectivity state is a live projection; PostgreSQL is the durable record of accepted
status transitions.

## Configuration

The mock-device Compose file configures the broker host and port and supplies each robot's ID,
serial number, IP address, and sequence state path. The heartbeat interval is currently defined in
`app/main.py` as `HEARTBEAT_INTERVAL_SECONDS`.

The backend uses `HEARTBEAT_REDIS_TTL_SECONDS` (default: 60) and Pleco's existing Redis client.
