-- Set a generous timeout for this heavy migration
SET statement_timeout = '300s';

-- Migration 006: Merge duplicate entities after type normalization
-- After 005 normalizes types, entities like "ExampleOrg" (organization + project + brand)
-- now all have different canonical types but represent the same thing.
-- This migration merges entities with the same name, keeping the one with most relations.

-- Step 1: Create a function to merge two entities (keep winner, absorb loser)
CREATE OR REPLACE FUNCTION merge_entities(winner_id uuid, loser_id uuid)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
  -- Move loser's outgoing relations to winner (skip if edge already exists)
  UPDATE relations SET source_entity = winner_id
  WHERE source_entity = loser_id
    AND NOT EXISTS (
      SELECT 1 FROM relations r2
      WHERE r2.source_entity = winner_id
        AND r2.target_entity = relations.target_entity
        AND r2.relation_type = relations.relation_type
    );

  -- Move loser's incoming relations to winner (skip if edge already exists)
  UPDATE relations SET target_entity = winner_id
  WHERE target_entity = loser_id
    AND NOT EXISTS (
      SELECT 1 FROM relations r2
      WHERE r2.target_entity = winner_id
        AND r2.source_entity = relations.source_entity
        AND r2.relation_type = relations.relation_type
    );

  -- Delete remaining duplicate relations (edges that already existed on winner)
  DELETE FROM relations WHERE source_entity = loser_id OR target_entity = loser_id;

  -- Move loser's observation links to winner
  UPDATE observations
  SET entity_ids = array_replace(entity_ids, loser_id, winner_id)
  WHERE loser_id = ANY(entity_ids);

  -- Move loser's task links to winner
  UPDATE tasks
  SET entity_ids = array_replace(entity_ids, loser_id, winner_id)
  WHERE loser_id = ANY(entity_ids);

  -- Merge aliases: add loser's name and aliases to winner
  UPDATE entities
  SET aliases = (
    SELECT array_agg(DISTINCT a)
    FROM (
      SELECT unnest(aliases) AS a FROM entities WHERE id = winner_id
      UNION
      SELECT unnest(aliases) FROM entities WHERE id = loser_id
      UNION
      SELECT name FROM entities WHERE id = loser_id
    ) sub
    WHERE a IS NOT NULL
  )
  WHERE id = winner_id;

  -- Delete the loser
  DELETE FROM entities WHERE id = loser_id;
END;
$$;

-- Step 2: Merge entities with identical lowercase names
-- For each group of duplicates, keep the one with the most relations
DO $$
DECLARE
  rec RECORD;
  winner_id uuid;
  loser RECORD;
BEGIN
  -- Find all groups of entities with the same lowercase name (2+ entities)
  FOR rec IN
    SELECT lower(name) AS lname, array_agg(id ORDER BY id) AS ids
    FROM entities
    GROUP BY lower(name)
    HAVING count(*) > 1
  LOOP
    -- Pick the winner: entity with most relations
    SELECT e.id INTO winner_id
    FROM entities e
    LEFT JOIN (
      SELECT source_entity AS eid, count(*) AS cnt FROM relations GROUP BY source_entity
      UNION ALL
      SELECT target_entity AS eid, count(*) AS cnt FROM relations GROUP BY target_entity
    ) r ON r.eid = e.id
    WHERE e.id = ANY(rec.ids)
    GROUP BY e.id
    ORDER BY coalesce(sum(r.cnt), 0) DESC
    LIMIT 1;

    -- Merge all others into the winner
    FOR loser IN
      SELECT id FROM entities WHERE id = ANY(rec.ids) AND id != winner_id
    LOOP
      PERFORM merge_entities(winner_id, loser.id);
    END LOOP;
  END LOOP;
END;
$$;

-- Step 3: Drop the old name+type unique index (too restrictive now)
DROP INDEX IF EXISTS idx_entities_name_type_unique;

-- Step 4: Add name-only unique index (case-insensitive)
CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_name_unique
  ON entities (lower(name));

-- Step 5: Clean up the merge function (keep for future use)
-- COMMENT ON FUNCTION merge_entities IS 'Merges two entities: moves relations, observations, tasks, aliases from loser to winner, then deletes loser.';
