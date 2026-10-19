-- An event's location may be a physical place, an online link (video call or
-- livestream), both, or neither. The link is a nullable revision content
-- field alongside location_name/location_address, and scoped single-date
-- edits may override it through event_occurrences.content_override.
BEGIN;

ALTER TABLE event_revisions ADD COLUMN IF NOT EXISTS virtual_url text;

-- CREATE OR REPLACE VIEW may only append columns, so virtual_url goes last.
CREATE OR REPLACE VIEW published_events AS
SELECT
  e.id AS event_id,
  r.id AS event_revision_id,
  r.revision_number,
  r.title,
  r.description,
  r.location_name,
  r.location_address,
  r.event_url,
  r.is_all_day,
  r.starts_at,
  r.ends_at,
  r.start_date,
  r.end_date,
  r.timezone,
  r.recurrence_rule,
  r.submitted_at,
  r.reviewed_at,
  r.virtual_url
FROM events e
JOIN event_revisions r ON r.id = e.published_revision_id
WHERE r.approval_status = 'approved'
  AND e.archived_at IS NULL;

COMMIT;
