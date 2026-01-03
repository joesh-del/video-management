-- Add 2FA columns to users table
-- Run: psql -h mv-database.cshawwjevydx.us-east-1.rds.amazonaws.com -U postgres -d video_management -f migrations/007_add_2fa.sql

ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret VARCHAR(32);
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled INTEGER DEFAULT 0;

-- Index for faster lookups
CREATE INDEX IF NOT EXISTS idx_users_totp_enabled ON users(totp_enabled);
