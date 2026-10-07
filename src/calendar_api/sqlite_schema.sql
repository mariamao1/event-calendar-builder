PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS groups (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL CHECK (trim(name) <> ''),
  description TEXT NOT NULL DEFAULT '',
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  current_revision_id TEXT,
  published_revision_id TEXT,
  original_submitter_name TEXT NOT NULL,
  original_submitter_channel TEXT NOT NULL
    CHECK (original_submitter_channel IN ('email', 'sms')),
  original_submitter_contact TEXT NOT NULL,
  management_token_hash BLOB
    CHECK (management_token_hash IS NULL OR length(management_token_hash) = 32),
  submitted_at TEXT NOT NULL,
  archived_at TEXT,
  cancelled_at TEXT,
  cancelled_by TEXT,
  cancel_reason TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (current_revision_id) REFERENCES event_revisions(id),
  FOREIGN KEY (published_revision_id) REFERENCES event_revisions(id)
);

CREATE TABLE IF NOT EXISTS event_revisions (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id),
  revision_number INTEGER NOT NULL CHECK (revision_number > 0),
  supersedes_revision_id TEXT REFERENCES event_revisions(id),
  approval_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (approval_status IN ('pending', 'approved', 'rejected', 'revoked')),
  title TEXT NOT NULL CHECK (trim(title) <> ''),
  description TEXT NOT NULL DEFAULT '',
  location_name TEXT,
  location_address TEXT,
  event_url TEXT,
  is_all_day INTEGER NOT NULL CHECK (is_all_day IN (0, 1)),
  starts_at TEXT,
  ends_at TEXT,
  start_date TEXT,
  end_date TEXT,
  timezone TEXT NOT NULL,
  recurrence_rule TEXT,
  submitted_by_name TEXT NOT NULL,
  submitted_by_channel TEXT NOT NULL
    CHECK (submitted_by_channel IN ('email', 'sms')),
  submitted_by_contact TEXT NOT NULL,
  submitted_at TEXT NOT NULL,
  reviewed_at TEXT,
  reviewed_by TEXT,
  review_note TEXT,
  UNIQUE (event_id, revision_number)
);

CREATE TABLE IF NOT EXISTS event_revision_groups (
  event_revision_id TEXT NOT NULL REFERENCES event_revisions(id) ON DELETE CASCADE,
  group_id TEXT NOT NULL REFERENCES groups(id),
  created_at TEXT NOT NULL,
  PRIMARY KEY (event_revision_id, group_id)
);

CREATE TABLE IF NOT EXISTS event_revision_recurrence_dates (
  event_revision_id TEXT NOT NULL REFERENCES event_revisions(id) ON DELETE CASCADE,
  local_start TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('include', 'exclude')),
  created_at TEXT NOT NULL,
  PRIMARY KEY (event_revision_id, local_start)
);

CREATE TABLE IF NOT EXISTS event_review_actions (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id),
  event_revision_id TEXT NOT NULL REFERENCES event_revisions(id),
  action TEXT NOT NULL CHECK (action IN ('approve', 'reject', 'revoke', 'cancel', 'delete', 'restore')),
  actor TEXT NOT NULL,
  note TEXT,
  occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_occurrences (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id),
  source_revision_id TEXT NOT NULL REFERENCES event_revisions(id),
  recurrence_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'scheduled'
    CHECK (status IN ('scheduled', 'cancelled')),
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  is_exception INTEGER NOT NULL DEFAULT 0 CHECK (is_exception IN (0, 1)),
  is_all_day INTEGER NOT NULL CHECK (is_all_day IN (0, 1)),
  starts_at TEXT,
  ends_at TEXT,
  start_date TEXT,
  end_date TEXT,
  timezone TEXT NOT NULL,
  cancellation_reason TEXT,
  -- Per-occurrence divergence from the published series, set by scoped
  -- single/future edits. content_override is a JSON object with any subset
  -- of {title, description, location_name, location_address, event_url};
  -- readers merge it over the published revision. instance_cancelled marks
  -- a scoped cancellation: the date stays visible, flagged as cancelled.
  -- A later series-wide edit clears both, so the latest series change wins.
  content_override TEXT,
  instance_cancelled INTEGER NOT NULL DEFAULT 0 CHECK (instance_cancelled IN (0, 1)),
  -- A single-date removal ('cancelled' keeps the date visible and flagged;
  -- 'skipped' removes it). Unlike per-date edits it survives later
  -- series-wide edits while the series still produces that date, and it can
  -- be restored.
  instance_exception TEXT CHECK (instance_exception IN ('cancelled', 'skipped')),
  materialized_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (event_id, recurrence_id)
);

CREATE TABLE IF NOT EXISTS event_occurrence_materializations (
  event_id TEXT PRIMARY KEY REFERENCES events(id),
  source_revision_id TEXT NOT NULL REFERENCES event_revisions(id),
  window_start TEXT NOT NULL,
  window_end_exclusive TEXT NOT NULL,
  completed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS local_event_revisions_review_queue_idx
  ON event_revisions(approval_status, submitted_at, event_id);
CREATE INDEX IF NOT EXISTS local_event_revision_groups_group_idx
  ON event_revision_groups(group_id, event_revision_id);
CREATE INDEX IF NOT EXISTS local_event_occurrences_timed_calendar_idx
  ON event_occurrences(starts_at, ends_at, event_id)
  WHERE status = 'scheduled' AND is_all_day = 0;
CREATE INDEX IF NOT EXISTS local_event_occurrences_all_day_calendar_idx
  ON event_occurrences(start_date, end_date, event_id)
  WHERE status = 'scheduled' AND is_all_day = 1;
