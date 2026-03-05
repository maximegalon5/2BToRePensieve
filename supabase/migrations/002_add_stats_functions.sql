-- Function: get top N entities by relation count
CREATE OR REPLACE FUNCTION get_top_connected_entities(result_limit int DEFAULT 10)
RETURNS TABLE (
  entity_id uuid,
  entity_name text,
  entity_type text,
  relation_count bigint
)
LANGUAGE sql STABLE
AS $$
  SELECT
    e.id AS entity_id,
    e.name AS entity_name,
    e.entity_type,
    (
      SELECT COUNT(*)
      FROM relations r
      WHERE r.source_entity = e.id OR r.target_entity = e.id
    ) AS relation_count
  FROM entities e
  ORDER BY relation_count DESC
  LIMIT result_limit;
$$;
