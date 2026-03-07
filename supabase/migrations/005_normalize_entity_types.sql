-- Migration 005: Normalize entity types to 6 canonical types
-- Maps 500+ ad-hoc entity types to 6 canonical types.
-- After this, new ingests are constrained by the updated extraction prompt.

-- Step 0: Drop the old (name, type) unique index — normalizing types will create
-- collisions. Migration 006 will merge duplicates and add a name-only constraint.
DROP INDEX IF EXISTS idx_entities_name_type_unique;

-- Step 1: Normalize to canonical types (order matters — specific before catch-all)

UPDATE entities SET entity_type = 'person'
WHERE lower(entity_type) IN (
  'person', 'character', 'client', 'audience', 'cohort'
);

UPDATE entities SET entity_type = 'organization'
WHERE lower(entity_type) IN (
  'organization', 'company', 'brand', 'team', 'institution',
  'business structure', 'agency'
);

UPDATE entities SET entity_type = 'project'
WHERE lower(entity_type) IN (
  'project', 'product', 'codebase', 'code_base', 'application',
  'app', 'backend', 'bucket'
);

UPDATE entities SET entity_type = 'tool'
WHERE lower(entity_type) IN (
  'tool', 'software', 'library', 'api', 'api call', 'platform',
  'service', 'class', 'component', 'code', 'code_file',
  'code_section', 'code section', 'code/python', 'agent',
  'ai', 'ai assistant', 'ai coach', 'ai model', 'ai_tool',
  'accelerator', 'algorithm', 'column', 'appliance', 'asset'
);

UPDATE entities SET entity_type = 'content'
WHERE lower(entity_type) IN (
  'content', 'book', 'article', 'media', 'comic_series',
  'animation style', 'artistic_style', 'camera technique',
  'artifact'
);

-- Step 2: Everything else becomes 'concept' (the broad catch-all)
-- This absorbs: concept, event, place, decision, substance, biomarker,
-- brain region, compound, category, pattern, methodology, etc.
UPDATE entities SET entity_type = 'concept'
WHERE entity_type NOT IN (
  'person', 'organization', 'project', 'tool', 'content'
);

-- Step 3: Add CHECK constraint to enforce 6 types going forward
ALTER TABLE entities DROP CONSTRAINT IF EXISTS entities_type_check;
ALTER TABLE entities ADD CONSTRAINT entities_type_check
  CHECK (entity_type IN (
    'person', 'organization', 'project', 'concept', 'tool', 'content'
  ));
