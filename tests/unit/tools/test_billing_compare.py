"""Unit tests for compare_billing_periods (mocked; response shapes follow the public
usage/query and list-invoices docs). Amount units are not asserted as currency units."""
import json
from unittest.mock import patch

import pytest

from zilliz_mcp_server.common.hints import StructuredToolError

MOD = "zilliz_mcp_server.tools.zilliz.billing_compare"


def _item(cluster, cost, qty, amount, unit="hour"):
    return {"costType": cost, "quantity": qty, "unit": unit, "amount": amount,
            "properties": {"clusterId": cluster, "projectId": "proj-1", "regionId": "aws-us-west-2"}}


def _usage(*items, currency="USD"):
    return {"code": 0, "data": {"results": [{"intervalStart": "2026-09-01T00:00:00Z",
                                             "currency": currency, "items": list(items)}]}}


INVOICES = {"code": 0, "data": {"count": 2, "invoices": [
    {"id": "inv-10", "periodStart": "2026-10-01T00:00:00Z", "periodEnd": "2026-10-31T23:59:59Z",
     "status": "unpaid", "usageAmount": 1200, "creditsApplied": 0, "total": 1200},
    {"id": "inv-09", "periodStart": "2026-09-01T00:00:00Z", "periodEnd": "2026-09-30T23:59:59Z",
     "status": "paid", "usageAmount": 800, "creditsApplied": 0, "total": 800},
]}}


def _client(base, target, invoices=INVOICES):
    def fake(uri, params_map=None, body_map=None, method="GET"):
        if uri == "/v2/invoices":
            if isinstance(invoices, Exception):
                raise invoices
            return invoices
        return base if body_map["start"].startswith("2026-09") else target
    return fake


async def _run(**kw):
    from zilliz_mcp_server.tools.zilliz.billing_compare import compare_billing_periods
    args = dict(base_start="2026-09-01", base_end="2026-09-30", target_start="2026-10-01", target_end="2026-10-31")
    args.update(kw)
    return json.loads(await compare_billing_periods(**args))


@pytest.mark.asyncio
async def test_quantity_driver_new_gone_and_hints():
    base = _usage(_item("in01-a", "compute", 1000, 500), _item("in01-a", "storage", 100, 50),
                  _item("in01-old", "compute", 100, 250))
    target = _usage(_item("in01-a", "compute", 2000, 1000), _item("in01-a", "storage", 100, 50),
                    _item("in01-new", "storage", 10, 150))
    with patch(f"{MOD}.openapi_client.control_plane_api_request", side_effect=_client(base, target)):
        out = await _run()
    d = out["data"]
    assert d["base"]["usage_total"] == 800.0 and d["target"]["usage_total"] == 1200.0
    assert d["change"]["pct"] == 50.0
    assert d["base"]["days"] == 30 and d["target"]["days"] == 31
    top = d["top_changes"][0]
    assert top["key"] == "in01-a / compute" and top["driver"] == "quantity"
    assert top["quantity_effect"] == 500.0 and top["price_effect"] == 0.0
    assert top["share_of_change_pct"] == 125.0
    assert d["new_keys"] == ["in01-new / storage"] and d["gone_keys"] == ["in01-old / compute"]
    flat = next(r for r in d["top_changes"] if r["key"] == "in01-a / storage")
    assert flat["driver"] == "flat"
    assert d["invoice_view"]["base"]["invoice_id"] == "inv-09"
    assert d["invoice_view"]["target"]["invoice_id"] == "inv-10"
    steps = out["next_steps"]
    assert steps[0]["tool"] == "query_daily_usage"
    assert steps[0]["args"] == {"start": "2026-10-01", "end": "2026-10-07", "cluster_id": "in01-a"}
    assert steps[1]["tool"] == "describe_cluster"
    assert any("daily_avg_pct" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_price_driver_and_effects_sum_to_change():
    base = _usage(_item("in01-a", "compute", 1000, 500))
    target = _usage(_item("in01-a", "compute", 1000, 650))
    with patch(f"{MOD}.openapi_client.control_plane_api_request", side_effect=_client(base, target)):
        out = await _run(target_end="2026-10-30")
    row = out["data"]["top_changes"][0]
    assert row["driver"] == "price" and row["unit_price_pct"] == 30.0
    assert row["quantity_effect"] + row["price_effect"] == row["abs"] == 150.0
    assert "notes" not in out or not any("differ in length" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_invoices_unavailable_still_returns_usage_comparison():
    base = _usage(_item("in01-a", "compute", 1, 1))
    target = _usage(_item("in01-a", "compute", 2, 2))
    with patch(f"{MOD}.openapi_client.control_plane_api_request",
               side_effect=_client(base, target, invoices=Exception("boom"))):
        out = await _run()
    assert "invoice_view" not in out["data"]
    assert any("Invoices unavailable" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_multiple_currencies_rejected():
    base = _usage(_item("in01-a", "compute", 1, 1), currency="USD")
    target = _usage(_item("in01-a", "compute", 1, 1), currency="EUR")
    with patch(f"{MOD}.openapi_client.control_plane_api_request", side_effect=_client(base, target)):
        with pytest.raises(StructuredToolError) as ei:
            await _run()
    p = ei.value.payload
    assert p["error"] == "INVALID_ARGUMENT" and "currencies" in p["message"]


@pytest.mark.asyncio
async def test_bad_group_by_and_dates_are_invalid_argument():
    with pytest.raises(StructuredToolError) as ei:
        await _run(group_by="day")
    assert "INVALID_ARGUMENT" in str(ei.value) and "cluster_cost_type" in str(ei.value)
    with pytest.raises(StructuredToolError):
        await _run(base_start="2026/09/01")


@pytest.mark.asyncio
async def test_permission_denied_gives_actionable_error():
    from zilliz_mcp_server.common.openapi_client import ZillizAPIError
    err = ZillizAPIError("Missing ACL permission: orgId=org-x, resource=billing, action=view", code=96041) \
        if "code" in ZillizAPIError.__init__.__code__.co_varnames else ZillizAPIError(
            "Missing ACL permission: orgId=org-x, resource=billing, action=view")
    if getattr(err, "code", None) is None:
        err.code = 96041
    with patch(f"{MOD}.openapi_client.control_plane_api_request", side_effect=err):
        with pytest.raises(StructuredToolError) as ei:
            await _run()
    p = ei.value.payload
    assert p["error"] == "PERMISSION_DENIED"
    assert p["missing_permission"] == "billing:view"
    for k in ("who_can_grant", "fix", "message_for_admin", "next_steps"):
        assert p.get(k), k
    assert p["next_steps"][0]["tool"] == "compare_billing_periods"
    assert p["next_steps"][0]["args"]["base_start"] == "2026-09-01"
