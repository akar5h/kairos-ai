-- 0017 eval_sets MCC scores
ALTER TABLE eval_sets
    ADD COLUMN IF NOT EXISTS mcc float,
    ADD COLUMN IF NOT EXISTS mcc_label_count int,
    ADD COLUMN IF NOT EXISTS mcc_computed_at timestamptz;
