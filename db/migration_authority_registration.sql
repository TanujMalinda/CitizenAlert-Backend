-- Authority Registration Migration
-- Run this once in pgAdmin on the citizenalert database

-- 1. Add account status to users (active = normal, pending_approval = waiting review, rejected = denied)
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS account_status VARCHAR(20) NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS designation    VARCHAR(100),
    ADD COLUMN IF NOT EXISTS department     VARCHAR(150),
    ADD COLUMN IF NOT EXISTS employee_id    VARCHAR(50);

-- 2. Add super_admin role support (no schema change needed — stored as role = 'super_admin')

-- 3. Seed the first super-admin (change email/password before running)
--    Password below is:  Admin@1234
INSERT INTO users (full_name, email, password_hash, role, account_status, district)
VALUES (
    'System Administrator',
    'admin@citizenalert.lk',
    '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMUMted9bwGb4nxZ6HerKlHmVK',
    'super_admin',
    'active',
    'Colombo'
)
ON CONFLICT (email) DO NOTHING;
