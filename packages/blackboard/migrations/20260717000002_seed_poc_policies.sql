-- migrate:up
-- The POC has no server-side policy tables (authorization is contract-driven in the
-- application layer, per HANDOFF §11 "Open finding" and Appendix A tool matrix).
-- This migration is a placeholder so the migration sequence matches the handoff's
-- 001_initial + 002_seed_poc_policies layout and leaves room for future seed rows.
-- Intentionally a no-op for the PoC.
select 1;

-- migrate:down
select 1;
