"""Prometheus-compatible metrics for platform observability.

Exposes key operational metrics: LLM call counts, latencies, costs,
pipeline run status, and error rates. Metrics are registered globally
and can be scraped by a Prometheus server or exported on demand.

Usage:
    from core.observability.metrics import LLM_REQUESTS, LLM_LATENCY

    LLM_REQUESTS.labels(model="claude-sonnet-4-20250514", status="success").inc()
    LLM_LATENCY.labels(model="claude-sonnet-4-20250514").observe(1.23)
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram, Info

# --- Platform info ---
PLATFORM_INFO = Info("kokomoor_platform", "Platform build information")

# --- LLM metrics ---
LLM_REQUESTS = Counter(
    "kokomoor_llm_requests_total",
    "Total LLM API requests",
    ["model", "status"],
)

LLM_LATENCY = Histogram(
    "kokomoor_llm_latency_seconds",
    "LLM request latency in seconds",
    ["model"],
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120),
)

LLM_TOKENS = Counter(
    "kokomoor_llm_tokens_total",
    "Total LLM tokens consumed",
    ["model", "direction"],  # direction: input | output
)

LLM_COST_USD = Counter(
    "kokomoor_llm_cost_usd_total",
    "Cumulative LLM cost in USD",
    ["model"],
)

# --- Pipeline metrics ---
PIPELINE_RUNS = Counter(
    "kokomoor_pipeline_runs_total",
    "Total pipeline runs",
    ["pipeline", "status"],
)

PIPELINE_NODE_DURATION = Histogram(
    "kokomoor_pipeline_node_seconds",
    "Duration of individual pipeline nodes",
    ["pipeline", "node"],
    buckets=(0.1, 0.5, 1, 5, 10, 30, 60, 300),
)

# --- Browser metrics ---
BROWSER_NAVIGATIONS = Counter(
    "kokomoor_browser_navigations_total",
    "Total browser page navigations",
    ["status"],
)
