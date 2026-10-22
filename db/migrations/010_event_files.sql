-- Files uploaded from a creator's own machine. They belong to the event
-- (not to a revision) and take effect immediately for the creator or an
-- admin, like scoped single-date edits. Together with the revision's link
-- attachments they share one cap of 3 per event, enforced by the service.
BEGIN;

CREATE TABLE IF NOT EXISTS event_files (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id uuid NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
  filename text NOT NULL
    CONSTRAINT event_file_name_shape CHECK (
      btrim(filename) <> '' AND char_length(filename) <= 120
    ),
  content_type text NOT NULL
    CONSTRAINT event_file_type_not_blank CHECK (btrim(content_type) <> ''),
  size integer NOT NULL CONSTRAINT event_file_size_positive CHECK (size > 0),
  data bytea NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS event_files_event_idx
  ON event_files(event_id, created_at, id);

COMMIT;
