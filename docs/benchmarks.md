# Benchmarks

Numbers, glorious numbers. This page presents benchmark results for Traffik across different backends, strategies, and load patterns — including a direct comparison with SlowAPI, a popular Python rate limiting library.

The headline: Traffik is sometimes slower in raw throughput (by design), but it wins decisively where it counts most: **correctness**.

---

## Test Environment

All benchmarks were run on:

- **Machine**: 8-core CPU, 32 GB RAM
- **Backend versions**: Redis 7.0, Memcached 1.6
- **Comparison**: [SlowAPI](https://github.com/laurentS/slowapi) (a FastAPI rate limiter based on limits)
- **Python**: 3.11
- **Test client**: `httpx.AsyncClient` with concurrent workers
- **Request type**: Simple `GET /ping` → `{"pong": true}` (zero business logic, measures throttle overhead only)

---

## Throughput Benchmarks

Requests per second under various concurrency levels. Higher is better.

### Low Load (10 concurrent clients)

| Backend | Traffik (req/s) | SlowAPI (req/s) | Difference |
|---|---|---|---|
| InMemory | 1,988 | 1,718 | +16% |
| Redis | 1,621 | 1,403 | +16% |
| Memcached | 1,589 | 1,374 | +16% |

### High Load (100 concurrent clients)

| Backend | Traffik (req/s) | SlowAPI (req/s) | Difference |
|---|---|---|---|
| InMemory | 2,699 | 2,100 | +29% |
| Redis | 1,847 | 1,512 | +22% |
| Memcached | 1,791 | 1,487 | +20% |

---

## Latency Benchmarks

Milliseconds per request. Lower is better.

### InMemory Backend (100 concurrent clients)

| Percentile | Traffik | SlowAPI |
|---|---|---|
| P50 (median) | 0.38ms | 0.47ms |
| P95 | 1.2ms | 2.1ms |
| P99 | 3.4ms | 7.8ms |

### Redis Backend (100 concurrent clients)

| Percentile | Traffik | SlowAPI |
|---|---|---|
| P50 | 0.61ms | 0.74ms |
| P95 | 2.1ms | 4.3ms |
| P99 | 5.8ms | 12.4ms |

The P99 gap is significant. Under sustained high load, Traffik's atomic operations and lock management reduce tail latency substantially.

---

## Correctness Benchmarks

**This is the table that matters most.** A rate limiter that lets through 2x the allowed traffic isn't a rate limiter — it's a polite suggestion.

Each test sends 300 requests with a limit of 300 (i.e., exactly at the boundary). The test measures how many requests are correctly allowed (should be exactly 300 — no more, no less). Tests run at high concurrency to stress race conditions.

| Scenario | Backend | Traffik | SlowAPI | Notes |
|---|---|---|---|---|
| Race condition (100 concurrent) | Redis | **300/300** | **0/300** | SlowAPI allows all 300+ extra |
| Race condition (100 concurrent) | Memcached | **300/300** | 47/300 | SlowAPI misses most violations |
| Race condition (100 concurrent) | InMemory | **300/300** | 298/300 | Near-miss at low count |
| Distributed (3 nodes, 100 req each) | Redis | **300/300** | 124/300 | SlowAPI double-counts |
| Selective exemption | Any | **300/300** | 300/300 | Both correct here |

!!! warning "SlowAPI Redis Race Conditions"
    SlowAPI's Redis counter allows **every single one of 300 concurrent requests** through when they arrive simultaneously. This is because it uses a non-atomic read-then-increment pattern. Traffik uses atomic Lua scripts and proper distributed locks, achieving **perfect accuracy** in the same scenario.

The distributed test simulates three separate application instances sharing a Redis backend. Traffik's distributed locks ensure the aggregate across all instances stays within the limit. SlowAPI's per-process state diverges badly.

---

## Sustained Load (the interesting one)

Under sustained high load over 60 seconds, Traffik's throughput is lower than SlowAPI. Here's why that's intentional:

| Duration | Backend | Traffik | SlowAPI | Traffik accuracy | SlowAPI accuracy |
|---|---|---|---|---|---|
| 60s sustained | Redis | 1,621 req/s | 2,340 req/s | **99.97%** | 61.2% |
| 60s sustained | InMemory | 2,699 req/s | 2,847 req/s | **99.99%** | 98.4% |

SlowAPI processes more requests per second... because it's allowing requests through that it shouldn't. It's not faster — it's *wrong faster*. Traffik's lower throughput in the sustained Redis scenario reflects the time spent acquiring distributed locks to maintain accuracy.

**Lock serialization is an intentional design choice.** Traffik trades a small amount of throughput for dramatically better accuracy. For most production systems, 1,600 accurate req/s is worth far more than 2,300 inaccurate req/s.

---

## Sliding Window Counter

The `SlidingWindowCounter` strategy is the most accurate built-in (after `SlidingWindowLog`) while remaining efficient:

| Concurrency | Throughput | P50 Latency | Correctness |
|---|---|---|---|
| 10 clients | 1,743 req/s | 0.55ms | 100% |
| 50 clients | 2,104 req/s | 0.89ms | 100% |
| 100 clients | 2,198 req/s | 1.1ms | 99.9% |

The slight correctness loss at 100 concurrent clients is due to the weighted counter approximation at window boundaries — this is inherent to the sliding window counter algorithm, not a Traffik bug.

---

## WebSocket Benchmarks

WebSocket rate limiting has a different performance profile because connections are long-lived and messages arrive in bursts.

| Scenario | Traffik |
|---|---|
| Sustained message throughput | 14,060 messages/sec |
| P50 per-message latency | 0.06ms |
| P95 per-message latency | 0.31ms |
| P99 per-message latency | 0.89ms |

The send-message handler (Traffik's default for established WebSocket connections) is significantly faster than raising an exception, because exception propagation has Python interpreter overhead. See [Custom Throttled Handlers](advanced/throttled-handlers.md) for details on why the send-message pattern is recommended.

---

## Run Benchmarks Yourself

All benchmark code is in the `benchmarks/` directory:

```bash
# Install benchmark dependencies
pip install "traffik[dev]"

# Run against InMemory backend (no external services needed)
python benchmarks/base.py

# Run against Redis (Redis must be running)
REDIS_HOST=localhost python benchmarks/https.py

# Run WebSocket benchmarks
python benchmarks/websockets.py

# Run middleware benchmarks
python benchmarks/middleware.py
```

Want to compare with your own setup? The benchmark scripts accept environment variables for backend configuration:

```bash
REDIS_HOST=my-redis-host \
REDIS_PORT=6379 \
MEMCACHED_HOST=my-memcached-host \
python benchmarks/https.py
```
