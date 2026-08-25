from collections import defaultdict
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context import AppContext
from app.models.robot import Robot
from app.models.robot_tags import robot_tags
from app.models.tag import Tag


class RobotTagsRepository:
    async def get_by_robot_ids(
        self,
        session: AsyncSession,
        robot_ids: Sequence[UUID],
        group_id: UUID,
        ctx: AppContext,
    ) -> dict[UUID, list[Tag]]:
        """Return group-scoped tags keyed by robot ID."""
        if not robot_ids:
            return {}

        stmt = (
            select(robot_tags.c.robot_id, Tag)
            .join(Robot, Robot.id == robot_tags.c.robot_id)
            .join(Tag, Tag.id == robot_tags.c.tag_id)
            .where(
                robot_tags.c.robot_id.in_(robot_ids),
                Robot.group_id == group_id,
                Tag.group_id == group_id,
            )
            .order_by(robot_tags.c.robot_id.asc(), Tag.name.asc(), Tag.id.asc())
        )
        result = await session.execute(stmt)
        tags_by_robot: dict[UUID, list[Tag]] = defaultdict(list)
        for robot_id, tag in result.all():
            tags_by_robot[robot_id].append(tag)
        return dict(tags_by_robot)
