"""Metric catalog, local validation and null interpretation for query_cluster_metrics.

The catalog only lists metric names that the Query Cluster Metrics API
(POST /v2/clusters/{CLUSTER_ID}/metrics/query) actually accepts.
"""

from typing import Any, Dict, List, Optional

from zilliz_mcp_server.common.hints import StructuredToolError

# Request operations that have count / latency / failure-rate series.
_OPS = ["INSERT", "BULK_INSERT", "UPSERT", "DELETE", "SEARCH", "QUERY"]

# name -> short description. Order is the order shown to the model.
METRICS: Dict[str, str] = {
    # Resource
    "CU_COMPUTATION": "Compute unit computation usage (percent)",
    "CU_CAPACITY": "Compute unit capacity usage (percent)",
    "STORAGE_USE": "Storage used (GB)",
    "CLUSTER_WRITE_CAPACITY": "Write capacity usage (percent)",
    "READ_VCU": "Read vCU consumption (Serverless clusters only)",
    "WRITE_VCU": "Write vCU consumption (Serverless clusters only)",
    # Request throughput
    "REQ_INSERT_COUNT": "Insert requests per second (qps)",
    "REQ_BULK_INSERT_COUNT": "Bulk insert requests per second (qps)",
    "REQ_UPSERT_COUNT": "Upsert requests per second (qps)",
    "REQ_DELETE_COUNT": "Delete requests per second (qps)",
    "REQ_SEARCH_COUNT": "Search requests per second (qps)",
    "REQ_QUERY_COUNT": "Query requests per second (qps)",
    "VECTOR_REQ_INSERT_COUNT": "Vectors inserted per second (vps)",
    "VECTOR_REQ_BULK_INSERT_COUNT": "Vectors bulk-inserted per second (vps)",
    "VECTOR_REQ_UPSERT_COUNT": "Vectors upserted per second (vps)",
    "VECTOR_REQ_DELETE_COUNT": "Vectors deleted per second (vps)",
    "VECTOR_REQ_SEARCH_COUNT": "Query vectors searched per second (vps)",
    "SLOW_QUERY_COUNT": "Slow queries per minute (count/m)",
    # Latency (the percentile is part of the name)
    "REQ_INSERT_LATENCY_AVG": "Insert latency, average (ms)",
    "REQ_INSERT_LATENCY_P99": "Insert latency, P99 (ms)",
    "REQ_UPSERT_LATENCY_AVG": "Upsert latency, average (ms)",
    "REQ_UPSERT_LATENCY_P99": "Upsert latency, P99 (ms)",
    "REQ_DELETE_LATENCY_AVG": "Delete latency, average (ms)",
    "REQ_DELETE_LATENCY_P99": "Delete latency, P99 (ms)",
    "REQ_SEARCH_LATENCY_AVG": "Search latency, average (ms)",
    "REQ_SEARCH_LATENCY_P99": "Search latency, P99 (ms)",
    "REQ_QUERY_LATENCY_AVG": "Query latency, average (ms)",
    "REQ_QUERY_LATENCY_P99": "Query latency, P99 (ms)",
    # Failure rate (there is no overall success/fail rate; query per operation)
    "REQ_FAIL_RATE_INSERT": "Failed insert requests (percent)",
    "REQ_FAIL_RATE_BULK_INSERT": "Failed bulk insert requests (percent)",
    "REQ_FAIL_RATE_UPSERT": "Failed upsert requests (percent)",
    "REQ_FAIL_RATE_DELETE": "Failed delete requests (percent)",
    "REQ_FAIL_RATE_SEARCH": "Failed search requests (percent)",
    "REQ_FAIL_RATE_QUERY": "Failed query requests (percent)",
    # Data
    "ENTITIES_COUNT": "Total entities (count)",
    "ENTITIES_LOADED": "Loaded entities (count)",
    "ENTITIES_INDEXED": "Indexed entities (count)",
    "COLLECTIONS_COUNT": "Collections (count)",
    "UNLOADED_COLLECTIONS_COUNT": "Unloaded collections (count)",
    "FLUSH_COUNT": "Flush operations per minute (count/m)",
}

# Names that look plausible (and appeared in older docs) but are rejected by the API.
REPLACEMENTS: Dict[str, str] = {
    "REQ_SUCCESS_RATE": "There is no overall success rate. Use REQ_FAIL_RATE_<OP> per operation "
                        "(e.g. REQ_FAIL_RATE_SEARCH); success rate = 100 - fail rate.",
    "REQ_FAIL_RATE": "There is no overall fail rate. Use REQ_FAIL_RATE_<OP> per operation, "
                     "e.g. REQ_FAIL_RATE_SEARCH, REQ_FAIL_RATE_QUERY, REQ_FAIL_RATE_INSERT.",
    "ENTITIES_INSERT_RATE": "Use VECTOR_REQ_INSERT_COUNT (vectors inserted per second).",
    "REQ_BULK_INSERT_LATENCY_P99": "Bulk insert latency is not available. Use REQ_BULK_INSERT_COUNT "
                                   "and REQ_FAIL_RATE_BULK_INSERT.",
}

DEFAULT_STAT = "AVG"


def _invalid(message: str, expected: str, got: Any, fix: str) -> StructuredToolError:
    """INVALID_ARGUMENT error for metric_queries. The full list of valid values goes in
    'expected' so it is never cut by the message length limit."""
    return StructuredToolError({
        "error": "INVALID_ARGUMENT",
        "message": message,
        "field": "metric_queries",
        "expected": expected,
        "got": got,
        "fix": fix,
    })


VALID_NAMES = "Valid metric names: " + ", ".join(METRICS)


def allowed_stats(name: str) -> List[str]:
    """Stats accepted for a metric.

    The API treats 'stat' as deprecated: every result comes back as the
    per-bucket value of the named metric (the response always echoes AVG).
    The percentile of a latency metric is chosen by its name, so the only
    accepted values are AVG, plus P99 for *_LATENCY_P99 for compatibility.
    """
    if name.endswith("_LATENCY_P99"):
        return ["AVG", "P99"]
    return ["AVG"]


def validate_metric_queries(metric_queries: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Validate and normalize metric queries. Raises ValueError with valid choices."""
    if not metric_queries:
        raise _invalid("metric_queries is empty.", VALID_NAMES, metric_queries,
                       'Pass at least one item like {"metricName": "REQ_SEARCH_COUNT"}.')
    out = []
    for q in metric_queries:
        if not isinstance(q, dict) or "metricName" not in q:
            raise _invalid("Each metric query must be an object with 'metricName' (and optional 'stat').",
                           '[{"metricName": "<METRIC_NAME>", "stat": "AVG"}]. ' + VALID_NAMES, q,
                           'Use e.g. {"metricName": "REQ_SEARCH_LATENCY_P99"}.')
        name = str(q["metricName"]).strip().upper()
        if name not in METRICS:
            fix = REPLACEMENTS.get(name, "Pick a name from 'expected'.")
            raise _invalid(f"Unknown metric name '{q['metricName']}'.", VALID_NAMES, q["metricName"], fix)
        stat = str(q.get("stat") or DEFAULT_STAT).strip().upper()
        valid = allowed_stats(name)
        if stat not in valid:
            fix = "Omit stat or use AVG."
            if "_LATENCY_" in name or stat in ("P99", "P90"):
                fix += (" For latency percentiles pick the metric by name instead, e.g. "
                        "REQ_SEARCH_LATENCY_P99 or REQ_SEARCH_LATENCY_AVG.")
            raise _invalid(f"stat '{q.get('stat')}' is not valid for {name}.",
                           f"Valid stat values for {name}: {', '.join(valid)} (or omit stat)",
                           q.get("stat"), fix)
        out.append({"name": name, "stat": stat})
    return out


def paired_count_metric(name: str) -> Optional[str]:
    """Request-count metric whose traffic explains nulls in a latency / fail-rate metric."""
    for op in _OPS:
        if name == f"REQ_FAIL_RATE_{op}" or name.startswith(f"REQ_{op}_LATENCY_"):
            count = f"REQ_{op}_COUNT"
            return count if count in METRICS else None
    return None


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


NULL_NOTE = ("value=null means the API reported no data point for that time bucket. It is NOT "
             "the same as 0. See each point's 'value_status'.")
NULL_NOTE_UNEXPLAINED = (
    "value=null means no data point was reported for that bucket. It is not 0. Common causes: "
    "the cluster was not running yet, or this operation type has never been called on the "
    "cluster. Count metrics report 0 (not null) once the operation has been used. To verify, "
    "query a resource metric such as CU_COMPUTATION for the same window: if it is also null, "
    "the cluster was not reporting metrics then.")


def annotate(results: List[Dict[str, Any]], counts: Dict[str, Dict[str, Optional[float]]]) -> None:
    """Add value_status to every point and a null_note to results that contain nulls.

    counts: metric name -> {timestamp: numeric value or None} for paired request-count metrics.
    value_status values:
      - "reported": the API returned a value (it may legitimately be "0").
      - "unknown_or_no_failures": fail-rate is null while requests were served in that bucket.
        Usually this means nothing failed, but the fail-rate series alone cannot prove it.
      - "no_requests": latency / fail-rate is null and the paired request count is 0.
      - "no_data": no data point reported; cause cannot be determined from this metric alone.
    """
    for res in results:
        name = res.get("name", "")
        values = res.get("values") or []
        pair = paired_count_metric(name)
        pair_series = counts.get(pair) if pair else None
        nulls = 0
        for point in values:
            if point.get("value") is not None:
                point["value_status"] = "reported"
                continue
            nulls += 1
            status = "no_data"
            if pair_series is not None and point.get("timestamp") in pair_series:
                c = pair_series[point["timestamp"]]
                if c == 0:
                    status = "no_requests"
                elif c is not None and name.startswith("REQ_FAIL_RATE_"):
                    status = "unknown_or_no_failures"
            point["value_status"] = status
        if nulls:
            res["null_points"] = nulls
            if pair_series is not None:
                res["null_note"] = (
                    NULL_NOTE + f" Statuses were derived from {pair} for the same window: "
                    "'unknown_or_no_failures' = requests were served but no failure data was reported; "
                    "this usually means no request failed, but the metric itself cannot confirm it, so "
                    "say so rather than stating 0%; "
                    "'no_requests' = the request count was 0 in that bucket; "
                    f"'no_data' = neither this metric nor {pair} reported a point (e.g. the operation "
                    "had never been used yet).")
            else:
                res["null_note"] = NULL_NOTE_UNEXPLAINED


def needed_count_metrics(results: List[Dict[str, Any]], already: Dict[str, Any]) -> List[str]:
    """Paired count metrics needed to explain nulls and not already in the response."""
    need = []
    for res in results:
        if not any(p.get("value") is None for p in res.get("values") or []):
            continue
        pair = paired_count_metric(res.get("name", ""))
        if pair and pair not in already and pair not in need:
            need.append(pair)
    return need


def series_by_timestamp(results: List[Dict[str, Any]]) -> Dict[str, Dict[str, Optional[float]]]:
    return {r.get("name", ""): {p.get("timestamp"): _num(p.get("value")) for p in r.get("values") or []}
            for r in results}


def api_error_fix(names: List[str]) -> Optional[str]:
    """Extra fix text for a server-side rejection of these metric names, if any applies."""
    if any(n in ("READ_VCU", "WRITE_VCU") for n in names):
        return ("READ_VCU / WRITE_VCU are only available on Serverless clusters; Dedicated clusters "
                "reject them. Remove them, or use CU_COMPUTATION / CU_CAPACITY on Dedicated clusters.")
    return None
