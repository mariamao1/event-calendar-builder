BEGIN;

-- Scoped single/future edits of a recurring event diverge individual
-- occurrences from the published series without touching the series itself.
-- content_override is a JSON object holding any subset of the published
-- content fields {title, description, location_name, location_address,
-- event_url}; readers merge it over the published revision.
-- instance_cancelled marks a scoped cancellation: the date stays visible,
-- flagged as cancelled (scoped deletion instead cancels the row).
-- A later series-wide edit clears both columns, so the latest change to the
-- series always persists over earlier per-occurrence exceptions.
ALTER TABLE event_occurrences
  ADD COLUMN IF NOT EXISTS content_override text,
  ADD COLUMN IF NOT EXISTS instance_cancelled boolean NOT NULL DEFAULT false;

COMMIT;
