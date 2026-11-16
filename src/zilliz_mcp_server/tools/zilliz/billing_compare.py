"""Compare billing usage between two periods and attribute the change.

Answers "why is this month's bill higher than last month's" in one call: both periods are
pulled from the public usage API (POST /v2/usage/query), grouped by cluster x cost type,
and each group's change is split into a quantity effect and a unit-price effect.
"""

import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from zilliz_mcp_server.app import read_tool
from zilliz_mcp_server.common import hints, openapi_client
from zilliz_mcp_server.tools.zilliz.billing_tools import (
    MAX_SUMMARY_DAYS, MAX_RAW_DAYS, UNATTRIBUTED, _decimal, _fetch_usage, _money, _parse_range,
)

logger = logging.getLogger(__name__)

_GROUPINGS = ("cluster_cost_type", "cluster", "cost_type", "project", "region")
# A unit price / quantity move smaller than this is treated as "unchanged" when naming the driver.
_FLAT = Decimal("0.02")
# Keep the result well under what MCP clients show in full: measured on a large organization,
# 10 rows + all new/gone keys was ~10 KB and 380 rows was ~100 KB.
MAX_TOP_N = 50
MAX_KEY_LIST = 20


def _key(item: Dict[str, Any], group_by: str) -> str:
    p = item.get("properties") or {}
    cluster = p.get("clusterId") or UNATTRIBUTED
    cost = item.get("costType") or UNATTRIBUTED
    return {
        "cluster_cost_type": f"{cluster} / {cost}",
        "cluster": cluster,
        "cost_type": cost,
        "project": p.get("projectId") or UNATTRIBUTED,
        "region": p.get("regionId") or UNATTRIBUTED,
    }[group_by]


def _aggregate(results: List[Dict[str, Any]], group_by: str) -> Tuple[Dict[str, Dict[str, Any]], set]:
    groups: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"amount": Decimal(0), "quantity": Decimal(0), "units": set(), "cluster": None, "cost_type": None,
                 "project": None})
    currencies = set()
    for bucket in results:
        for item in bucket.get("items") or []:
            g = groups[_key(item, group_by)]
            g["amount"] += _decimal(item.get("amount"))
            g["quantity"] += _decimal(item.get("quantity"))
            if item.get("unit"):
                g["units"].add(item["unit"])
            p = item.get("properties") or {}
            # Only keep ids that are constant within the group; anything else would point the
            # next step at an arbitrary cluster/project.
            if group_by in ("cluster_cost_type", "cluster"):
                g["cluster"] = p.get("clusterId")
            if group_by in ("cluster_cost_type", "cost_type"):
                g["cost_type"] = item.get("costType")
            if group_by == "project":
                g["project"] = p.get("projectId")
            currencies.add(bucket.get("currency"))
    currencies.discard(None)
    return groups, currencies


def _pct(new: Decimal, old: Decimal) -> Optional[float]:
    if not old:
        return None
    return float(((new - old) / old * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _driver(b: Dict[str, Any], t: Dict[str, Any]) -> Dict[str, Any]:
    """Split one group's change into quantity and unit-price effects (base-price / target-quantity)."""
    if not b["amount"] and t["amount"]:
        return {"driver": "new"}
    if b["amount"] and not t["amount"]:
        return {"driver": "gone"}
    # Quantities in different units (e.g. hours + GB summed in a cluster or project group) cannot be compared.
    if not b["quantity"] or not t["quantity"] or len(b["units"]) > 1 or len(t["units"]) > 1 \
            or (b["units"] and t["units"] and b["units"] != t["units"]):
        return {"driver": "unknown"}
    pb, pt = b["amount"] / b["quantity"], t["amount"] / t["quantity"]
    qty_effect = (t["quantity"] - b["quantity"]) * pb
    price_effect = (pt - pb) * t["quantity"]
    q_move = abs(t["quantity"] - b["quantity"]) / b["quantity"]
    p_move = abs(pt - pb) / pb if pb else Decimal(0)
    if q_move < _FLAT and p_move < _FLAT:
        driver = "flat"
    elif p_move < _FLAT:
        driver = "quantity"
    elif q_move < _FLAT:
        driver = "price"
    else:
        driver = "both"
    return {"driver": driver, "quantity_effect": _money(qty_effect), "price_effect": _money(price_effect),
            "base_quantity": float(b["quantity"]), "target_quantity": float(t["quantity"]),
            "quantity_pct": _pct(t["quantity"], b["quantity"]), "unit_price_pct": _pct(pt, pb)}


def _invoice_for(invoices: List[Dict[str, Any]], start: date, end: date) -> Optional[Dict[str, Any]]:
    """Invoices whose billing period starts inside start..end; several are summed."""
    hits = []
    for inv in invoices:
        try:
            ps = date.fromisoformat(str(inv.get("periodStart", ""))[:10])
        except ValueError:
            continue
        if start <= ps <= end:
            hits.append(inv)
    if not hits:
        return None
    if len(hits) == 1:
        inv = hits[0]
        return {"invoice_id": inv.get("id"), "period_start": inv.get("periodStart"),
                "period_end": inv.get("periodEnd"), "status": inv.get("status"),
                "usage_amount": inv.get("usageAmount"), "credits_applied": inv.get("creditsApplied"),
                "total": inv.get("total")}
    hits.sort(key=lambda i: str(i.get("periodStart")))

    def total(field):
        return float(sum((_decimal(i.get(field)) for i in hits), Decimal(0)))
    return {"invoices": len(hits), "period_start": hits[0].get("periodStart"),
            "period_end": hits[-1].get("periodEnd"),
            "statuses": sorted({str(i.get("status")) for i in hits}),
            "usage_amount": total("usageAmount"), "credits_applied": total("creditsApplied"),
            "total": total("total")}


@read_tool()
async def compare_billing_periods(
    base_start: str,
    base_end: str,
    target_start: str,
    target_end: str,
    group_by: str = "cluster_cost_type",
    top_n: int = 10,
) -> str:
    """
    Explain why billing usage (cost) changed between two periods, e.g. "why is this month's bill
    50% higher than last month's". Use this instead of calling get_usage_summary twice and
    subtracting the results yourself. Requires an API key whose user is an Organization Owner or Billing Admin;
    without that it fails with PERMISSION_DENIED. Pass on the error's fix and message_for_admin
    to the user; do not estimate amounts.

    For partial periods (e.g. the current month so far) compare change.daily_avg_pct, not change.pct.
    Usage is metered cost BEFORE credits/discounts/tax. If usage is flat but the bill rose, look at
    invoice_view (credits_applied). Usage amounts are decimals in whole units of currency
    (e.g. 12.34 = 12.34 USD, not cents); invoice_view amounts are integers in whole units, rounded
    by billing, and an invoice covers its whole billing month even if the period is shorter.
    Treat quantity as a billing label (compute quantity can be CU-hours with unit "hour").
    driver is only computed when both periods use one and the same unit; groups that mix units
    (typical for group_by=cluster/project/region) get driver "unknown": use cluster_cost_type for
    a quantity vs price split.

    Args:
        base_start: First day of the earlier period, YYYY-MM-DD (UTC), e.g. "2026-09-01"
        base_end: Last day of the earlier period, inclusive, YYYY-MM-DD, e.g. "2026-09-30"
        target_start: First day of the later period, YYYY-MM-DD, e.g. "2026-10-01"
        target_end: Last day of the later period, inclusive, YYYY-MM-DD, e.g. "2026-10-31". Each period max 366 days.
        group_by: "cluster_cost_type" (default), "cluster", "cost_type", "project" or "region"
        top_n: How many of the largest changes to return (default 10, max 50)
    Returns:
        JSON {"notes": [...], "next_steps": [...], "data": <result>}. <result>:
        {"currency": "USD",
         "base": {"start", "end", "days", "usage_total", "daily_avg"},
         "target": {"start", "end", "days", "usage_total", "daily_avg"},
         "change": {"abs", "pct", "daily_avg_pct"},
         "top_changes": [{"key": "in01-xxx / compute", "base", "target", "abs", "pct",
                          "share_of_change_pct", "driver": "quantity|price|both|flat|new|gone|unknown",
                          "quantity_effect", "price_effect", "base_quantity", "target_quantity",
                          "quantity_pct", "unit_price_pct", "unit"}],
         "new_keys": [...], "gone_keys": [...] (largest 20 each; new_key_count / gone_key_count give totals),
         "invoice_view": {"base": {...}|null, "target": {...}|null} (omitted if invoices are unavailable;
                          a period spanning several invoices gets their sum with "invoices": <count>)}
        quantity_effect + price_effect = abs for groups that exist in both periods (same unit).
    """
    ctx = {"base_start": base_start, "base_end": base_end, "target_start": target_start,
           "target_end": target_end, "group_by": group_by, "top_n": top_n}
    try:
        logger.info(f"COMPARE_BILLING_PERIODS: {ctx}")
        if group_by not in _GROUPINGS:
            raise ValueError(f"group_by must be one of: {', '.join(_GROUPINGS)}")
        if not 1 <= top_n <= MAX_TOP_N:
            raise ValueError(f"top_n must be between 1 and {MAX_TOP_N}")
        bs, be = _parse_range(base_start, base_end, MAX_SUMMARY_DAYS)
        ts, te = _parse_range(target_start, target_end, MAX_SUMMARY_DAYS)

        base, cur_b = _aggregate(_fetch_usage(base_start, base_end, MAX_SUMMARY_DAYS), group_by)
        target, cur_t = _aggregate(_fetch_usage(target_start, target_end, MAX_SUMMARY_DAYS), group_by)
        currencies = cur_b | cur_t
        if len(currencies) > 1:
            raise ValueError(f"usage spans multiple currencies: {', '.join(sorted(currencies))}")

        bdays, tdays = (be - bs).days + 1, (te - ts).days + 1
        btot = sum((g["amount"] for g in base.values()), Decimal(0))
        ttot = sum((g["amount"] for g in target.values()), Decimal(0))
        delta = ttot - btot
        bavg, tavg = btot / bdays, ttot / tdays

        empty = {"amount": Decimal(0), "quantity": Decimal(0), "units": set()}
        rows = []
        for key in set(base) | set(target):
            b, t = base.get(key, empty), target.get(key, empty)
            d = t["amount"] - b["amount"]
            row = {"key": key, "base": _money(b["amount"]), "target": _money(t["amount"]), "abs": _money(d),
                   "pct": _pct(t["amount"], b["amount"]),
                   "share_of_change_pct": float((d / delta * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
                   if delta else None}
            row.update(_driver(b, t))
            units = t["units"] or b["units"]
            if units:
                row["unit"] = sorted(units)[0]
            row["_abs"] = abs(d)
            for f in ("cluster", "cost_type", "project"):
                row[f"_{f}"] = t.get(f) or b.get(f)
            rows.append(row)
        rows.sort(key=lambda r: (-r["_abs"], r["key"]))
        top = rows[:top_n]
        new_rows = [r for r in rows if r.get("driver") == "new"]
        gone_rows = [r for r in rows if r.get("driver") == "gone"]

        result: Dict[str, Any] = {
            "currency": currencies.pop() if currencies else None,
            "base": {"start": base_start, "end": base_end, "days": bdays,
                     "usage_total": _money(btot), "daily_avg": _money(bavg)},
            "target": {"start": target_start, "end": target_end, "days": tdays,
                       "usage_total": _money(ttot), "daily_avg": _money(tavg)},
            "change": {"abs": _money(delta), "pct": _pct(ttot, btot), "daily_avg_pct": _pct(tavg, bavg)},
            "top_changes": [{k: v for k, v in r.items() if not k.startswith("_")} for r in top],
            "new_keys": [r["key"] for r in new_rows[:MAX_KEY_LIST]],
            "gone_keys": [r["key"] for r in gone_rows[:MAX_KEY_LIST]],
        }
        if len(new_rows) > MAX_KEY_LIST or len(gone_rows) > MAX_KEY_LIST:
            result["new_key_count"], result["gone_key_count"] = len(new_rows), len(gone_rows)

        notes: List[str] = []
        try:
            inv = openapi_client.control_plane_api_request(
                "/v2/invoices", params_map={"pageSize": 24, "currentPage": 1})
            invoices = (inv.get("data") or {}).get("invoices") or []
            result["invoice_view"] = {"base": _invoice_for(invoices, bs, be), "target": _invoice_for(invoices, ts, te)}
        except Exception as inv_err:  # usage comparison is still valid without invoices
            notes.append(f"Invoices unavailable ({str(inv_err)[:100]}); amounts are usage before credits.")
        if bdays != tdays:
            notes.append(f"Periods differ in length ({bdays} vs {tdays} days): use change.daily_avg_pct "
                         "for like-for-like growth.")
        if not btot and not ttot:
            notes.append("No usage in either period: check the dates, or whether this key's organization is the billed one.")

        steps: List[Dict[str, Any]] = []
        lead = top[0] if top and top[0]["_abs"] else None
        if lead:
            window_end = min(te, ts + timedelta(days=MAX_RAW_DAYS - 1))
            scope = {"cluster_id": lead["_cluster"]} if lead["_cluster"] else (
                {"project_id": lead["_project"]} if lead["_project"] else {})
            steps.append(hints.step(
                "query_daily_usage",
                f"Largest change is {lead['key']} ({lead['abs']:+}, driver={lead['driver']}); "
                f"look at daily rows to find the day it started ({MAX_RAW_DAYS} days per call).",
                start=ts.isoformat(), end=window_end.isoformat(), **scope))
            if lead["_cluster"] and lead["_cost_type"] == "compute" and lead.get("driver") in ("quantity", "both", "new"):
                steps.append(hints.step("describe_cluster", "Compute quantity changed: check the cluster's CU size and status.",
                                        cluster_id=lead["_cluster"]))

        logger.info(f"COMPARE_BILLING_PERIODS RESULT: base={result['base']['usage_total']} "
                    f"target={result['target']['usage_total']} rows={len(rows)}")
        return hints.ok(result, steps, notes)

    except Exception as e:
        logger.error(f"COMPARE_BILLING_PERIODS ERROR: {str(e)}")
        raise hints.tool_error("compare_billing_periods", e, ctx) from e
