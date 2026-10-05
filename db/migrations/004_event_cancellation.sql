-- Distinguish cancellation from deletion.
--
-- Cancellation means "this event is cancelled": the published content stays
-- visible (flagged via events.cancelled_at) because people may already have
-- planned around it. Deletion means "this event should no longer exist" and
-- reuses the existing events.archived_at soft-delete, which already hides
-- the event from every public read.
--
-- ALTER TYPE ... ADD VALUE cannot run inside a transaction block, so this
-- migration intentionally has no BEGIN/COMMIT wrapper. The migration runner
-- applies files with autocommit enabled.

ALTER TYPE event_review_action ADD VALUE IF NOT EXISTS 'cancel';
ALTER TYPE event_review_action ADD VALUE IF NOT EXISTS 'delete';

ALTER TABLE events ADD COLUMN IF NOT EXISTS cancelled_at timestamptz;
ALTER TABLE events ADD COLUMN IF NOT EXISTS cancelled_by text;
ALTER TABLE events ADD COLUMN IF NOT EXISTS cancel_reason text;
