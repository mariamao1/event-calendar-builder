-- Featured events. The calendar owner (an admin) pins up to 3 events so they
-- stand out in a strip above the calendar. A pin is owner curation rather
-- than event content, so it lives on the event instead of a revision and
-- never goes through review. A pin only counts while its event is published,
-- not cancelled, and still has a scheduled date that has not ended; past
-- pins drop out of the feature on their own and are cleared on the next pin.
BEGIN;

ALTER TABLE events ADD COLUMN IF NOT EXISTS featured_at timestamptz;

CREATE INDEX IF NOT EXISTS events_featured_idx
  ON events(featured_at DESC)
  WHERE featured_at IS NOT NULL;

COMMIT;
