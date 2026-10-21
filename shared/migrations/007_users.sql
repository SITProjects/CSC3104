-- Admins see every job and the nodes. Signup always makes USER; promote by hand:
--   UPDATE users SET role = 'ADMIN' WHERE email = '...';
CREATE TYPE user_role AS ENUM ('USER', 'ADMIN');

-- User accounts for the public API. Emails are stored lower-cased, so the
-- unique constraint is case-insensitive.
CREATE TABLE users (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email          TEXT NOT NULL UNIQUE CHECK (email = lower(email)),
    password_hash  TEXT NOT NULL,                      -- scrypt$n$r$p$salt$hash
    role           user_role NOT NULL DEFAULT 'USER',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Login sessions. Only the SHA-256 of the bearer token is kept, like replicas.token_hash.
CREATE TABLE user_sessions (
    token_hash  CHAR(64) PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX user_sessions_user_id_idx ON user_sessions (user_id);

-- Jobs belong to a user instead of a free-text email. Jobs made before accounts
-- existed get a user per owner_email whose password_hash can never match, so
-- nothing is lost and nobody can sign in as them.
ALTER TABLE jobs ADD COLUMN owner_id UUID REFERENCES users(id);
INSERT INTO users (email, password_hash)
SELECT DISTINCT lower(owner_email), '!' FROM jobs
ON CONFLICT (email) DO NOTHING;
UPDATE jobs SET owner_id = users.id FROM users WHERE users.email = lower(jobs.owner_email);
ALTER TABLE jobs ALTER COLUMN owner_id SET NOT NULL;
ALTER TABLE jobs DROP COLUMN owner_email;
CREATE INDEX jobs_owner_id_idx ON jobs (owner_id, created_at DESC);
