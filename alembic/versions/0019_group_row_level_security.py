"""Protect group-owned rows with transaction-scoped PostgreSQL RLS.

The provisioner must create the non-owner ``pleaco_app`` role before this
migration. It must run this migration as a role with BYPASSRLS, because the two
deliberately narrow SECURITY DEFINER functions operate across groups.

Revision ID: 0019_group_row_level_security
Revises: 0018_create_docking_station
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0019_group_row_level_security"
down_revision: Union[str, None] = "0018_create_docking_station"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACTOR = "NULLIF(current_setting('app.actor_id', true), '')::uuid"
GROUP = "NULLIF(current_setting('app.group_id', true), '')::uuid"

PROTECTED_TABLES = (
    "groups",
    "group_members",
    "maps",
    "robots",
    "tags",
    "map_boundaries",
    "environment_zones",
    "docking_station",
    "map_tags",
    "robot_tags",
)


def _policy(table: str, command: str, using: str | None, check: str | None) -> None:
    clause = f"CREATE POLICY {table}_{command.lower()}_app ON public.{table} FOR {command} TO pleaco_app"
    if using is not None:
        clause += f" USING ({using})"
    if check is not None:
        clause += f" WITH CHECK ({check})"
    op.execute(clause)


def upgrade() -> None:
    # Reject pre-existing references that the new same-group constraint cannot hold.
    invalid = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM public.robots r "
            "JOIN public.maps m ON m.id = r.map_id "
            "WHERE r.group_id <> m.group_id"
        )
    ).scalar_one()
    if invalid:
        raise RuntimeError(f"Cannot add robot/map group constraint: {invalid} invalid references")

    op.create_unique_constraint("uq_maps_id_group_id", "maps", ["id", "group_id"])
    op.drop_constraint("robots_map_id_fkey", "robots", type_="foreignkey")
    # PostgreSQL 15 supports a column list for SET NULL on composite FKs.
    op.execute(
        "ALTER TABLE public.robots ADD CONSTRAINT fk_robots_map_group "
        "FOREIGN KEY (map_id, group_id) REFERENCES public.maps (id, group_id) "
        "ON DELETE SET NULL (map_id)"
    )

    for table in PROTECTED_TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")

    direct = f"(group_id = {GROUP})"
    for table in ("maps", "robots", "tags"):
        for command in ("SELECT", "DELETE"):
            _policy(table, command, direct, None)
        _policy(table, "INSERT", None, direct)
        _policy(table, "UPDATE", direct, direct)

    parent_map = (
        f"(EXISTS ("
        f"SELECT 1 FROM public.maps m WHERE m.id = map_id AND m.group_id = {GROUP}))"
    )
    for table in ("map_boundaries", "environment_zones"):
        for command in ("SELECT", "DELETE"):
            _policy(table, command, parent_map, None)
        _policy(table, "INSERT", None, parent_map)
        _policy(table, "UPDATE", parent_map, parent_map)

    dock = (
        f"({parent_map} AND (robot_id IS NULL OR EXISTS ("
        f"SELECT 1 FROM public.robots r WHERE r.id = robot_id AND r.group_id = {GROUP})))"
    )
    for command in ("SELECT", "DELETE"):
        _policy("docking_station", command, dock, None)
    _policy("docking_station", "INSERT", None, dock)
    _policy("docking_station", "UPDATE", dock, dock)

    for table, parent_id, parent_table in (
        ("map_tags", "map_id", "maps"),
        ("robot_tags", "robot_id", "robots"),
    ):
        linked = (
            f"(EXISTS (SELECT 1 FROM public.{parent_table} p "
            f"WHERE p.id = {parent_id} AND p.group_id = {GROUP}) AND EXISTS ("
            f"SELECT 1 FROM public.tags t WHERE t.id = tag_id AND t.group_id = {GROUP}))"
        )
        for command in ("SELECT", "DELETE"):
            _policy(table, command, linked, None)
        _policy(table, "INSERT", None, linked)
        _policy(table, "UPDATE", linked, linked)

    # A member can discover their own invitations/memberships without selecting
    # an active group. Tenant-wide reads and all writes require group context.
    member_read = f"({ACTOR} IS NOT NULL AND (member_id = {ACTOR} OR group_id = {GROUP}))"
    member_write = f"({ACTOR} IS NOT NULL AND group_id = {GROUP})"
    _policy("group_members", "SELECT", member_read, None)
    _policy("group_members", "DELETE", member_write, None)
    _policy("group_members", "INSERT", None, member_write)
    _policy("group_members", "UPDATE", member_write, member_write)

    group_read = (
        f"({ACTOR} IS NOT NULL AND (owner_id = {ACTOR} OR EXISTS ("
        "SELECT 1 FROM public.group_members gm "
        f"WHERE gm.group_id = id AND gm.member_id = {ACTOR} "
        "AND gm.invitation_status = 'ACCEPTED')))"
    )
    group_write = f"({ACTOR} IS NOT NULL AND id = {GROUP})"
    _policy("groups", "SELECT", group_read, None)
    _policy("groups", "INSERT", None, f"({ACTOR} IS NOT NULL AND owner_id = {ACTOR})")
    _policy("groups", "UPDATE", group_write, group_write)
    _policy("groups", "DELETE", group_write, None)

    # These functions are owned by the migration role, whose BYPASSRLS privilege
    # lets them perform only their stated cross-group operations. No dynamic SQL.
    op.execute(
        """CREATE FUNCTION public.pleaco_robot_group(p_robot_id uuid)
        RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$ SELECT r.group_id FROM public.robots AS r WHERE r.id = p_robot_id $$"""
    )
    op.execute("REVOKE ALL ON FUNCTION public.pleaco_robot_group(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.pleaco_robot_group(uuid) TO pleaco_app")

    op.execute(
        """CREATE FUNCTION public.pleaco_expire_invitations()
        RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE affected integer;
        BEGIN
          UPDATE public.group_members
          SET invitation_status = 'REJECTED', invitation_id = NULL,
              invitation_expires_at = NULL, updated_at = now()
          WHERE invitation_status = 'PENDING'
            AND invitation_expires_at IS NOT NULL
            AND invitation_expires_at <= now();
          GET DIAGNOSTICS affected = ROW_COUNT;
          RETURN affected;
        END $$"""
    )
    op.execute("REVOKE ALL ON FUNCTION public.pleaco_expire_invitations() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.pleaco_expire_invitations() TO pleaco_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION public.pleaco_expire_invitations()")
    op.execute("DROP FUNCTION public.pleaco_robot_group(uuid)")
    for table in reversed(PROTECTED_TABLES):
        for command in ("select", "insert", "update", "delete"):
            op.execute(f"DROP POLICY {table}_{command}_app ON public.{table}")
        op.execute(f"ALTER TABLE public.{table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY")
    op.drop_constraint("fk_robots_map_group", "robots", type_="foreignkey")
    op.create_foreign_key(
        "robots_map_id_fkey", "robots", "maps", ["map_id"], ["id"], ondelete="SET NULL"
    )
    op.drop_constraint("uq_maps_id_group_id", "maps", type_="unique")
