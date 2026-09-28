BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE contact_channel AS ENUM ('email', 'sms');
CREATE TYPE event_approval_status AS ENUM (
  'pending',
  'approved',
  'rejected',
  'revoked'
);
CREATE TYPE event_review_action AS ENUM ('approve', 'reject', 'revoke');
CREATE TYPE recurrence_date_kind AS ENUM ('include', 'exclude');
CREATE TYPE occurrence_status AS ENUM ('scheduled', 'cancelled');
CREATE TYPE subscription_status AS ENUM (
  'pending_confirmation',
  'active',
  'paused',
  'unsubscribed'
);
CREATE TYPE notification_cadence AS ENUM ('immediate', 'daily', 'weekly');
CREATE TYPE notification_delivery_status AS ENUM (
  'queued',
  'sending',
  'sent',
  'failed',
  'suppressed'
);
CREATE TYPE notification_delivery_format AS ENUM ('immediate', 'digest');
CREATE TYPE notification_change_kind AS ENUM (
  'event_published',
  'event_updated',
  'event_cancelled'
);
CREATE TYPE notification_attempt_outcome AS ENUM ('succeeded', 'failed');

CREATE TABLE groups (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug text NOT NULL UNIQUE,
  name text NOT NULL,
  description text NOT NULL DEFAULT '',
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT groups_slug_format CHECK (
    slug = lower(slug)
    AND slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'
  ),
  CONSTRAINT groups_name_not_blank CHECK (btrim(name) <> '')
);

-- An event is a durable identity. Its mutable content lives in revisions.
-- The revision foreign keys are added after event_revisions is created.
CREATE TABLE events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  current_revision_id uuid,
  published_revision_id uuid,
  original_submitter_name text NOT NULL,
  original_submitter_channel contact_channel NOT NULL,
  original_submitter_contact text NOT NULL,
  submitted_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT events_submitter_name_not_blank
    CHECK (btrim(original_submitter_name) <> ''),
  CONSTRAINT events_submitter_contact_not_blank
    CHECK (btrim(original_submitter_contact) <> '')
);

CREATE TABLE event_revisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id uuid NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
  revision_number integer NOT NULL CHECK (revision_number > 0),
  supersedes_revision_id uuid,
  approval_status event_approval_status NOT NULL DEFAULT 'pending',

  title text NOT NULL,
  description text NOT NULL DEFAULT '',
  location_name text,
  location_address text,
  event_url text,

  is_all_day boolean NOT NULL DEFAULT false,
  starts_at timestamptz,
  ends_at timestamptz,
  start_date date,
  end_date date,
  timezone text NOT NULL,
  recurrence_rule text,

  submitted_by_name text NOT NULL,
  submitted_by_channel contact_channel NOT NULL,
  submitted_by_contact text NOT NULL,
  submitted_at timestamptz NOT NULL DEFAULT now(),
  reviewed_at timestamptz,
  reviewed_by text,
  review_note text,

  UNIQUE (event_id, revision_number),
  UNIQUE (id, event_id),
  CONSTRAINT event_revision_title_not_blank CHECK (btrim(title) <> ''),
  CONSTRAINT event_revision_timezone_not_blank CHECK (btrim(timezone) <> ''),
  CONSTRAINT event_revision_submitter_name_not_blank
    CHECK (btrim(submitted_by_name) <> ''),
  CONSTRAINT event_revision_submitter_contact_not_blank
    CHECK (btrim(submitted_by_contact) <> ''),
  CONSTRAINT event_revision_recurrence_rule_not_blank
    CHECK (recurrence_rule IS NULL OR btrim(recurrence_rule) <> ''),
  CONSTRAINT event_revision_timing_shape CHECK (
    (
      is_all_day
      AND start_date IS NOT NULL
      AND end_date IS NOT NULL
      AND end_date > start_date
      AND starts_at IS NULL
      AND ends_at IS NULL
    )
    OR
    (
      NOT is_all_day
      AND starts_at IS NOT NULL
      AND ends_at IS NOT NULL
      AND ends_at > starts_at
      AND start_date IS NULL
      AND end_date IS NULL
    )
  ),
  CONSTRAINT event_revision_review_metadata CHECK (
    (
      approval_status = 'pending'
      AND reviewed_at IS NULL
      AND reviewed_by IS NULL
    )
    OR
    (
      approval_status <> 'pending'
      AND reviewed_at IS NOT NULL
      AND reviewed_by IS NOT NULL
      AND btrim(reviewed_by) <> ''
    )
  ),
  CONSTRAINT event_revision_chain_shape CHECK (
    (revision_number = 1 AND supersedes_revision_id IS NULL)
    OR (revision_number > 1 AND supersedes_revision_id IS NOT NULL)
  ),
  CONSTRAINT event_revision_supersedes_same_event
    FOREIGN KEY (supersedes_revision_id, event_id)
    REFERENCES event_revisions(id, event_id)
    DEFERRABLE INITIALLY DEFERRED
);

ALTER TABLE events
  ADD CONSTRAINT events_current_revision_same_event
    FOREIGN KEY (current_revision_id, id)
    REFERENCES event_revisions(id, event_id)
    DEFERRABLE INITIALLY DEFERRED,
  ADD CONSTRAINT events_published_revision_same_event
    FOREIGN KEY (published_revision_id, id)
    REFERENCES event_revisions(id, event_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE event_revision_groups (
  event_revision_id uuid NOT NULL
    REFERENCES event_revisions(id) ON DELETE CASCADE,
  group_id uuid NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (event_revision_id, group_id)
);

-- RRULE is stored on event_revisions. These local values implement RFC 5545
-- RDATE/EXDATE semantics. Midnight is used for all-day revisions.
CREATE TABLE event_revision_recurrence_dates (
  event_revision_id uuid NOT NULL
    REFERENCES event_revisions(id) ON DELETE CASCADE,
  local_start timestamp without time zone NOT NULL,
  kind recurrence_date_kind NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (event_revision_id, local_start)
);

CREATE TABLE event_review_actions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id uuid NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
  event_revision_id uuid NOT NULL,
  action event_review_action NOT NULL,
  actor text NOT NULL,
  note text,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT event_review_action_actor_not_blank CHECK (btrim(actor) <> ''),
  CONSTRAINT event_review_action_revision_same_event
    FOREIGN KEY (event_revision_id, event_id)
    REFERENCES event_revisions(id, event_id)
    ON DELETE RESTRICT
);

-- Occurrences are a durable, rolling materialization of the published
-- revision. recurrence_id is the original local slot, so a rescheduled
-- occurrence keeps its identity and notification history.
CREATE TABLE event_occurrences (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id uuid NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
  source_revision_id uuid NOT NULL,
  recurrence_id timestamp without time zone NOT NULL,
  status occurrence_status NOT NULL DEFAULT 'scheduled',
  version integer NOT NULL DEFAULT 1 CHECK (version > 0),
  is_exception boolean NOT NULL DEFAULT false,

  is_all_day boolean NOT NULL,
  starts_at timestamptz,
  ends_at timestamptz,
  start_date date,
  end_date date,
  timezone text NOT NULL,

  cancellation_reason text,
  materialized_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE (event_id, recurrence_id),
  UNIQUE (id, event_id),
  CONSTRAINT event_occurrence_source_same_event
    FOREIGN KEY (source_revision_id, event_id)
    REFERENCES event_revisions(id, event_id)
    ON DELETE RESTRICT,
  CONSTRAINT event_occurrence_timezone_not_blank CHECK (btrim(timezone) <> ''),
  CONSTRAINT event_occurrence_timing_shape CHECK (
    (
      is_all_day
      AND start_date IS NOT NULL
      AND end_date IS NOT NULL
      AND end_date > start_date
      AND starts_at IS NULL
      AND ends_at IS NULL
    )
    OR
    (
      NOT is_all_day
      AND starts_at IS NOT NULL
      AND ends_at IS NOT NULL
      AND ends_at > starts_at
      AND start_date IS NULL
      AND end_date IS NULL
    )
  ),
  CONSTRAINT event_occurrence_cancellation_reason CHECK (
    status = 'cancelled' OR cancellation_reason IS NULL
  )
);

CREATE TABLE event_occurrence_materializations (
  event_id uuid PRIMARY KEY REFERENCES events(id) ON DELETE RESTRICT,
  source_revision_id uuid NOT NULL,
  window_start date NOT NULL,
  window_end_exclusive date NOT NULL,
  completed_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT event_materialization_window_valid
    CHECK (window_end_exclusive > window_start),
  CONSTRAINT event_materialization_revision_same_event
    FOREIGN KEY (source_revision_id, event_id)
    REFERENCES event_revisions(id, event_id)
    ON DELETE RESTRICT
);

CREATE TABLE subscriptions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_channel contact_channel NOT NULL,
  contact_value text NOT NULL,
  contact_value_normalized text NOT NULL,
  status subscription_status NOT NULL DEFAULT 'pending_confirmation',
  cadence notification_cadence NOT NULL DEFAULT 'daily',
  timezone text NOT NULL,
  digest_time time without time zone NOT NULL DEFAULT TIME '09:00:00',
  digest_weekday smallint,

  -- Store SHA-256 digests of application-generated 256-bit random tokens.
  -- Raw management/confirmation tokens must never be persisted or logged.
  management_token_hash bytea NOT NULL UNIQUE,
  confirmation_token_hash bytea UNIQUE,
  confirmation_expires_at timestamptz,
  confirmed_at timestamptz,
  unsubscribed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE (contact_channel, contact_value_normalized),
  CONSTRAINT subscription_contact_not_blank CHECK (btrim(contact_value) <> ''),
  CONSTRAINT subscription_normalized_contact_not_blank
    CHECK (btrim(contact_value_normalized) <> ''),
  CONSTRAINT subscription_timezone_not_blank CHECK (btrim(timezone) <> ''),
  CONSTRAINT subscription_management_token_hash_length
    CHECK (octet_length(management_token_hash) = 32),
  CONSTRAINT subscription_confirmation_token_hash_length
    CHECK (
      confirmation_token_hash IS NULL
      OR octet_length(confirmation_token_hash) = 32
    ),
  CONSTRAINT subscription_confirmation_pair CHECK (
    (confirmation_token_hash IS NULL) = (confirmation_expires_at IS NULL)
  ),
  CONSTRAINT subscription_weekday_for_weekly_only CHECK (
    (cadence = 'weekly' AND digest_weekday BETWEEN 0 AND 6)
    OR (cadence <> 'weekly' AND digest_weekday IS NULL)
  ),
  CONSTRAINT subscription_confirmation_state CHECK (
    (
      status = 'pending_confirmation'
      AND confirmed_at IS NULL
      AND confirmation_token_hash IS NOT NULL
    )
    OR
    (
      status <> 'pending_confirmation'
      AND confirmed_at IS NOT NULL
    )
  ),
  CONSTRAINT subscription_unsubscribe_state CHECK (
    (status = 'unsubscribed' AND unsubscribed_at IS NOT NULL)
    OR (status <> 'unsubscribed' AND unsubscribed_at IS NULL)
  )
);

CREATE TABLE subscription_groups (
  subscription_id uuid NOT NULL
    REFERENCES subscriptions(id) ON DELETE CASCADE,
  group_id uuid NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (subscription_id, group_id)
);

-- A delivery is one provider message. A daily/weekly digest can own many
-- items; an immediate delivery normally owns one.
CREATE TABLE notification_deliveries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  subscription_id uuid NOT NULL
    REFERENCES subscriptions(id) ON DELETE RESTRICT,
  format notification_delivery_format NOT NULL,
  status notification_delivery_status NOT NULL DEFAULT 'queued',
  scheduled_for timestamptz NOT NULL,
  idempotency_key uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,

  -- Recipient snapshots make the audit trail meaningful after an edit.
  recipient_channel contact_channel NOT NULL,
  recipient_contact text NOT NULL,
  provider_message_id text,
  sent_at timestamptz,
  locked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE (id, subscription_id),
  CONSTRAINT notification_recipient_not_blank
    CHECK (btrim(recipient_contact) <> ''),
  CONSTRAINT notification_sent_has_timestamp
    CHECK (status <> 'sent' OR sent_at IS NOT NULL)
);

CREATE UNIQUE INDEX notification_deliveries_provider_message_id_unique
  ON notification_deliveries(provider_message_id)
  WHERE provider_message_id IS NOT NULL;

CREATE TABLE notification_delivery_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  delivery_id uuid NOT NULL,
  subscription_id uuid NOT NULL,
  occurrence_id uuid NOT NULL,
  event_id uuid NOT NULL,
  event_revision_id uuid NOT NULL,
  occurrence_version integer NOT NULL CHECK (occurrence_version > 0),
  change_kind notification_change_kind NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),

  -- The key below is the durable duplicate-send guard. Retries reuse the
  -- delivery rather than creating another item.
  UNIQUE (subscription_id, occurrence_id, occurrence_version),
  CONSTRAINT notification_item_delivery_subscription
    FOREIGN KEY (delivery_id, subscription_id)
    REFERENCES notification_deliveries(id, subscription_id)
    ON DELETE CASCADE,
  CONSTRAINT notification_item_occurrence_event
    FOREIGN KEY (occurrence_id, event_id)
    REFERENCES event_occurrences(id, event_id)
    ON DELETE RESTRICT,
  CONSTRAINT notification_item_revision_event
    FOREIGN KEY (event_revision_id, event_id)
    REFERENCES event_revisions(id, event_id)
    ON DELETE RESTRICT
);

CREATE TABLE notification_delivery_item_groups (
  notification_delivery_item_id uuid NOT NULL
    REFERENCES notification_delivery_items(id) ON DELETE CASCADE,
  group_id uuid NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
  PRIMARY KEY (notification_delivery_item_id, group_id)
);

CREATE TABLE notification_delivery_attempts (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  delivery_id uuid NOT NULL
    REFERENCES notification_deliveries(id) ON DELETE CASCADE,
  attempt_number integer NOT NULL CHECK (attempt_number > 0),
  outcome notification_attempt_outcome NOT NULL,
  provider_message_id text,
  error_code text,
  error_detail text,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz NOT NULL,
  UNIQUE (delivery_id, attempt_number),
  CONSTRAINT notification_attempt_time_order CHECK (finished_at >= started_at)
);

CREATE INDEX event_revisions_review_queue_idx
  ON event_revisions(submitted_at, event_id)
  WHERE approval_status = 'pending';
CREATE INDEX event_revision_groups_group_idx
  ON event_revision_groups(group_id, event_revision_id);
CREATE INDEX event_occurrences_calendar_idx
  ON event_occurrences(starts_at, event_id)
  WHERE status = 'scheduled' AND NOT is_all_day;
CREATE INDEX event_occurrences_all_day_calendar_idx
  ON event_occurrences(start_date, event_id)
  WHERE status = 'scheduled' AND is_all_day;
CREATE INDEX event_occurrences_revision_idx
  ON event_occurrences(source_revision_id);
CREATE INDEX subscription_groups_group_idx
  ON subscription_groups(group_id, subscription_id);
CREATE INDEX notification_deliveries_work_queue_idx
  ON notification_deliveries(scheduled_for, id)
  WHERE status IN ('queued', 'failed');
CREATE INDEX notification_items_occurrence_idx
  ON notification_delivery_items(occurrence_id, subscription_id);

-- The event's moderation state is the state of current_revision_id. The
-- separately exposed published revision may intentionally be older.
CREATE VIEW current_event_state AS
SELECT
  e.id AS event_id,
  e.current_revision_id,
  r.revision_number,
  r.approval_status,
  e.published_revision_id,
  (e.published_revision_id IS NOT NULL) AS is_published,
  e.submitted_at,
  e.updated_at
FROM events e
JOIN event_revisions r ON r.id = e.current_revision_id;

CREATE VIEW published_events AS
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
  r.reviewed_at
FROM events e
JOIN event_revisions r ON r.id = e.published_revision_id
WHERE r.approval_status = 'approved'
  AND e.archived_at IS NULL;

-- A published pointer may only target an approved revision. The composite
-- foreign key already guarantees that the revision belongs to this event.
CREATE FUNCTION enforce_approved_published_revision()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  target_status event_approval_status;
BEGIN
  IF NEW.published_revision_id IS NULL THEN
    RETURN NEW;
  END IF;

  SELECT approval_status
    INTO target_status
    FROM event_revisions
   WHERE id = NEW.published_revision_id
     AND event_id = NEW.id;

  IF target_status IS DISTINCT FROM 'approved' THEN
    RAISE EXCEPTION 'published revision % must be approved',
      NEW.published_revision_id;
  END IF;

  RETURN NEW;
END;
$$;

CREATE TRIGGER events_approved_published_revision
BEFORE INSERT OR UPDATE OF published_revision_id ON events
FOR EACH ROW EXECUTE FUNCTION enforce_approved_published_revision();

-- Content may be edited while pending, but review transitions cannot smuggle
-- in a content change. Final revisions are immutable except approved->revoked.
CREATE FUNCTION enforce_event_revision_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  old_content jsonb;
  new_content jsonb;
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.approval_status <> 'pending' THEN
      RAISE EXCEPTION 'reviewed revision % is immutable', OLD.id;
    END IF;
    RETURN OLD;
  END IF;

  old_content := to_jsonb(OLD)
    - ARRAY['approval_status', 'reviewed_at', 'reviewed_by', 'review_note'];
  new_content := to_jsonb(NEW)
    - ARRAY['approval_status', 'reviewed_at', 'reviewed_by', 'review_note'];

  IF OLD.approval_status = 'pending' THEN
    IF NEW.approval_status = 'pending' THEN
      RETURN NEW;
    END IF;

    IF NEW.approval_status NOT IN ('approved', 'rejected') THEN
      RAISE EXCEPTION 'invalid revision transition: % -> %',
        OLD.approval_status, NEW.approval_status;
    END IF;

    IF old_content IS DISTINCT FROM new_content THEN
      RAISE EXCEPTION 'content cannot change in the review transition for %',
        OLD.id;
    END IF;

    RETURN NEW;
  END IF;

  IF OLD.approval_status = 'approved'
     AND NEW.approval_status = 'revoked'
     AND to_jsonb(OLD) - 'approval_status'
         = to_jsonb(NEW) - 'approval_status' THEN
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'reviewed revision % is immutable', OLD.id;
END;
$$;

CREATE TRIGGER event_revisions_lifecycle
BEFORE UPDATE OR DELETE ON event_revisions
FOR EACH ROW EXECUTE FUNCTION enforce_event_revision_lifecycle();

-- Group and recurrence membership are part of the reviewed content. Once a
-- revision leaves pending, these child rows cannot be changed.
CREATE FUNCTION enforce_pending_revision_children()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  target_revision_id uuid;
  target_status event_approval_status;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    RAISE EXCEPTION 'revision child keys are immutable; delete and insert';
  END IF;

  target_revision_id := CASE
    WHEN TG_OP = 'DELETE' THEN OLD.event_revision_id
    ELSE NEW.event_revision_id
  END;

  SELECT approval_status
    INTO target_status
    FROM event_revisions
   WHERE id = target_revision_id;

  IF target_status IS DISTINCT FROM 'pending' THEN
    RAISE EXCEPTION 'revision % is not pending and is immutable',
      target_revision_id;
  END IF;

  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;

  RETURN NEW;
END;
$$;

CREATE TRIGGER event_revision_groups_pending_only
BEFORE INSERT OR UPDATE OR DELETE ON event_revision_groups
FOR EACH ROW EXECUTE FUNCTION enforce_pending_revision_children();

CREATE TRIGGER event_revision_recurrence_dates_pending_only
BEFORE INSERT OR UPDATE OR DELETE ON event_revision_recurrence_dates
FOR EACH ROW EXECUTE FUNCTION enforce_pending_revision_children();

CREATE FUNCTION set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

CREATE TRIGGER groups_set_updated_at
BEFORE UPDATE ON groups
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER events_set_updated_at
BEFORE UPDATE ON events
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER event_occurrences_set_updated_at
BEFORE UPDATE ON event_occurrences
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER subscriptions_set_updated_at
BEFORE UPDATE ON subscriptions
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER notification_deliveries_set_updated_at
BEFORE UPDATE ON notification_deliveries
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
