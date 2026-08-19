-- migrate:up
create extension if not exists pgcrypto;

create table goals (
    goal_id uuid primary key,
    title text not null,
    objective text not null,
    success_criteria jsonb not null,
    constraints jsonb not null,
    owner jsonb not null,
    state text not null,
    version bigint not null default 0,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table tasks (
    task_id uuid primary key,
    goal_id uuid not null references goals(goal_id) on delete cascade,
    task_key text not null,
    title text not null,
    objective text not null,
    required_actor_kind text not null,
    contract jsonb not null,
    state text not null,
    version bigint not null default 0,
    assignment_epoch bigint not null default 0,
    assigned_actor_id text,
    omnigent_conversation_id text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (goal_id, task_key)
);

create index tasks_goal_state_idx on tasks (goal_id, state);

create table task_dependencies (
    task_id uuid not null references tasks(task_id) on delete cascade,
    depends_on_task_id uuid not null references tasks(task_id) on delete cascade,
    dependency_type text not null default 'completion',
    primary key (task_id, depends_on_task_id, dependency_type),
    constraint no_self_dependency check (task_id <> depends_on_task_id)
);

create table task_assignments (
    assignment_id uuid primary key,
    task_id uuid not null references tasks(task_id) on delete cascade,
    assignment_epoch bigint not null,
    actor_id text not null,
    omnigent_conversation_id text,
    state text not null,
    created_at timestamptz not null default now(),
    ended_at timestamptz
);

-- The final defense against double assignment (handoff §11).
create unique index one_active_assignment_per_task
    on task_assignments (task_id)
    where state in ('assigned', 'running');

create table runtime_runs (
    run_id uuid primary key,
    task_id uuid not null references tasks(task_id) on delete cascade,
    assignment_epoch bigint not null,
    actor_id text not null,
    omnigent_conversation_id text,
    state text not null,
    input_manifest jsonb not null,
    result_manifest jsonb,
    -- Model provenance (handoff §15A.8): required for cost/repro/failure analysis.
    provider text,
    model_id text,
    aws_region text,
    routing_class text,
    harness text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint runtime_runs_routing_class_check check (
        routing_class is null or routing_class in (
            'global_inference_profile',
            'geo_inference_profile',
            'in_region_runtime',
            'regional_mantle'
        )
    )
);

create table artifact_revisions (
    revision_id uuid primary key,
    artifact_id uuid not null,
    goal_id uuid not null references goals(goal_id) on delete cascade,
    produced_by_task_id uuid not null references tasks(task_id),
    produced_by_run_id uuid not null references runtime_runs(run_id),
    artifact_type text not null,
    logical_name text not null,
    content_uri text not null,
    content_hash text not null,
    summary text not null,
    parent_revision_ids uuid[] not null default '{}',
    evidence jsonb not null default '[]'::jsonb,
    status text not null,
    created_at timestamptz not null default now(),
    unique (artifact_id, content_hash)
);

create index artifact_logical_name_idx
    on artifact_revisions (goal_id, logical_name, created_at desc);

create table artifact_aliases (
    goal_id uuid not null references goals(goal_id) on delete cascade,
    logical_name text not null,
    current_revision_id uuid not null references artifact_revisions(revision_id),
    version bigint not null default 0,
    updated_at timestamptz not null default now(),
    primary key (goal_id, logical_name)
);

create table findings (
    finding_id uuid primary key,
    goal_id uuid not null references goals(goal_id) on delete cascade,
    task_id uuid not null references tasks(task_id),
    category text not null,
    severity text not null,
    statement text not null,
    affected_artifacts jsonb not null,
    evidence jsonb not null,
    blocking boolean not null,
    resolution_criteria jsonb not null,
    state text not null,
    version bigint not null default 0,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index findings_goal_open_idx on findings (goal_id, blocking, state);

create table reviews (
    review_id uuid primary key,
    goal_id uuid not null references goals(goal_id) on delete cascade,
    review_task_id uuid not null references tasks(task_id),
    reviewer jsonb not null,
    review_type text not null,
    binding_fingerprint text not null,
    disposition text not null,
    summary text not null,
    evidence jsonb not null,
    finding_ids uuid[] not null default '{}',
    stale boolean not null default false,
    created_at timestamptz not null default now()
);

create table review_artifact_bindings (
    review_id uuid not null references reviews(review_id) on delete cascade,
    artifact_id uuid not null,
    revision_id uuid not null,
    content_hash text not null,
    primary key (review_id, artifact_id, revision_id)
);

-- A reviewer submits at most one review per (task, type, binding) (handoff §8).
create unique index one_review_per_actor_type_binding
    on reviews (
        review_task_id,
        review_type,
        binding_fingerprint,
        ((reviewer ->> 'actor_id'))
    );

create table approvals (
    approval_id uuid primary key,
    goal_id uuid not null references goals(goal_id) on delete cascade,
    approval_type text not null,
    approver jsonb not null,
    binding_fingerprint text not null,
    conditions jsonb not null,
    revoked boolean not null default false,
    created_at timestamptz not null default now()
);

create table approval_artifact_bindings (
    approval_id uuid not null references approvals(approval_id) on delete cascade,
    artifact_id uuid not null,
    revision_id uuid not null,
    content_hash text not null,
    primary key (approval_id, artifact_id, revision_id)
);

create table decisions (
    decision_id uuid primary key,
    goal_id uuid not null references goals(goal_id) on delete cascade,
    question text not null,
    selected_option text not null,
    rationale text not null,
    evidence jsonb not null,
    decided_by jsonb not null,
    affected_artifacts jsonb not null,
    supersedes uuid[],
    created_at timestamptz not null default now()
);

create table team_events (
    event_id uuid primary key,
    goal_id uuid not null references goals(goal_id) on delete cascade,
    task_id uuid references tasks(task_id),
    aggregate_type text not null,
    aggregate_id uuid not null,
    aggregate_version bigint not null,
    event_type text not null,
    actor jsonb not null,
    correlation_id uuid not null,
    causation_id uuid,
    artifact_bindings jsonb not null default '[]'::jsonb,
    payload jsonb not null,
    evidence jsonb not null default '[]'::jsonb,
    occurred_at timestamptz not null default now()
);

create index team_events_goal_cursor_idx on team_events (goal_id, occurred_at, event_id);

create table processed_commands (
    command_id uuid primary key,
    actor_id text not null,
    tool_name text not null,
    request_hash text not null,
    response jsonb not null,
    created_at timestamptz not null default now()
);

create table outbox (
    outbox_id bigserial primary key,
    event_id uuid not null unique,
    event_type text not null,
    aggregate_type text not null,
    aggregate_id uuid not null,
    payload jsonb not null,
    published_at timestamptz,
    attempts integer not null default 0,
    created_at timestamptz not null default now()
);

create index outbox_unpublished_idx on outbox (outbox_id) where published_at is null;

create table human_requests (
    request_id uuid primary key,
    goal_id uuid not null references goals(goal_id) on delete cascade,
    request_type text not null,
    question text not null,
    options jsonb not null,
    state text not null,
    response jsonb,
    created_at timestamptz not null default now(),
    responded_at timestamptz
);

-- migrate:down
drop table if exists human_requests;
drop table if exists outbox;
drop table if exists processed_commands;
drop table if exists team_events;
drop table if exists decisions;
drop table if exists approval_artifact_bindings;
drop table if exists approvals;
drop table if exists review_artifact_bindings;
drop table if exists reviews;
drop table if exists findings;
drop table if exists artifact_aliases;
drop table if exists artifact_revisions;
drop table if exists runtime_runs;
drop table if exists task_assignments;
drop table if exists task_dependencies;
drop table if exists tasks;
drop table if exists goals;
