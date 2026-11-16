"""Tests for coarse-granularity coverage hints in query_cluster_metrics."""

import json
from unittest.mock import patch

import pytest

from zilliz_mcp_server.tools.zilliz import metrics as m
from zilliz_mcp_server.tools.zilliz import zilliz_tools

CID = "in01-abc"
MQ = [{"metricName": "REQ_SEARCH_COUNT"}]


def res(values):
    return [{"name": "REQ_SEARCH_COUNT", "values": [{"timestamp": f"t{i}", "value": v} for i, v in enumerate(values)]}]


class TestDuration:
    @pytest.mark.parametrize("iso,sec", [("PT30S", 30), ("PT5M", 300), ("PT1H", 3600), ("P1D", 86400),
                                         ("P180D", 180 * 86400), ("P1DT12H", 129600)])
    def test_parse(self, iso, sec):
        assert m.duration_seconds(iso) == sec

    @pytest.mark.parametrize("iso", ["P6M", "P2W", "", None, "P"])
    def test_unsupported(self, iso):
        assert m.duration_seconds(iso) is None

    def test_finest_granularity_respects_point_cap(self):
        assert m.finest_granularity(7 * 86400) == "PT30S"
        assert m.finest_granularity(30 * 86400) == "PT2M"
        assert m.finest_granularity(180 * 86400) == "PT15M"
        for days in (1, 7, 30, 90, 180, 366):
            g = m.finest_granularity(days * 86400)
            assert days * 86400 / m.duration_seconds(g) <= m.MAX_POINTS


class TestCoverageHints:
    def test_all_null_coarse_suggests_finer_same_window_and_recent(self):
        steps, notes = m.coverage_hints(res([None] * 180), CID, MQ, "P1D", "P180D", None, None)
        assert "does NOT mean no traffic" in notes[0] and "0/180" in notes[0]
        assert steps[0]["args"] == {"cluster_id": CID, "period": "P180D", "granularity": "PT15M",
                                    "metric_queries": MQ}
        assert steps[1]["args"]["period"] == "P7D" and steps[1]["args"]["granularity"] == "PT30S"

    def test_one_day_at_pt1h_suggests_pt30s_only(self):
        steps, _ = m.coverage_hints(res([None] * 24), CID, MQ, "PT1H", "P1D", None, None)
        assert len(steps) == 1 and steps[0]["args"]["granularity"] == "PT30S"

    def test_start_end_window_kept(self):
        s, e = "2026-11-01T00:00:00Z", "2026-11-08T00:00:00Z"
        steps, _ = m.coverage_hints(res([None] * 168), CID, MQ, "PT1H", None, s, e)
        assert steps[0]["args"]["start"] == s and steps[0]["args"]["end"] == e and "period" not in steps[0]["args"]

    def test_good_coverage_is_quiet(self):
        assert m.coverage_hints(res(["1"] * 24), CID, MQ, "PT1H", "P1D", None, None) == ([], [])

    def test_fine_granularity_is_quiet(self):
        assert m.coverage_hints(res([None] * 120), CID, MQ, "PT30S", "PT1H", None, None) == ([], [])
        assert m.coverage_hints(res([None] * 30), CID, MQ, "PT2M", "PT1H", None, None) == ([], [])


@pytest.mark.asyncio
@patch("zilliz_mcp_server.tools.zilliz.zilliz_tools.openapi_client")
async def test_tool_returns_coverage_hint(client):
    client.control_plane_api_request.return_value = {"code": 0, "data": {"results": res([None, None, "2.0", None])}}
    out = json.loads(await zilliz_tools.query_cluster_metrics(
        cluster_id=CID, period="P7D", granularity="P1D", metric_queries=MQ))
    assert out["next_steps"][0]["args"]["granularity"] == "PT30S"
    assert any("does NOT mean no traffic" in n for n in out["notes"])
    assert out["data"]["results"][0]["values"][0]["value_status"] == "no_data"


def test_fail_rate_nulls_do_not_trigger_when_counts_are_covered():
    results = res(["1"] * 24) + [{"name": "REQ_FAIL_RATE_SEARCH", "values": [{"timestamp": "t", "value": None}] * 24}]
    assert m.coverage_hints(results, CID, MQ, "PT1H", "P1D", None, None) == ([], [])


@pytest.mark.asyncio
async def test_month_period_rejected_locally():
    with pytest.raises(Exception) as ei:
        await zilliz_tools.query_cluster_metrics(cluster_id=CID, period="P6M", granularity="P1D", metric_queries=MQ)
    p = json.loads(str(ei.value))
    assert p["error"] == "INVALID_ARGUMENT" and "P180D" in p["message"]
