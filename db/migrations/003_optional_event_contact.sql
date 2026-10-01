-- Event submissions require only a title and submitter name. Contact details
-- remain useful for moderation but may be omitted.
BEGIN;

ALTER TABLE events
  DROP CONSTRAINT events_submitter_contact_not_blank;

ALTER TABLE event_revisions
  DROP CONSTRAINT event_revision_submitter_contact_not_blank;

COMMIT;
