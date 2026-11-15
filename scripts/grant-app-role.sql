GRANT USAGE ON SCHEMA public TO pleaco_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
  public.users, public.groups, public.group_members, public.maps, public.robots,
  public.tags, public.map_boundaries, public.environment_zones,
  public.docking_station, public.map_tags, public.robot_tags
TO pleaco_app;
REVOKE TRUNCATE ON TABLE
  public.users, public.groups, public.group_members, public.maps, public.robots,
  public.tags, public.map_boundaries, public.environment_zones,
  public.docking_station, public.map_tags, public.robot_tags
FROM pleaco_app;
