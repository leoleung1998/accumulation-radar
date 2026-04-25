#!/usr/bin/env python3
import hashlib
import hmac
import os
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

# Load .env.oi
env_file = Path(__file__).parent / ".env.oi"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

SAPI = "https://api.binance.com"
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
MIN_VALUE_USD = 1.0

app = FastAPI()

# Simple in-memory cache for avg prices (avoids hammering trade history API)
_avg_cache: dict[str, tuple[float | None, float]] = {}
CACHE_TTL = 300  # 5 minutes


def signed_get(endpoint: str, params: dict = None):
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    query = urlencode(params)
    sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{SAPI}{endpoint}?{query}&signature={sig}"
    r = requests.get(url, headers={"X-MBX-APIKEY": API_KEY}, timeout=10)
    r.raise_for_status()
    return r.json()


def public_get(endpoint: str, params: dict = None):
    r = requests.get(f"{SAPI}{endpoint}", params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def calc_avg_price(trades: list) -> float | None:
    """Weighted average entry price accounting for partial sells."""
    qty = 0.0
    cost = 0.0
    for t in sorted(trades, key=lambda x: x["time"]):
        amount = float(t["qty"])
        price = float(t["price"])
        if t["isBuyer"]:
            cost += amount * price
            qty += amount
        else:
            if qty > 0:
                avg = cost / qty
                cost -= avg * amount
                qty -= amount
    if qty < 1e-10:
        return None
    return cost / qty


def get_avg_price(asset: str) -> float | None:
    cached = _avg_cache.get(asset)
    if cached and time.time() - cached[1] < CACHE_TTL:
        return cached[0]
    try:
        trades = signed_get("/api/v3/myTrades", {"symbol": f"{asset}USDT", "limit": 1000})
        avg = calc_avg_price(trades)
    except Exception:
        avg = None
    _avg_cache[asset] = (avg, time.time())
    return avg


@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / "static" / "index.html").read_text()


@app.get("/api/portfolio")
def portfolio():
    try:
        account = signed_get("/api/v3/account")

        balances_raw = {
            b["asset"]: {"free": float(b["free"]), "locked": float(b["locked"])}
            for b in account["balances"]
            if float(b["free"]) + float(b["locked"]) > 0
        }

        if not balances_raw:
            return JSONResponse({"ok": True, "data": {"holdings": [], "total_usdt": 0}})

        tickers = {t["symbol"]: t for t in public_get("/api/v3/ticker/24hr")}
        btc_price = float(tickers.get("BTCUSDT", {}).get("lastPrice", 0) or 0)

        STABLES = {"USDT", "USDC", "BUSD", "FDUSD", "TUSD"}
        holdings = []
        total_usdt = 0.0

        for asset, bal in balances_raw.items():
            free = bal["free"]
            locked = bal["locked"]
            total = free + locked

            if asset in STABLES:
                holdings.append({
                    "asset": asset, "total": total, "free": free, "locked": locked,
                    "price": 1.0, "value_usdt": total,
                    "avg_price": None, "pnl_usdt": None, "pnl_pct": None,
                    "change_24h_pct": 0.0,
                })
                total_usdt += total
                continue

            ticker = tickers.get(f"{asset}USDT")
            if not ticker and btc_price:
                t2 = tickers.get(f"{asset}BTC")
                if t2:
                    price = float(t2["lastPrice"]) * btc_price
                    change_24h = float(t2["priceChangePercent"])
                else:
                    continue
            elif ticker:
                price = float(ticker["lastPrice"])
                change_24h = float(ticker["priceChangePercent"])
            else:
                continue

            value = total * price
            if value < MIN_VALUE_USD:
                continue

            avg_price = get_avg_price(asset)
            if avg_price and avg_price > 0:
                pnl_usdt = (price - avg_price) * total
                pnl_pct = (price - avg_price) / avg_price * 100
            else:
                pnl_usdt = None
                pnl_pct = None

            holdings.append({
                "asset": asset, "total": total, "free": free, "locked": locked,
                "price": price, "value_usdt": value,
                "avg_price": avg_price, "pnl_usdt": pnl_usdt, "pnl_pct": pnl_pct,
                "change_24h_pct": change_24h,
            })
            total_usdt += value

        holdings.sort(key=lambda x: x["value_usdt"], reverse=True)

        return JSONResponse({"ok": True, "data": {
            "holdings": holdings,
            "total_usdt": total_usdt,
        }})

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
