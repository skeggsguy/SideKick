-- Calibrated approval prediction (v3 Track C): the utility model's forecast
-- P(user approves), written fire-and-forget while the approval waits on a
-- human. NULL = no prediction captured (predictor off, failed, or predates
-- this column). Additive — auto-applies on next open (004/006 precedent).
ALTER TABLE approvals ADD COLUMN predicted_p REAL;
