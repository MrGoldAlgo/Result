import hashlib
import os
import secrets
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

app = FastAPI(title=SITE_TITLE, version="1.35-free")
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


def supa_all(table: str, params: Optional[dict] = None, chunk: int = 1000, max_rows: int = 100000) -> list[dict]:
    """Fetch all PostgREST rows in pages (Supabase commonly caps one response)."""
    out: list[dict] = []
    start = 0
    while start < max_rows:
        end = min(start + chunk - 1, max_rows - 1)
        with httpx.Client(timeout=30.0) as c:
            r = c.get(
                f"{REST}/{table}",
                headers=_headers({"Range": f"{start}-{end}"}),
                params={"select": "*", **(params or {})},
            )
        _check(r)
        rows = r.json()
        out.extend(rows)
        if len(rows) < chunk:
            break
        start += chunk
    return out


def supa_insert(table: str, payload, upsert: bool = False) -> list[dict]:
    prefer = "return=representation"
    if upsert:
        prefer += ",resolution=merge-duplicates"
    with httpx.Client(timeout=30.0) as c:
        r = c.post(f"{REST}/{table}", headers=_headers({"Prefer": prefer}), json=payload)
    _check(r)
    return r.json() if r.text else []


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
        "version": "1.35-free",
        "storage": "supabase",
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_SECRET_KEY),
        "unique_id_tracking": True,
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
    supa_insert(
        "snapshots",
        {
            "account_id": acc["id"],
            "ts": iso(payload.ts),
            "balance": payload.balance,
            "equity": payload.equity,
            "margin": payload.margin,
            "free_margin": payload.free_margin,
            "floating_profit": payload.floating_profit,
        },
    )
    return {"ok": True}


@app.post("/api/v1/sync/trades")
def sync_trades(payload: TradeBatch, x_api_key: str = Header(default="")):
    acc = account_from_key(x_api_key)
    if not payload.trades:
        return {"ok": True, "inserted": 0, "updated": 0, "skipped": 0}

    tickets = [str(t.deal_ticket) for t in payload.trades]
    # MT5 deal tickets are numeric, which makes this PostgREST IN filter safe/simple.
    in_value = "in.(" + ",".join(tickets) + ")"
    existing_rows = supa_get(
        "trades",
        {
            "account_id": f"eq.{acc['id']}",
            "deal_ticket": in_value,
            "select": "id,deal_ticket,unique_id,strategy",
        },
    )
    existing = {str(r["deal_ticket"]): r for r in existing_rows}
    inserts = []
    inserted = updated = skipped = 0

    for t in payload.trades:
        d = t.model_dump()
        ticket = str(d["deal_ticket"])
        d["unique_id"] = (d.get("unique_id") or "Unassigned").strip()[:80] or "Unassigned"
        if d["net_profit"] is None:
            d["net_profit"] = d["profit"] + d["commission"] + d["swap"] + d["fee"]
        d["account_id"] = acc["id"]
        d["open_time"] = iso(d["open_time"])
        d["close_time"] = iso(d["close_time"])

        old = existing.get(ticket)
        if old:
            patch = {}
            if (old.get("unique_id") in (None, "", "Unassigned")) and d["unique_id"] != "Unassigned":
                patch["unique_id"] = d["unique_id"]
            if (old.get("strategy") in (None, "", "Unlabelled") or str(old.get("strategy", "")).startswith("Unlabelled")) and d["strategy"]:
                patch["strategy"] = d["strategy"]
            if patch:
                supa_patch("trades", {"id": f"eq.{old['id']}"}, patch)
                updated += 1
            else:
                skipped += 1
            continue
        inserts.append(d)
        inserted += 1

    if inserts:
        supa_insert("trades", inserts)
    return {"ok": True, "inserted": inserted, "updated": updated, "skipped": skipped}


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
    """All-time gain percentages for the current UID/Magic/Symbol scope.

    Daily/monthly averages are arithmetic means across active trading days/months
    (periods containing at least one closed trade), using the same estimated
    funding base as the growth calendar.
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


@app.get("/api/v1/public/{slug}/summary")
def public_summary(
    slug: str,
    days: int = Query(0, ge=0, le=3650),
    unique_id: str = "",
    magic: str = "",
    symbol: str = "",
):
    acc = get_acc_by_slug(slug)
    params = {"account_id": f"eq.{acc['id']}", "order": "close_time.asc"}
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        params["close_time"] = f"gte.{cutoff.isoformat()}"
    if unique_id:
        params["unique_id"] = f"eq.{unique_id}"
    if magic:
        params["magic"] = f"eq.{magic}"
    if symbol:
        params["symbol"] = f"eq.{symbol}"
    trades = supa_all("trades", params)
    stats = trade_stats(trades)

    snap_params = {"account_id": f"eq.{acc['id']}", "order": "ts.asc"}
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        snap_params["ts"] = f"gte.{cutoff.isoformat()}"
    snaps = supa_all("snapshots", snap_params, max_rows=50000)
    latest_rows = supa_get("snapshots", {"account_id": f"eq.{acc['id']}", "order": "ts.desc"}, limit=1)
    latest = latest_rows[0] if latest_rows else None

    eq_peak = max_eq_dd = max_eq_dd_pct = 0.0
    snap_curve = []
    for s in snaps:
        eq = float(s.get("equity") or 0)
        eq_peak = max(eq_peak, eq)
        dd = eq_peak - eq
        max_eq_dd = max(max_eq_dd, dd)
        if eq_peak > 0:
            max_eq_dd_pct = max(max_eq_dd_pct, dd / eq_peak * 100)
        snap_curve.append({"ts": s["ts"], "balance": s["balance"], "equity": s["equity"]})

    uid_buckets: dict[str, list] = {}
    strategy_buckets: dict[tuple, list] = {}
    for t in trades:
        uid = t.get("unique_id") or "Unassigned"
        uid_buckets.setdefault(uid, []).append(t)
        skey = (uid, t.get("strategy") or f"Magic {t.get('magic')}", t.get("magic") or "0")
        strategy_buckets.setdefault(skey, []).append(t)
    uid_groups = [build_group(rows, "unique_id", uid) for uid, rows in uid_buckets.items()]
    uid_groups.sort(key=lambda x: x["net"], reverse=True)
    strategy_groups = [
        build_group(rows, "strategy", strategy, {"unique_id": uid, "magic": mg})
        for (uid, strategy, mg), rows in strategy_buckets.items()
    ]
    strategy_groups.sort(key=lambda x: x["net"], reverse=True)

    # Filter choices from whole account. This is usually small enough for a personal tracker.
    all_trades = supa_all("trades", {"account_id": f"eq.{acc['id']}", "order": "close_time.desc"})
    all_time_net = round(sum(float(t.get("net_profit") or 0) for t in all_trades), 2)
    latest_balance = float(latest.get("balance") or 0) if latest else 0.0
    estimated_deposit = round(latest_balance - all_time_net, 2) if latest else 0.0
    # The sync EA sends trading deals and account snapshots, not broker cash-flow deals.
    # This value is therefore an estimated net funding base: current balance minus all closed-trade P/L.
    if abs(estimated_deposit) < 1e-9 and latest_balance:
        estimated_deposit = round(latest_balance, 2)

    # Gain metrics are all-time for the currently selected UID/Magic/Symbol scope.
    # The 30D/90D/ALL curve selector does not change these "Total/Average" values.
    scope_params = {"account_id": f"eq.{acc['id']}", "order": "close_time.asc"}
    if unique_id:
        scope_params["unique_id"] = f"eq.{unique_id}"
    if magic:
        scope_params["magic"] = f"eq.{magic}"
    if symbol:
        scope_params["symbol"] = f"eq.{symbol}"
    if not unique_id and not magic and not symbol:
        scope_all_trades = all_trades
    else:
        scope_all_trades = supa_all("trades", scope_params)
    gains = gain_metrics(scope_all_trades, estimated_deposit)

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
            "deposit": estimated_deposit,
            "current_equity": latest.get("equity") if latest else None,
            "period_net": stats["net_profit"],
            "all_time_net": all_time_net,
        },
        "stats": {k: v for k, v in stats.items() if k != "closed_curve"}
        | {"max_equity_dd": round(max_eq_dd, 2), "max_equity_dd_pct": round(max_eq_dd_pct, 2)},
        "gain_stats": gains,
        "closed_curve": stats["closed_curve"],
        "snapshot_curve": snap_curve[-2000:],
        "unique_ids": uid_groups,
        "strategies": strategy_groups,
        "time_basis": "broker_server",
        "filter_state": {"days": days, "unique_id": unique_id, "magic": magic, "symbol": symbol},
        "filters": {
            "unique_ids": sorted(set((t.get("unique_id") or "Unassigned") for t in all_trades)),
            "magics": sorted(set(str(t.get("magic") or "0") for t in all_trades)),
            "symbols": sorted(set(t.get("symbol") or "" for t in all_trades if t.get("symbol"))),
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
    params = {
        "account_id": f"eq.{acc['id']}",
        "and": f"(close_time.gte.{start.isoformat()},close_time.lt.{end.isoformat()})",
        "order": "close_time.asc",
        "select": "close_time,net_profit,volume",
    }
    if unique_id:
        params["unique_id"] = f"eq.{unique_id}"
    if magic:
        params["magic"] = f"eq.{magic}"
    if symbol:
        params["symbol"] = f"eq.{symbol}"
    trades = supa_all("trades", params, max_rows=100000)
    data = growth_calendar_data(trades, month)

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
    unique_id: str = "",
    magic: str = "",
    symbol: str = "",
):
    acc = get_acc_by_slug(slug)
    params = {"account_id": f"eq.{acc['id']}", "order": "close_time.desc"}
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        params["close_time"] = f"gte.{cutoff.isoformat()}"
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
