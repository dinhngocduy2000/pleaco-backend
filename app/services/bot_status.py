"""Domain reconciliation for ordered, idempotent robot status events."""

import json
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enum.robot import RobotConnectionStatus, RobotOperationalStatus
from app.common.middleware.logger import Logger
from app.common.schemas.robot_status import RobotStatusEvent
from app.repository.registry import Registry

logger = Logger()


class BotStatusService:
    def __init__(self, repo: Registry, websocket_manager) -> None:
        self.repo = repo
        self._websocket_manager = websocket_manager

    @staticmethod
    def _cache_key(robot_id) -> str:
        return f"robot:{robot_id}:state"

    async def process_status_event(self, event: RobotStatusEvent) -> None:
        received_at = datetime.now(timezone.utc)
        cache = await self._get_cache(event.robot_id)
        if cache is not None and self._is_duplicate_or_stale(event, cache):
            logger.warning(msg=f"Ignoring duplicate or stale robot status event for {event.robot_id}")
            return

        result = await self.repo.transaction_wrapper(
            lambda session: self._reconcile(session, event, cache, received_at)
        )
        if result is None:
            return
        state, meaningful_change = result
        await self._set_cache(event, received_at, state)
        logger.info(
            msg=(
                f"Accepted robot status event for {event.robot_id} "
                f"connection={state['connection_status']} "
                f"sequence={event.sequence_number}"
            )
        )
        if meaningful_change:
            await self._websocket_manager.broadcast(
                state["group_id"],
                {
                    "type": "robot.status.changed",
                    "data": {
                        "robot_id": str(event.robot_id),
                        "ip_address": state["ip_address"],
                        "connection_status": state["connection_status"],
                        "operational_status": state["operational_status"],
                        "last_seen_at": received_at.isoformat(),
                    },
                },
            )
        else:
            logger.info(
                msg=(
                    f"Skipped WebSocket broadcast for robot {event.robot_id}; "
                    "the accepted event did not change connection, operational, or IP state"
                )
            )

    async def _reconcile(self, session: AsyncSession, event: RobotStatusEvent, cache, received_at):
        bot = await self.repo.bot_repo().get_by_id(session, event.robot_id, ctx=None)
        if bot is None:
            logger.warning(msg=f"Ignoring status event for unknown robot {event.robot_id}")
            return None
        database_state = self._database_state(bot)
        if cache is None and self._is_duplicate_or_stale(event, database_state):
            logger.warning(msg=f"Ignoring duplicate or stale persisted robot status event for {event.robot_id}")
            return None

        next_state = self._next_state(database_state, event)
        meaningful_change = any(
            next_state[field] != database_state[field]
            for field in ("ip_address", "connection_status", "operational_status")
        )
        if meaningful_change:
            await self.repo.bot_repo().update_status(
                session,
                bot,
                ip_address=next_state["ip_address"],
                connection_status=RobotConnectionStatus(next_state["connection_status"]),
                operational_status=RobotOperationalStatus(next_state["operational_status"]),
                last_seen_at=received_at,
                last_sequence_number=event.sequence_number,
                last_message_id=event.message_id,
                ctx=None,
            )
        elif cache is None:
            # Redis is being seeded from the durable, already matching state.
            next_state = database_state
        return ({**next_state, "group_id": bot.group_id}, meaningful_change)

    async def _get_cache(self, robot_id):
        try:
            raw = await self.repo._redis_client.get(self._cache_key(robot_id))
            return json.loads(raw) if raw else None
        except Exception:
            logger.exception(msg="Redis unavailable while reading robot state; falling back to PostgreSQL")
            return None

    async def _set_cache(self, event: RobotStatusEvent, received_at: datetime, state: dict) -> None:
        cached = event.cache_state(last_seen_at=received_at)
        cached.update({
            "ip_address": state["ip_address"],
            "connection_status": state["connection_status"],
            "operational_status": state["operational_status"],
        })
        try:
            await self.repo._redis_client.set(self._cache_key(event.robot_id), json.dumps(cached))
        except Exception:
            logger.exception(msg="Redis unavailable while writing robot state")

    @staticmethod
    def _database_state(bot) -> dict:
        return {
            "ip_address": str(bot.ip_address) if bot.ip_address is not None else None,
            "connection_status": bot.connection_status.value,
            "operational_status": bot.operational_status.value,
            "last_sequence_number": bot.last_sequence_number,
            "last_message_id": str(bot.last_message_id) if bot.last_message_id else None,
        }

    @staticmethod
    def _next_state(state: dict, event: RobotStatusEvent) -> dict:
        return {
            **state,
            "ip_address": str(event.ip_address) if event.ip_address is not None else state["ip_address"],
            "connection_status": (
                event.connection_status.value if event.connection_status else state["connection_status"]
            ),
            "operational_status": (
                event.operational_status.value if event.operational_status else state["operational_status"]
            ),
            "last_sequence_number": event.sequence_number,
            "last_message_id": str(event.message_id),
        }

    @staticmethod
    def _is_duplicate_or_stale(event: RobotStatusEvent, state: dict) -> bool:
        if state.get("last_message_id") == str(event.message_id):
            return True
        last_sequence = state.get("last_sequence_number")
        return last_sequence is not None and event.sequence_number <= int(last_sequence)
