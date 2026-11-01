-- Rollback of the open-loops feature (was migration 008, since deleted):
-- the sweep missed most closed loops and contextual Gmail reading covers
-- the need. IF EXISTS makes this a no-op on fresh DBs that never ran 008.
DROP TABLE IF EXISTS commitments;
DROP TABLE IF EXISTS contact_latency;
