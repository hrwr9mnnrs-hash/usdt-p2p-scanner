
#!/usr/bin/env python3
"""
USDT INR P2P Scanner — Binance-first, read-only.

What it does:
- Fetches current Binance P2P INR/USDT ads using the public P2P search endpoint.
- Checks whether ads are actually usable for the configured INR capital.
- Builds a depth-aware blended BUY and SELL price.
- Calculates gross spread, estimated costs, net profit and ROI.
- Logs snapshots to CSV so you can later analyze the best hours/days.
- Never places trades and never asks for API keys.

IMPORTANT:
The Binance P2P search endpoint used here is an undocumented/internal web endpoint.
It can change without notice. Treat the output as a research/alert tool, not an
execution guarantee. Always re-open the ad in Binance before trading.

Security:
- Do NOT put Binance API keys, passwords, OTPs or seed phrases in this program.
- Do NOT automate payment/release of P2P orders.
"""

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

MGS_BASE = "https://www.binance.com"
AD_LIST_ENDPOINT = MGS_BASE + "/bapi/c2c/v1/public/c2c/agent/ad-list"
TRADE_METHODS_ENDPOINT = MGS_BASE + "/bapi/c2c/v1/public/c2c/agent/trade-methods"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; USDT-P2P-Research-Scanner/2.0)",
    "Accept": "application/json, text/plain, */*",
}

@dataclass
class Ad:
    side: str
    price: float
    available_usdt: float
    min_inr: float
    max_inr: float
    nick: str
    completion_pct: float
    completed_orders: int
    methods: list[str]
    merchant: bool
    shield: bool

def _unwrap(payload):
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        for key in ("ads", "data", "items", "tradeMethods"):
            if isinstance(data.get(key), list):
                return data[key]
    return data if isinstance(data, list) else []

def fetch_trade_methods() -> dict[str, str]:
    r = requests.get(
        TRADE_METHODS_ENDPOINT,
        params={"fiat": "INR"},
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    items = _unwrap(r.json())
    return {
        str(x["identifier"]): str(x.get("tradeMethodName") or x["identifier"])
        for x in items
        if isinstance(x, dict) and x.get("identifier")
    }

def fetch_ads(side: str, cfg: dict[str, Any]) -> list[Ad]:
    methods_map = fetch_trade_methods()
    requested = cfg.get("payment_methods") or []

    identifiers = []
    for wanted in requested:
        wanted = str(wanted)
        if wanted in methods_map:
            identifiers.append(wanted)
            continue
        for ident, name in methods_map.items():
            if wanted.lower() in (ident.lower(), name.lower()):
                identifiers.append(ident)
                break

    params = [
        ("fiat", "INR"),
        ("asset", "USDT"),
        ("tradeType", side),
        ("limit", min(int(cfg.get("rows", 20)), 20)),
        ("order", "price"),
    ]
    # Binance documents tradeMethodIdentifiers as repeated query parameters,
    # not a JSON array.
    for ident in identifiers:
        params.append(("tradeMethodIdentifiers", ident))

    r = requests.get(
        AD_LIST_ENDPOINT,
        params=params,
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    items = _unwrap(r.json())

    ads = []
    for item in items:
        if not isinstance(item, dict):
            continue
        adv = item.get("adv", item)
        advertiser = item.get("advertiser", item.get("merchant", {}))

        def pick(*keys, default=None):
            for obj in (adv, item, advertiser):
                if isinstance(obj, dict):
                    for key in keys:
                        val = obj.get(key)
                        if val not in (None, ""):
                            return val
            return default

        try:
            raw_methods = pick("tradeMethods", "tradeMethodIdentifiers", default=[]) or []
            methods = []
            for m in raw_methods:
                if isinstance(m, dict):
                    m = m.get("identifier") or m.get("tradeMethodName") or m.get("name")
                if m:
                    methods.append(str(m))

            ads.append(Ad(
                side=side,
                price=float(pick("price")),
                available_usdt=float(pick("surplusAmount", "tradableQuantity", "availableAmount", default=0)),
                min_inr=float(pick("minSingleTransAmount", "minAmount", "minLimit", default=0)),
                max_inr=float(pick("maxSingleTransAmount", "dynamicMaxSingleTransAmount", "maxAmount", "maxLimit", default=0)),
                nick=str(pick("nickName", "merchantNickName", "nick", default="unknown")),
                completion_pct=float(pick("monthFinishRate", "completionRate", "finishRate", default=0) or 0),
                completed_orders=int(float(pick("monthOrderCount", "completedOrderNum", "orderCount", default=0) or 0)),
                methods=methods,
                merchant=bool(pick("userType", default="") == "merchant"),
                shield=bool(pick("isShieldMerchant", default=False)),
            ))
        except (TypeError, ValueError, KeyError):
            continue

    return ads


def method_ok(ad: Ad, methods: list[str]) -> bool:
    if not methods:
        return True
    if not ad.methods:
        return False
    wanted = {m.lower() for m in methods}
    return any(m.lower() in wanted for m in ad.methods)

def filter_ads(ads: list[Ad], cfg: dict[str, Any]) -> list[Ad]:
    out = []
    for a in ads:
        if a.price <= 0 or a.available_usdt <= 0:
            continue
        if a.completion_pct < float(cfg["min_completion_pct"]):
            continue
        if a.completed_orders < int(cfg["min_completed_orders"]):
            continue
        if not method_ok(a, cfg["payment_methods"]):
            continue
        out.append(a)
    return out

def executable_depth(ads: list[Ad], capital_inr: float, side: str):
    """
    For BUY: we spend INR and receive USDT.
    For SELL: we sell USDT whose notional is limited by capital INR.
    We greedily consume best price first, respecting each ad's INR limits and
    available USDT. This is a conservative depth calculation.
    """
    if side == "BUY":
        ads = sorted(ads, key=lambda a: a.price)  # cheapest first
    else:
        ads = sorted(ads, key=lambda a: a.price, reverse=True)  # highest first

    remaining_inr = capital_inr
    total_usdt = 0.0
    total_inr = 0.0
    fills = []

    for a in ads:
        if remaining_inr <= 0:
            break

        low = max(a.min_inr, 0.0)
        high = min(a.max_inr if a.max_inr > 0 else capital_inr,
                   a.available_usdt * a.price)

        # We cannot use an ad if even its minimum exceeds the remaining capital.
        if high < low or remaining_inr < low:
            continue

        notional = min(remaining_inr, high)
        usdt = notional / a.price

        total_inr += notional
        total_usdt += usdt
        remaining_inr -= notional
        fills.append((a, notional, usdt))

    if total_inr <= 0:
        return None

    return {
        "total_inr": total_inr,
        "total_usdt": total_usdt,
        "avg_price": total_inr / total_usdt,
        "fills": fills,
    }

def calculate(cfg, buy_depth, sell_depth):
    # We must use the same USDT amount on both legs. If SELL depth cannot
    # absorb all USDT, scale the cycle down to the sellable amount.
    buy_usdt = buy_depth["total_usdt"]
    sell_usdt = sell_depth["total_usdt"]

    cycle_usdt = min(buy_usdt, sell_usdt)
    if cycle_usdt <= 0:
        return None

    # Recompute approximate INR cost/proceeds at the depth-average prices.
    buy_inr = cycle_usdt * buy_depth["avg_price"]
    sell_inr = cycle_usdt * sell_depth["avg_price"]

    gross = sell_inr - buy_inr
    variable = (buy_inr + sell_inr) * float(cfg["variable_cost_pct"]) / 100.0
    fixed = float(cfg["fixed_cost_inr"])
    net = gross - variable - fixed
    roi = (net / buy_inr * 100.0) if buy_inr else 0.0

    return {
        "cycle_usdt": cycle_usdt,
        "buy_inr": buy_inr,
        "sell_inr": sell_inr,
        "buy_price": buy_depth["avg_price"],
        "sell_price": sell_depth["avg_price"],
        "gross": gross,
        "variable_cost": variable,
        "fixed_cost": fixed,
        "net": net,
        "roi": roi,
    }

def save_snapshot(path: str, result: dict[str, Any]):
    fields = [
        "timestamp_ist", "capital_inr", "buy_price", "sell_price",
        "spread_inr", "spread_pct", "cycle_usdt", "buy_inr", "sell_inr",
        "gross", "variable_cost", "fixed_cost", "net", "roi",
        "signal", "confidence", "buy_fills", "sell_fills"
    ]
    exists = Path(path).exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow({k: result.get(k, "") for k in fields})

def evaluate(cfg):
    now = datetime.now().astimezone()
    try:
        buy_raw = fetch_ads("BUY", cfg)
        sell_raw = fetch_ads("SELL", cfg)
    except Exception as e:
        print(f"\n⚠️ UNABLE TO VERIFY — DO NOT TRADE\nData fetch failed: {e}")
        return

    buy_ads = filter_ads(buy_raw, cfg)
    sell_ads = filter_ads(sell_raw, cfg)

    buy_depth = executable_depth(buy_ads, float(cfg["capital_inr"]), "BUY")
    sell_depth = executable_depth(sell_ads, float(cfg["capital_inr"]), "SELL")

    print("\n" + "=" * 78)
    print(f"USDT INR P2P SCANNER | {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("=" * 78)

    if not buy_depth or not sell_depth:
        print("⚠️ UNABLE TO VERIFY — DO NOT TRADE")
        print("Insufficient usable advertiser depth for the configured capital.")
        return

    result = calculate(cfg, buy_depth, sell_depth)
    if not result:
        print("🔴 NO TRADE")
        return

    spread = result["sell_price"] - result["buy_price"]
    spread_pct = spread / result["buy_price"] * 100.0

    # Simple confidence model. It deliberately does not claim execution certainty.
    confidence = 90
    if len(buy_depth["fills"]) > 3 or len(sell_depth["fills"]) > 3:
        confidence -= 15
    if result["net"] < float(cfg["target_min_profit_inr"]):
        confidence -= 15

    signal = "🟢 TRADE CANDIDATE" if result["net"] >= float(cfg["target_min_profit_inr"]) else "🔴 NO TRADE"

    print(f"Capital:              ₹{cfg['capital_inr']:,.2f}")
    print(f"Executable USDT:      {result['cycle_usdt']:,.4f}")
    print(f"Blended BUY price:    ₹{result['buy_price']:,.4f}")
    print(f"Blended SELL price:   ₹{result['sell_price']:,.4f}")
    print(f"Gross spread:         ₹{spread:,.4f}/USDT ({spread_pct:.3f}%)")
    print(f"Gross profit:         ₹{result['gross']:,.2f}")
    print(f"Identifiable costs:   ₹{result['variable_cost'] + result['fixed_cost']:,.2f}")
    print(f"Estimated net profit: ₹{result['net']:,.2f}")
    print(f"Estimated net ROI:    {result['roi']:.3f}%")
    print(f"Signal:               {signal}")
    print(f"Confidence:           {max(0, confidence)}%")
    print("\nBUY fills:")
    for a, notional, usdt in buy_depth["fills"]:
        print(f"  {a.nick:20} ₹{a.price:.4f} | ₹{notional:,.0f} | {usdt:,.4f} USDT | "
              f"limit ₹{a.min_inr:,.0f}-₹{a.max_inr:,.0f} | {a.completion_pct:.1f}%")
    print("SELL fills:")
    for a, notional, usdt in sell_depth["fills"]:
        print(f"  {a.nick:20} ₹{a.price:.4f} | ₹{notional:,.0f} | {usdt:,.4f} USDT | "
              f"limit ₹{a.min_inr:,.0f}-₹{a.max_inr:,.0f} | {a.completion_pct:.1f}%")

    snapshot = {
        "timestamp_ist": now.isoformat(),
        "capital_inr": cfg["capital_inr"],
        "buy_price": result["buy_price"],
        "sell_price": result["sell_price"],
        "spread_inr": spread,
        "spread_pct": spread_pct,
        "cycle_usdt": result["cycle_usdt"],
        "buy_inr": result["buy_inr"],
        "sell_inr": result["sell_inr"],
        "gross": result["gross"],
        "variable_cost": result["variable_cost"],
        "fixed_cost": result["fixed_cost"],
        "net": result["net"],
        "roi": result["roi"],
        "signal": signal,
        "confidence": max(0, confidence),
        "buy_fills": len(buy_depth["fills"]),
        "sell_fills": len(sell_depth["fills"]),
    }
    save_snapshot(cfg["log_file"], snapshot)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, help="INR capital, e.g. 50000")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--watch", action="store_true", help="Repeat at configured interval")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.capital:
        cfg["capital_inr"] = args.capital

    if args.watch:
        while True:
            evaluate(cfg)
            print(f"\nNext scan in {cfg['poll_seconds']} seconds. Ctrl+C to stop.")
            time.sleep(int(cfg["poll_seconds"]))
    else:
        evaluate(cfg)

if __name__ == "__main__":
    main()
