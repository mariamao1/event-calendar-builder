-- Accounts for anyone holding the calendar link, not just its owner.
-- Viewing and submitting stay possible without one. An account is an
-- identity; admin is an extra capability granted per account (is_admin),
-- read on every request so granting or revoking it applies immediately.
-- Passwords are stored only as salted PBKDF2 hashes. Usernames are unique
-- ignoring case and keep the casing chosen at registration.
BEGIN;

CREATE TABLE IF NOT EXISTS users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  username text NOT NULL
    CONSTRAINT users_username_shape
      CHECK (username ~ '^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$'),
  password_hash text NOT NULL
    CONSTRAINT users_password_hash_shape
      CHECK (password_hash LIKE 'pbkdf2-sha256$%'),
  is_admin boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS users_username_unique
  ON users(lower(username));

COMMIT;
