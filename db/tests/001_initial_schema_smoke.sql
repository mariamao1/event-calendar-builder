\set ON_ERROR_STOP on

BEGIN;

DO $$
DECLARE
  v_group_id uuid;
  v_event_id uuid;
  v_first_revision_id uuid;
  v_edit_revision_id uuid;
  v_occurrence_id uuid;
  v_subscription_id uuid;
  v_delivery_id uuid;
  rejected_write boolean;
BEGIN
  INSERT INTO groups (slug, name)
  VALUES ('neighborhood', 'Neighborhood')
  RETURNING id INTO v_group_id;

  INSERT INTO events (
    original_submitter_name,
    original_submitter_channel,
    original_submitter_contact
  ) VALUES ('Ada', 'email', 'ada@example.test')
  RETURNING id INTO v_event_id;

  INSERT INTO event_revisions (
    event_id,
    revision_number,
    title,
    is_all_day,
    starts_at,
    ends_at,
    timezone,
    submitted_by_name,
    submitted_by_channel,
    submitted_by_contact
  ) VALUES (
    v_event_id,
    1,
    'Community picnic',
    false,
    '2027-06-01 16:00:00-04',
    '2027-06-01 18:00:00-04',
    'America/New_York',
    'Ada',
    'email',
    'ada@example.test'
  ) RETURNING id INTO v_first_revision_id;

  INSERT INTO event_revision_groups (event_revision_id, group_id)
  VALUES (v_first_revision_id, v_group_id);

  UPDATE events
     SET current_revision_id = v_first_revision_id
   WHERE id = v_event_id;

  rejected_write := false;
  BEGIN
    UPDATE events
       SET published_revision_id = v_first_revision_id
     WHERE id = v_event_id;
  EXCEPTION WHEN OTHERS THEN
    rejected_write := true;
  END;
  ASSERT rejected_write, 'a pending revision was publishable';

  UPDATE event_revisions
     SET approval_status = 'approved',
         reviewed_at = now(),
         reviewed_by = 'moderator@example.test'
   WHERE id = v_first_revision_id;

  INSERT INTO event_review_actions (
    event_id,
    event_revision_id,
    action,
    actor
  ) VALUES (
    v_event_id,
    v_first_revision_id,
    'approve',
    'moderator@example.test'
  );

  UPDATE events
     SET published_revision_id = v_first_revision_id
   WHERE id = v_event_id;

  rejected_write := false;
  BEGIN
    UPDATE event_revisions
       SET title = 'Unreviewed title change'
     WHERE id = v_first_revision_id;
  EXCEPTION WHEN OTHERS THEN
    rejected_write := true;
  END;
  ASSERT rejected_write, 'approved content was mutable';

  rejected_write := false;
  BEGIN
    DELETE FROM event_revision_groups
     WHERE event_revision_id = v_first_revision_id
       AND group_id = v_group_id;
  EXCEPTION WHEN OTHERS THEN
    rejected_write := true;
  END;
  ASSERT rejected_write, 'approved group membership was mutable';

  INSERT INTO event_revisions (
    event_id,
    revision_number,
    supersedes_revision_id,
    title,
    is_all_day,
    starts_at,
    ends_at,
    timezone,
    submitted_by_name,
    submitted_by_channel,
    submitted_by_contact
  ) VALUES (
    v_event_id,
    2,
    v_first_revision_id,
    'Community picnic and games',
    false,
    '2027-06-01 16:00:00-04',
    '2027-06-01 19:00:00-04',
    'America/New_York',
    'Ada',
    'email',
    'ada@example.test'
  ) RETURNING id INTO v_edit_revision_id;

  INSERT INTO event_revision_groups (event_revision_id, group_id)
  VALUES (v_edit_revision_id, v_group_id);

  UPDATE events
     SET current_revision_id = v_edit_revision_id
   WHERE id = v_event_id;

  ASSERT (
    SELECT approval_status = 'pending' AND is_published
      FROM current_event_state
     WHERE current_event_state.event_id = v_event_id
  ), 'pending edit did not preserve the published revision';

  INSERT INTO event_occurrences (
    event_id,
    source_revision_id,
    recurrence_id,
    is_all_day,
    starts_at,
    ends_at,
    timezone
  ) VALUES (
    v_event_id,
    v_first_revision_id,
    TIMESTAMP '2027-06-01 16:00:00',
    false,
    '2027-06-01 16:00:00-04',
    '2027-06-01 18:00:00-04',
    'America/New_York'
  ) RETURNING id INTO v_occurrence_id;

  INSERT INTO subscriptions (
    contact_channel,
    contact_value,
    contact_value_normalized,
    status,
    cadence,
    timezone,
    management_token_hash,
    confirmed_at
  ) VALUES (
    'email',
    'reader@example.test',
    'reader@example.test',
    'active',
    'daily',
    'America/New_York',
    digest('stable-management-token', 'sha256'),
    now()
  ) RETURNING id INTO v_subscription_id;

  INSERT INTO subscription_groups (subscription_id, group_id)
  VALUES (v_subscription_id, v_group_id);

  INSERT INTO notification_deliveries (
    subscription_id,
    format,
    scheduled_for,
    recipient_channel,
    recipient_contact
  ) VALUES (
    v_subscription_id,
    'digest',
    now(),
    'email',
    'reader@example.test'
  ) RETURNING id INTO v_delivery_id;

  INSERT INTO notification_delivery_items (
    delivery_id,
    subscription_id,
    occurrence_id,
    event_id,
    event_revision_id,
    occurrence_version,
    change_kind
  ) VALUES (
    v_delivery_id,
    v_subscription_id,
    v_occurrence_id,
    v_event_id,
    v_first_revision_id,
    1,
    'event_published'
  );

  rejected_write := false;
  BEGIN
    INSERT INTO notification_delivery_items (
      delivery_id,
      subscription_id,
      occurrence_id,
      event_id,
      event_revision_id,
      occurrence_version,
      change_kind
    ) VALUES (
      v_delivery_id,
      v_subscription_id,
      v_occurrence_id,
      v_event_id,
      v_first_revision_id,
      1,
      'event_updated'
    );
  EXCEPTION WHEN unique_violation THEN
    rejected_write := true;
  END;
  ASSERT rejected_write, 'duplicate notification item was accepted';
END;
$$;

ROLLBACK;
