"""Synthetic four-plane incident dataset with a machine-checked information-asymmetry property.

A five-service fleet (edge-gateway -> checkout-api -> {pricing-svc, cart-store}, auth-svc on the
side) suffers a latency + error incident, onset 14:20Z. All timestamps 2026-07-28 UTC, fixed
literals. Full per-plane accounting: `docs/design/incident.md`.

PLANTED TRUTH (the only one): checkout-api change ``chg-4417`` (v2.31.0, rolled 5% at 14:12Z, 25% at
14:16Z, 100% at 14:20Z) flipped its outbound HTTP client to ``max_retries: 2 -> 5``, ``backoff:
exp_jitter -> fixed_50ms`` and ``retry_budget_pct: 10 -> disabled``. Mechanism =
``retry_amplification``: every checkout request re-issues the same logical downstream call up to 5x
with no jitter and no budget, multiplying pricing-svc + cart-store inbound load ~4.8x while *user*
edge traffic stays flat. Rolled back by ``chg-4423`` at 14:44Z, recovery 14:48Z.

INFORMATION ASYMMETRY — BY CONSTRUCTION. Each plane alone is consistent with >= 2 mechanisms in
MECHANISMS; only the intersection of >= 3 collapses to one, and every *pair* still leaves >= 2 live
candidates. ``self_check()`` asserts both, so the asymmetry is machine-checked rather than claimed
in prose, where it would drift. Each plane makes one elimination nobody else can:

* TRACES: tr-9d41's 5 extra ``pricing.quote`` spans share ONE idempotency key and run sequentially
  ~50ms apart -> repeats, not distinct per-item work, and not parallel. Kills n_plus_one_fanout and
  unbounded_concurrency. Alone it dates nothing: tr-7b02 is a legitimate pre-existing fanout with
  DISTINCT keys, ``pricing-svc-7`` a chronic -118ms clock offset.
* METRICS: pool_in_use rises only as rps_in rises, never ahead of it (pricing-svc 16/48 at 1130 rps,
  44/48 at 2604, 48/48 at 4970), so the pool is a follower. Kills connection_pool_exhaustion. Alone
  it names no change id: checkout's ``rps_out``/``rps_in`` ratio jumps 1.19 -> 5.8 with edge rps
  flat, and cart-store's 0.94 -> 0.70 reads as key-space.
* DEPLOYS: the only change whose staged rollout matches the graded onset, and the only plane naming
  the retry knobs. Kills the cache_key_collision decoy on timing. Alone it has no symptoms, and its
  diff bundles four suspicious knobs, so it cannot say which mattered.
* LOGS + baseline TRACES: the ``pricing-svc-7`` -118ms offset is present at 13:52Z and 14:56Z with
  normal latency. Kills clock_skew. Logs alone have no parent/child structure and no rates, so "5
  retries of one call" and "5 distinct calls" are indistinguishable.

So an investigator that reads one plane well and stops is wrong here by construction. Four decoys
look guilty, each excluded by a different kind of evidence so no single heuristic clears the field:
``chg-4420`` auth-svc cache-key change at 14:31Z, 11 minutes AFTER onset (timing); ``pricing-svc``,
worst metrics in the fleet but unchanged since ``chg-4402`` (change history); cart-store's hit-ratio
collapse, which is retry-driven duplicate traffic diluting the working set (causal direction); and
``chg-4415`` edge-gateway, TLS-session-cache only (blast radius).
"""

# ruff: noqa: E501 -- the record tables below are wide literal data rows, kept one-per-line
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

MECHANISMS: Final[tuple[str, ...]] = (
    "cache_key_collision",
    "clock_skew",
    "connection_pool_exhaustion",
    "n_plus_one_fanout",
    "retry_amplification",
    "unbounded_concurrency",
)

PLANE_NAMES: Final[tuple[str, ...]] = ("deploys", "metrics", "logs", "traces")

ONSET_TS: Final[str] = "2026-07-28T14:20:00Z"
RECOVERY_TS: Final[str] = "2026-07-28T14:48:00Z"


@dataclass(frozen=True, slots=True)
class Deploy:
    record_id: str
    change_id: str
    ts: str
    service: str
    version: str
    commit: str
    canary_pct: int
    config_diff: str
    note: str


@dataclass(frozen=True, slots=True)
class MetricBucket:
    record_id: str
    ts: str
    service: str
    p50_ms: float
    p99_ms: float
    error_rate: float
    rps_in: float
    rps_out: float | None
    saturation: float
    pool_in_use: int
    pool_max: int
    cache_hit_ratio: float | None


@dataclass(frozen=True, slots=True)
class LogLine:
    record_id: str
    ts: str
    service: str
    level: str
    message: str
    exception: str | None
    trace_id: str | None


@dataclass(frozen=True, slots=True)
class Span:
    record_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    service: str
    operation: str
    start_ts: str
    duration_ms: float
    status: str
    attributes: str


_DEPLOY_ROWS: Final[tuple[tuple[str, str, str, str, str, int, str, str], ...]] = (
    (
        "chg-4380",
        "2026-07-24T10:18:00Z",
        "edge-gateway",
        "v5.9.3",
        "3d81f0a",
        100,
        "http2.max_streams: 128 -> 192",
        "weekly release",
    ),
    (
        "chg-4402",
        "2026-07-25T16:41:00Z",
        "pricing-svc",
        "v7.2.1",
        "4ab77de",
        100,
        "grpc.keepalive_s: 30 -> 45",
        "last pricing-svc change of any kind",
    ),
    (
        "chg-4390",
        "2026-07-26T09:14:00Z",
        "cart-store",
        "v1.14.2",
        "9f2c1ab",
        100,
        "metrics.tags: +region",
        "observability only",
    ),
    (
        "chg-4408",
        "2026-07-27T11:05:00Z",
        "checkout-api",
        "v2.30.6",
        "7c19e04",
        100,
        "feature.flag.split_ship: off -> on",
        "stable baseline version",
    ),
    (
        "chg-4411",
        "2026-07-27T18:22:00Z",
        "auth-svc",
        "v3.8.0",
        "b21f8cc",
        100,
        "oidc.discovery_cache_s: 120 -> 300",
        "routine",
    ),
    (
        "chg-4413",
        "2026-07-28T09:31:00Z",
        "cart-store",
        "v1.14.3",
        "e0d4471",
        100,
        "cache.max_entries: 200000 -> 260000",
        "capacity bump, 5h before onset",
    ),
    (
        "chg-4415",
        "2026-07-28T14:02:00Z",
        "edge-gateway",
        "v5.9.4",
        "51aa9b2",
        100,
        "tls.session_cache: 8192 -> 16384",
        "coincident deploy, TLS only",
    ),
    (
        "chg-4417",
        "2026-07-28T14:12:00Z",
        "checkout-api",
        "v2.31.0",
        "a83b6f1",
        5,
        "http.client.max_retries: 2 -> 5; http.client.backoff: exp_jitter -> fixed_50ms; http.client.retry_budget_pct: 10 -> disabled; http.client.max_inflight: 32 -> unlimited; pool.max_idle: 8 -> 16",
        "resilience hardening, canary stage 1",
    ),
    (
        "chg-4417",
        "2026-07-28T14:16:00Z",
        "checkout-api",
        "v2.31.0",
        "a83b6f1",
        25,
        "(same diff as stage 1)",
        "canary stage 2",
    ),
    (
        "chg-4417",
        "2026-07-28T14:20:00Z",
        "checkout-api",
        "v2.31.0",
        "a83b6f1",
        100,
        "(same diff as stage 1)",
        "canary stage 3, full fleet",
    ),
    (
        "chg-4419",
        "2026-07-28T14:26:00Z",
        "cart-store",
        "v1.14.3",
        "e0d4471",
        100,
        "trace.sampling: 0.10 -> 0.25",
        "raised sampling to debug",
    ),
    (
        "chg-4420",
        "2026-07-28T14:31:00Z",
        "auth-svc",
        "v3.8.1",
        "6df90c2",
        100,
        "jwt.cache.key: sub -> sub+aud; jwt.cache.ttl_s: 60 -> 900",
        "cache-key change, AFTER onset",
    ),
    (
        "chg-4421",
        "2026-07-28T14:34:00Z",
        "edge-gateway",
        "v5.9.4",
        "51aa9b2",
        100,
        "upstream.timeout_ms: 1500 -> 2500",
        "mitigation attempt, no effect",
    ),
    (
        "chg-4423",
        "2026-07-28T14:44:00Z",
        "checkout-api",
        "v2.30.6",
        "7c19e04",
        100,
        "rollback_of: chg-4417 (all http.client keys restored)",
        "rollback",
    ),
    (
        "chg-4425",
        "2026-07-28T14:52:00Z",
        "pricing-svc",
        "v7.2.1",
        "4ab77de",
        100,
        "replicas: 12 -> 20",
        "post-recovery headroom",
    ),
)

DEPLOYS: Final[tuple[Deploy, ...]] = tuple(
    Deploy(f"d-{i:02d}", cid, ts, svc, ver, sha, pct, diff, note)
    for i, (cid, ts, svc, ver, sha, pct, diff, note) in enumerate(_DEPLOY_ROWS, start=1)
)

BUCKET_TS: Final[tuple[str, ...]] = (
    "2026-07-28T13:52:00Z",
    "2026-07-28T14:04:00Z",
    "2026-07-28T14:12:00Z",
    "2026-07-28T14:16:00Z",
    "2026-07-28T14:20:00Z",
    "2026-07-28T14:24:00Z",
    "2026-07-28T14:28:00Z",
    "2026-07-28T14:36:00Z",
    "2026-07-28T14:44:00Z",
    "2026-07-28T14:48:00Z",
    "2026-07-28T14:56:00Z",
)

# rows: (p50, p99, err, rps_in, rps_out|None, saturation, pool_in_use, pool_max, hit_ratio|None)
_METRIC_ROWS: Final[
    dict[
        str,
        tuple[tuple[float, float, float, float, float | None, float, int, int, float | None], ...],
    ]
] = {
    "edge-gateway": (
        (42, 310, 0.001, 1180, 942, 0.41, 22, 64, None),
        (43, 318, 0.001, 1204, 957, 0.42, 23, 64, None),
        (43, 321, 0.001, 1191, 950, 0.42, 23, 64, None),
        (44, 352, 0.002, 1198, 953, 0.44, 25, 64, None),
        (52, 690, 0.009, 1187, 946, 0.49, 29, 64, None),
        (88, 1480, 0.031, 1195, 954, 0.58, 36, 64, None),
        (131, 2410, 0.062, 1183, 941, 0.66, 41, 64, None),
        (150, 2680, 0.071, 1201, 959, 0.69, 43, 64, None),
        (96, 1520, 0.028, 1192, 948, 0.57, 35, 64, None),
        (55, 470, 0.004, 1188, 945, 0.45, 26, 64, None),
        (44, 330, 0.001, 1196, 953, 0.42, 23, 64, None),
    ),
    # checkout-api: rps_in flat, rps_out explodes -> the amplification fingerprint
    "checkout-api": (
        (61, 480, 0.002, 940, 1120, 0.38, 24, 128, None),
        (62, 492, 0.002, 955, 1140, 0.39, 25, 128, None),
        (62, 486, 0.002, 948, 1132, 0.39, 25, 128, None),
        (66, 610, 0.004, 951, 1290, 0.42, 31, 128, None),
        (140, 1900, 0.021, 944, 2610, 0.55, 62, 128, None),
        (290, 3900, 0.058, 952, 4980, 0.71, 94, 128, None),
        (410, 5200, 0.094, 939, 5420, 0.80, 112, 128, None),
        (470, 5600, 0.101, 957, 5510, 0.83, 118, 128, None),
        (300, 3800, 0.049, 946, 5290, 0.70, 96, 128, None),
        (80, 700, 0.006, 943, 1310, 0.44, 33, 128, None),
        (63, 500, 0.002, 951, 1150, 0.39, 25, 128, None),
    ),
    # pricing-svc: loudest plane-2 signal, pool pegged at 48/48. Unchanged since 07-25.
    "pricing-svc": (
        (22, 95, 0.000, 1118, None, 0.31, 15, 48, None),
        (23, 98, 0.000, 1139, None, 0.32, 16, 48, None),
        (23, 97, 0.001, 1130, None, 0.32, 16, 48, None),
        (26, 140, 0.003, 1288, None, 0.37, 19, 48, None),
        (71, 880, 0.048, 2604, None, 0.74, 44, 48, None),
        (180, 2600, 0.152, 4970, None, 0.93, 48, 48, None),
        (260, 4100, 0.243, 5410, None, 0.97, 48, 48, None),
        (300, 4400, 0.261, 5498, None, 0.98, 48, 48, None),
        (175, 2500, 0.140, 5280, None, 0.92, 48, 48, None),
        (28, 190, 0.007, 1308, None, 0.40, 20, 48, None),
        (23, 100, 0.001, 1148, None, 0.33, 16, 48, None),
    ),
    # cart-store: hit ratio collapses because retried duplicates dilute the working set
    "cart-store": (
        (4, 28, 0.000, 610, None, 0.22, 9, 32, 0.94),
        (4, 29, 0.000, 618, None, 0.23, 9, 32, 0.94),
        (4, 28, 0.000, 613, None, 0.23, 9, 32, 0.94),
        (5, 33, 0.001, 640, None, 0.25, 11, 32, 0.93),
        (7, 58, 0.002, 900, None, 0.34, 15, 32, 0.88),
        (11, 120, 0.003, 1420, None, 0.51, 21, 32, 0.79),
        (14, 180, 0.004, 1560, None, 0.59, 24, 32, 0.71),
        (15, 190, 0.004, 1580, None, 0.61, 24, 32, 0.70),
        (10, 110, 0.003, 1500, None, 0.56, 22, 32, 0.76),
        (5, 36, 0.001, 650, None, 0.26, 11, 32, 0.92),
        (4, 29, 0.000, 620, None, 0.23, 9, 32, 0.94),
    ),
    # auth-svc: never degrades; its 14:31 cache-key deploy only improves hit ratio
    "auth-svc": (
        (9, 44, 0.000, 1310, None, 0.27, 11, 64, 0.91),
        (9, 45, 0.000, 1332, None, 0.28, 11, 64, 0.91),
        (9, 44, 0.000, 1319, None, 0.27, 11, 64, 0.91),
        (9, 46, 0.001, 1325, None, 0.28, 12, 64, 0.91),
        (10, 47, 0.001, 1316, None, 0.29, 12, 64, 0.91),
        (10, 48, 0.001, 1329, None, 0.29, 12, 64, 0.90),
        (10, 47, 0.001, 1311, None, 0.29, 12, 64, 0.91),
        (11, 52, 0.001, 1334, None, 0.31, 13, 64, 0.91),
        (10, 49, 0.001, 1320, None, 0.30, 12, 64, 0.96),
        (9, 45, 0.000, 1317, None, 0.28, 12, 64, 0.97),
        (9, 44, 0.000, 1327, None, 0.28, 12, 64, 0.97),
    ),
}

METRICS: Final[tuple[MetricBucket, ...]] = tuple(
    MetricBucket(
        f"m-{svc}-{i:02d}",
        BUCKET_TS[i - 1],
        svc,
        float(r[0]),
        float(r[1]),
        float(r[2]),
        float(r[3]),
        None if r[4] is None else float(r[4]),
        float(r[5]),
        int(r[6]),
        int(r[7]),
        None if r[8] is None else float(r[8]),
    )
    for svc, rows in _METRIC_ROWS.items()
    for i, r in enumerate(rows, start=1)
)

# rows: (ts, service, level, message, exception|None, trace_id|None)
_LOG_ROWS: Final[tuple[tuple[str, str, str, str, str | None, str | None], ...]] = (
    (
        "2026-07-28T13:52:11Z",
        "checkout-api",
        "INFO",
        "quote ok upstream=pricing-svc attempt=1/2 dur_ms=58",
        None,
        "tr-4a10",
    ),
    (
        "2026-07-28T13:55:40Z",
        "pricing-svc",
        "INFO",
        "quote served items=3 pool_in_use=15 max=48",
        None,
        "tr-4a10",
    ),
    (
        "2026-07-28T14:02:07Z",
        "edge-gateway",
        "INFO",
        "config applied tls.session_cache=16384 change=chg-4415",
        None,
        None,
    ),
    (
        "2026-07-28T14:04:22Z",
        "cart-store",
        "INFO",
        "get hit key=cart:v2:8842 hit_ratio=0.94",
        None,
        "tr-7b02",
    ),
    (
        "2026-07-28T14:12:03Z",
        "checkout-api",
        "INFO",
        "rollout start version=v2.31.0 canary=5% change=chg-4417",
        None,
        None,
    ),
    (
        "2026-07-28T14:12:58Z",
        "checkout-api",
        "WARN",
        "retrying upstream call op=pricing.quote attempt=3/5 backoff_ms=50 idem=IDMP-8842-q3",
        None,
        "tr-9d41",
    ),
    (
        "2026-07-28T14:14:30Z",
        "pricing-svc",
        "WARN",
        "quote latency high dur_ms=214 pool_in_use=31 max=48",
        None,
        None,
    ),
    (
        "2026-07-28T14:16:05Z",
        "checkout-api",
        "INFO",
        "rollout advance canary=25% change=chg-4417",
        None,
        None,
    ),
    (
        "2026-07-28T14:17:41Z",
        "checkout-api",
        "WARN",
        "retrying upstream call op=pricing.quote attempt=4/5 backoff_ms=50 idem=IDMP-3120-q1",
        None,
        None,
    ),
    (
        "2026-07-28T14:18:02Z",
        "pricing-svc",
        "ERROR",
        "PoolTimeout: no connection available in_use=48 max=48 waited_ms=812",
        "PoolTimeoutError",
        None,
    ),
    (
        "2026-07-28T14:20:04Z",
        "checkout-api",
        "INFO",
        "rollout advance canary=100% change=chg-4417",
        None,
        None,
    ),
    (
        "2026-07-28T14:20:33Z",
        "edge-gateway",
        "WARN",
        "upstream slow service=checkout-api dur_ms=1904 status=200",
        None,
        None,
    ),
    (
        "2026-07-28T14:20:51Z",
        "pricing-svc",
        "ERROR",
        "PoolTimeout: no connection available in_use=48 max=48 waited_ms=1503",
        "PoolTimeoutError",
        "tr-9d41",
    ),
    (
        "2026-07-28T14:21:09Z",
        "checkout-api",
        "WARN",
        "retrying upstream call op=pricing.quote attempt=5/5 backoff_ms=50 idem=IDMP-8842-q3",
        None,
        "tr-9d41",
    ),
    (
        "2026-07-28T14:21:10Z",
        "checkout-api",
        "ERROR",
        "upstream exhausted retries op=pricing.quote attempts=5 idem=IDMP-8842-q3",
        "UpstreamUnavailable",
        "tr-9d41",
    ),
    (
        "2026-07-28T14:21:44Z",
        "cart-store",
        "WARN",
        "get miss key=cart:v2:8842 hit_ratio=0.86 inflight=19",
        None,
        None,
    ),
    (
        "2026-07-28T14:22:02Z",
        "pricing-svc",
        "ERROR",
        "PoolTimeout: no connection available in_use=48 max=48 waited_ms=1710",
        "PoolTimeoutError",
        None,
    ),
    (
        "2026-07-28T14:22:18Z",
        "checkout-api",
        "WARN",
        "outbound inflight=214 limit=unlimited queue_depth=0",
        None,
        None,
    ),
    (
        "2026-07-28T14:22:47Z",
        "edge-gateway",
        "ERROR",
        "504 gateway timeout route=/checkout upstream=checkout-api dur_ms=2501",
        "GatewayTimeout",
        None,
    ),
    (
        "2026-07-28T14:23:12Z",
        "pricing-svc",
        "WARN",
        "duplicate request detected idem=IDMP-8842-q3 seen=4 window_ms=250",
        None,
        "tr-9d41",
    ),
    (
        "2026-07-28T14:23:40Z",
        "cart-store",
        "WARN",
        "get miss key=cart:v2:3120 hit_ratio=0.81 evictions_5m=41200",
        None,
        None,
    ),
    (
        "2026-07-28T14:24:03Z",
        "pricing-svc",
        "ERROR",
        "PoolTimeout: no connection available in_use=48 max=48 waited_ms=1902",
        "PoolTimeoutError",
        None,
    ),
    (
        "2026-07-28T14:24:29Z",
        "checkout-api",
        "ERROR",
        "upstream exhausted retries op=pricing.quote attempts=5 idem=IDMP-5507-q2",
        "UpstreamUnavailable",
        None,
    ),
    ("2026-07-28T14:25:01Z", "auth-svc", "INFO", "verify ok dur_ms=10 cache_hit=1", None, None),
    (
        "2026-07-28T14:25:36Z",
        "cart-store",
        "WARN",
        "get miss key=cart:v2:5507 hit_ratio=0.77",
        None,
        None,
    ),
    (
        "2026-07-28T14:26:11Z",
        "cart-store",
        "INFO",
        "config applied trace.sampling=0.25 change=chg-4419",
        None,
        None,
    ),
    (
        "2026-07-28T14:26:44Z",
        "pricing-svc",
        "ERROR",
        "shed load: queue full depth=512 rejected=311",
        "LoadShedError",
        None,
    ),
    (
        "2026-07-28T14:27:20Z",
        "edge-gateway",
        "ERROR",
        "502 bad gateway route=/checkout upstream=checkout-api",
        "BadGateway",
        None,
    ),
    (
        "2026-07-28T14:27:55Z",
        "checkout-api",
        "WARN",
        "retry budget disabled; not throttling retries op=pricing.quote",
        None,
        None,
    ),
    (
        "2026-07-28T14:28:18Z",
        "pricing-svc",
        "ERROR",
        "PoolTimeout: no connection available in_use=48 max=48 waited_ms=2104",
        "PoolTimeoutError",
        None,
    ),
    (
        "2026-07-28T14:28:50Z",
        "checkout-api",
        "ERROR",
        "quote failed after 5 attempts, returning 503 idem=IDMP-9931-q1",
        "UpstreamUnavailable",
        None,
    ),
    (
        "2026-07-28T14:29:27Z",
        "cart-store",
        "WARN",
        "get miss key=cart:v2:9931 hit_ratio=0.72",
        None,
        None,
    ),
    (
        "2026-07-28T14:30:02Z",
        "pricing-svc",
        "WARN",
        "duplicate request detected idem=IDMP-9931-q1 seen=5 window_ms=250",
        None,
        None,
    ),
    (
        "2026-07-28T14:31:06Z",
        "auth-svc",
        "INFO",
        "config applied jwt.cache.key=sub+aud ttl_s=900 change=chg-4420",
        None,
        None,
    ),
    (
        "2026-07-28T14:31:44Z",
        "auth-svc",
        "INFO",
        "jwt cache warm entries=41290 hit_ratio=0.90",
        None,
        None,
    ),
    (
        "2026-07-28T14:32:15Z",
        "pricing-svc",
        "ERROR",
        "PoolTimeout: no connection available in_use=48 max=48 waited_ms=2260",
        "PoolTimeoutError",
        None,
    ),
    (
        "2026-07-28T14:33:01Z",
        "checkout-api",
        "ERROR",
        "circuit half-open trip=pricing.quote failures_1m=1841",
        "CircuitOpen",
        None,
    ),
    (
        "2026-07-28T14:34:09Z",
        "edge-gateway",
        "INFO",
        "config applied upstream.timeout_ms=2500 change=chg-4421",
        None,
        None,
    ),
    (
        "2026-07-28T14:35:22Z",
        "edge-gateway",
        "ERROR",
        "504 gateway timeout route=/checkout upstream=checkout-api dur_ms=2503",
        "GatewayTimeout",
        None,
    ),
    (
        "2026-07-28T14:36:03Z",
        "pricing-svc",
        "ERROR",
        "PoolTimeout: no connection available in_use=48 max=48 waited_ms=2411",
        "PoolTimeoutError",
        "tr-1c88",
    ),
    (
        "2026-07-28T14:36:40Z",
        "checkout-api",
        "WARN",
        "retrying upstream call op=pricing.quote attempt=2/5 backoff_ms=50 idem=IDMP-2204-q1",
        None,
        "tr-1c88",
    ),
    ("2026-07-28T14:37:18Z", "auth-svc", "INFO", "verify ok dur_ms=11 cache_hit=1", None, None),
    (
        "2026-07-28T14:38:44Z",
        "pricing-svc",
        "WARN",
        "inbound rps=5498 baseline_rps=1139 ratio=4.83",
        None,
        None,
    ),
    (
        "2026-07-28T14:40:02Z",
        "checkout-api",
        "WARN",
        "outbound rps=5510 inbound rps=957 fanout=5.76",
        None,
        None,
    ),
    (
        "2026-07-28T14:42:35Z",
        "cart-store",
        "WARN",
        "get miss key=cart:v2:2204 hit_ratio=0.70",
        None,
        None,
    ),
    (
        "2026-07-28T14:44:08Z",
        "checkout-api",
        "INFO",
        "rollback start target=v2.30.6 change=chg-4423 reverts=chg-4417",
        None,
        None,
    ),
    (
        "2026-07-28T14:44:51Z",
        "checkout-api",
        "INFO",
        "http.client restored max_retries=2 backoff=exp_jitter retry_budget_pct=10",
        None,
        None,
    ),
    (
        "2026-07-28T14:46:12Z",
        "pricing-svc",
        "INFO",
        "pool recovered in_use=27 max=48 queue_depth=0",
        None,
        None,
    ),
    (
        "2026-07-28T14:48:03Z",
        "edge-gateway",
        "INFO",
        "error_rate=0.004 p99_ms=470 route=/checkout",
        None,
        None,
    ),
    (
        "2026-07-28T14:49:30Z",
        "cart-store",
        "INFO",
        "get hit key=cart:v2:8842 hit_ratio=0.92",
        None,
        "tr-6f55",
    ),
    (
        "2026-07-28T14:52:07Z",
        "pricing-svc",
        "INFO",
        "scaled replicas=20 change=chg-4425",
        None,
        None,
    ),
    (
        "2026-07-28T14:56:40Z",
        "checkout-api",
        "INFO",
        "quote ok upstream=pricing-svc attempt=1/2 dur_ms=60",
        None,
        "tr-6f55",
    ),
)

LOGS: Final[tuple[LogLine, ...]] = tuple(
    LogLine(f"l-{i:02d}", ts, svc, lvl, msg, exc, tid)
    for i, (ts, svc, lvl, msg, exc, tid) in enumerate(_LOG_ROWS, start=1)
)

# rows: (trace_id, span_id, parent, service, operation, start_ts, dur_ms, status, attrs)
# tr-7b02 = pre-existing legitimate per-item fanout (distinct item ids) -> the n_plus_one lead.
# tr-9d41 = the incident: 5 sibling spans, IDENTICAL idem key, 50ms apart -> retry, not fanout.
# host=pricing-svc-7 spans start ~118ms before their parent: chronic clock offset, present in
# the healthy 14:04 baseline too -> the clock_skew lead, dead on arrival once dated.
_SPAN_ROWS: Final[tuple[tuple[str, str, str | None, str, str, str, float, str, str], ...]] = (
    (
        "tr-7b02",
        "s-7b02-1",
        None,
        "edge-gateway",
        "POST /checkout",
        "2026-07-28T14:04:10.100Z",
        331.0,
        "OK",
        "route=/checkout user_tier=std",
    ),
    (
        "tr-7b02",
        "s-7b02-2",
        "s-7b02-1",
        "checkout-api",
        "checkout.submit",
        "2026-07-28T14:04:10.140Z",
        274.0,
        "OK",
        "version=v2.30.6 max_retries=2 items=3",
    ),
    (
        "tr-7b02",
        "s-7b02-3",
        "s-7b02-2",
        "pricing-svc",
        "pricing.quote",
        "2026-07-28T14:04:10.180Z",
        61.0,
        "OK",
        "host=pricing-svc-3 idem=IDMP-7702-q1 item=SKU-11 attempt=1",
    ),
    (
        "tr-7b02",
        "s-7b02-4",
        "s-7b02-2",
        "pricing-svc",
        "pricing.quote",
        "2026-07-28T14:04:10.250Z",
        58.0,
        "OK",
        "host=pricing-svc-3 idem=IDMP-7702-q2 item=SKU-42 attempt=1",
    ),
    (
        "tr-7b02",
        "s-7b02-5",
        "s-7b02-2",
        "pricing-svc",
        "pricing.quote",
        "2026-07-28T14:04:10.315Z",
        60.0,
        "OK",
        "host=pricing-svc-9 idem=IDMP-7702-q3 item=SKU-88 attempt=1",
    ),
    (
        "tr-7b02",
        "s-7b02-6",
        "s-7b02-2",
        "cart-store",
        "cart.get",
        "2026-07-28T14:04:10.380Z",
        5.0,
        "OK",
        "key=cart:v2:7702 cache=hit",
    ),
    (
        "tr-7b02",
        "s-7b02-7",
        "s-7b02-2",
        "auth-svc",
        "auth.verify",
        "2026-07-28T14:04:10.392Z",
        9.0,
        "OK",
        "cache=hit",
    ),
    (
        "tr-4a10",
        "s-4a10-1",
        None,
        "checkout-api",
        "checkout.submit",
        "2026-07-28T13:52:11.000Z",
        118.0,
        "OK",
        "version=v2.30.6 max_retries=2 items=1",
    ),
    (
        "tr-4a10",
        "s-4a10-2",
        "s-4a10-1",
        "pricing-svc",
        "pricing.quote",
        "2026-07-28T13:52:10.882Z",
        57.0,
        "OK",
        "host=pricing-svc-7 idem=IDMP-4410-q1 attempt=1 clock_offset_ms=-118",
    ),
    (
        "tr-9d41",
        "s-9d41-1",
        None,
        "edge-gateway",
        "POST /checkout",
        "2026-07-28T14:20:47.900Z",
        2501.0,
        "ERROR",
        "route=/checkout http.status=504",
    ),
    (
        "tr-9d41",
        "s-9d41-2",
        "s-9d41-1",
        "checkout-api",
        "checkout.submit",
        "2026-07-28T14:20:47.940Z",
        2440.0,
        "ERROR",
        "version=v2.31.0 max_retries=5 backoff=fixed_50ms retry_budget=disabled items=1",
    ),
    (
        "tr-9d41",
        "s-9d41-3",
        "s-9d41-2",
        "pricing-svc",
        "pricing.quote",
        "2026-07-28T14:20:47.862Z",
        402.0,
        "ERROR",
        "host=pricing-svc-7 idem=IDMP-8842-q3 attempt=1 clock_offset_ms=-118 pool_wait_ms=380",
    ),
    (
        "tr-9d41",
        "s-9d41-4",
        "s-9d41-2",
        "pricing-svc",
        "pricing.quote",
        "2026-07-28T14:20:48.410Z",
        451.0,
        "ERROR",
        "host=pricing-svc-2 idem=IDMP-8842-q3 attempt=2 pool_wait_ms=430",
    ),
    (
        "tr-9d41",
        "s-9d41-5",
        "s-9d41-2",
        "pricing-svc",
        "pricing.quote",
        "2026-07-28T14:20:48.912Z",
        498.0,
        "ERROR",
        "host=pricing-svc-5 idem=IDMP-8842-q3 attempt=3 pool_wait_ms=470",
    ),
    (
        "tr-9d41",
        "s-9d41-6",
        "s-9d41-2",
        "pricing-svc",
        "pricing.quote",
        "2026-07-28T14:20:49.462Z",
        512.0,
        "ERROR",
        "host=pricing-svc-2 idem=IDMP-8842-q3 attempt=4 pool_wait_ms=500",
    ),
    (
        "tr-9d41",
        "s-9d41-7",
        "s-9d41-2",
        "pricing-svc",
        "pricing.quote",
        "2026-07-28T14:20:50.026Z",
        344.0,
        "ERROR",
        "host=pricing-svc-9 idem=IDMP-8842-q3 attempt=5 pool_timeout=true",
    ),
    (
        "tr-9d41",
        "s-9d41-8",
        "s-9d41-2",
        "cart-store",
        "cart.get",
        "2026-07-28T14:20:50.380Z",
        14.0,
        "OK",
        "key=cart:v2:8842 cache=miss",
    ),
    (
        "tr-1c88",
        "s-1c88-1",
        None,
        "edge-gateway",
        "POST /checkout",
        "2026-07-28T14:36:38.500Z",
        1902.0,
        "ERROR",
        "route=/checkout http.status=502",
    ),
    (
        "tr-1c88",
        "s-1c88-2",
        "s-1c88-1",
        "checkout-api",
        "checkout.submit",
        "2026-07-28T14:36:38.540Z",
        1840.0,
        "ERROR",
        "version=v2.31.0 max_retries=5 backoff=fixed_50ms items=1",
    ),
    (
        "tr-1c88",
        "s-1c88-3",
        "s-1c88-2",
        "pricing-svc",
        "pricing.quote",
        "2026-07-28T14:36:38.580Z",
        604.0,
        "ERROR",
        "host=pricing-svc-4 idem=IDMP-2204-q1 attempt=1 pool_wait_ms=580",
    ),
    (
        "tr-1c88",
        "s-1c88-4",
        "s-1c88-2",
        "pricing-svc",
        "pricing.quote",
        "2026-07-28T14:36:39.234Z",
        611.0,
        "ERROR",
        "host=pricing-svc-4 idem=IDMP-2204-q1 attempt=2 pool_wait_ms=590",
    ),
    (
        "tr-1c88",
        "s-1c88-5",
        "s-1c88-2",
        "pricing-svc",
        "pricing.quote",
        "2026-07-28T14:36:39.895Z",
        470.0,
        "ERROR",
        "host=pricing-svc-1 idem=IDMP-2204-q1 attempt=3 pool_timeout=true",
    ),
    (
        "tr-1c88",
        "s-1c88-6",
        "s-1c88-2",
        "cart-store",
        "cart.get",
        "2026-07-28T14:36:40.370Z",
        15.0,
        "OK",
        "key=cart:v2:2204 cache=miss",
    ),
    (
        "tr-6f55",
        "s-6f55-1",
        None,
        "edge-gateway",
        "POST /checkout",
        "2026-07-28T14:56:39.900Z",
        328.0,
        "OK",
        "route=/checkout http.status=200",
    ),
    (
        "tr-6f55",
        "s-6f55-2",
        "s-6f55-1",
        "checkout-api",
        "checkout.submit",
        "2026-07-28T14:56:39.940Z",
        268.0,
        "OK",
        "version=v2.30.6 max_retries=2 items=2",
    ),
    (
        "tr-6f55",
        "s-6f55-3",
        "s-6f55-2",
        "pricing-svc",
        "pricing.quote",
        "2026-07-28T14:56:39.862Z",
        60.0,
        "OK",
        "host=pricing-svc-7 idem=IDMP-6655-q1 attempt=1 clock_offset_ms=-118",
    ),
    (
        "tr-6f55",
        "s-6f55-4",
        "s-6f55-2",
        "pricing-svc",
        "pricing.quote",
        "2026-07-28T14:56:40.050Z",
        58.0,
        "OK",
        "host=pricing-svc-3 idem=IDMP-6655-q2 attempt=1",
    ),
    (
        "tr-6f55",
        "s-6f55-5",
        "s-6f55-2",
        "cart-store",
        "cart.get",
        "2026-07-28T14:56:40.140Z",
        4.0,
        "OK",
        "key=cart:v2:8842 cache=hit",
    ),
)

TRACES: Final[tuple[Span, ...]] = tuple(
    Span(f"t-{i:02d}", tid, sid, par, svc, op, ts, dur, st, attrs)
    for i, (tid, sid, par, svc, op, ts, dur, st, attrs) in enumerate(_SPAN_ROWS, start=1)
)


@dataclass(frozen=True, slots=True)
class GroundTruth:
    culprit_service: str
    culprit_change_id: str
    mechanism: str
    onset_ts: str
    supporting_evidence: dict[str, tuple[str, ...]]
    innocent_services: tuple[str, ...]
    decoy_change_ids: tuple[str, ...]


GROUND_TRUTH: Final[GroundTruth] = GroundTruth(
    culprit_service="checkout-api",
    culprit_change_id="chg-4417",
    mechanism="retry_amplification",
    onset_ts=ONSET_TS,
    supporting_evidence={
        "deploys": ("d-08", "d-09", "d-10", "d-14"),
        "metrics": (
            "m-checkout-api-05",
            "m-checkout-api-07",
            "m-checkout-api-08",
            "m-edge-gateway-08",
            "m-pricing-svc-08",
        ),
        "logs": ("l-06", "l-14", "l-15", "l-20", "l-29", "l-43", "l-44", "l-47"),
        "traces": ("t-13", "t-14", "t-15", "t-16", "t-17", "t-21", "t-22", "t-23"),
    },
    innocent_services=("pricing-svc", "cart-store", "auth-svc", "edge-gateway"),
    decoy_change_ids=("chg-4420", "chg-4415", "chg-4413", "chg-4402"),
)

_PLANE_AMBIGUITY: Final[dict[str, tuple[str, ...]]] = {
    "deploys": (
        "retry_amplification",
        "unbounded_concurrency",
        "connection_pool_exhaustion",
        "cache_key_collision",
    ),
    "metrics": ("retry_amplification", "n_plus_one_fanout", "cache_key_collision"),
    "logs": (
        "retry_amplification",
        "n_plus_one_fanout",
        "connection_pool_exhaustion",
        "cache_key_collision",
    ),
    "traces": (
        "retry_amplification",
        "n_plus_one_fanout",
        "clock_skew",
        "connection_pool_exhaustion",
    ),
}


def single_plane_ambiguity() -> dict[str, tuple[str, ...]]:
    return {k: v for k, v in _PLANE_AMBIGUITY.items()}


def _fmt(value: float) -> str:
    return f"{value:g}"


def _render_deploys() -> str:
    head = "record_id|change_id|ts|service|version|commit|canary_pct|config_diff|note"
    rows = [
        f"{d.record_id}|{d.change_id}|{d.ts}|{d.service}|{d.version}|{d.commit}|{d.canary_pct}|{d.config_diff}|{d.note}"
        for d in DEPLOYS
    ]
    return "\n".join(["PLANE=deploys (release/rollout records)", head, *rows])


def _render_metrics() -> str:
    head = "record_id|ts|service|p50_ms|p99_ms|error_rate|rps_in|rps_out|saturation|pool_in_use/max|cache_hit_ratio"
    rows = [
        f"{m.record_id}|{m.ts}|{m.service}|{_fmt(m.p50_ms)}|{_fmt(m.p99_ms)}|{m.error_rate:.3f}"
        f"|{_fmt(m.rps_in)}|{'-' if m.rps_out is None else _fmt(m.rps_out)}|{m.saturation:.2f}"
        f"|{m.pool_in_use}/{m.pool_max}|"
        f"{'-' if m.cache_hit_ratio is None else format(m.cache_hit_ratio, '.2f')}"
        for m in METRICS
    ]
    return "\n".join(
        ["PLANE=metrics (per-service buckets; rps_out = outbound calls emitted)", head, *rows]
    )


def _render_logs() -> str:
    head = "record_id|ts|service|level|message|exception|trace_id"
    rows = [
        f"{g.record_id}|{g.ts}|{g.service}|{g.level}|{g.message}|{g.exception or '-'}|{g.trace_id or '-'}"
        for g in LOGS
    ]
    return "\n".join(["PLANE=logs (sampled lines)", head, *rows])


def _render_traces() -> str:
    head = "record_id|trace_id|span_id|parent_span_id|service|operation|start_ts|duration_ms|status|attributes"
    rows = [
        f"{s.record_id}|{s.trace_id}|{s.span_id}|{s.parent_span_id or '-'}|{s.service}"
        f"|{s.operation}|{s.start_ts}|{_fmt(s.duration_ms)}|{s.status}|{s.attributes}"
        for s in TRACES
    ]
    return "\n".join(["PLANE=traces (sampled spans, 5 traces)", head, *rows])


_RENDERERS: Final[dict[str, Callable[[], str]]] = {
    "deploys": _render_deploys,
    "metrics": _render_metrics,
    "logs": _render_logs,
    "traces": _render_traces,
}


def render_plane(name: str) -> str:
    key = name.strip().lower()
    if key not in _RENDERERS:
        raise KeyError(f"unknown plane {name!r}; expected one of {PLANE_NAMES}")
    return _RENDERERS[key]()


PLANES: Final[dict[str, str]] = {name: render_plane(name) for name in PLANE_NAMES}


def _norm_service(raw: str) -> str:
    s = raw.strip().lower().replace("_", "-").replace(" ", "-")
    for suffix in ("-service", "-svc-fleet"):
        if s.endswith(suffix):
            s = s[: -len(suffix)] + "-svc"
    return s.removeprefix("service:").removeprefix("svc:")


def _norm_change_id(raw: str) -> str:
    s = raw.strip().lower()
    for token in ("change-id", "change_id", "change", "chg", "#", "-", "_", " ", ":", "id"):
        s = s.replace(token, "")
    return s


def _norm_mechanism(raw: str) -> str:
    s = raw.strip().lower()
    for ch in (" ", "-", "/", "."):
        s = s.replace(ch, "_")
    while "__" in s:
        s = s.replace("__", "_")
    aliases = {
        "retry_storm": "retry_amplification",
        "retry_amplification_storm": "retry_amplification",
        "retries": "retry_amplification",
        "connection_pool_exhausted": "connection_pool_exhaustion",
        "pool_exhaustion": "connection_pool_exhaustion",
        "n+1_fanout": "n_plus_one_fanout",
        "n1_fanout": "n_plus_one_fanout",
        "nplus1_fanout": "n_plus_one_fanout",
        "cache_key_collisions": "cache_key_collision",
    }
    return aliases.get(s, s)


_MECHANISM_HINTS: Final[dict[str, str]] = {
    "connection_pool_exhaustion": (
        "pricing-svc's pool pegging at 48/48 is a downstream consequence: its inbound rps "
        "rose 4.8x while its own config was unchanged since 2026-07-25 (chg-4402)."
    ),
    "cache_key_collision": (
        "cart-store's hit-ratio drop and auth-svc's jwt.cache.key change are both innocent; "
        "the auth-svc change (chg-4420, 14:31Z) landed 11 minutes AFTER onset at 14:20Z."
    ),
    "clock_skew": (
        "the -118ms offset on host pricing-svc-7 is chronic — it is present in the healthy "
        "13:52Z and 14:56Z traces too, so it cannot explain a 14:20Z onset."
    ),
    "n_plus_one_fanout": (
        "the extra pricing.quote spans in tr-9d41/tr-1c88 share ONE idempotency key "
        "(IDMP-8842-q3), i.e. repeats of the same call, not per-item fanout as in tr-7b02."
    ),
    "unbounded_concurrency": (
        "chg-4417 did also set max_inflight to unlimited, but the retry spans are sequential "
        "(~50ms apart, one in flight at a time), so concurrency is not the mechanism."
    ),
}


def verify(culprit_service: str, culprit_change_id: str, mechanism: str) -> list[str]:
    """Oracle. Empty list == correct verdict. Tolerant on formatting, strict on substance."""
    failures: list[str] = []

    got_service = _norm_service(culprit_service)
    want_service = _norm_service(GROUND_TRUTH.culprit_service)
    if got_service != want_service:
        msg = f"wrong culprit_service: got {culprit_service!r}, expected {GROUND_TRUTH.culprit_service!r}"
        if got_service in {_norm_service(s) for s in GROUND_TRUTH.innocent_services}:
            msg += f" ({got_service} is a decoy: symptom or coincidence, not cause)"
        failures.append(msg)

    got_change = _norm_change_id(culprit_change_id)
    want_change = _norm_change_id(GROUND_TRUTH.culprit_change_id)
    if got_change != want_change:
        msg = f"wrong culprit_change_id: got {culprit_change_id!r}, expected {GROUND_TRUTH.culprit_change_id!r}"
        if got_change in {_norm_change_id(c) for c in GROUND_TRUTH.decoy_change_ids}:
            msg += " (that change is a planted decoy; its timing or blast radius does not fit the 14:20Z onset)"
        failures.append(msg)

    got_mech = _norm_mechanism(mechanism)
    if got_mech not in MECHANISMS:
        failures.append(f"mechanism {mechanism!r} is outside the allowed vocabulary {MECHANISMS}")
    elif got_mech != GROUND_TRUTH.mechanism:
        msg = f"wrong mechanism: got {got_mech!r}, expected {GROUND_TRUTH.mechanism!r}"
        hint = _MECHANISM_HINTS.get(got_mech)
        if hint:
            msg += f" — {hint}"
        failures.append(msg)

    return failures


def self_check() -> None:
    """Prove the dataset's structural invariants. Raises AssertionError on violation."""
    assert GROUND_TRUTH.mechanism in MECHANISMS

    amb = single_plane_ambiguity()
    assert set(amb) == set(PLANE_NAMES), "every plane needs an ambiguity entry"
    for plane, cands in amb.items():
        assert len(set(cands)) >= 2, f"plane {plane} is not ambiguous"
        assert set(cands) <= set(MECHANISMS), f"plane {plane} names unknown mechanisms"
        # each plane is consistent with the truth PLUS at least one decoy mechanism
        assert GROUND_TRUTH.mechanism in cands, f"plane {plane} excludes the truth"
        assert len(set(cands) - {GROUND_TRUTH.mechanism}) >= 1

    # no mechanism other than the truth survives the intersection of all four planes
    intersection = set(MECHANISMS)
    for cands in amb.values():
        intersection &= set(cands)
    assert intersection == {GROUND_TRUTH.mechanism}, f"intersection is {intersection}"

    # ...and no group of two planes suffices: every pair leaves >= 2 candidates
    keys = list(amb)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            pair = set(amb[keys[i]]) & set(amb[keys[j]])
            assert len(pair) >= 2, f"pair {keys[i]}+{keys[j]} already resolves to {pair}"

    ids: dict[str, set[str]] = {
        "deploys": {d.record_id for d in DEPLOYS},
        "metrics": {m.record_id for m in METRICS},
        "logs": {g.record_id for g in LOGS},
        "traces": {s.record_id for s in TRACES},
    }
    for plane, refs in GROUND_TRUTH.supporting_evidence.items():
        assert refs, f"no supporting evidence for {plane}"
        missing = set(refs) - ids[plane]
        assert not missing, f"{plane} evidence references unknown ids {missing}"
    for plane, plane_ids in ids.items():
        assert len(plane_ids) == len(
            {"deploys": DEPLOYS, "metrics": METRICS, "logs": LOGS, "traces": TRACES}[plane]
        ), f"duplicate record ids in {plane}"

    culprit = [d for d in DEPLOYS if d.change_id == GROUND_TRUTH.culprit_change_id]
    assert len(culprit) == 3 and all(d.service == GROUND_TRUTH.culprit_service for d in culprit)
    assert min(d.ts for d in culprit) < ONSET_TS, "cause must precede effect"
    decoy = next(d for d in DEPLOYS if d.change_id == "chg-4420")
    assert decoy.ts > ONSET_TS, "the auth-svc decoy must land after onset"

    assert not verify("checkout-api", "chg-4417", "retry_amplification")
    assert not verify("  Checkout_API ", "CHG 4417", "Retry-Amplification")  # tolerance
    for bad in (
        ("pricing-svc", "chg-4402", "connection_pool_exhaustion"),
        ("auth-svc", "chg-4420", "cache_key_collision"),
        ("cart-store", "chg-4413", "cache_key_collision"),
        ("edge-gateway", "chg-4415", "unbounded_concurrency"),
        ("checkout-api", "chg-4417", "unbounded_concurrency"),
        ("checkout-api", "chg-4417", "gremlins"),
    ):
        assert verify(*bad), f"oracle wrongly accepted {bad}"

    for name in PLANE_NAMES:
        assert len(render_plane(name)) < 12000, f"plane {name} too large for a prompt"


self_check()
