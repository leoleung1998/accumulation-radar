#!/usr/bin/env python3
"""
庄家收筹雷达 v1 — 发现庄家横盘吸筹 + OI异动

核心逻辑（Patrick教的）：
1. 庄家拉盘前必须先收筹 → 长期横盘+低量 = 收筹中
2. OI暴涨 = 大资金进场建仓 = 即将拉盘
3. 两个信号叠加 = 最强信号

两个模块：
A. 横盘收筹标的池（每天扫一次）→ 找正在被庄家收筹的币
B. OI异动监控（每小时扫）→ 标的池内的币有OI异动立即报警

数据源：币安合约API（免费公开，零成本）
"""

import os
import sys
import time
import requests
import sqlite3
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

# === 配置 ===
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
FAPI = "https://fapi.binance.com"
DB_PATH = Path(__file__).parent / "accumulation.db"

# 收筹标的池参数
MIN_SIDEWAYS_DAYS = 45        # 至少横盘45天
MAX_RANGE_PCT = 80            # 横盘期价格波动<80%（宽松点，庄家盘波动可以大）
MAX_AVG_VOL_USD = 20_000_000  # 日均成交<$20M（低量才是收筹）
MIN_DATA_DAYS = 50            # 至少50天数据

# OI异动参数
MIN_OI_DELTA_PCT = 3.0        # OI变化至少3%
MIN_OI_USD = 2_000_000        # 最低OI门槛 $2M

# 放量突破参数
VOL_BREAKOUT_MULT = 3.0       # 当日Vol > 3x均值 = 放量


def api_get(endpoint, params=None):
    """币安API请求"""
    url = f"{FAPI}{endpoint}"
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                time.sleep(2)
            else:
                return None
        except:
            time.sleep(1)
    return None


def init_db():
    """初始化数据库"""
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS watchlist (
        symbol TEXT PRIMARY KEY,
        coin TEXT,
        added_date TEXT,
        sideways_days INT,
        range_pct REAL,
        avg_vol REAL,
        low_price REAL,
        high_price REAL,
        current_price REAL,
        score REAL,
        status TEXT DEFAULT 'watching',
        last_oi_alert TEXT,
        notes TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT,
        alert_type TEXT,
        alert_time TEXT,
        price REAL,
        oi_delta_pct REAL,
        vol_ratio REAL,
        details TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS hl_seen_trades (
        trade_id   TEXT PRIMARY KEY,
        coin       TEXT,
        notional   REAL,
        alerted_at TEXT
    )""")
    conn.commit()
    return conn


def get_all_perp_symbols():
    """获取所有USDT永续合约"""
    info = api_get("/fapi/v1/exchangeInfo")
    if not info:
        return []
    return [s["symbol"] for s in info["symbols"]
            if s["quoteAsset"] == "USDT" 
            and s["contractType"] == "PERPETUAL"
            and s["status"] == "TRADING"]


def analyze_accumulation(symbol, klines):
    """分析单个币的收筹特征"""
    if len(klines) < MIN_DATA_DAYS:
        return None
    
    data = []
    for k in klines:
        data.append({
            "ts": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "vol": float(k[7]),  # quote volume (USDT)
        })
    
    coin = symbol.replace("USDT", "")
    
    # === 排除稳定币和指数 ===
    EXCLUDE = {"USDC", "USDP", "TUSD", "FDUSD", "BTCDOM", "DEFI", "USDM"}
    if coin in EXCLUDE:
        return None
    
    # === 排除已经暴涨过+崩盘的币 ===
    # 最近7天vs之前的均价，如果已经涨>300%就跳过（来不及了）
    recent_7d = data[-7:]
    prior = data[:-7]
    if not prior:
        return None
    
    recent_avg_px = sum(d["close"] for d in recent_7d) / len(recent_7d)
    prior_avg_px = sum(d["close"] for d in prior) / len(prior)
    
    if prior_avg_px > 0 and ((recent_avg_px - prior_avg_px) / prior_avg_px) > 3.0:
        return None  # 已经涨了300%+，来不及了
    
    # === 寻找横盘区间 ===
    # 从最近往回找，找最长的横盘期（价格波动<MAX_RANGE_PCT%）
    # 关键：必须是真横盘（斜率接近零），阴跌不算横盘！
    best_sideways = 0
    best_range = 0
    best_low = 0
    best_high = 0
    best_avg_vol = 0
    best_slope_pct = 0
    
    # 用滑动窗口从60天到全部
    for window in range(MIN_SIDEWAYS_DAYS, len(prior) + 1):
        window_data = prior[-window:]
        lows = [d["low"] for d in window_data]
        highs = [d["high"] for d in window_data]
        
        w_low = min(lows)
        w_high = max(highs)
        
        if w_low <= 0:
            continue
        
        range_pct = ((w_high - w_low) / w_low) * 100
        
        if range_pct <= MAX_RANGE_PCT:
            avg_vol = sum(d["vol"] for d in window_data) / len(window_data)
            if avg_vol <= MAX_AVG_VOL_USD:
                # 线性回归算斜率：阴跌/暴涨不算横盘
                closes = [d["close"] for d in window_data]
                n = len(closes)
                x_mean = (n - 1) / 2.0
                y_mean = sum(closes) / n
                num = sum((i - x_mean) * (c - y_mean) for i, c in enumerate(closes))
                den = sum((i - x_mean) ** 2 for i in range(n))
                slope = num / den if den > 0 else 0
                # 累计变化占起始价的百分比
                slope_pct = (slope * n / closes[0] * 100) if closes[0] > 0 else 0
                
                # 斜率过滤：累计变化超过±20%不算横盘
                if abs(slope_pct) > 20:
                    continue
                
                if window > best_sideways:
                    best_sideways = window
                    best_range = range_pct
                    best_low = w_low
                    best_high = w_high
                    best_avg_vol = avg_vol
                    best_slope_pct = slope_pct
    
    if best_sideways < MIN_SIDEWAYS_DAYS:
        return None
    
    # === 计算收筹评分 ===
    # 横盘越久越好（庄家需要时间吸筹）
    days_score = min(best_sideways / 90, 1.0) * 25  # 90天满分25
    
    # 区间越窄越好（控盘紧）
    range_score = max(0, (1 - best_range / MAX_RANGE_PCT)) * 20  # 越窄越高，满分20
    
    # 成交量越低越好（死水一潭 = 筹码集中）
    vol_score = max(0, (1 - best_avg_vol / MAX_AVG_VOL_USD)) * 20  # 越低越高，满分20
    
    # 最近是否开始放量？（放量是启动信号）
    recent_vol = sum(d["vol"] for d in recent_7d) / len(recent_7d)
    vol_breakout = recent_vol / best_avg_vol if best_avg_vol > 0 else 0
    breakout_score = min(vol_breakout / VOL_BREAKOUT_MULT, 1.0) * 15  # 放量加分，满分15
    
    # 市值越低空间越大（核心！Patrick: 低市值=大空间）
    # 用当前价格*日均成交量/换手率来粗估市值排名
    # 实际市值在推送时用CoinGecko补充
    est_mcap = data[-1]["close"] * best_avg_vol * 30  # 粗略估算
    if est_mcap > 0 and est_mcap < 50_000_000:
        mcap_score = 20  # <$50M 满分
    elif est_mcap < 100_000_000:
        mcap_score = 15
    elif est_mcap < 200_000_000:
        mcap_score = 10
    elif est_mcap < 500_000_000:
        mcap_score = 5
    else:
        mcap_score = 0
    
    total_score = days_score + range_score + vol_score + breakout_score + mcap_score
    
    # 横盘质量加分：斜率越接近零越好（真横盘bonus，满分+5）
    flatness_bonus = max(0, (1 - abs(best_slope_pct) / 20)) * 5
    total_score += flatness_bonus
    
    # 状态判断
    if vol_breakout >= VOL_BREAKOUT_MULT:
        status = "🔥放量启动"
    elif vol_breakout >= 1.5:
        status = "⚡开始放量"
    else:
        status = "💤收筹中"
    
    return {
        "symbol": symbol,
        "coin": coin,
        "sideways_days": best_sideways,
        "range_pct": best_range,
        "slope_pct": best_slope_pct,
        "low_price": best_low,
        "high_price": best_high,
        "avg_vol": best_avg_vol,
        "current_price": data[-1]["close"],
        "recent_vol": recent_vol,
        "vol_breakout": vol_breakout,
        "score": total_score,
        "status": status,
        "data_days": len(data),
    }


def scan_accumulation_pool():
    """扫描全市场，找正在被收筹的币"""
    print("📊 扫描全市场收筹标的...")
    
    symbols = get_all_perp_symbols()
    print(f"  共 {len(symbols)} 个合约")
    
    results = []
    
    for i, sym in enumerate(symbols):
        klines = api_get("/fapi/v1/klines", {
            "symbol": sym, "interval": "1d", "limit": 180
        })
        
        if klines and isinstance(klines, list):
            r = analyze_accumulation(sym, klines)
            if r:
                results.append(r)
        
        if (i + 1) % 10 == 0:
            time.sleep(0.5)
        if (i + 1) % 100 == 0:
            print(f"  进度: {i+1}/{len(symbols)}... 已发现{len(results)}个")
    
    results.sort(key=lambda x: x["score"], reverse=True)
    print(f"  ✅ 发现 {len(results)} 个收筹标的")
    return results


def get_ohlc_timing(symbol: str) -> dict:
    """
    拉取最近 72 根 1h K线，判断当前是"太早/入场/太晚/已转跌"。

    返回:
      label  : "🟢早" | "🟡入场" | "🔴晚" | "⚫转跌"
      reason : 一句话理由
      ext_pct: 当前价格距 MA25 的偏离百分比（+= 已涨多少）
      spike  : 近6根K内有无单根 >8% 的暴涨柱
      trend3 : 最近3根K的颜色 ("↑↑↑" / "↓↓↑" 等)
    """
    kl = api_get("/fapi/v1/klines", {"symbol": symbol, "interval": "1h", "limit": 72})
    if not kl or len(kl) < 25:
        return {"label": "❓", "reason": "数据不足", "ext_pct": 0, "spike": False, "trend3": ""}

    bars = [{"o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
             "v": float(k[7])} for k in kl]

    closes = [b["c"] for b in bars]
    price  = closes[-1]

    # ── MAs ──────────────────────────────────────────────────────────────
    ma7  = sum(closes[-7:])  / 7
    ma25 = sum(closes[-25:]) / 25

    ext_ma25 = (price - ma25) / ma25 * 100   # % above MA25 (key: shows how extended)

    # ── Spike detection: any single 1h candle >8% body in last 6 bars ──
    spike = False
    spike_bars_ago = 0
    for i, b in enumerate(bars[-6:], 1):
        body_pct = abs(b["c"] - b["o"]) / b["o"] * 100 if b["o"] > 0 else 0
        if body_pct >= 8:
            spike = True
            spike_bars_ago = i

    # ── Candle color trend (last 3 bars) ──────────────────────────────
    def arrow(b): return "↑" if b["c"] >= b["o"] else "↓"
    trend3 = "".join(arrow(b) for b in bars[-3:])

    # ── Volume trend: recent 6h avg vs prior 18h avg ──────────────────
    vol_recent = sum(b["v"] for b in bars[-6:])  / 6
    vol_prior  = sum(b["v"] for b in bars[-24:-6]) / 18
    vol_ratio  = vol_recent / vol_prior if vol_prior > 0 else 1

    # ── Price position in 48h range ────────────────────────────────────
    hi48 = max(b["h"] for b in bars[-48:])
    lo48 = min(b["l"] for b in bars[-48:])
    range_pos = (price - lo48) / (hi48 - lo48) * 100 if hi48 > lo48 else 50

    # ── Classification logic ──────────────────────────────────────────
    # ⚫ 转跌: spiked recently, now red candles pulling back below MA7
    if spike and trend3.count("↓") >= 2 and price < ma7:
        label  = "⚫转跌"
        reason = f"近{spike_bars_ago}h有暴涨柱，现价已跌破MA7，做空回调中"

    # 🔴 太晚: price massively extended above MA25, or spike + price still above but vol dying
    elif ext_ma25 > 25 or (spike and vol_ratio < 0.6 and price > ma25):
        label  = "🔴太晚"
        reason = f"价格偏离MA25 +{ext_ma25:.0f}%，已在高位{'' if not spike else f'（近{spike_bars_ago}h暴涨后）'}"

    # 🟡 入场: breaking out but not yet extended — MA7 just crossed above MA25, vol rising
    elif (ma7 > ma25) and (0 < ext_ma25 <= 25) and vol_ratio > 1.2 and trend3.count("↑") >= 2:
        label  = "🟡入场"
        reason = f"MA7上穿MA25，量增{vol_ratio:.1f}x，突破初期 偏离{ext_ma25:.0f}%"

    # 🟡 入场: price at bottom of recent range but OI already rising (暗流)
    elif range_pos < 35 and vol_ratio > 1.3 and ext_ma25 < 5:
        label  = "🟡入场"
        reason = f"48h低位区({range_pos:.0f}%)但量在放大{vol_ratio:.1f}x，暗流蓄势"

    # 🟢 太早: flat, low vol, below/near MAs — still in base
    elif ext_ma25 < 8 and vol_ratio < 1.2 and not spike:
        label  = "🟢太早"
        reason = f"价格贴近MA25({ext_ma25:+.0f}%)，量未放大({vol_ratio:.1f}x)，仍在底部蓄势"

    # default: ambiguous
    else:
        label  = "🟡观察"
        reason = f"偏离MA25 {ext_ma25:+.0f}%，量比{vol_ratio:.1f}x，趋势{trend3}"

    return {
        "label":    label,
        "reason":   reason,
        "ext_pct":  round(ext_ma25, 1),
        "spike":    spike,
        "trend3":   trend3,
        "vol_ratio": round(vol_ratio, 2),
        "range_pos": round(range_pos, 0),
    }


def scan_oi_changes(watchlist_symbols):
    """对标的池内的币扫描OI异动"""
    print(f"📊 扫描OI异动（{len(watchlist_symbols)}个标的）...")
    
    alerts = []
    
    for sym in watchlist_symbols:
        # OI历史
        oi_hist = api_get("/futures/data/openInterestHist", {
            "symbol": sym, "period": "1h", "limit": 3
        })
        
        if not oi_hist or len(oi_hist) < 2:
            continue
        
        prev_oi = float(oi_hist[-2]["sumOpenInterestValue"])
        curr_oi = float(oi_hist[-1]["sumOpenInterestValue"])
        
        if prev_oi <= 0 or curr_oi < MIN_OI_USD:
            continue
        
        delta_pct = ((curr_oi - prev_oi) / prev_oi) * 100
        
        if abs(delta_pct) >= MIN_OI_DELTA_PCT:
            # 拿当前价格
            ticker = api_get("/fapi/v1/ticker/24hr", {"symbol": sym})
            if not ticker:
                continue
            
            price = float(ticker["lastPrice"])
            vol_24h = float(ticker["quoteVolume"])
            px_chg = float(ticker["priceChangePercent"])
            
            # 拿费率
            funding = api_get("/fapi/v1/fundingRate", {"symbol": sym, "limit": 1})
            fr = float(funding[0]["fundingRate"]) if funding else 0
            
            coin = sym.replace("USDT", "")
            
            alerts.append({
                "symbol": sym,
                "coin": coin,
                "price": price,
                "oi_usd": curr_oi,
                "oi_delta_pct": delta_pct,
                "oi_delta_usd": curr_oi - prev_oi,
                "vol_24h": vol_24h,
                "px_chg_pct": px_chg,
                "funding_rate": fr,
            })
        
        time.sleep(0.3)
    
    alerts.sort(key=lambda x: abs(x["oi_delta_pct"]), reverse=True)
    print(f"  ✅ 发现 {len(alerts)} 个OI异动")
    return alerts


def format_usd(v):
    if v >= 1e9: return f"${v/1e9:.1f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def build_pool_report(results):
    """生成收筹标的池报告"""
    if not results:
        return ""
    
    now = datetime.now(timezone(timedelta(hours=1)))
    
    lines = [
        f"🏦 **庄家收筹雷达** — 标的池更新",
        f"⏰ {now.strftime('%Y-%m-%d %H:%M')} BST",
        f"━━━━━━━━━━━━━━━━━━",
        f"扫描 {len(results)} 个合约，发现标的：",
        "",
    ]
    
    # 分组：放量启动 > 开始放量 > 收筹中
    firing = [r for r in results if "放量启动" in r["status"]]
    warming = [r for r in results if "开始放量" in r["status"]]
    sleeping = [r for r in results if "收筹中" in r["status"]]
    
    if firing:
        lines.append(f"🔥 **放量启动** ({len(firing)}个) — 最高优先级！")
        for r in firing[:10]:
            lines.append(
                f"  🔥 **{r['coin']}** | 分:{r['score']:.0f} | "
                f"横盘{r['sideways_days']}天 | 波动{r['range_pct']:.0f}% | "
                f"Vol放大{r['vol_breakout']:.1f}x"
            )
            lines.append(
                f"     ${r['current_price']:.6f} | "
                f"区间: ${r['low_price']:.6f}~${r['high_price']:.6f} | "
                f"日均Vol: {format_usd(r['avg_vol'])}"
            )
        lines.append("")
    
    if warming:
        lines.append(f"⚡ **开始放量** ({len(warming)}个) — 关注中")
        for r in warming[:10]:
            lines.append(
                f"  ⚡ {r['coin']} | 分:{r['score']:.0f} | "
                f"横盘{r['sideways_days']}天 | 波动{r['range_pct']:.0f}% | "
                f"Vol{r['vol_breakout']:.1f}x"
            )
        lines.append("")
    
    if sleeping:
        lines.append(f"💤 **收筹中** ({len(sleeping)}个) — 持续监控")
        for r in sleeping[:15]:
            lines.append(
                f"  💤 {r['coin']} | 分:{r['score']:.0f} | "
                f"横盘{r['sideways_days']}天 | 波动{r['range_pct']:.0f}% | "
                f"日均Vol {format_usd(r['avg_vol'])}"
            )
    
    return "\n".join(lines)


def build_oi_alert_report(alerts, watchlist_coins):
    """生成OI异动报告（只报标的池内的）"""
    if not alerts:
        return ""
    
    now = datetime.now(timezone(timedelta(hours=1)))
    
    # 区分：池内 vs 池外
    in_pool = [a for a in alerts if a["symbol"] in watchlist_coins]
    out_pool = [a for a in alerts if a["symbol"] not in watchlist_coins]
    
    lines = [
        f"📊 **OI异动扫描** [收筹池]",
        f"⏰ {now.strftime('%Y-%m-%d %H:%M')} BST",
        f"━━━━━━━━━━━━━━━━━━",
        "",
    ]
    
    if in_pool:
        lines.append(f"🎯 **收筹池内异动** ({len(in_pool)}个) ⚠️ 重点关注!")
        for a in in_pool[:10]:
            emoji = "🟢" if a["oi_delta_pct"] > 0 else "🔴"
            lines.append(
                f"  {emoji} **{a['coin']}** | OI: {a['oi_delta_pct']:+.1f}% "
                f"({format_usd(a['oi_usd'])}) | 价格: {a['px_chg_pct']:+.1f}%"
            )
            # 信号解读
            if a["oi_delta_pct"] > 0 and abs(a["px_chg_pct"]) < 3:
                lines.append(f"     ⚡ 暗流涌动！OI涨但价格平 = 庄家建仓中")
            elif a["oi_delta_pct"] > 0 and a["px_chg_pct"] > 3:
                lines.append(f"     🚀 放量拉升！OI+价格同涨 = 启动中")
        lines.append("")
    
    if out_pool:
        lines.append(f"📋 池外异动 ({len(out_pool)}个)")
        for a in out_pool[:8]:
            emoji = "🟢" if a["oi_delta_pct"] > 0 else "🔴"
            lines.append(
                f"  {emoji} {a['coin']} | OI: {a['oi_delta_pct']:+.1f}% | "
                f"价格: {a['px_chg_pct']:+.1f}%"
            )
    
    return "\n".join(lines)


def send_telegram(text):
    """发送TG消息"""
    if not TG_BOT_TOKEN:
        print("\n[TG] No token, stdout:\n")
        print(text)
        return
    
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    
    # 分段发送（TG限制4096字）
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = line
        else:
            current += "\n" + line if current else line
    if current:
        chunks.append(current)
    
    for chunk in chunks:
        try:
            resp = requests.post(url, json={
                "chat_id": TG_CHAT_ID,
                "text": chunk,
                "parse_mode": "Markdown"
            }, timeout=10)
            if resp.status_code == 200:
                print(f"[TG] Sent ✓ ({len(chunk)} chars)")
            else:
                # Markdown失败就用纯文本
                resp2 = requests.post(url, json={
                    "chat_id": TG_CHAT_ID,
                    "text": chunk.replace("*", "").replace("_", ""),
                }, timeout=10)
                print(f"[TG] Sent plain ({'✓' if resp2.status_code == 200 else '✗'})")
        except Exception as e:
            print(f"[TG] Error: {e}")
        time.sleep(0.5)


def save_watchlist(conn, results):
    """保存标的池到数据库"""
    c = conn.cursor()
    now = datetime.now(timezone(timedelta(hours=1))).strftime("%Y-%m-%d %H:%M")
    
    for r in results:
        c.execute("""INSERT OR REPLACE INTO watchlist 
            (symbol, coin, added_date, sideways_days, range_pct, avg_vol, 
             low_price, high_price, current_price, score, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r["symbol"], r["coin"], now, r["sideways_days"], r["range_pct"],
             r["avg_vol"], r["low_price"], r["high_price"], r["current_price"],
             r["score"], r["status"]))
    
    conn.commit()
    print(f"  💾 保存 {len(results)} 个标的到数据库")


def load_watchlist_symbols(conn):
    """从数据库加载标的池"""
    c = conn.cursor()
    c.execute("SELECT symbol FROM watchlist WHERE status != 'removed'")
    return [row[0] for row in c.fetchall()]


def scan_short_fuel():
    """策略2: 空头燃料 — 涨了+费率负+OI大 = 庄家拉盘爆空单"""
    print("📊 扫描空头燃料（费率为负+在涨的币）...")
    
    tickers = api_get("/fapi/v1/ticker/24hr")
    premiums = api_get("/fapi/v1/premiumIndex")
    
    if not tickers or not premiums:
        return [], []
    
    funding_map = {p["symbol"]: float(p["lastFundingRate"]) 
                   for p in premiums if p["symbol"].endswith("USDT")}
    
    fuel_targets = []     # 已在涨+费率负 = 正在squeeze
    squeeze_targets = []  # 费率极负+还没大涨 = 潜在squeeze
    
    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        
        px_chg = float(t["priceChangePercent"])
        vol = float(t["quoteVolume"])
        fr = funding_map.get(sym, 0)
        coin = sym.replace("USDT", "")
        price = float(t["lastPrice"])
        
        item = {
            "coin": coin, "symbol": sym,
            "px_chg": px_chg, "funding": fr,
            "vol": vol, "price": price,
        }
        
        # 正在squeeze: 涨>5% + 费率负 + Vol>$5M
        if px_chg > 5 and fr < -0.0003 and vol > 5_000_000:
            item["fuel_score"] = abs(fr) * 10000 * px_chg
            fuel_targets.append(item)
        
        # 潜在squeeze: 费率很负 + 还没大涨(<10%) + Vol>$2M
        elif fr < -0.0005 and px_chg < 10 and vol > 2_000_000:
            item["fuel_score"] = abs(fr) * 10000
            squeeze_targets.append(item)
    
    fuel_targets.sort(key=lambda x: x["fuel_score"], reverse=True)
    squeeze_targets.sort(key=lambda x: x["fuel_score"], reverse=True)
    
    print(f"  ✅ 正在squeeze: {len(fuel_targets)}个, 潜在squeeze: {len(squeeze_targets)}个")
    return fuel_targets, squeeze_targets


def build_fuel_report(fuel_targets, squeeze_targets):
    """生成空头燃料报告"""
    if not fuel_targets and not squeeze_targets:
        return ""
    
    now = datetime.now(timezone(timedelta(hours=1)))
    lines = [
        f"🔥 **空头燃料扫描**",
        f"⏰ {now.strftime('%Y-%m-%d %H:%M')} BST",
        f"━━━━━━━━━━━━━━━━━━",
        f"逻辑：费率负=大量做空，庄家拉盘爆空单+收资金费",
        "",
    ]
    
    if fuel_targets:
        lines.append(f"🚀 **正在Squeeze** ({len(fuel_targets)}个) — 涨了+空头还在扛")
        for t in fuel_targets[:8]:
            fr_pct = t["funding"] * 100
            flag = "🎯极度!" if fr_pct < -0.1 else "⚠️"
            lines.append(
                f"  {flag} **{t['coin']}** | 涨{t['px_chg']:+.1f}% | "
                f"费率🧊{fr_pct:.4f}% | Vol {format_usd(t['vol'])}"
            )
        lines.append("")
    
    if squeeze_targets:
        lines.append(f"🎯 **潜在Squeeze** ({len(squeeze_targets)}个) — 费率极负+还没大涨")
        for t in squeeze_targets[:8]:
            fr_pct = t["funding"] * 100
            lines.append(
                f"  🧊 {t['coin']} | 价格{t['px_chg']:+.1f}% | "
                f"费率{fr_pct:.4f}% | Vol {format_usd(t['vol'])}"
            )
    
    return "\n".join(lines)


# ── OI 暗流背离扫描 ───────────────────────────────────────────────────────────
#
#  全市场扫描「OI大涨但价格没动」的品种。
#  背离度 = OI变化% / max(|价格变化%|, 1.0)
#  越高 = 资金涌入越多，价格越没反应 = 潜在爆发前的蓄力

# 背离扫描参数
DIV_MIN_OI_DELTA_PCT  = 5.0    # OI 6h涨幅至少5%
DIV_MIN_OI_USD        = 1_000_000  # OI绝对值至少$1M
DIV_MAX_PRICE_CHG     = 5.0    # 价格24h变化不超过±5%（才算"没动"）
DIV_MIN_RATIO         = 2.0    # 背离度至少2.0
DIV_MIN_VOL           = 500_000    # 24h成交至少$500K


def scan_oi_divergence(ticker_map: dict, funding_map: dict, pool_syms: set) -> list:
    """
    全市场扫描 OI/价格背离度。
    ticker_map: {sym: {px_chg, vol, price}} — 已从主流程传入，不重复拉取
    funding_map: {sym: funding_rate}
    pool_syms: 收筹池 symbol 集合（用于标记）

    返回已排序的背离列表，每项包含 timing 判断。
    """
    print("🌊 扫描全市场 OI/价格背离度...")

    # 按24h成交量筛选候选（太小的没意义）
    candidates = [
        sym for sym, tk in ticker_map.items()
        if tk["vol"] >= DIV_MIN_VOL
    ]
    print(f"  候选 {len(candidates)} 个（24h成交>{format_usd(DIV_MIN_VOL)}）")

    results = []

    for i, sym in enumerate(candidates):
        # 拉1h OI历史，取7根：curr(0) / 1h前(-2) / 6h前(index 0)
        oi_hist = api_get("/futures/data/openInterestHist", {
            "symbol": sym, "period": "1h", "limit": 7
        })
        if not oi_hist or len(oi_hist) < 3:
            continue

        curr_oi  = float(oi_hist[-1]["sumOpenInterestValue"])
        prev_1h  = float(oi_hist[-2]["sumOpenInterestValue"])
        prev_6h  = float(oi_hist[0]["sumOpenInterestValue"])

        if prev_6h <= 0 or curr_oi < DIV_MIN_OI_USD:
            continue

        oi_d1h = (curr_oi - prev_1h) / prev_1h * 100 if prev_1h > 0 else 0
        oi_d6h = (curr_oi - prev_6h) / prev_6h * 100

        if oi_d6h < DIV_MIN_OI_DELTA_PCT:
            continue

        tk      = ticker_map[sym]
        px_chg  = tk["px_chg"]   # 24h price change %
        vol_24h = tk["vol"]
        price   = tk["price"]
        fr      = funding_map.get(sym, 0) * 100
        coin    = sym.replace("USDT", "")

        # 背离度：6h OI涨幅 ÷ 24h价格绝对变化（最小1%防止除零）
        divergence = oi_d6h / max(abs(px_chg), 1.0)

        if divergence < DIV_MIN_RATIO:
            continue

        # 价格变化不能太大（已大涨说明已经反映了，不是背离）
        if abs(px_chg) > DIV_MAX_PRICE_CHG:
            continue

        # OHLC 时机判断
        timing = get_ohlc_timing(sym)

        results.append({
            "sym":        sym,
            "coin":       coin,
            "price":      price,
            "oi_usd":     curr_oi,
            "oi_d1h":     oi_d1h,
            "oi_d6h":     oi_d6h,
            "px_chg":     px_chg,
            "divergence": divergence,
            "vol_24h":    vol_24h,
            "fr_pct":     fr,
            "in_pool":    sym in pool_syms,
            "timing":     timing,
        })

        if (i + 1) % 20 == 0:
            time.sleep(0.3)
        else:
            time.sleep(0.05)

    # 排序：背离度优先，其次OI涨幅
    results.sort(key=lambda x: (x["divergence"], x["oi_d6h"]), reverse=True)
    print(f"  ✅ 发现 {len(results)} 个 OI/价格背离标的")
    return results


def build_divergence_report(results: list) -> str:
    if not results:
        return ""

    now = datetime.now(timezone(timedelta(hours=1)))

    # 分层：有pool加持的优先
    in_pool  = [r for r in results if r["in_pool"]]
    out_pool = [r for r in results if not r["in_pool"]]

    # 过滤掉已经转跌/太晚的（时机不对）
    def timing_ok(r):
        return r["timing"]["label"] not in ("🔴太晚", "⚫转跌")

    pool_good = [r for r in in_pool  if timing_ok(r)]
    out_good  = [r for r in out_pool if timing_ok(r)]
    too_late  = [r for r in results  if not timing_ok(r)]

    lines = [
        f"🌊 **OI暗流背离榜** — 全市场",
        f"⏰ {now.strftime('%Y-%m-%d %H:%M')} BST",
        f"━━━━━━━━━━━━━━━━━━",
        f"逻辑：OI大涨+价格不动 = 资金建仓中，价格尚未反应",
        f"背离度 = OI涨幅% ÷ |价格涨幅%|（越高越强）",
        "",
    ]

    if pool_good:
        lines.append(f"🎯 **收筹池内背离** ({len(pool_good)}个) ← 双重确认，优先关注")
        for r in pool_good[:6]:
            t = r["timing"]
            lines += [
                f"  💤 **{r['coin']}** | 背离{r['divergence']:.1f}x | OI {r['oi_d6h']:+.1f}% (6h) / {r['oi_d1h']:+.1f}% (1h)",
                f"     价格 {r['px_chg']:+.1f}% | ${r['price']:.6g} | OI总量 {format_usd(r['oi_usd'])}",
                f"     费率 {r['fr_pct']:+.4f}% | 成交 {format_usd(r['vol_24h'])} | {t['label']} {t['reason']}",
                "",
            ]

    if out_good:
        show = out_good[:8]
        lines.append(f"📊 **全市场背离** (共{len(out_good)}个，展示前{len(show)})")
        for r in show:
            t = r["timing"]
            pool_tag = "💤池" if r["in_pool"] else ""
            lines.append(
                f"  {t['label']} **{r['coin']}** {pool_tag}| "
                f"背离{r['divergence']:.1f}x | OI {r['oi_d6h']:+.1f}% (6h) | "
                f"价格{r['px_chg']:+.1f}% | 费率{r['fr_pct']:+.3f}%"
            )
            lines.append(
                f"     {t['reason']}  (MA25偏{t['ext_pct']:+.0f}% 量比{t['vol_ratio']}x)"
            )
        lines.append("")

    if too_late:
        lines.append(f"⚠️ **已过时/已反转** ({len(too_late)}个) — 跳过")
        for r in too_late[:4]:
            lines.append(f"  {r['timing']['label']} {r['coin']} | 背离{r['divergence']:.1f}x | {r['timing']['reason']}")

    lines += [
        "",
        f"📖 背离度参考: >10=极强 | 5-10=强 | 3-5=中",
        f"  🟢太早=底部蓄势可建仓 | 🟡入场=突破初期 | 🔴/⚫跳过",
    ]

    return "\n".join(lines)


# ── Hyperliquid 鲸鱼扫描 ──────────────────────────────────────────────────────

HL_API          = "https://api.hyperliquid.xyz/info"
HL_MIN_NOTIONAL = 200_000   # 单笔 > $200K
HL_LOOKBACK_MIN = 65        # 回看65分钟


def hl_post(payload: dict):
    r = requests.post(HL_API, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def scan_hyperliquid_whales(conn, pool_coin_names: set) -> list:
    """扫描收筹池内的币在 Hyperliquid 上的大单，时间窗口65分钟，SQLite去重"""
    if not pool_coin_names:
        return []

    print(f"🐋 扫描 Hyperliquid 大单（{len(pool_coin_names)} 个池内币）...")

    try:
        data = hl_post({"type": "metaAndAssetCtxs"})
        universe, asset_ctxs = data[0]["universe"], data[1]
    except Exception as e:
        print(f"  ❌ HL API 失败: {e}")
        return []

    hl_idx = {m["name"]: i for i, m in enumerate(universe)}
    watchable = [c for c in pool_coin_names if c in hl_idx]
    print(f"  池内币在 HL 上有 {len(watchable)} 个")

    cutoff_ms = int((time.time() - HL_LOOKBACK_MIN * 60) * 1000)
    alerts = []

    for coin in watchable:
        ctx      = asset_ctxs[hl_idx[coin]]
        day_vol  = float(ctx.get("dayNtlVlm", 0))
        if day_vol < 500_000:
            continue

        try:
            trades = hl_post({"type": "recentTrades", "coin": coin})
        except Exception:
            continue

        for t in trades:
            if int(t["time"]) < cutoff_ms:
                continue

            notional = float(t["px"]) * float(t["sz"])
            if notional < HL_MIN_NOTIONAL:
                continue

            trade_id = str(t.get("tid") or t.get("hash", ""))
            if not trade_id:
                continue
            if conn.execute(
                "SELECT 1 FROM hl_seen_trades WHERE trade_id = ?", (trade_id,)
            ).fetchone():
                continue

            now_str = datetime.now(timezone(timedelta(hours=1))).strftime("%Y-%m-%d %H:%M")
            conn.execute(
                "INSERT OR IGNORE INTO hl_seen_trades VALUES (?, ?, ?, ?)",
                (trade_id, coin, notional, now_str)
            )

            oi_usd      = float(ctx.get("openInterest", 0)) * float(ctx.get("markPx", 0))
            funding_pct = float(ctx.get("funding", 0)) * 100
            side_label  = "🟢买多" if t["side"] == "B" else "🔴卖空"
            trade_time  = datetime.fromtimestamp(
                int(t["time"]) / 1000, tz=timezone(timedelta(hours=1))
            ).strftime("%H:%M:%S")

            alerts.append({
                "coin": coin, "side": side_label,
                "px": float(t["px"]), "sz": float(t["sz"]),
                "notional": notional, "trade_time": trade_time,
                "oi_usd": oi_usd, "funding_pct": funding_pct,
            })

        time.sleep(0.15)

    conn.commit()

    if alerts:
        alerts.sort(key=lambda x: x["notional"], reverse=True)
        now = datetime.now(timezone(timedelta(hours=1)))
        lines = [
            f"🐋 **Hyperliquid 鲸鱼入场**",
            f"⏰ {now.strftime('%Y-%m-%d %H:%M')} BST",
            f"━━━━━━━━━━━━━━━━━━",
            f"收筹池内检测到 {len(alerts)} 笔大单（>{format_usd(HL_MIN_NOTIONAL)}）",
            "",
        ]
        for a in alerts:
            lines += [
                f"{a['side']} **{a['coin']}** | {format_usd(a['notional'])} | {a['trade_time']}",
                f"  价格 ${a['px']:.6g} × {a['sz']:.2f} 张",
                f"  OI {format_usd(a['oi_usd'])} | 费率 {a['funding_pct']:.4f}%",
                f"  ⚠️ 收筹池 + HL 大单 = 最强信号！",
                "",
            ]
        send_telegram("\n".join(lines))
        print(f"  ✅ 发现 {len(alerts)} 笔大单，已推送")
    else:
        print("  ✅ 无新 HL 大单")

    return alerts


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    
    print(f"🏦 庄家收筹雷达 v1 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   模式: {mode}\n")
    
    conn = init_db()
    
    if mode in ("full", "pool"):
        # === 模块A: 更新收筹标的池 ===
        results = scan_accumulation_pool()
        
        if results:
            save_watchlist(conn, results)
            report = build_pool_report(results)
            if report:
                send_telegram(report)
    
    if mode in ("full", "oi"):
        # === 综合扫描：OI + 费率 + 收筹 三维合一 ===
        watchlist = load_watchlist_symbols(conn)
        
        if not watchlist:
            print("⚠️ 标的池为空，先运行 pool 模式")
            conn.close()
            return
        
        # 1. 拿全市场费率+行情
        tickers_raw = api_get("/fapi/v1/ticker/24hr")
        premiums_raw = api_get("/fapi/v1/premiumIndex")
        
        if not tickers_raw or not premiums_raw:
            print("❌ API失败")
            conn.close()
            return
        
        ticker_map = {}
        for t in tickers_raw:
            if t["symbol"].endswith("USDT"):
                ticker_map[t["symbol"]] = {
                    "px_chg": float(t["priceChangePercent"]),
                    "vol": float(t["quoteVolume"]),
                    "price": float(t["lastPrice"]),
                }
        
        funding_map = {}
        for p in premiums_raw:
            if p["symbol"].endswith("USDT"):
                funding_map[p["symbol"]] = float(p["lastFundingRate"])
        
        # 1.5 拉真实流通市值（币安现货API，一次全量）
        mcap_map = {}  # coin名 -> marketCap
        try:
            import requests as _req
            _r = _req.get("https://www.binance.com/bapi/composite/v1/public/marketing/symbol/list", timeout=10)
            if _r.status_code == 200:
                for item in _r.json().get("data", []):
                    name = item.get("name", "")
                    mc = item.get("marketCap", 0)
                    if name and mc:
                        mcap_map[name] = float(mc)
                print(f"✅ 拉到 {len(mcap_map)} 个币的真实市值")
        except Exception as e:
            print(f"⚠️ 市值API失败，走fallback: {e}")
        
        # 2. 拉热度数据（CoinGecko Trending + 成交量暴增）
        heat_map = {}  # coin名 -> heat_score (0-100)
        cg_trending = set()
        try:
            import requests as _req
            _r = _req.get("https://api.coingecko.com/api/v3/search/trending", timeout=10)
            if _r.status_code == 200:
                for item in _r.json().get("coins", []):
                    sym = item["item"]["symbol"].upper()
                    rank = item["item"].get("score", 99)
                    cg_trending.add(sym)
                    heat_map[sym] = heat_map.get(sym, 0) + max(50 - rank * 3, 10)  # top1=50分, top10=20分
                print(f"🔥 CoinGecko Trending: {len(cg_trending)}个币")
        except Exception as e:
            print(f"⚠️ CG Trending失败: {e}")
        
        # 成交量暴增检测（24hVol vs 5日均Vol）
        vol_surge_coins = set()
        for sym, tk in ticker_map.items():
            coin = sym.replace("USDT", "")
            vol_24h = tk["vol"]
            # 快速拿5天均量（用ticker的数据粗估，精确版在后面OI扫描时补充）
            # 这里先标记vol > $20M的为候选
            if vol_24h > 20_000_000:
                kl = api_get("/fapi/v1/klines", {"symbol": sym, "interval": "1d", "limit": 6})
                if kl and len(kl) >= 5:
                    avg_5d = sum(float(k[7]) for k in kl[:-1]) / (len(kl)-1)
                    if avg_5d > 0:
                        ratio = vol_24h / avg_5d
                        if ratio >= 2.5:  # 成交量放大2.5倍以上
                            vol_surge_coins.add(coin)
                            heat_map[coin] = heat_map.get(coin, 0) + min(ratio * 10, 50)  # 最高50分
                    import time; time.sleep(0.05)
        
        print(f"📈 成交量暴增(≥2.5x): {len(vol_surge_coins)}个币")
        # 双重热度
        dual_heat = cg_trending & vol_surge_coins
        if dual_heat:
            for coin in dual_heat:
                heat_map[coin] = heat_map.get(coin, 0) + 20  # 双重信号bonus
            print(f"🔥🔥 双重热度: {dual_heat}")
        
        # 3. 从DB读收筹数据
        c2 = conn.cursor()
        c2.execute("SELECT symbol, score, sideways_days, range_pct, avg_vol, status FROM watchlist")
        pool_map = {}
        for row in c2.fetchall():
            pool_map[row[0]] = {"pool_score": row[1], "sideways_days": row[2], "range_pct": row[3], "avg_vol": row[4], "status": row[5]}
        
        # 3. 扫OI（标的池中放量的 + Top100）
        scan_syms = set()
        for sym, pd in pool_map.items():
            if "放量" in pd.get("status", "") or "开始" in pd.get("status", ""):
                scan_syms.add(sym)
        top_by_vol = sorted(ticker_map.items(), key=lambda x: x[1]["vol"], reverse=True)[:100]
        for sym, _ in top_by_vol:
            scan_syms.add(sym)
        
        oi_map = {}
        for i, sym in enumerate(scan_syms):
            oi_hist = api_get("/futures/data/openInterestHist", {"symbol": sym, "period": "1h", "limit": 6})
            if oi_hist and len(oi_hist) >= 2:
                curr = float(oi_hist[-1]["sumOpenInterestValue"])
                prev_1h = float(oi_hist[-2]["sumOpenInterestValue"])
                prev_6h = float(oi_hist[0]["sumOpenInterestValue"])
                d1h = ((curr - prev_1h) / prev_1h * 100) if prev_1h > 0 else 0
                d6h = ((curr - prev_6h) / prev_6h * 100) if prev_6h > 0 else 0
                circ_supply = float(oi_hist[-1].get("CMCCirculatingSupply", 0))
                oi_map[sym] = {"oi_usd": curr, "d1h": d1h, "d6h": d6h, "circ_supply": circ_supply}
            if (i+1) % 10 == 0:
                import time; time.sleep(0.5)
        
        # 4. 三策略独立评分
        
        # 共用数据预处理
        all_syms = set(list(pool_map.keys()) + list(oi_map.keys()))
        coin_data = {}
        for sym in all_syms:
            tk = ticker_map.get(sym, {})
            if not tk: continue
            pool = pool_map.get(sym, {})
            oi = oi_map.get(sym, {})
            fr = funding_map.get(sym, 0)
            coin = sym.replace("USDT", "")
            
            d6h = oi.get("d6h", 0)
            fr_pct = fr * 100
            oi_usd = oi.get("oi_usd", 0)
            # 真实流通市值：优先现货API，fallback合约OI接口的CMC数据，最后粗估
            if coin in mcap_map:
                est_mcap = mcap_map[coin]
            else:
                circ_supply = oi.get("circ_supply", 0)
                price = tk.get("price", 0) if isinstance(tk, dict) else 0
                if circ_supply > 0 and price > 0:
                    est_mcap = circ_supply * price
                else:
                    est_mcap = max(tk["vol"] * 0.3, oi_usd * 2) if oi_usd > 0 else tk["vol"] * 0.3
            sw_days = pool.get("sideways_days", 0) if pool else 0
            pool_sc = pool.get("pool_score", 0) if pool else 0
            
            heat = heat_map.get(coin, 0)
            
            coin_data[sym] = {
                "coin": coin, "sym": sym,
                "px_chg": tk["px_chg"], "vol": tk["vol"],
                "fr_pct": fr_pct, "d6h": d6h,
                "oi_usd": oi_usd, "est_mcap": est_mcap,
                "sw_days": sw_days, "pool_sc": pool_sc,
                "in_pool": bool(pool), "heat": heat,
                "in_cg": coin in cg_trending,
                "vol_surge": coin in vol_surge_coins,
            }
        
        # ═══════════════════════════════════════
        # 策略1: 追多 — 纯费率排名
        # ═══════════════════════════════════════
        chase = []
        for sym, d in coin_data.items():
            if d["px_chg"] > 3 and d["fr_pct"] < -0.005 and d["vol"] > 1_000_000:
                # 查费率趋势
                fr_hist = api_get("/fapi/v1/fundingRate", {"symbol": sym, "limit": 5})
                fr_rates = [float(f["fundingRate"]) * 100 for f in fr_hist] if fr_hist else [d["fr_pct"]]
                fr_prev = fr_rates[-2] if len(fr_rates) >= 2 else d["fr_pct"]
                fr_delta = d["fr_pct"] - fr_prev
                
                trend = "🔥加速" if fr_delta < -0.05 else "⬇️变负" if fr_delta < -0.01 else "➡️" if abs(fr_delta) < 0.01 else "⬆️回升"
                
                chase.append({**d, "fr_delta": fr_delta, "trend": trend,
                              "rates": " → ".join([f"{x:.3f}" for x in fr_rates[-3:]])})
                import time; time.sleep(0.2)
        
        # 纯按费率绝对值排序（越负越前）
        chase.sort(key=lambda x: x["fr_pct"])
        
        # ═══════════════════════════════════════
        # 策略2: 综合 — 各维度均衡(各25分)
        # ═══════════════════════════════════════
        combined = []
        for sym, d in coin_data.items():
            # 费率分(25) — 越负越好
            fr = d["fr_pct"]
            if fr < -0.5: f_sc = 25
            elif fr < -0.1: f_sc = 22
            elif fr < -0.05: f_sc = 18
            elif fr < -0.03: f_sc = 14
            elif fr < -0.01: f_sc = 10
            elif fr < 0: f_sc = 5
            else: f_sc = 0
            
            # 市值分(25) — 用真实流通市值
            mc = d["est_mcap"]
            if mc > 0 and mc < 50e6: m_sc = 25
            elif mc < 100e6: m_sc = 22
            elif mc < 200e6: m_sc = 20
            elif mc < 300e6: m_sc = 17
            elif mc < 500e6: m_sc = 12
            elif mc < 1e9: m_sc = 7
            else: m_sc = 0
            
            # 横盘分(25)
            sw = d["sw_days"]
            if sw >= 120: s_sc = 25
            elif sw >= 90: s_sc = 22
            elif sw >= 75: s_sc = 18
            elif sw >= 60: s_sc = 14
            elif sw >= 45: s_sc = 10
            else: s_sc = 0
            
            # OI分(25)
            abs6 = abs(d["d6h"])
            if abs6 >= 15: o_sc = 25
            elif abs6 >= 8: o_sc = 22
            elif abs6 >= 5: o_sc = 18
            elif abs6 >= 3: o_sc = 14
            elif abs6 >= 2: o_sc = 10
            else: o_sc = 0
            
            total = f_sc + m_sc + s_sc + o_sc
            if total < 25: continue
            
            combined.append({**d, "total": total,
                            "f_sc": f_sc, "m_sc": m_sc, "s_sc": s_sc, "o_sc": o_sc})
        
        combined.sort(key=lambda x: x["total"], reverse=True)
        
        # ═══════════════════════════════════════
        # 策略3: 埋伏 — 市值>OI>横盘>费率
        # ═══════════════════════════════════════
        ambush = []
        for sym, d in coin_data.items():
            if not d["in_pool"]: continue  # 必须在收筹池
            if d["px_chg"] > 50: continue  # 已经暴涨的排除
            
            # 1.市值(35分) — 核心！越低越好（真实流通市值）
            mc = d["est_mcap"]
            if mc > 0 and mc < 50e6: m_sc = 35
            elif mc < 100e6: m_sc = 32
            elif mc < 150e6: m_sc = 28
            elif mc < 200e6: m_sc = 25
            elif mc < 300e6: m_sc = 20
            elif mc < 500e6: m_sc = 12
            elif mc < 1e9: m_sc = 5
            else: m_sc = 0
            
            # 2.OI异动(30分) — OI涨+市值低=极好
            abs6 = abs(d["d6h"])
            if abs6 >= 10: o_sc = 30
            elif abs6 >= 5: o_sc = 25
            elif abs6 >= 3: o_sc = 20
            elif abs6 >= 2: o_sc = 14
            elif abs6 >= 1: o_sc = 8
            else: o_sc = 0
            # 暗流加分：OI涨但价格平
            if d["d6h"] > 2 and abs(d["px_chg"]) < 5:
                o_sc = min(o_sc + 5, 30)
            
            # 3.横盘(20分)
            sw = d["sw_days"]
            if sw >= 120: s_sc = 20
            elif sw >= 90: s_sc = 17
            elif sw >= 75: s_sc = 14
            elif sw >= 60: s_sc = 10
            elif sw >= 45: s_sc = 6
            else: s_sc = 0
            
            # 4.负费率(15分) — 有负费率是bonus
            fr = d["fr_pct"]
            if fr < -0.1: f_sc = 15
            elif fr < -0.05: f_sc = 12
            elif fr < -0.03: f_sc = 9
            elif fr < -0.01: f_sc = 6
            elif fr < 0: f_sc = 3
            else: f_sc = 0
            
            total = m_sc + o_sc + s_sc + f_sc
            if total < 20: continue
            
            ambush.append({**d, "total": total,
                          "m_sc": m_sc, "o_sc": o_sc, "s_sc": s_sc, "f_sc": f_sc})
        
        ambush.sort(key=lambda x: x["total"], reverse=True)
        
        # ═══════════════════════════════════════
        # 5. 预先构建热度榜 + OHLC 入场时机扫描
        # ═══════════════════════════════════════
        hot_coins = sorted(
            [d for d in coin_data.values() if d["heat"] > 0],
            key=lambda x: x["heat"], reverse=True
        )

        # 收集所有上榜的 symbol（热度前8 + 追多前8 + 综合前15 + 埋伏前15）
        timing_syms = set()
        for s in hot_coins[:8]:  timing_syms.add(s["sym"])
        for s in chase[:8]:      timing_syms.add(s["sym"])
        for s in combined[:15]:  timing_syms.add(s["sym"])
        for s in ambush[:15]:    timing_syms.add(s["sym"])

        print(f"  ⏱ OHLC时机扫描 {len(timing_syms)} 个上榜币...")
        timing_map = {}  # sym -> timing dict
        for sym in timing_syms:
            timing_map[sym] = get_ohlc_timing(sym)
            time.sleep(0.1)

        # ═══════════════════════════════════════
        # 6. 生成推送 + 值得关注提醒
        # ═══════════════════════════════════════
        def mcap_str(v):
            if v >= 1e6: return f"${v/1e6:.0f}M"
            if v >= 1e3: return f"${v/1e3:.0f}K"
            return f"${v:.0f}"

        def timing_tag(sym):
            t = timing_map.get(sym)
            return t["label"] if t else ""

        now = datetime.now(timezone(timedelta(hours=1)))
        lines = [
            f"🏦 **庄家雷达** 三策略+热度",
            f"⏰ {now.strftime('%Y-%m-%d %H:%M')} BST",
        ]

        # 表0: 热度榜
        if hot_coins:
            lines.append(f"\n🔥 **热度榜** (CG趋势+成交量暴增)")
            for s in hot_coins[:8]:
                tags = []
                if s["in_cg"]: tags.append("🌐CG热搜")
                if s["vol_surge"]: tags.append("📈放量")
                oi_tag = f"OI{s['d6h']:+.0f}%" if abs(s["d6h"]) >= 3 else ""
                if oi_tag: tags.append(f"⚡{oi_tag}")
                if s["in_pool"]: tags.append(f"💤池{s['sw_days']}天")
                fr_tag = f"🧊{s['fr_pct']:.2f}%" if s["fr_pct"] < -0.03 else ""
                if fr_tag: tags.append(fr_tag)
                tt = timing_tag(s["sym"])
                lines.append(
                    f"  {s['coin']:<8} ~{mcap_str(s['est_mcap'])} 涨{s['px_chg']:+.0f}% | {' '.join(tags)} {tt}"
                )

        # 表1: 追多
        lines.append(f"\n🔥 **追多** (按费率排名)")
        if chase:
            for s in chase[:8]:
                tt = timing_tag(s["sym"])
                lines.append(
                    f"  {s['coin']:<7} 费率{s['fr_pct']:+.3f}% {s['trend']}"
                    f" | 涨{s['px_chg']:+.0f}% | ~{mcap_str(s['est_mcap'])} {tt}"
                )
        else:
            lines.append("  暂无（需涨>3%+费率负）")

        # 表2: 综合
        lines.append(f"\n📊 **综合** (费率+市值+横盘+OI 各25)")
        for s in combined[:8]:
            dims = []
            if s["f_sc"] >= 10: dims.append(f"🧊{s['fr_pct']:.2f}%")
            if s["m_sc"] >= 12: dims.append(f"💎{mcap_str(s['est_mcap'])}")
            if s["s_sc"] >= 10: dims.append(f"💤{s['sw_days']}天")
            if s["o_sc"] >= 10: dims.append(f"⚡OI{s['d6h']:+.0f}%")
            tt = timing_tag(s["sym"])
            lines.append(
                f"  {s['coin']:<7} {s['total']}分 | {' '.join(dims)} {tt}"
            )

        # 表3: 埋伏
        lines.append(f"\n🎯 **埋伏** (市值35+OI30+横盘20+费率15)")
        for s in ambush[:8]:
            tags = [f"~{mcap_str(s['est_mcap'])}"]
            if abs(s["d6h"]) >= 2: tags.append(f"OI{s['d6h']:+.0f}%")
            if s["d6h"] > 2 and abs(s["px_chg"]) < 5: tags.append("🎯暗流")
            if s["sw_days"] >= 45: tags.append(f"横盘{s['sw_days']}天")
            if s["fr_pct"] < -0.01: tags.append(f"费率{s['fr_pct']:.2f}%")
            tt = timing_tag(s["sym"])
            lines.append(
                f"  {s['coin']:<7} {s['total']}分 | {' '.join(tags)} {tt}"
            )

        # 表4: 综合×埋伏 双榜交集 — 按均分排序
        combined_score_map = {s["coin"]: s["total"] for s in combined}
        ambush_score_map   = {s["coin"]: s["total"] for s in ambush}
        dual_coins = set(combined_score_map) & set(ambush_score_map)
        if dual_coins:
            dual = sorted(
                [{"coin": c,
                  "c_sc": combined_score_map[c],
                  "a_sc": ambush_score_map[c],
                  "avg":  (combined_score_map[c] + ambush_score_map[c]) / 2,
                  "sym":  next(s["sym"] for s in combined if s["coin"] == c)}
                 for c in dual_coins],
                key=lambda x: x["avg"], reverse=True
            )
            lines.append(f"\n🏆 **双榜精选** (综合+埋伏均分，共{len(dual)}个)")
            for d in dual[:8]:
                tt = timing_tag(d["sym"])
                lines.append(
                    f"  {d['coin']:<8} 均分{d['avg']:.0f} | 综合{d['c_sc']}分 埋伏{d['a_sc']}分 {tt}"
                )

        # 表5: 入场时机详情（只展示🟢太早 和 🟡入场/观察 的币，过滤掉🔴🟫）
        early_entry = [
            (sym, t) for sym, t in timing_map.items()
            if t["label"] in ("🟢太早", "🟡入场", "🟡观察")
        ]
        late_dump = [
            (sym, t) for sym, t in timing_map.items()
            if t["label"] in ("🔴太晚", "⚫转跌")
        ]

        if early_entry or late_dump:
            lines.append(f"\n⏱ **入场时机** (1h K线确认)")
            if early_entry:
                lines.append("  可关注:")
                for sym, t in sorted(early_entry, key=lambda x: x[1]["label"]):
                    coin = sym.replace("USDT", "")
                    lines.append(f"    {t['label']} {coin:<8} {t['reason']}  (MA25偏离{t['ext_pct']:+.0f}% 量比{t['vol_ratio']}x K{t['trend3']})")
            if late_dump:
                lines.append("  已过时/转跌，谨慎:")
                for sym, t in late_dump:
                    coin = sym.replace("USDT", "")
                    lines.append(f"    {t['label']} {coin:<8} {t['reason']}")
        
        # ═══ 值得关注提醒 ═══
        highlights = []
        
        # 热度+收筹池重叠 = 最强信号（放最前面！热度领先OI）
        hot_pool = [d for d in coin_data.values() if d["heat"] > 0 and d["in_pool"]]
        for s in sorted(hot_pool, key=lambda x: x["heat"], reverse=True)[:2]:
            tags = []
            if s["in_cg"]: tags.append("CG热搜")
            if s["vol_surge"]: tags.append("放量")
            highlights.append(f"🔥💤 {s['coin']} 热度({'+'.join(tags)})+收筹{s['sw_days']}天=OI将涨")
        
        # 热度+OI已经在涨 = 正在发生
        hot_oi = [d for d in coin_data.values() if d["heat"] > 0 and d["d6h"] > 5]
        for s in sorted(hot_oi, key=lambda x: x["d6h"], reverse=True)[:2]:
            if s["coin"] not in " ".join(highlights):
                highlights.append(f"🔥⚡ {s['coin']} 热度+OI{s['d6h']:+.0f}%双涨！")
        
        # 追多里费率加速恶化的前2
        chase_fire = [s for s in chase[:5] if "加速" in s.get("trend", "")]
        for s in chase_fire[:2]:
            highlights.append(f"🔥 {s['coin']} 费率{s['fr_pct']:.3f}%加速恶化，空头涌入中")
        
        # 三个表都出现的币
        chase_coins = set(s["coin"] for s in chase[:10])
        combined_coins = set(s["coin"] for s in combined[:10])
        
        # 追多+综合都出现
        overlap_2 = chase_coins & combined_coins
        if overlap_2:
            for c in list(overlap_2)[:2]:
                highlights.append(f"⭐ {c} 追多+综合双榜上榜")
        
        # 埋伏里OI暗流涌动的
        ambush_dark = [s for s in ambush[:10] if s["d6h"] > 2 and abs(s["px_chg"]) < 5]
        for s in ambush_dark[:2]:
            highlights.append(f"🎯 {s['coin']} 暗流！OI{s['d6h']:+.0f}%但价格没动，市值仅{mcap_str(s['est_mcap'])}")
        
        # 埋伏里市值极低+OI异动的
        ambush_gem = [s for s in ambush[:10] if s["est_mcap"] < 100e6 and abs(s["d6h"]) >= 3]
        for s in ambush_gem[:2]:
            if s["coin"] not in [h.split(" ")[1] for h in highlights]:
                highlights.append(f"💎 {s['coin']} 低市值{mcap_str(s['est_mcap'])}+OI{s['d6h']:+.0f}%，埋伏首选")
        
        if highlights:
            lines.append(f"\n💡 **值得关注**")
            for h in highlights[:7]:
                lines.append(f"  {h}")
        
        # 图例说明
        lines.append(f"\n📖 **图例**")
        lines.append("  🔥热度=CG热搜+成交量暴增(OI领先指标)")
        lines.append("  费率负=空头燃料 | 💎市值 | 💤横盘(收筹)")
        lines.append("  🔥💤热度+收筹=最强预判 | 🔥⚡热度+OI=正在发生")
        lines.append("  ⏱时机: 🟢太早=底部蓄势 🟡入场=突破初期 🔴太晚=高位 ⚫转跌=已反转")
        
        report = "\n".join(lines)
        send_telegram(report)

        # === OI 暗流背离扫描（全市场）===
        pool_syms = set(pool_map.keys())
        div_results = scan_oi_divergence(ticker_map, funding_map, pool_syms)
        div_report = build_divergence_report(div_results)
        if div_report:
            send_telegram(div_report)

        # === Hyperliquid 鲸鱼扫描（收筹池内币，65分钟时间窗口）===
        pool_coin_names = {sym.replace("USDT", "") for sym in pool_map.keys()}
        scan_hyperliquid_whales(conn, pool_coin_names)

    conn.close()
    print("\n✅ 完成")


if __name__ == "__main__":
    main()
