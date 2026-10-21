"""Ordered robot status reconciliation and Redis-only heartbeat freshness."""

import json
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enum.robot import RobotConnectionStatus, RobotOperationalStatus
from app.common.middleware.logger import Logger
from app.common.schemas.robot_status import RobotHeartbeatEvent, RobotStatusEvent
from app.core.config import settings
from app.models.robot import Robot
from app.repository.registry import Registry

logger = Logger()


class BotStatusService:
    def __init__(self, repo: Registry, realtime_publisher) -> None:
        self.repo = repo
        self._realtime_publisher = realtime_publisher

    @staticmethod
    def _last_seen_key(robot_id) -> str:
        return f"robot:{robot_id}:last_seen_at"

    async def process_heartbeat_event(self, event: RobotHeartbeatEvent) -> None:
        """Refresh Redis freshness for an online robot without a database write.

        Heartbeats are not part of the status-event sequence. The row lock
        covers both the online check and Redis write: if the heartbeat wins,
        a later offline event can snapshot it; if offline wins, this check
        rejects the heartbeat. The transaction performs no robot-row update.
        """

        async def refresh_last_seen(session: AsyncSession) -> None:
            # Use the same row lock as status reconciliation across workers.
            # A plain read could observe ONLINE just before OFFLINE commits.
            bot = await self.repo.bot_repo().get_by_id_for_update(
                session, event.robot_id
            )
            if bot is None or bot.connection_status != RobotConnectionStatus.ONLINE:
                logger.warning(
                    msg=f"Ignoring heartbeat for unknown or offline robot {event.robot_id}"
                )
                return
            try:
                # Refresh the one live timestamp and its TTL together. The
                # device's timestamp is not authoritative.
                await self.repo._redis_client.set(
                    self._last_seen_key(event.robot_id),
                    datetime.now(timezone.utc).isoformat(),
                    expire=settings.HEARTBEAT_REDIS_TTL_SECONDS,
                )
            except Exception:
                # Without Redis there is no live freshness to record. Do not
                # fall back to writing each heartbeat into PostgreSQL.
                logger.exception(
                    msg=f"Redis unavailable while recording robot heartbeat for {event.robot_id}"
                )
                return
            logger.debug(
                msg=(
                    f"Redis refreshed heartbeat for robot {event.robot_id} "
                    f"message {event.message_id}"
                )
            )

        await self.repo.transaction_wrapper(refresh_last_seen)

    async def process_status_event(self, event: RobotStatusEvent) -> None:
        """Save an ordered status event, then update its Redis cache.

        PostgreSQL holds the durable sequence and connectivity state. Redis
        cache updates happen after commit so a failed database transaction
        cannot publish an uncommitted state. A second locked check prevents
        an older event from updating the cache after a newer one has committed.
        """
        received_at = datetime.now(timezone.utc)
        result = await self.repo.transaction_wrapper(
            lambda session: self._save_status(session, event, received_at)
        )
        if result is None:
            return

        state, connection_changed, last_seen_at = result
        # Build the cache value from the accepted database state, including
        # its sequence. The event payload cannot choose a group or override
        # values retained from the existing robot row.
        cache_state = {
            "robot_id": str(event.robot_id),
            **{
                key: state[key]
                for key in (
                    "ip_address",
                    "connection_status",
                    "operational_status",
                    "last_sequence_number",
                    "last_message_id",
                )
            },
        }
        is_current = True
        try:
            # Recheck under the row lock: another event may have committed
            # between the database write and this cache update. Redis has its
            # own sequence check because cache writes can arrive late.
            is_current = await self.repo.transaction_wrapper(
                lambda session: self._update_cache_if_current(
                    session, event, cache_state, state, connection_changed, received_at
                )
            )
        except Exception:
            # Redis is a live cache; the committed database state remains
            # authoritative, and an existing status change can still broadcast.
            logger.exception(msg=f"Unable to cache robot status for {event.robot_id}")

        # Only connectivity transitions reach Socket.IO. Newer operational or
        # IP updates still persist their ordering fields and cached state.
        if connection_changed and is_current:
            await self._realtime_publisher.publish(
                group_id=state["group_id"],
                map_id=state["map_id"],
                robot_id=event.robot_id,
                ip_address=state["ip_address"],
                connection_status=state["connection_status"],
                operational_status=state["operational_status"],
                last_seen_at=last_seen_at,
            )

    async def _update_cache_if_current(
        self,
        session: AsyncSession,
        event: RobotStatusEvent,
        cache_state: dict,
        state: dict,
        connection_changed: bool,
        received_at: datetime,
    ) -> bool:
        """Update Redis only while this event has the latest database sequence.

        The Redis script also checks its cached sequence, so an older caller
        cannot replace a newer state. Return ``False`` if a later event
        has already committed to PostgreSQL.
        """
        # This second transaction holds the lock through the Redis operation.
        # The first transaction has already committed, so the cache never
        # leads an uncommitted database transition.
        bot = await self.repo.bot_repo().get_by_id_for_update(session, event.robot_id)
        if bot is None or bot.last_sequence_number != event.sequence_number:
            return False
        await self.repo._redis_client.update_robot_status_cache(
            event.robot_id,
            event.sequence_number,
            json.dumps(cache_state),
            state["connection_status"],
            connection_changed,
            received_at.isoformat(),
            settings.HEARTBEAT_REDIS_TTL_SECONDS,
        )
        return True

    async def _save_status(
        self, session: AsyncSession, event: RobotStatusEvent, received_at: datetime
    ) -> tuple[dict, bool, datetime | None] | None:
        """Check ordering and persist one event while holding the robot row lock.

        Every accepted newer event persists its sequence. ``last_seen_at``
        changes only on a connectivity transition; going offline snapshots
        the latest Redis heartbeat when one is available.
        """
        bot = await self.repo.bot_repo().get_by_id_for_update(session, event.robot_id)
        if bot is None:
            logger.warning(
                msg=f"Ignoring status event for unknown robot {event.robot_id}"
            )
            return None

        old_state = self._robot_state(bot)
        if not self._accept_status_event(bot, event, old_state):
            return None

        state = self._new_state(old_state, event)
        connection_changed = self._connection_changed(old_state, state)
        last_seen_at = await self._last_seen_for_status(
            bot,
            event.robot_id,
            state["connection_status"],
            connection_changed,
            received_at,
        )
        await self._write_status(
            session, bot, event, state, last_seen_at, connection_changed
        )
        return (
            {**state, "group_id": bot.group_id, "map_id": bot.map_id},
            connection_changed,
            last_seen_at,
        )

    def _accept_status_event(
        self, bot: Robot, event: RobotStatusEvent, old_state: dict
    ) -> bool:
        """Reject stale events and operational updates that cannot apply yet."""
        # Check the durable sequence while holding the lock, even if Redis
        # already contains a cached copy. Redis can expire or lose its state.
        if self._is_duplicate_or_old(event, old_state):
            logger.warning(
                msg=f"Ignoring duplicate or stale robot status event for {event.robot_id}"
            )
            return False
        if (
            event.type == "robot.status.changed"
            and bot.connection_status != RobotConnectionStatus.ONLINE
        ):
            # A direct MQTT operational update can overtake the queued ONLINE
            # event. Do not advance the durable sequence while still offline,
            # or that later ONLINE event would be rejected as stale.
            logger.warning(
                msg=f"Ignoring operational status event for offline robot {event.robot_id}"
            )
            return False
        if (
            event.type == "robot.status.changed"
            and event.connection_status is not None
            and event.connection_status.value != old_state["connection_status"]
        ):
            logger.warning(
                msg=f"Ignoring non-connectivity event changing connection for {event.robot_id}"
            )
            return False
        return True

    @staticmethod
    def _connection_changed(old_state: dict, state: dict) -> bool:
        """Check whether this event changes the robot's connectivity."""
        return state["connection_status"] != old_state["connection_status"]

    async def _last_seen_for_status(
        self,
        bot: Robot,
        robot_id,
        connection_status: str,
        connection_changed: bool,
        received_at: datetime,
    ) -> datetime | None:
        """Choose the durable last-seen value for a connectivity transition."""
        if not connection_changed:
            return bot.last_seen_at
        if connection_status == RobotConnectionStatus.ONLINE.value:
            # Use backend receipt time rather than the device clock.
            return received_at
        if connection_status == RobotConnectionStatus.OFFLINE.value:
            return await self._offline_last_seen(robot_id, bot.last_seen_at)
        return bot.last_seen_at

    async def _offline_last_seen(
        self, robot_id, saved_at: datetime | None
    ) -> datetime | None:
        """Use a newer Redis heartbeat when taking the offline database snapshot."""
        try:
            # Keep the existing database snapshot if freshness expired
            # or Redis is unavailable; an offline event is not a heartbeat.
            raw = await self.repo._redis_client.get(self._last_seen_key(robot_id))
            if raw:
                parsed = datetime.fromisoformat(raw)
                if parsed.tzinfo is None:
                    raise ValueError("heartbeat timestamp has no timezone")
                # An old Redis value must never move the durable
                # last-known timestamp backwards.
                if saved_at is None or parsed > saved_at:
                    return parsed
        except (ValueError, TypeError):
            logger.warning(
                msg=f"Ignoring invalid Redis heartbeat timestamp for {robot_id}"
            )
        except Exception:
            logger.exception(
                msg=f"Redis unavailable while snapshotting heartbeat for {robot_id}"
            )
        return saved_at

    async def _write_status(
        self,
        session: AsyncSession,
        bot: Robot,
        event: RobotStatusEvent,
        state: dict,
        last_seen_at: datetime | None,
        connection_changed: bool,
    ) -> None:
        """Persist status and ordering fields for every accepted event."""
        # Persist ordering even for an accepted event with unchanged visible
        # state. Otherwise a delayed LWT could pass after Redis data loss.
        await self.repo.bot_repo().update_status(
            session,
            bot,
            ip_address=state["ip_address"],
            connection_status=RobotConnectionStatus(state["connection_status"]),
            operational_status=RobotOperationalStatus(state["operational_status"]),
            last_seen_at=last_seen_at if connection_changed else None,
            last_sequence_number=event.sequence_number,
            last_message_id=event.message_id,
            ctx=None,
        )

    @staticmethod
    def _robot_state(bot) -> dict:
        return {
            "ip_address": str(bot.ip_address) if bot.ip_address is not None else None,
            "connection_status": bot.connection_status.value,
            "operational_status": bot.operational_status.value,
            "last_sequence_number": bot.last_sequence_number,
            "last_message_id": (
                str(bot.last_message_id) if bot.last_message_id else None
            ),
        }

    @staticmethod
    def _new_state(state: dict, event: RobotStatusEvent) -> dict:
        return {
            **state,
            "ip_address": (
                str(event.ip_address)
                if event.ip_address is not None
                else state["ip_address"]
            ),
            "connection_status": (
                event.connection_status.value
                if event.connection_status
                else state["connection_status"]
            ),
            "operational_status": (
                event.operational_status.value
                if event.operational_status
                else state["operational_status"]
            ),
            "last_sequence_number": event.sequence_number,
            "last_message_id": str(event.message_id),
        }

    @staticmethod
    def _is_duplicate_or_old(event: RobotStatusEvent, state: dict) -> bool:
        if state.get("last_message_id") == str(event.message_id):
            return True
        last_sequence = state.get("last_sequence_number")
        return last_sequence is not None and event.sequence_number <= int(last_sequence)
