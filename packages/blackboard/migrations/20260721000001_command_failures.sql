-- migrate:up
-- Append-only observability ledger of failed commands (spec 001-routing-thrash, T1).
-- A failed mutating command previously left a trace only in structured logs; this
-- ledger makes command failures a first-class, queryable signal so the per-goal thrash
-- report can count conflicts / stale-version misses derived from real command results.
--
-- Deliberately NOT an aggregate and NOT part of idempotency: every failed attempt is
-- one row (dedup would defeat the point). NO foreign key to goals/tasks — a failure may
-- reference a not-yet-existing or already-deleted aggregate, and the ledger must record
-- the attempt regardless. Because there is no goals FK, this table is truncated
-- explicitly by the test harness / reset-demo (it does not ride the goals cascade).
create table command_failures (
    failure_id bigserial primary key,
    command_id uuid not null,
    tool_name text not null,
    actor_id text not null,
    goal_id uuid,
    task_id uuid,
    error_code text not null,
    occurred_at timestamptz not null default now()
);

-- The thrash report filters by goal scope and error code; task-scoped failures (goal_id
-- null) are joined back to their goal via tasks at read time.
create index command_failures_goal_error_idx on command_failures (goal_id, error_code);
create index command_failures_task_idx on command_failures (task_id);

-- migrate:down
drop table if exists command_failures;
