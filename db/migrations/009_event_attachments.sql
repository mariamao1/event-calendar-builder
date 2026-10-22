-- An event may carry up to three linked file attachments (a graphic or
-- image for the event, a flyer, an agenda, ...). Each is a
-- {"url", "name"} object with an absolute http(s) URL; the list is
-- revisioned content alongside the other descriptive fields, so scoped
-- single-date edits may override it through
-- event_occurrences.content_override. The service layer validates the
-- three-item cap; the column holds the canonical JSON array, NULL when the
-- event carries none.
BEGIN;

ALTER TABLE event_revisions ADD COLUMN IF NOT EXISTS attachments text;

-- CREATE OR REPLACE VIEW may only append columns, so attachments goes last.
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
  r.virtual_url,
  r.attachments
FROM events e
JOIN event_revisions r ON r.id = e.published_revision_id
WHERE r.approval_status = 'approved'
  AND e.archived_at IS NULL;

COMMIT;
