-- Durable, restorable single-date exceptions of a recurring event.
--
-- instance_exception records a date cancelled ('cancelled', still visible and
-- flagged) or deleted ('skipped', removed from the calendar) on its own.
-- Unlike per-date content edits, it survives later series-wide edits while
-- the series still produces that date (following the date across a
-- time-of-day change), and the new 'restore' review action undoes it.
-- Removals from a "future" scope truncate the series rule instead and are
-- not recorded here.
--
-- ALTER TYPE ... ADD VALUE cannot run inside a transaction block, so this
-- migration intentionally has no BEGIN/COMMIT wrapper. The migration runner
-- applies files with autocommit enabled.

ALTER TYPE event_review_action ADD VALUE IF NOT EXISTS 'restore';

ALTER TABLE event_occurrences
  ADD COLUMN IF NOT EXISTS instance_exception text
    CONSTRAINT event_occurrence_instance_exception_kind
    CHECK (instance_exception IN ('cancelled', 'skipped'));

-- Earlier single-date deletions are identifiable by their reason. Earlier
-- single-date cancellations cannot be told apart from "future" ones, so they
-- stay as they were.
UPDATE event_occurrences
   SET instance_exception = 'skipped'
 WHERE status = 'cancelled'
   AND cancellation_reason = 'deleted single occurrence'
   AND instance_exception IS NULL;
