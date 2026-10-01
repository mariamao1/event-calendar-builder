-- Accountless event ownership. The raw 256-bit token is returned once to the
-- creator; only its SHA-256 digest is retained. Existing events receive an
-- unclaimable random digest and remain editable by administrators.
BEGIN;

ALTER TABLE events
  ADD COLUMN management_token_hash bytea DEFAULT gen_random_bytes(32);

UPDATE events
   SET management_token_hash = gen_random_bytes(32)
 WHERE management_token_hash IS NULL;

ALTER TABLE events
  ALTER COLUMN management_token_hash SET NOT NULL,
  ADD CONSTRAINT events_management_token_hash_length
    CHECK (octet_length(management_token_hash) = 32),
  ADD CONSTRAINT events_management_token_hash_unique
    UNIQUE (management_token_hash);

COMMIT;
