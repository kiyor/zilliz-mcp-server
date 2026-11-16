"""Usage date boundary: local pre-check and the server's bare 'Bad request' both get an actionable error."""
import asyncio
from unittest.mock import patch

import pytest

from zilliz_mcp_server.common.hints import StructuredToolError, ZillizAPIError
from zilliz_mcp_server.tools.zilliz import billing_tools as bt

MOD = "zilliz_mcp_server.tools.zilliz.billing_tools.openapi_client"


def _err(tool, **kw):
    with pytest.raises(StructuredToolError) as e:
        asyncio.run(getattr(bt, tool)(**kw))
    return e.value.payload


@patch(MOD)
def test_start_before_boundary_rejected_locally_with_clamped_retry(mock_client):
    p = _err("get_usage_summary", start="2024-12-01", end="2025-03-31")
    mock_client.control_plane_api_request.assert_not_called()
    assert p["error"] == "INVALID_ARGUMENT" and p["field"] == "start" and p["got"] == "2024-12-01"
    assert "2025-03-01" in p["message"] and "2025-03-01" in p["expected"] and "list_invoices" in p["fix"]
    assert p["next_steps"][0]["tool"] == "get_usage_summary"
    assert p["next_steps"][0]["args"]["start"] == "2025-03-01" and p["next_steps"][0]["args"]["end"] == "2025-03-31"
    assert p["next_steps"][-1]["tool"] == "list_invoices"


@patch(MOD)
def test_range_entirely_before_boundary_points_to_invoices_only(mock_client):
    p = _err("query_daily_usage", start="2024-01-01", end="2024-01-31")
    assert [s["tool"] for s in p["next_steps"]] == ["list_invoices"]


@patch(MOD)
def test_boundary_day_is_allowed(mock_client):
    mock_client.control_plane_api_request.return_value = {"code": 0, "data": {"results": []}}
    asyncio.run(bt.query_daily_usage(start="2025-03-01", end="2025-03-02"))
    assert mock_client.control_plane_api_request.called


@patch(MOD)
def test_server_bad_request_on_early_start_gets_same_hint(mock_client, monkeypatch):
    # If the boundary moves later than the constant, the server's bare error is still explained.
    monkeypatch.setattr(bt, "EARLIEST_USAGE_DATE", bt.date(2025, 6, 1))
    monkeypatch.setattr(bt, "_parse_range", lambda s, e, m: (bt.date.fromisoformat(s), bt.date.fromisoformat(e)))
    mock_client.control_plane_api_request.side_effect = ZillizAPIError("Bad request.", code=3)
    p = _err("get_usage_summary", start="2025-04-01", end="2025-07-31")
    assert p["error"] == "INVALID_ARGUMENT" and p["field"] == "start" and "2025-06-01" in p["fix"]
    assert p["message"] == "Bad request."


@patch(MOD)
def test_server_bad_request_with_valid_start_stays_generic(mock_client):
    mock_client.control_plane_api_request.side_effect = ZillizAPIError("Bad request.", code=3)
    p = _err("get_usage_summary", start="2025-04-01", end="2025-04-30")
    assert p["error"] != "INVALID_ARGUMENT" or "2025-03-01" not in p.get("fix", "")
