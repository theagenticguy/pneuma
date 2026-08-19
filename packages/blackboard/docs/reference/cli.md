# omnigent-blackboard-poc · CLI

The `blackboard` CLI is a Typer app for operators (never agent access) with the following subcommands. `src/sdlc_blackboard/interfaces/cli.py:1-26`

## migrate

```
blackboard migrate
```

Apply pending SQL migrations via dbmate. `src/sdlc_blackboard/interfaces/cli.py:29-33`

## list-goals

```
blackboard list-goals
```

List all goals (id, title, state). `src/sdlc_blackboard/interfaces/cli.py:36-49`

## snapshot

```
blackboard snapshot {goal_id}
```

Print the goal snapshot as JSON; exits 1 when the goal is not found. `src/sdlc_blackboard/interfaces/cli.py:52-68`

## events

```
blackboard events {goal_id}
```

Print the goal's event trace in order. `src/sdlc_blackboard/interfaces/cli.py:71-84`

## gate

```
blackboard gate {goal_id}
```

Print the release-gate status as JSON. `src/sdlc_blackboard/interfaces/cli.py:87-101`

## thrash

```
blackboard thrash {goal_id}
```

Print the goal's coordination-thrash report (conflicts, stale versions, review rejections, reclaims) as JSON; operator-only and deliberately not exposed as an MCP tool so agents cannot read or game their own thrash metric. `src/sdlc_blackboard/interfaces/cli.py:104-122`

## outbox-relay

```
blackboard outbox-relay [--batch-size N] [--once | --loop] [--interval SECONDS]
```

Drain the transactional outbox: claim unpublished rows, publish each as a structured log line, and mark them published — all atomically per batch for at-least-once delivery (handoff §12). `--once` (default) drains a single batch and exits; `--loop` polls every `--interval` seconds until interrupted. `src/sdlc_blackboard/interfaces/cli.py:125-155`

Flags:

- `--batch-size` — Max unpublished rows to drain per pass (default 100). `src/sdlc_blackboard/interfaces/cli.py:127`
- `--loop / --once` — Poll forever (`--loop`) or drain a single batch and exit (`--once`, default). `src/sdlc_blackboard/interfaces/cli.py:128-132`
- `--interval` — Seconds between passes when `--loop` (default 2.0). `src/sdlc_blackboard/interfaces/cli.py:133`

## reset-demo

```
blackboard reset-demo
```

Truncate all domain state — goals, processed_commands, outbox, team_events, command_failures (destructive — CLI only, never on MCP). `src/sdlc_blackboard/interfaces/cli.py:158-174`
