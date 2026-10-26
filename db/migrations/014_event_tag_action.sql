-- Admins add group tags to many events at once without editing them. A tag
-- change publishes a copy of the event's published revision with the extra
-- groups, leaves occurrences alone, and records the new 'tag' review action,
-- distinct from an approval of new content.
--
-- ALTER TYPE ... ADD VALUE cannot run inside a transaction block, so this
-- migration intentionally has no BEGIN/COMMIT wrapper. The migration runner
-- applies files with autocommit enabled.

ALTER TYPE event_review_action ADD VALUE IF NOT EXISTS 'tag';
