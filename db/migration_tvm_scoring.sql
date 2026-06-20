-- ============================================================================
-- Migration: TVM scoring schema fixes
-- ----------------------------------------------------------------------------
-- 1. tvm_score must hold a decimal confidence value (0.000–1.000), not an int.
--    Old disaster rows used the 0–100 scale (100 = 100%); normalise to 0–1.
-- 2. users.trust_score is the reporter-credibility factor (0.30 weight in
--    TVM Tier 2). It was missing from the schema, causing a 500 on submission.
-- Run once per database (safe to re-run).
-- ============================================================================

-- 1. tvm_score → NUMERIC(5,3), normalising any legacy 0–100 values
ALTER TABLE alerts
    ALTER COLUMN tvm_score TYPE NUMERIC(5,3)
    USING (CASE WHEN tvm_score > 1 THEN tvm_score / 100.0 ELSE tvm_score END)::numeric;

-- 2. Reporter trust score (neutral default 0.50 for everyone)
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS trust_score NUMERIC(4,3) NOT NULL DEFAULT 0.500;

-- ----------------------------------------------------------------------------
-- 3. traffic_hazards: align legacy schema with the route code.
--    Old schema had road_name/estimated_duration; code uses road_segment,
--    confirmation_count (crowdsourced consensus) and expected_clear_time.
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'traffic_hazards' AND column_name = 'road_name')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'traffic_hazards' AND column_name = 'road_segment')
    THEN
        ALTER TABLE traffic_hazards RENAME COLUMN road_name TO road_segment;
    END IF;
END $$;

ALTER TABLE traffic_hazards
    ADD COLUMN IF NOT EXISTS confirmation_count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE traffic_hazards
    ADD COLUMN IF NOT EXISTS expected_clear_time TIMESTAMP WITH TIME ZONE;
