#!/usr/bin/env python3
"""
🐋 Hyperliquid 鲸鱼雷达 (standalone)
监控收筹池内的币在 Hyperliquid 上的大单异动
运行: python3 hyperliquid_radar.py
"""

import os
import time
import sqlite3
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# === 加载 .env ===
env_file = Path(__file__).parent / ".env.oi"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID", "")
HL_API       = "https://api.hyperliquid.xyz/info"
DB_PATH      = Path(__file__).parent / "accumulation.db"

MIN_NOTIONAL = 200_000  # 单笔 > $200K 才算大单
LOOKBACK_MIN = 65       # 回看65分钟（比1h多5分钟，防漏）


# ── Hyperliquid API ────────────────────────────────────────────────────────────

def hl_post(payload: dict):
    r = requests.post(HL_API, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def get_hl_meta() -> tuple[list, list]:
    """返回 (universe_list, asset_ctx_list)"""
    data = hl_post({"type": "metaAndAssetCtxs"})
    return data[0]["universe"], data[1]


def get_recent_trades(coin: str) -> list:
    try:
        return hl_post({"type": "recentTrades", "coin": coin})
    except Exception:
        return []


# ── SQLite ─────────────────────────────────────────────────────────────────────

def init_hl_table(conn: sqlite3.Connection):
    conn.execute("""CREATE TABLE IF NOT EXISTS hl_seen_trades (
        trade_id  TEXT PRIMARY KEY,
        coin      TEXT,
        notional  REAL,
        alerted_at TEXT
    )""")
    conn.commit()


def is_seen(conn: sqlite3.Connection, trade_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM hl_seen_trades WHERE trade_id = ?", (trade_id,)
    ).fetchone() is not None


def mark_seen(conn: sqlite3.Connection, trade_id: str, coin: str, notional: float):
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    conn.execute(
        "INSERT OR IGNORE INTO hl_seen_trades VALUES (?, ?, ?, ?)",
        (trade_id, coin, notional, now)
    )
    conn.commit()


def load_pool_coins(conn: sqlite3.Connection) -> set:
    try:
        rows = conn.execute(
            "SELECT coin FROM watchlist WHERE status != 'removed'"
        ).fetchall()
        return {row[0] for row in rows}
    except sqlite3.OperationalError:
        return set()


# ── Telegram ───────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    if not TG_BOT_TOKEN:
        print("\n[TG]\n" + text)
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown",
        }, timeout=10)
        if r.status_code != 200:
            requests.post(url, json={
                "chat_id": TG_CHAT_ID,
                "text": text.replace("*", "").replace("_", ""),
            }, timeout=10)
    except Exception as e:
        print(f"[TG] Error: {e}")


def fmt_usd(v: float) -> str:
    if v >= 1e6: return f"${v/1e6:.2f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


# ── Main scan ──────────────────────────────────────────────────────────────────

def scan_hyperliquid(conn: sqlite3.Connection | None = None) -> list:
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH))
        close_conn = True

    init_hl_table(conn)
    pool_coins = load_pool_coins(conn)

    if not pool_coins:
        print("⚠️  收筹池为空，先运行: python3 accumulation_radar.py pool")
        if close_conn: conn.close()
        return []

    print(f"🐋 扫描 Hyperliquid 大单（收筹池 {len(pool_coins)} 个币）...")

    try:
        universe, asset_ctxs = get_hl_meta()
    except Exception as e:
        print(f"❌ HL API 失败: {e}")
        if close_conn: conn.close()
        return []

    hl_idx = {m["name"]: i for i, m in enumerate(universe)}

    # 收筹池 ∩ Hyperliquid
    watchable = [c for c in pool_coins if c in hl_idx]
    print(f"  收筹池在 HL 上有 {len(watchable)} 个币")

    cutoff_ms = int((time.time() - LOOKBACK_MIN * 60) * 1000)
    alerts = []

    for coin in watchable:
        ctx = asset_ctxs[hl_idx[coin]]
        day_vol = float(ctx.get("dayNtlVlm", 0))
        if day_vol < 500_000:   # 日成交 < $500K，基本没流动性
            continue

        for t in get_recent_trades(coin):
            if int(t["time"]) < cutoff_ms:
                continue

            notional = float(t["px"]) * float(t["sz"])
            if notional < MIN_NOTIONAL:
                continue

            trade_id = str(t.get("tid") or t.get("hash", ""))
            if not trade_id or is_seen(conn, trade_id):
                continue

            mark_seen(conn, trade_id, coin, notional)

            oi_usd      = float(ctx.get("openInterest", 0)) * float(ctx.get("markPx", 0))
            funding_pct = float(ctx.get("funding", 0)) * 100
            side_label  = "🟢 买多" if t["side"] == "B" else "🔴 卖空"
            trade_time  = datetime.fromtimestamp(
                int(t["time"]) / 1000, tz=timezone(timedelta(hours=8))
            ).strftime("%H:%M:%S")

            alerts.append({
                "coin": coin, "side": side_label,
                "px": float(t["px"]), "sz": float(t["sz"]),
                "notional": notional, "trade_time": trade_time,
                "oi_usd": oi_usd, "day_vol": day_vol,
                "funding_pct": funding_pct,
            })

        time.sleep(0.15)

    if alerts:
        alerts.sort(key=lambda x: x["notional"], reverse=True)
        now = datetime.now(timezone(timedelta(hours=8)))
        lines = [
            f"🐋 **Hyperliquid 鲸鱼入场**",
            f"⏰ {now.strftime('%Y-%m-%d %H:%M')} CST",
            f"━━━━━━━━━━━━━━━━━━",
            f"收筹池内检测到 {len(alerts)} 笔大单（>{fmt_usd(MIN_NOTIONAL)}）",
            "",
        ]
        for a in alerts:
            lines += [
                f"{a['side']} **{a['coin']}** | {fmt_usd(a['notional'])} | {a['trade_time']}",
                f"  价格 ${a['px']:.6g} × {a['sz']:.2f} 张",
                f"  OI {fmt_usd(a['oi_usd'])} | 费率 {a['funding_pct']:.4f}%",
                f"  ⚠️ 收筹池 + HL 大单 = 最强信号！",
                "",
            ]
        send_telegram("\n".join(lines))
        print(f"  ✅ 发现 {len(alerts)} 笔，已推送")
    else:
        print("  ✅ 无新大单")

    if close_conn: conn.close()
    return alerts


if __name__ == "__main__":
    scan_hyperliquid()
