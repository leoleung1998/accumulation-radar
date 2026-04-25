#!/usr/bin/env python3
"""
庄家收筹雷达 — 回测框架
======================
测试收筹标的池策略（pool scan）的历史表现。

用法:
    python3 backtest.py                    # 默认：过去12个月，月度扫描
    python3 backtest.py --months 6         # 回测6个月
    python3 backtest.py --top 30           # 每次取评分前30名
    python3 backtest.py --fetch-only       # 只下载日线数据不跑回测
    python3 backtest.py --no-fetch         # 跳过下载，用已有缓存
    python3 backtest.py --short            # 额外运行24h/48h短线回测（需要1h klines）
    python3 backtest.py --short-only       # 只运行24h/48h短线回测
    python3 backtest.py --report           # 只打印报告

核心逻辑:
    1. 下载并缓存所有 USDT 永续合约的日线 klines（≈365天历史）
    2. 模拟在不同历史日期运行 pool scan（月度频率）
    3. 每个信号以次日收盘价入场，追踪 7/14/30/60 天收益
    4. --short 模式额外拉取信号币的1h klines，追踪 24h/48h/72h 收益
    5. 同时模拟 TP/SL 止盈止损组合
    6. 输出胜率、均值、最大回撤、夏普等指标
"""

import json
import os
import sys
import time
import sqlite3
import argparse
import statistics
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

import requests

# ── 配置 ──────────────────────────────────────────────────────────────────────

FAPI           = "https://fapi.binance.com"
CACHE_DIR      = Path(__file__).parent / "backtest_cache"
CACHE_DIR_1H   = Path(__file__).parent / "backtest_cache_1h"
DB_PATH        = Path(__file__).parent / "backtest.db"

# 策略参数（与 accumulation_radar.py 保持一致）
MIN_SIDEWAYS_DAYS = 45
MAX_RANGE_PCT     = 80
MAX_AVG_VOL_USD   = 20_000_000
MIN_DATA_DAYS     = 50
VOL_BREAKOUT_MULT = 3.0

# 回测参数
HOLD_PERIODS       = [7, 14, 30, 60]   # 持仓天数（日线）
SHORT_HOLD_HOURS   = [24, 48, 72]      # 短线持仓小时数（1h bars）

# TP/SL 组合（TP%, SL%）— 正数表示盈利百分比，负数表示亏损
TP_SL_COMBOS = [
    (20, -10),
    (30, -15),
    (50, -20),
    (100, -25),
    (None, -10),  # 无TP纯SL
]

# ── Binance API ───────────────────────────────────────────────────────────────

def api_get(endpoint, params=None, retries=3):
    url = f"{FAPI}{endpoint}"
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                print("  [429] rate limit, sleeping 5s...")
                time.sleep(5)
            else:
                return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
    return None


def get_all_perp_symbols():
    info = api_get("/fapi/v1/exchangeInfo")
    if not info:
        return []
    EXCLUDE = {"USDC", "USDP", "TUSD", "FDUSD", "BTCDOM", "DEFI", "USDM"}
    symbols = []
    for s in info["symbols"]:
        if (s["quoteAsset"] == "USDT"
                and s["contractType"] == "PERPETUAL"
                and s["status"] == "TRADING"):
            coin = s["baseAsset"].upper()
            if coin not in EXCLUDE:
                symbols.append(s["symbol"])
    return symbols


# ── 数据缓存层 ─────────────────────────────────────────────────────────────────
#
#  每个 symbol 缓存一个 JSON 文件: backtest_cache/BTCUSDT.json
#  格式: list of {ts, open, high, low, close, vol} sorted by ts

def cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol}.json"


def load_cached(symbol: str) -> list[dict]:
    p = cache_path(symbol)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return []
    return []


def save_cached(symbol: str, bars: list[dict]):
    CACHE_DIR.mkdir(exist_ok=True)
    cache_path(symbol).write_text(json.dumps(bars))


def fetch_klines(symbol: str, limit: int = 365) -> list[dict]:
    """拉日线 klines，返回 list of OHLCV dicts，按时间升序"""
    raw = api_get("/fapi/v1/klines", {"symbol": symbol, "interval": "1d", "limit": limit})
    if not raw:
        return []
    bars = []
    for k in raw:
        bars.append({
            "ts":    int(k[0]),         # open time ms
            "open":  float(k[1]),
            "high":  float(k[2]),
            "low":   float(k[3]),
            "close": float(k[4]),
            "vol":   float(k[7]),       # quote volume (USDT)
        })
    return bars


def fetch_and_cache_all(symbols: list[str], refresh=False):
    """下载并缓存所有 symbol 的日线数据"""
    CACHE_DIR.mkdir(exist_ok=True)
    total = len(symbols)
    print(f"\n📥 下载 {total} 个合约的日线数据（可能需要几分钟）...")

    updated = 0
    for i, sym in enumerate(symbols, 1):
        if not refresh and cache_path(sym).exists():
            continue  # 已有缓存，跳过

        bars = fetch_klines(sym, limit=365)
        if bars:
            save_cached(sym, bars)
            updated += 1

        if i % 50 == 0:
            print(f"  进度: {i}/{total}，已更新 {updated} 个")
            time.sleep(0.5)
        else:
            time.sleep(0.05)  # polite rate limiting

    print(f"  ✅ 完成，更新了 {updated} 个 symbol 的数据")


# ── 1h klines 缓存层 ──────────────────────────────────────────────────────────
#
#  用于 24h / 48h / 72h 短线回测。
#  每个 symbol 缓存: backtest_cache_1h/BTCUSDT.json
#  Binance 每次最多返回 1500 根，12个月需要分页拉取

def cache_path_1h(symbol: str) -> Path:
    return CACHE_DIR_1H / f"{symbol}.json"


def load_cached_1h(symbol: str) -> list[dict]:
    p = cache_path_1h(symbol)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return []
    return []


def save_cached_1h(symbol: str, bars: list[dict]):
    CACHE_DIR_1H.mkdir(exist_ok=True)
    cache_path_1h(symbol).write_text(json.dumps(bars))


def fetch_klines_1h_paginated(symbol: str, days: int = 365) -> list[dict]:
    """
    拉取最近 days 天的 1h klines（分页，Binance 每次限 1500 条）。
    返回按时间升序排列的 bars。
    """
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    all_bars: list[dict] = []
    cur_start = start_ms

    while cur_start < end_ms:
        raw = api_get("/fapi/v1/klines", {
            "symbol":    symbol,
            "interval":  "1h",
            "startTime": cur_start,
            "limit":     1500,
        })
        if not raw:
            break
        for k in raw:
            all_bars.append({
                "ts":    int(k[0]),
                "open":  float(k[1]),
                "high":  float(k[2]),
                "low":   float(k[3]),
                "close": float(k[4]),
                "vol":   float(k[7]),
            })
        if len(raw) < 1500:
            break
        cur_start = int(raw[-1][0]) + 3600_000  # next hour
        time.sleep(0.05)

    # deduplicate & sort
    seen = {}
    for b in all_bars:
        seen[b["ts"]] = b
    return sorted(seen.values(), key=lambda x: x["ts"])


def fetch_and_cache_1h(symbols: list[str], refresh=False):
    """下载并缓存指定 symbols 的 1h 数据（仅用于信号币）"""
    CACHE_DIR_1H.mkdir(exist_ok=True)
    total = len(symbols)
    print(f"\n📥 下载 {total} 个合约的1h数据（约需 {total//3+1} 分钟）...")

    updated = 0
    for i, sym in enumerate(symbols, 1):
        if not refresh and cache_path_1h(sym).exists():
            continue
        bars = fetch_klines_1h_paginated(sym, days=365)
        if bars:
            save_cached_1h(sym, bars)
            updated += 1
        if i % 20 == 0:
            print(f"  进度: {i}/{total}，已更新 {updated} 个")
        time.sleep(0.1)

    print(f"  ✅ 1h数据完成，更新了 {updated} 个 symbol")


# ── 策略信号层 ─────────────────────────────────────────────────────────────────
#
#  在某个历史时间点，用截至该时间点的数据运行 analyze_accumulation
#  返回当时能看到的信号列表

def bars_up_to(bars: list[dict], scan_ts_ms: int) -> list[dict]:
    """只保留 scan_ts_ms 之前（含当天）的 bars"""
    return [b for b in bars if b["ts"] <= scan_ts_ms]


def analyze_accumulation_hist(symbol: str, bars: list[dict]) -> dict | None:
    """
    与 accumulation_radar.py 中的 analyze_accumulation 逻辑相同，
    但操作纯历史 bars，不调用 API。
    """
    if len(bars) < MIN_DATA_DAYS:
        return None

    coin = symbol.replace("USDT", "")
    EXCLUDE = {"USDC", "USDP", "TUSD", "FDUSD", "BTCDOM", "DEFI", "USDM"}
    if coin in EXCLUDE:
        return None

    recent_7d = bars[-7:]
    prior     = bars[:-7]
    if not prior:
        return None

    recent_avg_px = sum(d["close"] for d in recent_7d) / len(recent_7d)
    prior_avg_px  = sum(d["close"] for d in prior)    / len(prior)
    if prior_avg_px > 0 and ((recent_avg_px - prior_avg_px) / prior_avg_px) > 3.0:
        return None  # 已涨300%+

    best_sideways = best_range = best_low = best_high = best_avg_vol = best_slope_pct = 0

    for window in range(MIN_SIDEWAYS_DAYS, len(prior) + 1):
        wd = prior[-window:]
        w_low  = min(d["low"]  for d in wd)
        w_high = max(d["high"] for d in wd)
        if w_low <= 0:
            continue
        range_pct = (w_high - w_low) / w_low * 100
        if range_pct > MAX_RANGE_PCT:
            continue
        avg_vol = sum(d["vol"] for d in wd) / len(wd)
        if avg_vol > MAX_AVG_VOL_USD:
            continue

        closes  = [d["close"] for d in wd]
        n       = len(closes)
        x_mean  = (n - 1) / 2.0
        y_mean  = sum(closes) / n
        num     = sum((i - x_mean) * (c - y_mean) for i, c in enumerate(closes))
        den     = sum((i - x_mean) ** 2 for i in range(n))
        slope   = num / den if den > 0 else 0
        slope_pct = (slope * n / closes[0] * 100) if closes[0] > 0 else 0
        if abs(slope_pct) > 20:
            continue

        if window > best_sideways:
            best_sideways  = window
            best_range     = range_pct
            best_low       = w_low
            best_high      = w_high
            best_avg_vol   = avg_vol
            best_slope_pct = slope_pct

    if best_sideways < MIN_SIDEWAYS_DAYS:
        return None

    days_score    = min(best_sideways / 90, 1.0) * 25
    range_score   = max(0, (1 - best_range / MAX_RANGE_PCT)) * 20
    vol_score     = max(0, (1 - best_avg_vol / MAX_AVG_VOL_USD)) * 20
    recent_vol    = sum(d["vol"] for d in recent_7d) / len(recent_7d)
    vol_breakout  = recent_vol / best_avg_vol if best_avg_vol > 0 else 0
    breakout_score = min(vol_breakout / VOL_BREAKOUT_MULT, 1.0) * 15
    est_mcap      = bars[-1]["close"] * best_avg_vol * 30
    if   est_mcap < 50e6:  mcap_score = 20
    elif est_mcap < 100e6: mcap_score = 15
    elif est_mcap < 200e6: mcap_score = 10
    elif est_mcap < 500e6: mcap_score = 5
    else:                  mcap_score = 0

    total_score    = days_score + range_score + vol_score + breakout_score + mcap_score
    flatness_bonus = max(0, (1 - abs(best_slope_pct) / 20)) * 5
    total_score   += flatness_bonus

    if   vol_breakout >= VOL_BREAKOUT_MULT: status = "放量启动"
    elif vol_breakout >= 1.5:               status = "开始放量"
    else:                                   status = "收筹中"

    return {
        "symbol":       symbol,
        "coin":         coin,
        "sideways_days": best_sideways,
        "range_pct":    best_range,
        "slope_pct":    best_slope_pct,
        "low_price":    best_low,
        "high_price":   best_high,
        "avg_vol":      best_avg_vol,
        "entry_price":  bars[-1]["close"],   # 信号当天收盘价（次日入场参考）
        "recent_vol":   recent_vol,
        "vol_breakout": vol_breakout,
        "score":        total_score,
        "status":       status,
        "scan_ts":      bars[-1]["ts"],      # 信号产生时间
    }


def run_pool_scan_on_date(symbols: list[str], scan_ts_ms: int, top_n: int = 50) -> list[dict]:
    """在指定历史时间点模拟运行一次 pool scan，返回前 top_n 个信号"""
    results = []
    for sym in symbols:
        bars_all  = load_cached(sym)
        bars_hist = bars_up_to(bars_all, scan_ts_ms)
        if len(bars_hist) < MIN_DATA_DAYS:
            continue
        r = analyze_accumulation_hist(sym, bars_hist)
        if r:
            results.append(r)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_n]


# ── 持仓追踪层 ─────────────────────────────────────────────────────────────────

def get_forward_bars(symbol: str, entry_ts_ms: int, n_days: int) -> list[dict]:
    """
    从入场日（entry_ts_ms 对应 bar 的次日）往后取 n_days 根日线。
    entry_ts_ms 是信号当天开盘时间戳。
    """
    bars = load_cached(symbol)
    # 找信号日的 index
    idx = next((i for i, b in enumerate(bars) if b["ts"] >= entry_ts_ms), None)
    if idx is None:
        return []
    # 入场 = 信号日次日（idx+1），往后 n_days
    start = idx + 1
    return bars[start: start + n_days]


def calc_fixed_hold(fwd_bars: list[dict], hold_days: int) -> float | None:
    """计算固定持仓 hold_days 天后的收益率（以 entry 当日收盘价为基准）"""
    if not fwd_bars:
        return None
    entry_price = fwd_bars[0]["open"]  # 次日开盘入场
    if len(fwd_bars) < hold_days:
        return None  # 数据不足
    exit_price = fwd_bars[hold_days - 1]["close"]
    if entry_price <= 0:
        return None
    return (exit_price - entry_price) / entry_price * 100


def calc_tp_sl(fwd_bars: list[dict], tp_pct: float | None, sl_pct: float) -> dict:
    """
    模拟 TP/SL 出场逻辑（基于日内高低点触发）。
    tp_pct: 止盈百分比（正数，如 30 表示+30%），None = 不设止盈
    sl_pct: 止损百分比（负数，如 -15 表示-15%）
    返回: {hit: 'tp'|'sl'|'timeout', ret_pct, days_held}
    """
    if not fwd_bars:
        return {"hit": "no_data", "ret_pct": None, "days_held": 0}

    entry_price = fwd_bars[0]["open"]
    if entry_price <= 0:
        return {"hit": "no_data", "ret_pct": None, "days_held": 0}

    tp_price = entry_price * (1 + tp_pct / 100) if tp_pct else None
    sl_price = entry_price * (1 + sl_pct / 100)

    for i, bar in enumerate(fwd_bars):
        # 假设日内先触高后触低（最乐观假设用高，最保守用低）
        # 保守模拟：同一天内先检查 SL 再检查 TP
        if bar["low"] <= sl_price:
            return {"hit": "sl", "ret_pct": sl_pct, "days_held": i + 1}
        if tp_price and bar["high"] >= tp_price:
            return {"hit": "tp", "ret_pct": tp_pct, "days_held": i + 1}

    # 到期未触发：以最后一天收盘价结算
    final = fwd_bars[-1]["close"]
    ret   = (final - entry_price) / entry_price * 100
    return {"hit": "timeout", "ret_pct": ret, "days_held": len(fwd_bars)}


def calc_max_drawdown(fwd_bars: list[dict]) -> float:
    """从入场到 fwd_bars 结束的最大回撤（以次日开盘为基准）"""
    if not fwd_bars:
        return 0.0
    entry = fwd_bars[0]["open"]
    if entry <= 0:
        return 0.0
    peak   = entry
    max_dd = 0.0
    for bar in fwd_bars:
        if bar["high"] > peak:
            peak = bar["high"]
        dd = (peak - bar["low"]) / peak * 100
        if dd > max_dd:
            max_dd = dd
    return max_dd


def calc_max_upside(fwd_bars: list[dict], n_days: int) -> float:
    """持仓期内的最大上涨幅度（最高点 vs 入场价）"""
    if not fwd_bars:
        return 0.0
    entry = fwd_bars[0]["open"]
    if entry <= 0:
        return 0.0
    bars  = fwd_bars[:n_days]
    peak  = max(b["high"] for b in bars)
    return (peak - entry) / entry * 100


# ── 短线 (1h bars) 持仓追踪 ───────────────────────────────────────────────────

def get_forward_bars_1h(symbol: str, entry_ts_ms: int, n_hours: int) -> list[dict]:
    """
    从入场时刻（信号日次日 00:00 UTC）往后取 n_hours 根 1h bars。
    """
    bars = load_cached_1h(symbol)
    if not bars:
        return []
    # 找到 entry_ts_ms 之后的第一根 bar
    idx = next((i for i, b in enumerate(bars) if b["ts"] >= entry_ts_ms), None)
    if idx is None:
        return []
    return bars[idx: idx + n_hours]


def calc_fixed_hold_hours(fwd_bars: list[dict], hold_hours: int) -> float | None:
    """固定持仓 hold_hours 小时后的收益率（以第一根 bar 的开盘价入场）"""
    if not fwd_bars or len(fwd_bars) < hold_hours:
        return None
    entry = fwd_bars[0]["open"]
    if entry <= 0:
        return None
    exit_p = fwd_bars[hold_hours - 1]["close"]
    return (exit_p - entry) / entry * 100


def calc_tp_sl_hours(fwd_bars: list[dict], tp_pct: float | None, sl_pct: float) -> dict:
    """与 calc_tp_sl 相同逻辑，但单位是小时"""
    if not fwd_bars:
        return {"hit": "no_data", "ret_pct": None, "hours_held": 0}
    entry = fwd_bars[0]["open"]
    if entry <= 0:
        return {"hit": "no_data", "ret_pct": None, "hours_held": 0}

    tp_price = entry * (1 + tp_pct / 100) if tp_pct else None
    sl_price = entry * (1 + sl_pct / 100)

    for i, bar in enumerate(fwd_bars):
        if bar["low"] <= sl_price:
            return {"hit": "sl", "ret_pct": sl_pct, "hours_held": i + 1}
        if tp_price and bar["high"] >= tp_price:
            return {"hit": "tp", "ret_pct": tp_pct, "hours_held": i + 1}

    final = fwd_bars[-1]["close"]
    ret   = (final - entry) / entry * 100
    return {"hit": "timeout", "ret_pct": ret, "hours_held": len(fwd_bars)}


def calc_max_upside_hours(fwd_bars: list[dict], n_hours: int) -> float:
    if not fwd_bars:
        return 0.0
    entry = fwd_bars[0]["open"]
    if entry <= 0:
        return 0.0
    peak = max(b["high"] for b in fwd_bars[:n_hours])
    return (peak - entry) / entry * 100


def calc_max_drawdown_hours(fwd_bars: list[dict]) -> float:
    if not fwd_bars:
        return 0.0
    entry = fwd_bars[0]["open"]
    peak  = entry
    max_dd = 0.0
    for bar in fwd_bars:
        if bar["high"] > peak:
            peak = bar["high"]
        dd = (peak - bar["low"]) / peak * 100
        if dd > max_dd:
            max_dd = dd
    return max_dd


# ── 统计层 ────────────────────────────────────────────────────────────────────

def aggregate_stats(returns: list[float]) -> dict:
    """对一批收益率计算统计指标"""
    if not returns:
        return {}
    wins      = [r for r in returns if r > 0]
    losses    = [r for r in returns if r <= 0]
    win_rate  = len(wins) / len(returns) * 100

    avg_ret   = statistics.mean(returns)
    avg_win   = statistics.mean(wins)   if wins   else 0
    avg_loss  = statistics.mean(losses) if losses else 0
    best      = max(returns)
    worst     = min(returns)
    rr        = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

    try:
        stdev = statistics.stdev(returns)
        sharpe = (avg_ret / stdev * (252 ** 0.5)) if stdev > 0 else 0
    except Exception:
        sharpe = 0

    return {
        "n":        len(returns),
        "win_rate": round(win_rate, 1),
        "avg_ret":  round(avg_ret, 2),
        "avg_win":  round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "best":     round(best, 2),
        "worst":    round(worst, 2),
        "rr":       round(rr, 2),
        "sharpe":   round(sharpe, 2),
    }


# ── 结果存储层 ─────────────────────────────────────────────────────────────────

def init_bt_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""CREATE TABLE IF NOT EXISTS signals (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_date   TEXT,
        symbol      TEXT,
        coin        TEXT,
        score       REAL,
        status      TEXT,
        sideways_days INT,
        range_pct   REAL,
        vol_breakout REAL,
        entry_price REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id   INTEGER,
        hold_days   INTEGER,
        ret_pct     REAL,
        max_up_pct  REAL,
        max_dd_pct  REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS tp_sl_trades (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id   INTEGER,
        tp_pct      REAL,
        sl_pct      REAL,
        hit         TEXT,
        ret_pct     REAL,
        days_held   INTEGER
    )""")
    # 短线 1h 回测表
    conn.execute("""CREATE TABLE IF NOT EXISTS short_trades (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id   INTEGER,
        hold_hours  INTEGER,
        ret_pct     REAL,
        max_up_pct  REAL,
        max_dd_pct  REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS short_tp_sl_trades (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id   INTEGER,
        tp_pct      REAL,
        sl_pct      REAL,
        hit         TEXT,
        ret_pct     REAL,
        hours_held  INTEGER
    )""")
    conn.commit()
    return conn


def save_signal(conn: sqlite3.Connection, scan_date: str, sig: dict) -> int:
    cur = conn.execute("""INSERT INTO signals
        (scan_date, symbol, coin, score, status, sideways_days, range_pct, vol_breakout, entry_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (scan_date, sig["symbol"], sig["coin"], sig["score"], sig["status"],
         sig["sideways_days"], sig["range_pct"], sig["vol_breakout"], sig["entry_price"]))
    conn.commit()
    return cur.lastrowid


def save_trade(conn, signal_id, hold_days, ret_pct, max_up, max_dd):
    conn.execute("INSERT INTO trades (signal_id, hold_days, ret_pct, max_up_pct, max_dd_pct) VALUES (?,?,?,?,?)",
                 (signal_id, hold_days, ret_pct, max_up, max_dd))
    conn.commit()


def save_tp_sl(conn, signal_id, tp_pct, sl_pct, hit, ret_pct, days_held):
    conn.execute("INSERT INTO tp_sl_trades (signal_id, tp_pct, sl_pct, hit, ret_pct, days_held) VALUES (?,?,?,?,?,?)",
                 (signal_id, tp_pct, sl_pct, hit, ret_pct, days_held))
    conn.commit()


def save_short_trade(conn, signal_id, hold_hours, ret_pct, max_up, max_dd):
    conn.execute("INSERT INTO short_trades (signal_id, hold_hours, ret_pct, max_up_pct, max_dd_pct) VALUES (?,?,?,?,?)",
                 (signal_id, hold_hours, ret_pct, max_up, max_dd))
    conn.commit()


def save_short_tp_sl(conn, signal_id, tp_pct, sl_pct, hit, ret_pct, hours_held):
    conn.execute("INSERT INTO short_tp_sl_trades (signal_id, tp_pct, sl_pct, hit, ret_pct, hours_held) VALUES (?,?,?,?,?,?)",
                 (signal_id, tp_pct, sl_pct, hit, ret_pct, hours_held))
    conn.commit()


# ── 报告层 ────────────────────────────────────────────────────────────────────

def print_separator(char="─", width=72):
    print(char * width)


def print_fixed_hold_report(conn: sqlite3.Connection):
    print("\n" + "═" * 72)
    print("  固定持仓期表现")
    print("═" * 72)
    print(f"  {'持仓':<8} {'样本':<6} {'胜率':<8} {'均收益':<10} {'均盈':<10} {'均亏':<10} {'最佳':<10} {'最差':<10} {'盈亏比':<8} {'Sharpe'}")
    print_separator()

    for hold in HOLD_PERIODS:
        rows = conn.execute(
            "SELECT ret_pct FROM trades WHERE hold_days=? AND ret_pct IS NOT NULL", (hold,)
        ).fetchall()
        rets = [r[0] for r in rows]
        if not rets:
            continue
        s = aggregate_stats(rets)
        print(f"  {hold:<8} {s['n']:<6} {s['win_rate']:>5.1f}%   "
              f"{s['avg_ret']:>+8.1f}%  {s['avg_win']:>+8.1f}%  {s['avg_loss']:>+8.1f}%  "
              f"{s['best']:>+8.1f}%  {s['worst']:>+8.1f}%  {s['rr']:>6.2f}   {s['sharpe']:>+6.2f}")


def print_tp_sl_report(conn: sqlite3.Connection):
    print("\n" + "═" * 72)
    print("  TP/SL 组合表现")
    print("═" * 72)
    print(f"  {'组合':<14} {'样本':<6} {'TP命中':<8} {'SL命中':<8} {'超时':<8} {'胜率':<8} {'均收益':<10} {'期望值'}")
    print_separator()

    for tp, sl in TP_SL_COMBOS:
        rows = conn.execute(
            "SELECT hit, ret_pct FROM tp_sl_trades WHERE tp_pct=? AND sl_pct=? AND ret_pct IS NOT NULL",
            (tp, sl)
        ).fetchall()
        if not rows:
            continue
        n         = len(rows)
        tp_hits   = sum(1 for r in rows if r[0] == "tp")
        sl_hits   = sum(1 for r in rows if r[0] == "sl")
        timeouts  = sum(1 for r in rows if r[0] == "timeout")
        rets      = [r[1] for r in rows]
        s         = aggregate_stats(rets)
        label     = f"TP{tp}%/SL{sl}%" if tp else f"No-TP/SL{sl}%"
        print(f"  {label:<14} {n:<6} {tp_hits/n*100:>5.1f}%   {sl_hits/n*100:>5.1f}%   "
              f"{timeouts/n*100:>5.1f}%   {s['win_rate']:>5.1f}%   {s['avg_ret']:>+8.1f}%   "
              f"{s['avg_ret']:>+.2f}%")


def print_status_breakdown(conn: sqlite3.Connection):
    print("\n" + "═" * 72)
    print("  按信号状态分组（30天持仓）")
    print("═" * 72)
    print(f"  {'状态':<12} {'样本':<6} {'胜率':<8} {'均收益':<10} {'均最大回撤'}")
    print_separator()

    for status in ["放量启动", "开始放量", "收筹中"]:
        rows = conn.execute("""
            SELECT t.ret_pct, t.max_dd_pct
            FROM trades t JOIN signals s ON t.signal_id = s.id
            WHERE t.hold_days = 30 AND s.status = ? AND t.ret_pct IS NOT NULL
        """, (status,)).fetchall()
        if not rows:
            continue
        rets   = [r[0] for r in rows]
        dds    = [r[1] for r in rows]
        s_stat = aggregate_stats(rets)
        avg_dd = statistics.mean(dds) if dds else 0
        print(f"  {status:<12} {s_stat['n']:<6} {s_stat['win_rate']:>5.1f}%   "
              f"{s_stat['avg_ret']:>+8.1f}%   {avg_dd:.1f}%")


def print_score_correlation(conn: sqlite3.Connection):
    print("\n" + "═" * 72)
    print("  评分区间 vs 30天收益（得分越高，表现越好？）")
    print("═" * 72)
    print(f"  {'评分':<14} {'样本':<6} {'胜率':<8} {'均收益':<10} {'均最大上涨'}")
    print_separator()

    buckets = [(0, 40), (40, 55), (55, 65), (65, 75), (75, 100)]
    for lo, hi in buckets:
        rows = conn.execute("""
            SELECT t.ret_pct, t.max_up_pct
            FROM trades t JOIN signals s ON t.signal_id = s.id
            WHERE t.hold_days = 30 AND s.score >= ? AND s.score < ?
              AND t.ret_pct IS NOT NULL
        """, (lo, hi)).fetchall()
        if not rows:
            continue
        rets    = [r[0] for r in rows]
        ups     = [r[1] for r in rows]
        s_stat  = aggregate_stats(rets)
        avg_up  = statistics.mean(ups) if ups else 0
        print(f"  [{lo:>2}-{hi:>3})       {s_stat['n']:<6} {s_stat['win_rate']:>5.1f}%   "
              f"{s_stat['avg_ret']:>+8.1f}%   {avg_up:.1f}%")


def print_top_signals(conn: sqlite3.Connection, n=10):
    print("\n" + "═" * 72)
    print(f"  历史最强信号（按30天收益排名前{n}）")
    print("═" * 72)
    rows = conn.execute("""
        SELECT s.scan_date, s.coin, s.score, s.status, s.sideways_days,
               t.ret_pct, t.max_up_pct, t.max_dd_pct
        FROM trades t JOIN signals s ON t.signal_id = s.id
        WHERE t.hold_days = 30 AND t.ret_pct IS NOT NULL
        ORDER BY t.ret_pct DESC LIMIT ?
    """, (n,)).fetchall()
    print(f"  {'日期':<12} {'币':<10} {'分':<6} {'状态':<10} {'横盘天':<8} {'30d收益':<10} {'最大涨幅':<10} {'最大回撤'}")
    print_separator()
    for r in rows:
        print(f"  {r[0]:<12} {r[1]:<10} {r[2]:<6.0f} {r[3]:<10} {r[4]:<8} "
              f"{r[5]:>+8.1f}%  {r[6]:>+8.1f}%  {r[7]:>6.1f}%")


def print_worst_signals(conn: sqlite3.Connection, n=10):
    print("\n" + "═" * 72)
    print(f"  历史最差信号（按30天收益排名后{n}）")
    print("═" * 72)
    rows = conn.execute("""
        SELECT s.scan_date, s.coin, s.score, s.status, t.ret_pct, t.max_dd_pct
        FROM trades t JOIN signals s ON t.signal_id = s.id
        WHERE t.hold_days = 30 AND t.ret_pct IS NOT NULL
        ORDER BY t.ret_pct ASC LIMIT ?
    """, (n,)).fetchall()
    print(f"  {'日期':<12} {'币':<10} {'分':<6} {'状态':<10} {'30d收益':<10} {'最大回撤'}")
    print_separator()
    for r in rows:
        print(f"  {r[0]:<12} {r[1]:<10} {r[2]:<6.0f} {r[3]:<10} "
              f"{r[4]:>+8.1f}%  {r[5]:>6.1f}%")


def print_scan_dates(conn: sqlite3.Connection):
    rows = conn.execute("SELECT scan_date, COUNT(*) FROM signals GROUP BY scan_date ORDER BY scan_date").fetchall()
    print("\n  扫描日期汇总:")
    for r in rows:
        print(f"    {r[0]}  →  {r[1]} 个信号")


# ── 主流程 ────────────────────────────────────────────────────────────────────

def run_backtest(months: int, top_n: int, symbols: list[str]):
    """
    核心回测循环：
    1. 生成月度扫描日期（最近 months 个月的第一天）
    2. 在每个日期运行 pool scan
    3. 对每个信号计算 forward returns
    4. 存入 DB
    """
    # 生成扫描日期（每月第1天，往前 months 个月）
    today     = date.today()
    scan_dates = []
    for m in range(months, 0, -1):
        # 当前月往前 m 个月
        y = today.year
        mo = today.month - m
        while mo <= 0:
            mo += 12
            y -= 1
        scan_dates.append(date(y, mo, 1))

    print(f"\n🔍 回测配置:")
    print(f"   扫描日期: {scan_dates[0]} → {scan_dates[-1]}")
    print(f"   月度扫描: {len(scan_dates)} 次")
    print(f"   每次取前: {top_n} 个信号")
    print(f"   持仓期:   {HOLD_PERIODS} 天")
    print(f"   TP/SL:    {len(TP_SL_COMBOS)} 种组合")

    conn = init_bt_db()
    # 清空旧数据
    conn.execute("DELETE FROM signals")
    conn.execute("DELETE FROM trades")
    conn.execute("DELETE FROM tp_sl_trades")
    conn.commit()

    total_signals = 0
    total_skipped = 0

    for scan_date in scan_dates:
        # scan_ts_ms = 该日 00:00 UTC（以毫秒）
        scan_ts_ms = int(datetime(scan_date.year, scan_date.month, scan_date.day,
                                  tzinfo=timezone.utc).timestamp() * 1000)

        print(f"\n📅 {scan_date}  扫描中...", end="", flush=True)
        signals = run_pool_scan_on_date(symbols, scan_ts_ms, top_n)
        print(f" → {len(signals)} 个信号")

        for sig in signals:
            signal_id = save_signal(conn, str(scan_date), sig)

            # 最长持仓 60 天的 forward bars
            fwd = get_forward_bars(sig["symbol"], sig["scan_ts"], max(HOLD_PERIODS))
            if len(fwd) < 7:
                total_skipped += 1
                continue

            # 固定持仓
            for hold in HOLD_PERIODS:
                ret = calc_fixed_hold(fwd, hold)
                if ret is None:
                    continue
                max_up = calc_max_upside(fwd, hold)
                max_dd = calc_max_drawdown(fwd[:hold])
                save_trade(conn, signal_id, hold, ret, max_up, max_dd)

            # TP/SL
            for tp, sl in TP_SL_COMBOS:
                result = calc_tp_sl(fwd, tp, sl)
                if result["ret_pct"] is not None:
                    save_tp_sl(conn, signal_id, tp, sl, result["hit"], result["ret_pct"], result["days_held"])

            total_signals += 1

    print(f"\n  ✅ 回测完成：{total_signals} 个有效信号，{total_skipped} 个跳过（数据不足）")
    conn.close()


def run_short_backtest(refresh_1h: bool = False):
    """
    短线回测（24h / 48h / 72h）：
    读取已有信号，拉取 1h klines，计算短线 forward returns。
    前提：已运行过 run_backtest() 生成 signals 表。
    """
    conn = init_bt_db()
    signals = conn.execute(
        "SELECT id, symbol, coin, scan_date, entry_price FROM signals"
    ).fetchall()

    if not signals:
        print("❌ 无信号数据，请先运行主回测")
        conn.close()
        return

    # 找出需要 1h 数据的唯一 symbol 集合
    unique_symbols = list({row[1] for row in signals})
    print(f"\n📊 短线回测：{len(signals)} 个信号 × {len(unique_symbols)} 个 symbol")

    # 下载 1h klines（只针对信号 symbol）
    fetch_and_cache_1h(unique_symbols, refresh=refresh_1h)

    # 清空旧短线数据
    conn.execute("DELETE FROM short_trades")
    conn.execute("DELETE FROM short_tp_sl_trades")
    conn.commit()

    total, skipped = 0, 0

    for sig_id, symbol, coin, scan_date, entry_price in signals:
        # 入场时刻 = 信号日次日 00:00 UTC
        sd = date.fromisoformat(scan_date)
        next_day = sd + timedelta(days=1)
        entry_ts_ms = int(datetime(next_day.year, next_day.month, next_day.day,
                                   tzinfo=timezone.utc).timestamp() * 1000)

        # 拉 72h 的 1h bars（最长持仓）
        fwd = get_forward_bars_1h(symbol, entry_ts_ms, max(SHORT_HOLD_HOURS))
        if len(fwd) < 12:   # 至少需要 12h 的数据
            skipped += 1
            continue

        # 固定持仓 24h / 48h / 72h
        for hold_h in SHORT_HOLD_HOURS:
            ret = calc_fixed_hold_hours(fwd, hold_h)
            if ret is None:
                continue
            max_up = calc_max_upside_hours(fwd, hold_h)
            max_dd = calc_max_drawdown_hours(fwd[:hold_h])
            save_short_trade(conn, sig_id, hold_h, ret, max_up, max_dd)

        # TP/SL — 使用较小的 TP/SL 值（短线惯例）
        short_tp_sl = [
            (5,  -3),
            (10, -5),
            (15, -7),
            (20, -10),
            (None, -5),  # 纯 SL
        ]
        for tp, sl in short_tp_sl:
            result = calc_tp_sl_hours(fwd, tp, sl)
            if result["ret_pct"] is not None:
                save_short_tp_sl(conn, sig_id, tp, sl, result["hit"],
                                 result["ret_pct"], result["hours_held"])
        total += 1

    print(f"  ✅ 短线回测完成：{total} 个有效信号，{skipped} 个跳过（1h数据不足）")
    conn.close()


# ── 短线报告 ──────────────────────────────────────────────────────────────────

def print_short_hold_report(conn: sqlite3.Connection):
    print("\n" + "═" * 72)
    print("  短线固定持仓表现（1h精度）")
    print("═" * 72)
    print(f"  {'持仓':<10} {'样本':<6} {'胜率':<8} {'均收益':<10} {'均盈':<10} {'均亏':<10} {'最佳':<10} {'最差':<10} {'Sharpe'}")
    print_separator()

    for hold_h in SHORT_HOLD_HOURS:
        rows = conn.execute(
            "SELECT ret_pct FROM short_trades WHERE hold_hours=? AND ret_pct IS NOT NULL", (hold_h,)
        ).fetchall()
        rets = [r[0] for r in rows]
        if not rets:
            continue
        s = aggregate_stats(rets)
        print(f"  {hold_h}h{'':<7} {s['n']:<6} {s['win_rate']:>5.1f}%   "
              f"{s['avg_ret']:>+8.2f}%  {s['avg_win']:>+8.2f}%  {s['avg_loss']:>+8.2f}%  "
              f"{s['best']:>+8.2f}%  {s['worst']:>+8.2f}%  {s['sharpe']:>+6.2f}")


def print_short_tp_sl_report(conn: sqlite3.Connection):
    print("\n" + "═" * 72)
    print("  短线 TP/SL 组合表现")
    print("═" * 72)
    print(f"  {'组合':<14} {'样本':<6} {'TP命中':<8} {'SL命中':<8} {'超时':<8} {'胜率':<8} {'均收益':<10} {'期望值'}")
    print_separator()

    short_tp_sl = [(5,-3),(10,-5),(15,-7),(20,-10),(None,-5)]
    for tp, sl in short_tp_sl:
        rows = conn.execute(
            "SELECT hit, ret_pct FROM short_tp_sl_trades WHERE tp_pct=? AND sl_pct=? AND ret_pct IS NOT NULL",
            (tp, sl)
        ).fetchall()
        if not rows:
            continue
        n        = len(rows)
        tp_hits  = sum(1 for r in rows if r[0] == "tp")
        sl_hits  = sum(1 for r in rows if r[0] == "sl")
        timeouts = sum(1 for r in rows if r[0] == "timeout")
        rets     = [r[1] for r in rows]
        s        = aggregate_stats(rets)
        label    = f"TP{tp}%/SL{sl}%" if tp else f"No-TP/SL{sl}%"
        print(f"  {label:<14} {n:<6} {tp_hits/n*100:>5.1f}%   {sl_hits/n*100:>5.1f}%   "
              f"{timeouts/n*100:>5.1f}%   {s['win_rate']:>5.1f}%   {s['avg_ret']:>+8.2f}%   "
              f"{s['avg_ret']:>+.2f}%")


def print_short_status_breakdown(conn: sqlite3.Connection):
    print("\n" + "═" * 72)
    print("  短线 — 按信号状态分组（48h持仓）")
    print("═" * 72)
    print(f"  {'状态':<12} {'样本':<6} {'胜率':<8} {'均收益':<10} {'均最大涨幅':<12} {'均最大回撤'}")
    print_separator()

    for status in ["放量启动", "开始放量", "收筹中"]:
        rows = conn.execute("""
            SELECT t.ret_pct, t.max_up_pct, t.max_dd_pct
            FROM short_trades t JOIN signals s ON t.signal_id = s.id
            WHERE t.hold_hours = 48 AND s.status = ? AND t.ret_pct IS NOT NULL
        """, (status,)).fetchall()
        if not rows:
            continue
        rets  = [r[0] for r in rows]
        ups   = [r[1] for r in rows]
        dds   = [r[2] for r in rows]
        s_    = aggregate_stats(rets)
        print(f"  {status:<12} {s_['n']:<6} {s_['win_rate']:>5.1f}%   "
              f"{s_['avg_ret']:>+8.2f}%   {statistics.mean(ups):>+8.1f}%     {statistics.mean(dds):>6.1f}%")


def print_short_hour_profile(conn: sqlite3.Connection):
    """逐小时胜率 — 持仓第几小时最佳？"""
    print("\n" + "═" * 72)
    print("  逐小时胜率剖面（平均最大涨幅 vs 持仓小时数）")
    print("  → 告诉你最佳出场时机在哪个小时")
    print("═" * 72)
    print(f"  {'持仓':<10} {'样本':<6} {'胜率':<8} {'均收益':<10} {'均最大涨幅':<12} {'均最大回撤'}")
    print_separator()

    for hold_h in SHORT_HOLD_HOURS:
        rows = conn.execute(
            "SELECT ret_pct, max_up_pct, max_dd_pct FROM short_trades WHERE hold_hours=? AND ret_pct IS NOT NULL",
            (hold_h,)
        ).fetchall()
        if not rows:
            continue
        rets = [r[0] for r in rows]
        ups  = [r[1] for r in rows]
        dds  = [r[2] for r in rows]
        s_   = aggregate_stats(rets)
        print(f"  {hold_h}h{'':<7} {s_['n']:<6} {s_['win_rate']:>5.1f}%   "
              f"{s_['avg_ret']:>+8.2f}%   {statistics.mean(ups):>+8.1f}%     {statistics.mean(dds):>6.1f}%")


def print_short_top_signals(conn: sqlite3.Connection, n=10):
    print("\n" + "═" * 72)
    print(f"  短线最强信号（48h收益排名前{n}）")
    print("═" * 72)
    rows = conn.execute("""
        SELECT s.scan_date, s.coin, s.score, s.status,
               t.ret_pct, t.max_up_pct, t.max_dd_pct
        FROM short_trades t JOIN signals s ON t.signal_id = s.id
        WHERE t.hold_hours = 48 AND t.ret_pct IS NOT NULL
        ORDER BY t.ret_pct DESC LIMIT ?
    """, (n,)).fetchall()
    print(f"  {'日期':<12} {'币':<10} {'分':<6} {'状态':<10} {'48h收益':<10} {'最大涨幅':<10} {'最大回撤'}")
    print_separator()
    for r in rows:
        print(f"  {r[0]:<12} {r[1]:<10} {r[2]:<6.0f} {r[3]:<10} "
              f"{r[4]:>+8.2f}%  {r[5]:>+8.2f}%  {r[6]:>6.2f}%")


def print_short_report(conn: sqlite3.Connection):
    n = conn.execute("SELECT COUNT(*) FROM short_trades").fetchone()[0]
    if n == 0:
        return
    print("\n" + "═" * 72)
    print("  ══ 短线回测报告（24h / 48h / 72h）══")
    print("═" * 72)
    print_short_hour_profile(conn)
    print_short_hold_report(conn)
    print_short_tp_sl_report(conn)
    print_short_status_breakdown(conn)
    print_short_top_signals(conn)
    print("\n  ℹ️  1h klines | 入场=信号日次日 00:00 UTC 开盘价")
    print("  ℹ️  TP/SL: 先SL后TP保守模拟（每小时bar触发）")


def print_full_report():
    if not DB_PATH.exists():
        print("❌ 回测数据库不存在，先运行回测")
        return
    conn = sqlite3.connect(str(DB_PATH))
    n_signals = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    if n_signals == 0:
        print("❌ 数据库中无数据")
        conn.close()
        return

    print("\n" + "═" * 72)
    print("  庄家收筹雷达 — 回测报告")
    print("═" * 72)
    print(f"  信号总数: {n_signals}")
    print_scan_dates(conn)
    print_fixed_hold_report(conn)
    print_tp_sl_report(conn)
    print_status_breakdown(conn)
    print_score_correlation(conn)
    print_top_signals(conn)
    print_worst_signals(conn)
    # 如果有短线数据也一并打印
    print_short_report(conn)

    print("\n  ℹ️  数据来源: Binance USDT永续合约 (历史日线 + 1h精度)")
    print("  ℹ️  入场假设: 信号日次日开盘价")
    print("  ℹ️  TP/SL假设: 日内高/低点触发（先SL后TP，保守模拟）")
    print("═" * 72 + "\n")
    conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="庄家收筹雷达回测框架")
    parser.add_argument("--months",      type=int, default=12,  help="回测月数（默认12）")
    parser.add_argument("--top",         type=int, default=50,  help="每次扫描取前N个信号（默认50）")
    parser.add_argument("--fetch-only",  action="store_true",   help="只下载日线数据，不运行回测")
    parser.add_argument("--no-fetch",    action="store_true",   help="跳过数据下载，用已有缓存")
    parser.add_argument("--report",      action="store_true",   help="只打印报告（不重跑回测）")
    parser.add_argument("--refresh",     action="store_true",   help="强制刷新所有日线缓存")
    parser.add_argument("--short",       action="store_true",   help="在主回测后额外运行24h/48h/72h短线回测")
    parser.add_argument("--short-only",  action="store_true",   help="只运行短线回测（复用已有信号）")
    parser.add_argument("--refresh-1h",  action="store_true",   help="强制刷新1h klines缓存")
    args = parser.parse_args()

    if args.report:
        print_full_report()
        return

    if args.short_only:
        run_short_backtest(refresh_1h=args.refresh_1h)
        print_full_report()
        return

    # 1. 获取所有合约列表
    print("📋 获取合约列表...")
    symbols = get_all_perp_symbols()
    print(f"  共 {len(symbols)} 个 USDT 永续合约")

    if not symbols:
        print("❌ 无法获取合约列表，请检查网络")
        return

    # 2. 下载/更新日线缓存
    if not args.no_fetch:
        fetch_and_cache_all(symbols, refresh=args.refresh)

    if args.fetch_only:
        print("✅ 数据下载完成")
        return

    # 3. 主回测（日线）
    run_backtest(months=args.months, top_n=args.top, symbols=symbols)

    # 4. 短线回测（可选，1h）
    if args.short:
        run_short_backtest(refresh_1h=args.refresh_1h)

    # 5. 打印报告
    print_full_report()


if __name__ == "__main__":
    main()
