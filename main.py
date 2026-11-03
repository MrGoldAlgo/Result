import hashlib
import os
import secrets
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me-now")
SITE_TITLE = os.getenv("SITE_TITLE", "Mr Gold Algo Performance")
REST = f"{SUPABASE_URL}/rest/v1" if SUPABASE_URL else ""
_META_CACHE: dict[int, tuple[float, dict]] = {}
_META_CACHE_TTL = 300.0

app = FastAPI(title=SITE_TITLE, version="1.37")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _headers(extra: Optional[dict] = None) -> dict:
    if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
        raise HTTPException(503, "Supabase is not configured on this server")
    h = {
        "apikey": SUPABASE_SECRET_KEY,
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _check(r: httpx.Response):
    if r.status_code >= 400:
        detail = r.text[:1000]
        raise HTTPException(502, f"Database request failed ({r.status_code}): {detail}")


def supa_get(table: str, params: Optional[dict] = None, limit: Optional[int] = None) -> list[dict]:
    q = dict(params or {})
    q.setdefault("select", "*")
    if limit is not None:
        q["limit"] = str(limit)
    with httpx.Client(timeout=25.0) as c:
        r = c.get(f"{REST}/{table}", headers=_headers(), params=q)
    _check(r)
    return r.json()


def supa_iter(table: str, params: Optional[dict] = None, chunk: int = 2000, max_rows: int = 1000000):
    """Stream PostgREST rows page-by-page so Render never holds a huge result set in RAM."""
    q = {"select": "*", **(params or {})}
    start = 0
    with httpx.Client(timeout=30.0) as c:
        while start < max_rows:
            end = min(start + chunk - 1, max_rows - 1)
            r = c.get(
                f"{REST}/{table}",
                headers=_headers({"Range": f"{start}-{end}"}),
                params=q,
            )
            _check(r)
            rows = r.json()
            for row in rows:
                yield row
            if len(rows) < chunk:
                break
            start += chunk


def supa_rpc(function_name: str, payload: Optional[dict] = None):
    """Call a small server-side Postgres function through Supabase RPC."""
    with httpx.Client(timeout=30.0) as c:
        r = c.post(
            f"{REST}/rpc/{function_name}",
            headers=_headers(),
            json=payload or {},
        )
    _check(r)
    return r.json() if r.text else None

def supa_insert(table: str, payload, upsert: bool = False) -> list[dict]:
    prefer = "return=representation"
    if upsert:
        prefer += ",resolution=merge-duplicates"
    with httpx.Client(timeout=30.0) as c:
        r = c.post(f"{REST}/{table}", headers=_headers({"Prefer": prefer}), json=payload)
    _check(r)
    return r.json() if r.text else []


def supa_upsert(table: str, payload, on_conflict: str) -> None:
    """Batch upsert rows using a named/composite unique constraint."""
    with httpx.Client(timeout=30.0) as c:
        r = c.post(
            f"{REST}/{table}",
            headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            params={"on_conflict": on_conflict},
            json=payload,
        )
    _check(r)


def supa_patch(table: str, params: dict, payload: dict) -> list[dict]:
    with httpx.Client(timeout=30.0) as c:
        r = c.patch(
            f"{REST}/{table}",
            headers=_headers({"Prefer": "return=representation"}),
            params={"select": "*", **params},
            json=payload,
        )
    _check(r)
    return r.json() if r.text else []


def require_admin(x_admin_token: str = Header(default="")):
    if not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(401, "Invalid admin token")


def account_from_key(x_api_key: str) -> dict:
    if not x_api_key:
        raise HTTPException(401, "Missing X-API-Key")
    rows = supa_get("accounts", {"api_key_hash": f"eq.{sha(x_api_key)}"}, limit=1)
    if not rows:
        raise HTTPException(401, "Invalid API key")
    return rows[0]


def get_acc_by_slug(slug: str) -> dict:
    rows = supa_get("accounts", {"public_slug": f"eq.{slug}"}, limit=1)
    if not rows:
        raise HTTPException(404, "Account not found")
    return rows[0]


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    public_slug: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")


class SnapshotIn(BaseModel):
    ts: datetime
    broker: Optional[str] = None
    server: Optional[str] = None
    login: Optional[str] = None
    currency: str = "USD"
    account_type: str = "unknown"
    balance: float
    equity: float
    margin: float = 0.0
    free_margin: float = 0.0
    floating_profit: float = 0.0
    net_deposit: Optional[float] = None  # legacy V1.36 compatibility
    deposit_total: Optional[float] = None
    withdrawal_total: Optional[float] = None


class TradeIn(BaseModel):
    deal_ticket: str
    position_id: Optional[str] = None
    magic: str = "0"
    unique_id: str = "Unassigned"
    strategy: str = "Unlabelled"
    symbol: str
    side: str
    volume: float = 0.0
    open_price: float = 0.0
    close_price: float = 0.0
    open_time: datetime
    close_time: datetime
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    fee: float = 0.0
    net_profit: Optional[float] = None
    comment: str = ""


class TradeBatch(BaseModel):
    trades: list[TradeIn]


def iso(d: datetime) -> str:
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat()


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@app.get("/")
def root():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/admin")
def admin_page():
    return FileResponse(BASE / "static" / "admin.html")


@app.get("/p/{slug}")
def public_page(slug: str):
    return FileResponse(BASE / "static" / "public.html")


@app.get("/health")
def health():
    return {
        "ok": True,
        "version": "1.37",
        "storage": "supabase",
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_SECRET_KEY),
        "unique_id_tracking": True,
        "optimized_loading": True,
        "equity_dd": "postgres_rpc",
    }


@app.post("/api/v1/admin/accounts")
def create_account(payload: AccountCreate, x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    existing = supa_get("accounts", {"public_slug": f"eq.{payload.public_slug}"}, limit=1)
    if existing:
        raise HTTPException(409, "Public slug already exists")
    raw_key = "mg_" + secrets.token_urlsafe(24)
    rows = supa_insert(
        "accounts",
        {"name": payload.name, "public_slug": payload.public_slug, "api_key_hash": sha(raw_key)},
    )
    if not rows:
        raise HTTPException(502, "Account was not returned by database")
    acc = rows[0]
    return {
        "id": acc["id"],
        "name": acc["name"],
        "public_slug": acc["public_slug"],
        "api_key": raw_key,
        "public_url": f"/p/{acc['public_slug']}",
    }


@app.get("/api/v1/admin/accounts")
def list_accounts(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    rows = supa_get("accounts", {"order": "id.desc"})
    return [
        {
            "id": a["id"],
            "name": a["name"],
            "public_slug": a["public_slug"],
            "broker": a.get("broker"),
            "server": a.get("server"),
            "login": a.get("login"),
            "currency": a.get("currency"),
            "account_type": a.get("account_type"),
        }
        for a in rows
    ]


@app.post("/api/v1/sync/snapshot")
def sync_snapshot(payload: SnapshotIn, x_api_key: str = Header(default="")):
    acc = account_from_key(x_api_key)
    supa_patch(
        "accounts",
        {"id": f"eq.{acc['id']}"},
        {
            "broker": payload.broker,
            "server": payload.server,
            "login": payload.login,
            "currency": payload.currency,
            "account_type": payload.account_type,
        },
    )
    snapshot_row = {
        "account_id": acc["id"],
        "ts": iso(payload.ts),
        "balance": payload.balance,
        "equity": payload.equity,
        "margin": payload.margin,
        "free_margin": payload.free_margin,
        "floating_profit": payload.floating_profit,
    }
    if payload.net_deposit is not None:
        snapshot_row["net_deposit"] = payload.net_deposit
    if payload.deposit_total is not None:
        snapshot_row["deposit_total"] = payload.deposit_total
    if payload.withdrawal_total is not None:
        snapshot_row["withdrawal_total"] = payload.withdrawal_total
    supa_insert("snapshots", snapshot_row)
    return {"ok": True}


@app.post("/api/v1/sync/trades")
def sync_trades(payload: TradeBatch, x_api_key: str = Header(default="")):
    acc = account_from_key(x_api_key)
    if not payload.trades:
        return {"ok": True, "upserted": 0}

    tickets = [str(t.deal_ticket) for t in payload.trades]
    in_value = "in.(" + ",".join(tickets) + ")"
    existing_rows = supa_get(
        "trades",
        {
            "account_id": f"eq.{acc['id']}",
            "deal_ticket": in_value,
            "select": "deal_ticket,unique_id,strategy",
        },
    )
    existing = {str(r["deal_ticket"]): r for r in existing_rows}
    rows = []

    for t in payload.trades:
        d = t.model_dump()
        ticket = str(d["deal_ticket"])
        d["unique_id"] = (d.get("unique_id") or "Unassigned").strip()[:80] or "Unassigned"
        old = existing.get(ticket)
        if old:
            if d["unique_id"] == "Unassigned" and old.get("unique_id") not in (None, "", "Unassigned"):
                d["unique_id"] = old["unique_id"]
            if (not d.get("strategy") or str(d.get("strategy", "")).startswith("Unlabelled")) and old.get("strategy"):
                d["strategy"] = old["strategy"]
        if d["net_profit"] is None:
            d["net_profit"] = d["profit"] + d["commission"] + d["swap"] + d["fee"]
        d["account_id"] = acc["id"]
        d["open_time"] = iso(d["open_time"])
        d["close_time"] = iso(d["close_time"])
        rows.append(d)

    # Upsert, rather than ignore duplicate tickets. This lets V1.36 backfill
    # historical trades with corrected two-sided commission on the first resync.
    supa_upsert("trades", rows, "account_id,deal_ticket")
    _META_CACHE.pop(int(acc["id"]), None)
    return {"ok": True, "upserted": len(rows)}

def trade_stats(trades: list[dict]) -> dict:
    vals = [float(t.get("net_profit") or 0) for t in trades]
    wins = [x for x in vals if x > 0]
    losses = [x for x in vals if x < 0]
    gp, gl, net = sum(wins), abs(sum(losses)), sum(vals)
    running = peak = maxdd = maxdd_pct = 0.0
    curve = []
    for t in trades:
        running += float(t.get("net_profit") or 0)
        peak = max(peak, running)
        dd = peak - running
        maxdd = max(maxdd, dd)
        if peak > 0:
            maxdd_pct = max(maxdd_pct, dd / peak * 100)
        curve.append({"ts": t["close_time"], "value": round(running, 2)})
    return {
        "net_profit": round(net, 2),
        "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0,
        "profit_factor": round(gp / gl, 2) if gl else (999 if gp else 0),
        "max_closed_dd": round(maxdd, 2),
        "max_closed_dd_pct": round(maxdd_pct, 2),
        "total_trades": len(trades),
        "avg_trade": round(net / len(trades), 2) if trades else 0,
        "gross_profit": round(gp, 2),
        "gross_loss": round(-gl, 2),
        "closed_curve": curve,
    }


def gain_metrics(trades: list[dict], base_capital: float) -> dict:
    """Gain percentages for the currently selected date/UID/Magic/Symbol scope.

    Daily/monthly averages are arithmetic means across active trading days/months
    (periods containing at least one closed trade), using total positive MT5
    balance deposits as the funding base when V1.36 Rev1 snapshots are available.
    """
    base = abs(float(base_capital or 0))
    daily: dict[str, float] = {}
    monthly: dict[str, float] = {}
    total = 0.0
    for t in trades:
        net = float(t.get("net_profit") or 0)
        total += net
        try:
            dt = parse_ts(str(t.get("close_time") or ""))
        except Exception:
            continue
        day_key = dt.strftime("%Y-%m-%d")
        month_key = dt.strftime("%Y-%m")
        daily[day_key] = daily.get(day_key, 0.0) + net
        monthly[month_key] = monthly.get(month_key, 0.0) + net

    if base <= 1e-12:
        return {
            "total_gain_pct": 0.0,
            "avg_daily_gain_pct": 0.0,
            "avg_monthly_gain_pct": 0.0,
            "active_days": len(daily),
            "active_months": len(monthly),
        }

    total_gain = total / base * 100
    avg_daily = (sum(v / base * 100 for v in daily.values()) / len(daily)) if daily else 0.0
    avg_monthly = (sum(v / base * 100 for v in monthly.values()) / len(monthly)) if monthly else 0.0
    return {
        "total_gain_pct": round(total_gain, 2),
        "avg_daily_gain_pct": round(avg_daily, 2),
        "avg_monthly_gain_pct": round(avg_monthly, 2),
        "active_days": len(daily),
        "active_months": len(monthly),
    }


def build_group(rows: list[dict], key_name: str, key_value: str, extra: Optional[dict] = None) -> dict:
    s = trade_stats(rows)
    out = {
        key_name: key_value,
        "trades": s["total_trades"],
        "net": s["net_profit"],
        "win_rate": s["win_rate"],
        "profit_factor": s["profit_factor"],
        "max_closed_dd": s["max_closed_dd"],
        "max_closed_dd_pct": s["max_closed_dd_pct"],
        "avg_trade": s["avg_trade"],
    }
    if extra:
        out.update(extra)
    return out



def _stat_state() -> dict:
    return {
        "wins": 0,
        "losses": 0,
        "gross_profit": 0.0,
        "gross_loss_abs": 0.0,
        "net": 0.0,
        "count": 0,
        "running": 0.0,
        "peak": 0.0,
        "maxdd": 0.0,
        "maxdd_pct": 0.0,
    }


def _stat_add(state: dict, net: float) -> None:
    net = float(net or 0.0)
    state["count"] += 1
    state["net"] += net
    if net > 0:
        state["wins"] += 1
        state["gross_profit"] += net
    elif net < 0:
        state["losses"] += 1
        state["gross_loss_abs"] += abs(net)

    state["running"] += net
    if state["running"] > state["peak"]:
        state["peak"] = state["running"]
    dd = state["peak"] - state["running"]
    if dd > state["maxdd"]:
        state["maxdd"] = dd
    if state["peak"] > 0:
        pct = dd / state["peak"] * 100.0
        if pct > state["maxdd_pct"]:
            state["maxdd_pct"] = pct


def _stat_result(state: dict) -> dict:
    count = int(state["count"])
    gp = float(state["gross_profit"])
    gl = float(state["gross_loss_abs"])
    net = float(state["net"])
    return {
        "net_profit": round(net, 2),
        "win_rate": round(state["wins"] / count * 100.0, 2) if count else 0.0,
        "profit_factor": round(gp / gl, 2) if gl else (999 if gp else 0),
        "max_closed_dd": round(float(state["maxdd"]), 2),
        "max_closed_dd_pct": round(float(state["maxdd_pct"]), 2),
        "total_trades": count,
        "avg_trade": round(net / count, 2) if count else 0.0,
        "gross_profit": round(gp, 2),
        "gross_loss": round(-gl, 2),
    }


def _compact_curve(points: list[dict], max_points: int = 1200) -> list[dict]:
    """Adaptive downsampling: keep the chart compact even for very long histories."""
    if len(points) <= max_points * 2:
        return points
    last = points[-1]
    compacted = points[::2]
    if compacted[-1] is not last:
        compacted.append(last)
    return compacted


def _gain_result(daily: dict[str, float], monthly: dict[str, float], total: float, base_capital: float) -> dict:
    base = abs(float(base_capital or 0))
    if base <= 1e-12:
        return {
            "total_gain_pct": 0.0,
            "avg_daily_gain_pct": 0.0,
            "avg_monthly_gain_pct": 0.0,
            "active_days": len(daily),
            "active_months": len(monthly),
        }
    return {
        "total_gain_pct": round(total / base * 100.0, 2),
        "avg_daily_gain_pct": round(sum(v / base * 100.0 for v in daily.values()) / len(daily), 2) if daily else 0.0,
        "avg_monthly_gain_pct": round(sum(v / base * 100.0 for v in monthly.values()) / len(monthly), 2) if monthly else 0.0,
        "active_days": len(daily),
        "active_months": len(monthly),
    }


def _period_trade_aggregate(params: dict, base_capital: float) -> dict:
    """Aggregate period trades while streaming rows from Supabase.

    Only five narrow columns are fetched. Raw rows are discarded immediately,
    so memory use stays nearly flat even if the account history becomes large.
    """
    q = dict(params)
    q["select"] = "close_time,net_profit,unique_id,strategy,magic"
    q["order"] = "close_time.asc"

    overall = _stat_state()
    uid_states: dict[str, dict] = {}
    strategy_states: dict[tuple[str, str, str], dict] = {}
    daily: dict[str, float] = {}
    monthly: dict[str, float] = {}
    curve: list[dict] = []

    for t in supa_iter("trades", q, chunk=2500, max_rows=1000000):
        net = float(t.get("net_profit") or 0)
        _stat_add(overall, net)

        uid = (t.get("unique_id") or "Unassigned").strip() or "Unassigned"
        strategy = t.get("strategy") or f"Magic {t.get('magic') or '0'}"
        magic = str(t.get("magic") or "0")

        us = uid_states.setdefault(uid, _stat_state())
        _stat_add(us, net)
        ss = strategy_states.setdefault((uid, strategy, magic), _stat_state())
        _stat_add(ss, net)

        raw = str(t.get("close_time") or "")
        try:
            dt = parse_ts(raw)
            day_key = dt.strftime("%Y-%m-%d")
            month_key = dt.strftime("%Y-%m")
            daily[day_key] = daily.get(day_key, 0.0) + net
            monthly[month_key] = monthly.get(month_key, 0.0) + net
        except Exception:
            pass

        curve.append({"ts": raw, "value": round(float(overall["running"]), 2)})
        curve = _compact_curve(curve)

    stats = _stat_result(overall)
    uid_groups = []
    for uid, state in uid_states.items():
        s = _stat_result(state)
        uid_groups.append({
            "unique_id": uid,
            "trades": s["total_trades"],
            "net": s["net_profit"],
            "win_rate": s["win_rate"],
            "profit_factor": s["profit_factor"],
            "max_closed_dd": s["max_closed_dd"],
            "max_closed_dd_pct": s["max_closed_dd_pct"],
            "avg_trade": s["avg_trade"],
        })
    uid_groups.sort(key=lambda x: x["net"], reverse=True)

    strategy_groups = []
    for (uid, strategy, magic), state in strategy_states.items():
        s = _stat_result(state)
        strategy_groups.append({
            "strategy": strategy,
            "unique_id": uid,
            "magic": magic,
            "trades": s["total_trades"],
            "net": s["net_profit"],
            "win_rate": s["win_rate"],
            "profit_factor": s["profit_factor"],
            "max_closed_dd": s["max_closed_dd"],
            "max_closed_dd_pct": s["max_closed_dd_pct"],
            "avg_trade": s["avg_trade"],
        })
    strategy_groups.sort(key=lambda x: x["net"], reverse=True)

    return {
        "stats": stats,
        "closed_curve": curve,
        "unique_ids": uid_groups,
        "strategies": strategy_groups,
        "gain_stats": _gain_result(daily, monthly, float(overall["net"]), base_capital),
    }


def _account_meta(account_id: int) -> dict:
    """Compact all-time metadata calculated inside PostgreSQL, cached briefly."""
    cached = _META_CACHE.get(account_id)
    now = time.monotonic()
    if cached and now - cached[0] < _META_CACHE_TTL:
        return cached[1]
    result = supa_rpc("mg_account_meta_v137", {"p_account_id": account_id})
    row = (result[0] if result else {}) if isinstance(result, list) else (result or {})
    _META_CACHE[account_id] = (now, row)
    return row


def _equity_dd(account_id: int, start: Optional[datetime], end: Optional[datetime]) -> tuple[float, float]:
    """Exact peak-to-trough equity DD calculated inside PostgreSQL, not in Render RAM."""
    result = supa_rpc(
        "mg_equity_dd_v137",
        {
            "p_account_id": account_id,
            "p_start": start.isoformat() if start else None,
            "p_end": end.isoformat() if end else None,
        },
    )
    row = result[0] if isinstance(result, list) and result else (result or {})
    return float(row.get("max_equity_dd") or 0), float(row.get("max_equity_dd_pct") or 0)


def _custom_bounds(from_date: str = "", to_date: str = "") -> tuple[Optional[datetime], Optional[datetime]]:
    start = end = None
    if from_date:
        try:
            start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(422, "from_date must be YYYY-MM-DD")
    if to_date:
        try:
            end = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        except ValueError:
            raise HTTPException(422, "to_date must be YYYY-MM-DD")
    if start and end and start >= end:
        raise HTTPException(422, "from_date must be on or before to_date")
    return start, end


def _apply_period(params: dict, field: str, days: int = 0, from_date: str = "", to_date: str = "") -> tuple[Optional[datetime], Optional[datetime]]:
    """Apply an inclusive broker-date range. Custom dates override rolling days."""
    start, end = _custom_bounds(from_date, to_date)
    clauses = []
    if start or end:
        if start:
            clauses.append(f"{field}.gte.{start.isoformat()}")
        if end:
            clauses.append(f"{field}.lt.{end.isoformat()}")
    elif days:
        start = datetime.now(timezone.utc) - timedelta(days=days)
        clauses.append(f"{field}.gte.{start.isoformat()}")
    if clauses:
        if len(clauses) == 1:
            op_value = clauses[0].split(f"{field}.", 1)[1]
            params[field] = op_value
        else:
            params["and"] = "(" + ",".join(clauses) + ")"
    return start, end


@app.get("/api/v1/public/{slug}/summary")
def public_summary(
    slug: str,
    days: int = Query(0, ge=0, le=3650),
    from_date: str = "",
    to_date: str = "",
    unique_id: str = "",
    magic: str = "",
    symbol: str = "",
):
    acc = get_acc_by_slug(slug)

    latest_rows = supa_get(
        "snapshots",
        {
            "account_id": f"eq.{acc['id']}",
            "order": "ts.desc",
            "select": "ts,balance,equity,floating_profit,net_deposit,deposit_total,withdrawal_total",
        },
        limit=1,
    )
    latest = latest_rows[0] if latest_rows else None

    meta = _account_meta(int(acc["id"]))
    all_time_net = round(float(meta.get("all_time_net") or 0), 2)

    latest_balance = float(latest.get("balance") or 0) if latest else 0.0
    synced_deposit_total = latest.get("deposit_total") if latest else None
    synced_withdrawal_total = latest.get("withdrawal_total") if latest else None
    synced_net_deposit = latest.get("net_deposit") if latest else None
    if synced_deposit_total is not None:
        deposit_total = round(float(synced_deposit_total), 2)
        withdrawal_total = round(float(synced_withdrawal_total or 0), 2)
        deposit_source = "mt5_balance_transactions_split"
    elif synced_net_deposit is not None:
        deposit_total = round(float(synced_net_deposit), 2)
        withdrawal_total = 0.0
        deposit_source = "legacy_v1_36_net_deposit"
    else:
        deposit_total = round(latest_balance - all_time_net, 2) if latest else 0.0
        withdrawal_total = 0.0
        deposit_source = "legacy_estimate"
        if abs(deposit_total) < 1e-9 and latest_balance:
            deposit_total = round(latest_balance, 2)

    params = {"account_id": f"eq.{acc['id']}"}
    period_start, period_end = _apply_period(params, "close_time", days, from_date, to_date)
    if unique_id:
        params["unique_id"] = f"eq.{unique_id}"
    if magic:
        params["magic"] = f"eq.{magic}"
    if symbol:
        params["symbol"] = f"eq.{symbol}"

    agg = _period_trade_aggregate(params, deposit_total)
    stats = agg["stats"]
    max_eq_dd, max_eq_dd_pct = _equity_dd(int(acc["id"]), period_start, period_end)

    login = acc.get("login") or ""
    return {
        "site_title": SITE_TITLE,
        "account": {
            "name": acc["name"],
            "broker": acc.get("broker"),
            "server": acc.get("server"),
            "login_masked": ("***" + login[-3:]) if login else None,
            "currency": acc.get("currency", "USD"),
            "account_type": acc.get("account_type", "unknown"),
            "public_slug": acc["public_slug"],
        },
        "current": {
            "balance": latest.get("balance") if latest else None,
            "equity": latest.get("equity") if latest else None,
            "floating_profit": latest.get("floating_profit") if latest else None,
            "ts": latest.get("ts") if latest else None,
        },
        "equity_curve": {
            "deposit": deposit_total,
            "withdrawal": withdrawal_total,
            "deposit_source": deposit_source,
            "current_equity": latest.get("equity") if latest else None,
            "period_net": stats["net_profit"],
            "all_time_net": all_time_net,
        },
        "stats": stats
        | {"max_equity_dd": round(max_eq_dd, 2), "max_equity_dd_pct": round(max_eq_dd_pct, 2)},
        "gain_stats": agg["gain_stats"],
        "closed_curve": agg["closed_curve"],
        "unique_ids": agg["unique_ids"],
        "strategies": agg["strategies"],
        "time_basis": "broker_server",
        "optimization": {
            "version": "1.37",
            "raw_snapshots_loaded_by_render": 0,
            "closed_curve_max_points": 2400,
        },
        "filter_state": {
            "days": days,
            "from_date": from_date,
            "to_date": to_date,
            "unique_id": unique_id,
            "magic": magic,
            "symbol": symbol,
        },
        "filters": {
            "unique_ids": meta.get("unique_ids") or [],
            "magics": meta.get("magics") or [],
            "symbols": meta.get("symbols") or [],
        },
    }


def _month_shift(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def growth_calendar_data(trades: list[dict], selected_month: str) -> dict:
    """Aggregate closed-trade net P/L, trade count and total lot size for the growth calendar."""
    try:
        selected = datetime.strptime(selected_month, "%Y-%m")
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM")

    year, month = selected.year, selected.month
    daily_map: dict[int, dict] = {}
    monthly_map: dict[int, dict] = {m: {"net": 0.0, "trades": 0, "lots": 0.0} for m in range(1, 13)}

    for t in trades:
        raw = str(t.get("close_time") or "")
        try:
            dt = parse_ts(raw)
        except Exception:
            continue
        net = float(t.get("net_profit") or 0)
        lots = float(t.get("volume") or 0)
        if dt.year == year:
            monthly_map[dt.month]["net"] += net
            monthly_map[dt.month]["trades"] += 1
            monthly_map[dt.month]["lots"] += lots
        if dt.year == year and dt.month == month:
            bucket = daily_map.setdefault(dt.day, {"net": 0.0, "trades": 0, "lots": 0.0})
            bucket["net"] += net
            bucket["trades"] += 1
            bucket["lots"] += lots

    daily = [
        {
            "day": day,
            "date": f"{year:04d}-{month:02d}-{day:02d}",
            "net": round(v["net"], 2),
            "trades": v["trades"],
            "lots": round(v["lots"], 2),
        }
        for day, v in sorted(daily_map.items())
    ]
    monthly = [
        {
            "month": f"{year:04d}-{m:02d}",
            "net": round(monthly_map[m]["net"], 2),
            "trades": monthly_map[m]["trades"],
            "lots": round(monthly_map[m]["lots"], 2),
        }
        for m in range(1, 13)
    ]
    return {
        "selected_month": f"{year:04d}-{month:02d}",
        "month_net": round(sum(x["net"] for x in daily), 2),
        "month_trades": sum(x["trades"] for x in daily),
        "month_lots": round(sum(x["lots"] for x in daily), 2),
        "year_net": round(sum(x["net"] for x in monthly), 2),
        "year_trades": sum(x["trades"] for x in monthly),
        "year_lots": round(sum(x["lots"] for x in monthly), 2),
        "daily": daily,
        "monthly": monthly,
    }


@app.get("/api/v1/public/{slug}/growth-calendar")
def public_growth_calendar(
    slug: str,
    month: str = "",
    from_date: str = "",
    to_date: str = "",
    unique_id: str = "",
    magic: str = "",
    symbol: str = "",
):
    acc = get_acc_by_slug(slug)
    if not month:
        now = datetime.now(timezone.utc)
        month = f"{now.year:04d}-{now.month:02d}"
    try:
        selected = datetime.strptime(month, "%Y-%m")
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM")

    # Fetch the selected calendar year. The Unique ID / Magic / Symbol filters
    # are intentionally shared with the rest of the public dashboard.
    start = datetime(selected.year, 1, 1, tzinfo=timezone.utc)
    end = datetime(selected.year + 1, 1, 1, tzinfo=timezone.utc)
    range_start, range_end = _custom_bounds(from_date, to_date)
    clauses = [f"close_time.gte.{start.isoformat()}", f"close_time.lt.{end.isoformat()}"]
    if range_start:
        clauses.append(f"close_time.gte.{range_start.isoformat()}")
    if range_end:
        clauses.append(f"close_time.lt.{range_end.isoformat()}")
    params = {
        "account_id": f"eq.{acc['id']}",
        "and": "(" + ",".join(clauses) + ")",
        "order": "close_time.asc",
        "select": "close_time,net_profit,volume",
    }
    if unique_id:
        params["unique_id"] = f"eq.{unique_id}"
    if magic:
        params["magic"] = f"eq.{magic}"
    if symbol:
        params["symbol"] = f"eq.{symbol}"
    params["select"] = "close_time,net_profit,volume"
    data = growth_calendar_data(supa_iter("trades", params, chunk=2500, max_rows=1000000), month)

    # Month-strip navigation: six months before and after the selection.
    strip = []
    for delta in range(-6, 7):
        y, m = _month_shift(selected.year, selected.month, delta)
        strip.append(f"{y:04d}-{m:02d}")
    data["month_strip"] = strip
    return data


@app.get("/api/v1/public/{slug}/trades")
def public_trades(
    slug: str,
    limit: int = Query(200, ge=1, le=2000),
    days: int = Query(0, ge=0, le=3650),
    from_date: str = "",
    to_date: str = "",
    unique_id: str = "",
    magic: str = "",
    symbol: str = "",
):
    acc = get_acc_by_slug(slug)
    params = {"account_id": f"eq.{acc['id']}", "order": "close_time.desc"}
    _apply_period(params, "close_time", days, from_date, to_date)
    if unique_id:
        params["unique_id"] = f"eq.{unique_id}"
    if magic:
        params["magic"] = f"eq.{magic}"
    if symbol:
        params["symbol"] = f"eq.{symbol}"
    rows = supa_get("trades", params, limit=limit)
    return [
        {
            "deal_ticket": t["deal_ticket"],
            "unique_id": t.get("unique_id") or "Unassigned",
            "magic": t.get("magic") or "0",
            "strategy": t.get("strategy") or "Unlabelled",
            "symbol": t["symbol"],
            "side": t["side"],
            "volume": t.get("volume") or 0,
            "open_price": t.get("open_price") or 0,
            "close_price": t.get("close_price") or 0,
            "open_time": t["open_time"],
            "close_time": t["close_time"],
            "profit": t.get("profit") or 0,
            "commission": t.get("commission") or 0,
            "swap": t.get("swap") or 0,
            "fee": t.get("fee") or 0,
            "net_profit": t.get("net_profit") or 0,
            "comment": t.get("comment") or "",
        }
        for t in rows
    ]
