BEGIN;

-- Admin group management: every group carries a color identifier, and a
-- group can be deleted.
--
-- Reviewed revisions, subscriptions, and notification history reference
-- groups through immutable or ON DELETE RESTRICT rows, so deletion retires
-- the group as a tombstone (deleted_at) instead of removing it. A deleted
-- group is no longer listed, assignable, or shown; events that carried it
-- stay visible without it. Its slug becomes free for a new group, so slug
-- uniqueness now applies to live groups only.
ALTER TABLE groups
  ADD COLUMN color text NOT NULL DEFAULT '#447d68'
    CONSTRAINT groups_color_hex CHECK (color ~ '^#[0-9a-f]{6}$'),
  ADD COLUMN deleted_at timestamptz,
  ADD CONSTRAINT groups_deleted_inactive
    CHECK (deleted_at IS NULL OR NOT is_active);

-- Existing groups get distinct palette colors in name order (the same
-- palette the service uses for new groups without an explicit color).
WITH palette AS (
  SELECT ARRAY[
    '#447d68', '#d8674b', '#4f8097', '#b98420',
    '#7c6ca6', '#b5527a', '#5e7d2f', '#8a6a4f'
  ] AS colors
), ordered AS (
  SELECT id, row_number() OVER (ORDER BY name, id) - 1 AS position
    FROM groups
)
UPDATE groups g
   SET color = palette.colors[((ordered.position % 8) + 1)::integer]
  FROM ordered, palette
 WHERE ordered.id = g.id;

ALTER TABLE groups DROP CONSTRAINT groups_slug_key;
CREATE UNIQUE INDEX groups_live_slug_unique
  ON groups(slug) WHERE deleted_at IS NULL;

COMMIT;
