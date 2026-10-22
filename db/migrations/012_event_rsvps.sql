-- RSVP for account holders. An event opts in with the revisioned
-- `rsvp_enabled` toggle (off by default); signed-in users may then RSVP
-- "attending" or "declined" and see each other's RSVPs. One row per
-- (event, user): RSVPing again changes the answer instead of adding a row.
-- `user_id` is the account id (or "operator" for the configured operator
-- identity, which is not a users row), so it is plain text rather than a
-- foreign key; usernames cannot be renamed, so the snapshot never stales.
BEGIN;

-- A table from an incompatible RSVP implementation under the same name
-- would survive CREATE TABLE IF NOT EXISTS and break every RSVP read with
-- a missing-column error. Fail loudly here instead of 500ing later.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables WHERE table_name = 'event_rsvps'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_name = 'event_rsvps' AND column_name = 'status'
  ) THEN
    RAISE EXCEPTION
      'table event_rsvps exists with an incompatible shape; move it aside before applying 012_event_rsvps.sql';
  END IF;
END $$;

ALTER TABLE event_revisions ADD COLUMN IF NOT EXISTS rsvp_enabled boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS event_rsvps (
  event_id uuid NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  user_id text NOT NULL,
  username text NOT NULL
    CONSTRAINT event_rsvp_username_not_blank CHECK (btrim(username) <> ''),
  status text NOT NULL
    CONSTRAINT event_rsvp_status CHECK (status IN ('attending', 'declined')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (event_id, user_id)
);

CREATE INDEX IF NOT EXISTS event_rsvps_user_idx ON event_rsvps(user_id);

-- CREATE OR REPLACE VIEW may only append columns, so rsvp_enabled goes last.
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
  r.attachments,
  r.rsvp_enabled
FROM events e
JOIN event_revisions r ON r.id = e.published_revision_id
WHERE r.approval_status = 'approved'
  AND e.archived_at IS NULL;

COMMIT;
