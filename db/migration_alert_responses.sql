-- ============================================================================
-- Migration: Alert Responses (citizen tip line)
-- Lets any user send information about an existing alert
-- (e.g. "I saw the reported bicycle near Pettah"). Works for all alert types.
-- ============================================================================

CREATE TABLE IF NOT EXISTS alert_responses (
    id            SERIAL PRIMARY KEY,
    alert_id      INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    responder_id  INTEGER REFERENCES users(id),
    message       TEXT NOT NULL,
    latitude      DOUBLE PRECISION,
    longitude     DOUBLE PRECISION,
    contact_info  TEXT,
    created_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alert_responses_alert ON alert_responses(alert_id);
