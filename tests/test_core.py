import os
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SECRET_KEY", "sb_secret_example")
os.environ.setdefault("ADMIN_TOKEN", "test")

from main import trade_stats


def test_trade_stats():
    rows = [
        {"net_profit": 100, "close_time": "2026-01-01T00:00:00+00:00"},
        {"net_profit": -40, "close_time": "2026-01-02T00:00:00+00:00"},
        {"net_profit": 60, "close_time": "2026-01-03T00:00:00+00:00"},
    ]
    s = trade_stats(rows)
    assert s["net_profit"] == 120
    assert s["total_trades"] == 3
    assert s["win_rate"] == 66.67
    assert s["profit_factor"] == 4.0
    assert s["max_closed_dd"] == 40
