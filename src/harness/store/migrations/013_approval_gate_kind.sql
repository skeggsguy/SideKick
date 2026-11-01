-- Which egress gate parked a READ_ONLY approval (policy GATE_* constant:
-- secret_read | internal_probe | egress_budget | new_destination). NULL for
-- classification-gated rows and rows from before this column existed.
ALTER TABLE approvals ADD COLUMN gate_kind TEXT;
