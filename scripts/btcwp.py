#!/usr/bin/env python3
"""
BTC Weekend Predictor — egyfajlos valtozat.

Hasznalat:
    python scripts/btcwp.py predict    # uj hetvegi predikcio
    python scripts/btcwp.py resolve    # elozo hetvege kiertekelese

Ezt a fajlt a build_single.py generalja a scripts/btc_weekend/ modulokbol.
Kezzel ne szerkeszd -- a modulokat szerkeszd, es generald ujra.

Kimenet:
    <BTCWP_DATA_DIR>/btc-weekend.json          aktualis predikcio + pontossag
    <BTCWP_DATA_DIR>/btc-weekend-history.json  minden predikcio es kimenet
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / os.environ.get("BTCWP_DATA_DIR", "data")
CURRENT = DATA / "btc-weekend.json"
HISTORY = DATA / "btc-weekend-history.json"


def load_json(p: Path, default):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


# ===========================================================================
# LIKVIDACIOS TERKEP
# ===========================================================================

MAINT_MARGIN = 0.005  # 0.5% karbantartasi margin, BTC perp tipikus

# retail-realisztikus tokeattetel-eloszlas kripto perpeken
LEVERAGE_BUCKETS = [(10, 0.20), (25, 0.34), (50, 0.30), (100, 0.16)]

DECAY_HALFLIFE_H = 168.0  # 7 nap: ennyi ido alatt felezodik a nyitva maradas


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def build_map(klines_1h: list[dict], oi_usd: float,
              long_share: float = 0.5, n_bins: int = 90) -> dict:
    """Likvidacios terkep felepitese.

    klines_1h: [{open_time, high, low, close, volume}, ...] idorendben
    oi_usd:    aktualis teljes open interest notional USD-ben
    long_share: a nyitott pozicio hany resze long (0..1)
    """
    if not klines_1h or not oi_usd or oi_usd <= 0:
        return {"ok": False, "error": "nincs eleg adat a terkephez"}

    price = klines_1h[-1]["close"]
    n = len(klines_1h)

    # 1-2. sulyok: volumen x exponencialis ido-lecsenges
    weights = []
    for i, k in enumerate(klines_1h):
        age_h = n - 1 - i
        decay = math.exp(-math.log(2) * age_h / DECAY_HALFLIFE_H)
        weights.append(max(k.get("volume", 0.0), 1e-9) * decay)
    wsum = sum(weights) or 1.0

    long_share = _clamp(long_share, 0.15, 0.85)
    short_share = 1.0 - long_share

    # 4. likvidacios szintek generalasa
    levels: list[tuple[float, float, str]] = []  # (liq_price, notional, side)
    for k, w in zip(klines_1h, weights):
        entry = (k["high"] + k["low"] + k["close"]) / 3.0
        notional = oi_usd * (w / wsum)
        for lev, lw in LEVERAGE_BUCKETS:
            band = 1.0 / lev - MAINT_MARGIN
            if band <= 0:
                continue
            levels.append((entry * (1 - band), notional * long_share * lw, "long"))
            levels.append((entry * (1 + band), notional * short_share * lw, "short"))

    # 5. binnelés: +-25% sav az aktualis ar korul
    lo, hi = price * 0.75, price * 1.25
    step = (hi - lo) / n_bins
    bins = [{"low": lo + i * step, "high": lo + (i + 1) * step,
             "mid": lo + (i + 0.5) * step, "long": 0.0, "short": 0.0}
            for i in range(n_bins)]
    total = 0.0
    for p, notional, side in levels:
        if p < lo or p >= hi:
            continue
        bins[int((p - lo) / step)][side] += notional
        total += notional
    if total <= 0:
        return {"ok": False, "error": "minden szint a savon kivul esett"}

    for b in bins:
        b["total"] = b["long"] + b["short"]

    # ---- klaszterek: osszefuggo, atlag folotti savok osszevonva ----
    avg = total / n_bins
    clusters, cur = [], None
    for b in bins:
        if b["total"] > avg * 1.35:
            if cur is None:
                cur = {"low": b["low"], "high": b["high"], "long": 0.0, "short": 0.0}
            cur["high"] = b["high"]
            cur["long"] += b["long"]
            cur["short"] += b["short"]
        elif cur is not None:
            clusters.append(cur); cur = None
    if cur is not None:
        clusters.append(cur)

    for c in clusters:
        c["total"] = c["long"] + c["short"]
        c["mid"] = (c["low"] + c["high"]) / 2.0
        c["dist_pct"] = 100.0 * (c["mid"] / price - 1.0)
        c["side"] = "long" if c["long"] > c["short"] else "short"
        c["share_of_total"] = c["total"] / total
    clusters.sort(key=lambda c: -c["total"])

    above = [c for c in clusters if c["dist_pct"] > 0]
    below = [c for c in clusters if c["dist_pct"] <= 0]

    return {
        "ok": True,
        "source": "model",
        "price": price,
        "total_notional": total,
        "bins": [{"p": round(b["mid"]), "l": round(b["long"]), "s": round(b["short"])}
                 for b in bins if b["total"] > avg * 0.5],
        "clusters": [
            {"mid": round(c["mid"]), "dist_pct": round(c["dist_pct"], 2),
             "notional": round(c["total"]), "side": c["side"],
             "share": round(c["share_of_total"], 4)}
            for c in clusters[:10]
        ],
        "notional_above": sum(c["total"] for c in above),
        "notional_below": sum(c["total"] for c in below),
        "nearest_above": (min(above, key=lambda c: c["dist_pct"])["dist_pct"] if above else None),
        "nearest_below": (max(below, key=lambda c: c["dist_pct"])["dist_pct"] if below else None),
        "long_share_used": round(long_share, 3),
    }


def attraction_score(m: dict) -> dict:
    """Liquidity Attraction Score.

    Amit figyelembe vesz, pontosan ahogy kerted:
      - tavolsag az aktualis artol   (inverz, telitodo)
      - klaszter merete              (notional reszaranya)
      - long vs short oldal          (a short klaszterek FELFELE huznak)
      - klaszter suruseg             (keskeny sav = koncentraltabb)
      - tobb szint egyuttes koncentracioja (az osszes klaszter aggregalva)
    """
    if not m.get("ok") or not m.get("clusters"):
        return {"ok": False, "value": 0.0, "why": "nincs likvidacios terkep"}

    pull_up = pull_dn = 0.0
    for c in m["clusters"]:
        d = abs(c["dist_pct"])
        # telitodo inverz tavolsag: 1%-on belul nem no a vegtelenbe
        prox = 1.0 / (1.0 + (d / 2.2) ** 1.5)
        # a short-likvidacios klaszter felfele huz (short squeeze), a long lefele
        w = c["share"] * prox
        if c["dist_pct"] > 0:
            pull_up += w * (1.25 if c["side"] == "short" else 0.85)
        else:
            pull_dn += w * (1.25 if c["side"] == "long" else 0.85)

    tot = pull_up + pull_dn
    value = 0.0 if tot <= 0 else _clamp((pull_up - pull_dn) / tot, -1, 1) * 0.85

    na, nb = m.get("nearest_above"), m.get("nearest_below")
    why = (f"legkozelebbi klaszter +{na:.1f}% / {nb:.1f}%; "
           f"notional felette ${m['notional_above'] / 1e9:.1f}Mrd, "
           f"alatta ${m['notional_below'] / 1e9:.1f}Mrd"
           if na is not None and nb is not None else "egyoldalu klaszter-eloszlas")

    # bizalom: mennyire koncentralt a terkep (ha minden szet van kenve, gyenge jel)
    top3 = sum(c["share"] for c in m["clusters"][:3])
    conf = _clamp(0.35 + 0.65 * top3, 0.2, 0.85)
    if m.get("source") == "coinglass":
        conf = min(0.9, conf + 0.1)

    return {"ok": True, "value": value, "confidence": conf,
            "why": why + (" [CoinGlass]" if m.get("source") == "coinglass"
                          else " [sajat modell, OI + tokeattetel-savok]")}


# --------------------------------------------------------------------------
# Valos CoinGlass, ha van kulcs (Professional csomag kell hozza)
# --------------------------------------------------------------------------

def fetch_coinglass(get_fn, symbol: str = "BTC", rng: str = "24h") -> dict | None:
    key = os.environ.get("COINGLASS_API_KEY")
    if not key:
        return None
    try:
        url = ("https://open-api-v4.coinglass.com/api/futures/liquidation/"
               f"aggregated-heatmap/model2?symbol={symbol}&range={rng}")
        req = urllib.request.Request(url, headers={"CG-API-KEY": key, "accept": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode())
        data = d.get("data") or {}
        y = data.get("y_axis") or []
        lev = data.get("liquidation_leverage_data") or []
        candles = data.get("price_candlesticks") or []
        if not y or not lev or not candles:
            return None
        price = float(candles[-1][4])
        agg: dict[int, float] = {}
        for row in lev:
            yi, val = int(row[1]), float(row[2])
            agg[yi] = agg.get(yi, 0.0) + val
        total = sum(agg.values()) or 1.0
        clusters = []
        for yi, val in sorted(agg.items(), key=lambda kv: -kv[1])[:10]:
            p = float(y[yi])
            dist = 100.0 * (p / price - 1.0)
            clusters.append({"mid": round(p), "dist_pct": round(dist, 2),
                             "notional": round(val), "share": round(val / total, 4),
                             "side": "short" if dist > 0 else "long"})
        above = [c for c in clusters if c["dist_pct"] > 0]
        below = [c for c in clusters if c["dist_pct"] <= 0]
        return {
            "ok": True, "source": "coinglass", "price": price, "total_notional": total,
            "clusters": clusters, "bins": [],
            "notional_above": sum(c["notional"] for c in above),
            "notional_below": sum(c["notional"] for c in below),
            "nearest_above": (min(above, key=lambda c: c["dist_pct"])["dist_pct"] if above else None),
            "nearest_below": (max(below, key=lambda c: c["dist_pct"])["dist_pct"] if below else None),
        }
    except Exception:  # noqa: BLE001
        return None

# ===========================================================================
# ADATFORRASOK
# ===========================================================================

UA = "btc-weekend-predictor/1.0 (github actions; personal research)"
# A Reddit adatkozponti IP-rol 403-at ad; bongeszo-UA-val neha atmegy.
# Ha nem, a faktor egyszeruen kiesik es a sulya automatikusan ujraoszlik.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
TIMEOUT = 20


# --------------------------------------------------------------------------
# alap HTTP
# --------------------------------------------------------------------------

def _get(url: str, params: dict | None = None, retries: int = 3, ua: str | None = None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua or UA,
                                                      "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def _safe(fn, name: str):
    try:
        out = fn()
        out["ok"] = True
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "source": name}


# --------------------------------------------------------------------------
# 1. AR + TECHNIKAI (OKX spot gyertyak)
# --------------------------------------------------------------------------

# OKX, nem Binance. Indok (2026-09-08-i meres GitHub Actions futtatorol):
# a Binance minden vegpontja 451 "Service unavailable from a restricted
# location" az USA-beli runner-IP-krol, a Bybit 403 (CloudFront orszagtiltas).
# Az OKX mind a negy szukseges vegponton 200-at ad: gyertyak, funding,
# open interest ES long/short account ratio.
OKX = "https://www.okx.com"
INST_SPOT = "BTC-USDT"
INST_SWAP = "BTC-USDT-SWAP"

# UTC-re igazitott savok. A sima "1D"/"4H" az OKX-en hongkongi napzarashoz
# igazodik (UTC+8) -- a "utc" utotag nelkul eltolt gyertyakat kapnank.
BAR = {"1d": "1Dutc", "4h": "4Hutc", "1h": "1H"}


def _okx(path: str, params: dict, retries: int = 3):
    d = _get(f"{OKX}{path}", params, retries=retries)
    if str(d.get("code")) != "0":
        raise RuntimeError(f"OKX {path} code={d.get('code')} msg={d.get('msg')}")
    return d.get("data") or []


def _row(k) -> dict:
    """OKX gyertya -> a Binance-szel azonos alaku dict, hogy a tobbi kod valtozatlan maradjon.
    OKX sor: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
    A ts a gyertya NYITASI ideje, ms-ben, stringkent.
    """
    return {
        "open_time": int(k[0]),
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        # volCcyQuote = forgalom USD-ben; ez stabilabb suly a likvidacios terkephez
        "volume": float(k[7]) if len(k) > 7 and k[7] not in ("", None) else float(k[5]),
        "close_time": int(k[0]) + 1,
    }


def _klines(symbol: str, interval: str, limit: int, inst: str | None = None):
    """Idorendben (legregibb eloszor) adja vissza a gyertyakat, tetszoleges
    darabszamra lapozva. Az OKX ujdonsag-eloszor ad vissza, es limitalja
    a lapmeretet (candles: 300, history-candles: 100).
    """
    inst = inst or INST_SPOT
    bar = BAR.get(interval, interval)
    out: list[dict] = []
    after = None
    while len(out) < limit:
        need = limit - len(out)
        if after is None:
            data = _okx("/api/v5/market/candles",
                        {"instId": inst, "bar": bar, "limit": min(300, need)})
        else:
            data = _okx("/api/v5/market/history-candles",
                        {"instId": inst, "bar": bar, "after": after, "limit": min(100, need)})
        if not data:
            break
        out = [_row(k) for k in data] + out
        after = int(data[-1][0])  # a legregibb kapott gyertya -> ennel korabbiak jonnek
        if len(data) < 2:
            break
    out.sort(key=lambda c: c["open_time"])
    return out[-limit:]


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[-i] - closes[-i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def _atr_pct(candles: list[dict], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        h, l = candles[i]["high"], candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs) / period
    return 100.0 * atr / candles[-1]["close"]


def fetch_price_and_technical(symbol: str = "BTCUSDT") -> dict:
    def run():
        d = _klines(symbol, "1d", 220)
        h4 = _klines(symbol, "4h", 200)
        closes = [c["close"] for c in d]
        price = closes[-1]

        sma20 = sum(closes[-20:]) / 20
        sma50 = sum(closes[-50:]) / 50
        sma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None

        # swing szintek az utolso 30 napbol (ide gyulnek a stopok)
        win = d[-30:]
        swing_high = max(c["high"] for c in win)
        swing_low = min(c["low"] for c in win)

        # realizalt hetvegi mozgas eloszlasa az utolso 26 hetvegen (1h gyertyakbol)
        wk = _weekend_moves(symbol, weeks=26)

        return {
            "symbol": symbol,
            "price": price,
            "rsi14_d": _rsi(closes, 14),
            "rsi14_4h": _rsi([c["close"] for c in h4], 14),
            "atr14_pct_d": _atr_pct(d, 14),
            "sma20": sma20,
            "sma50": sma50,
            "sma200": sma200,
            "pct_vs_sma20": 100.0 * (price / sma20 - 1),
            "pct_vs_sma50": 100.0 * (price / sma50 - 1),
            "pct_vs_sma200": (100.0 * (price / sma200 - 1)) if sma200 else None,
            "swing_high_30d": swing_high,
            "swing_low_30d": swing_low,
            "dist_to_high_pct": 100.0 * (swing_high / price - 1),
            "dist_to_low_pct": 100.0 * (1 - swing_low / price),
            "ret_7d_pct": 100.0 * (price / closes[-8] - 1),
            "ret_30d_pct": 100.0 * (price / closes[-31] - 1),
            "weekend_move_stats": wk,
        }

    return _safe(run, "okx_spot")


def _weekend_moves(symbol: str, weeks: int = 26) -> dict:
    """Az elmult N hetvege tenyleges mozgasa: Fri 21:00 UTC -> Sun 23:59 UTC.

    Ez NEM backtest, csak a hetvegi volatilitas realizalt eloszlasa,
    amibol az expected range szelesseget skalazzuk.
    """
    hours = weeks * 7 * 24 + 48
    out, moves = [], []
    kl = _klines(symbol, "1h", hours)
    collected = [{"t": c["open_time"], "c": c["close"], "h": c["high"], "l": c["low"]} for c in kl]
    by_ts = {c["t"]: c for c in collected}
    if not collected:
        return {"n": 0}

    last_ts = collected[-1]["t"]
    cursor = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)
    # menj vissza az utolso penteki 21:00 UTC-ig
    while cursor.weekday() != 4:
        cursor -= timedelta(days=1)
    cursor = cursor.replace(hour=21, minute=0, second=0, microsecond=0)

    for _ in range(weeks):
        fri = cursor
        sun = fri + timedelta(days=2, hours=2)  # Sun 23:00 UTC gyertya
        f = by_ts.get(int(fri.timestamp() * 1000))
        s = by_ts.get(int(sun.timestamp() * 1000))
        if f and s:
            pct = 100.0 * (s["c"] / f["c"] - 1)
            moves.append(pct)
            # a hetvege alatti max kilenges
            lo, hi = f["c"], f["c"]
            t = fri
            while t <= sun:
                cc = by_ts.get(int(t.timestamp() * 1000))
                if cc:
                    hi = max(hi, cc["h"])
                    lo = min(lo, cc["l"])
                t += timedelta(hours=1)
            out.append({"fri": f["c"], "sun": s["c"], "pct": pct,
                        "up_ext_pct": 100.0 * (hi / f["c"] - 1),
                        "dn_ext_pct": 100.0 * (1 - lo / f["c"])})
        cursor -= timedelta(days=7)

    if not moves:
        return {"n": 0}
    absm = sorted(abs(m) for m in moves)
    ups = sum(1 for m in moves if m > 0)
    return {
        "n": len(moves),
        "mean_pct": sum(moves) / len(moves),
        "abs_median_pct": absm[len(absm) // 2],
        "abs_p80_pct": absm[int(len(absm) * 0.8)],
        "up_share": ups / len(moves),
        "up_ext_median": sorted(o["up_ext_pct"] for o in out)[len(out) // 2],
        "dn_ext_median": sorted(o["dn_ext_pct"] for o in out)[len(out) // 2],
    }


# --------------------------------------------------------------------------
# 2. FUNDING + OPEN INTEREST + RETAIL POZICIONALTSAG (OKX swap + rubik)
# --------------------------------------------------------------------------

def fetch_derivatives(symbol: str = "BTCUSDT") -> dict:
    def run():
        # OKX. A rubik-statisztikak [ts, ertek] parokat adnak, UJDONSAG-ELOSZOR,
        # ezert mindenhol megforditjuk, hogy a [-1] legyen a legfrissebb.
        prem = _okx("/api/v5/public/funding-rate", {"instId": INST_SWAP})
        fr_hist = _okx("/api/v5/public/funding-rate-history",
                       {"instId": INST_SWAP, "limit": 42})          # ~14 nap (8oras periodus)
        oi_hist = _okx("/api/v5/rubik/stat/contracts/open-interest-volume",
                       {"ccy": "BTC", "period": "1D"})
        ls_acc = _okx("/api/v5/rubik/stat/contracts/long-short-account-ratio",
                      {"ccy": "BTC", "period": "1D"})
        try:
            ls_top = _okx("/api/v5/rubik/stat/contracts/long-short-account-ratio-contract-top-trader",
                          {"instId": INST_SWAP, "period": "1D"})
        except Exception:  # noqa: BLE001 — nem kritikus, csak erositi a retail jelet
            ls_top = []
        try:
            tk = _okx("/api/v5/rubik/stat/taker-volume",
                      {"ccy": "BTC", "instType": "CONTRACTS", "period": "1D"})
        except Exception:  # noqa: BLE001
            tk = []

        now_rate = float(prem[0]["fundingRate"]) if prem else 0.0
        rates = [float(x["fundingRate"]) for x in reversed(fr_hist)]
        oi = [float(r[1]) for r in reversed(oi_hist)][-30:]
        retail = [float(r[1]) for r in reversed(ls_acc)][-30:]
        top = [float(r[1]) for r in reversed(ls_top)][-30:]
        # taker-volume sor: [ts, sellVol, buyVol]
        taker = ([{"buySellRatio": (float(r[2]) / float(r[1])) if float(r[1]) else 1.0}
                  for r in reversed(tk)][-14:]) if tk else []

        def pctile(series, v):
            if not series:
                return None
            return 100.0 * sum(1 for s in series if s <= v) / len(series)

        return {
            "funding_now": now_rate,
            "funding_mean_7d": sum(rates[-21:]) / max(len(rates[-21:]), 1),
            "funding_mean_14d": sum(rates) / max(len(rates), 1),
            "funding_pctile_14d": pctile(rates, now_rate),
            "funding_annualized_pct": now_rate * 3 * 365 * 100,
            "oi_now_usd": oi[-1] if oi else None,
            "oi_chg_7d_pct": (100.0 * (oi[-1] / oi[-8] - 1)) if len(oi) >= 8 else None,
            "oi_chg_30d_pct": (100.0 * (oi[-1] / oi[0] - 1)) if len(oi) >= 2 else None,
            "oi_pctile_30d": pctile(oi, oi[-1]) if oi else None,
            "retail_ls_ratio": retail[-1] if retail else None,
            "retail_ls_mean_30d": (sum(retail) / len(retail)) if retail else None,
            "retail_ls_pctile_30d": pctile(retail, retail[-1]) if retail else None,
            "top_trader_ls_ratio": top[-1] if top else None,
            "top_trader_ls_pctile_30d": pctile(top, top[-1]) if top else None,
            "taker_buy_sell_ratio": float(taker[-1]["buySellRatio"]) if taker else None,
        }

    return _safe(run, "okx_derivatives")


# --------------------------------------------------------------------------
# 3. FEAR & GREED
# --------------------------------------------------------------------------

def fetch_fear_greed() -> dict:
    def run():
        d = _get("https://api.alternative.me/fng/", {"limit": 90, "format": "json"})
        vals = [int(x["value"]) for x in d["data"]]  # [0] = ma
        now = vals[0]
        return {
            "value": now,
            "classification": d["data"][0].get("value_classification"),
            "chg_7d": now - vals[7] if len(vals) > 7 else None,
            "chg_30d": now - vals[30] if len(vals) > 30 else None,
            "mean_30d": sum(vals[:30]) / min(30, len(vals)),
            "pctile_90d": 100.0 * sum(1 for v in vals if v <= now) / len(vals),
        }

    return _safe(run, "alternative.me")


# --------------------------------------------------------------------------
# 4. KALSHI — implied eloszlas a havi max/min piacokbol
# --------------------------------------------------------------------------

KALSHI_HOSTS = [
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2",
]


def _kalshi_markets(series: str) -> list[dict]:
    last = None
    for host in KALSHI_HOSTS:
        try:
            d = _get(f"{host}/markets", {"series_ticker": series, "status": "open", "limit": 200},
                     retries=2)
            return d.get("markets", [])
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"kalshi unreachable: {last}")


def _mid(m: dict):
    """Kozeparfolyam 0..1 valoszinusegre. Kalshi centben adja (0-100)."""
    b, a = m.get("yes_bid"), m.get("yes_ask")
    if b is None or a is None:
        lp = m.get("last_price")
        return (lp / 100.0) if lp else None
    if a == 0 and b == 0:
        return None
    if b == 0 or a == 100:  # egyoldalu konyv: a last_price megbizhatobb
        lp = m.get("last_price")
        if lp:
            return lp / 100.0
    return (b + a) / 200.0


def fetch_kalshi(spot_price: float | None = None) -> dict:
    """A hetvegere szolo Kalshi piac hetfon meg nem letezik (a napi piacok
    ~1 nappal elore nyilnak). Amit hetfon ki lehet olvasni: a HAVI max/min
    egy-erintes piacok implied eloszlasa -> range es skew a honap hatralevo
    reszere. Ez INDIREKT jel, ezert kulon van jelolve.
    """

    def run():
        out = {"direct_weekend_market": False, "note": "monthly max/min one-touch proxy"}

        def ladder(series, key):
            mk = _kalshi_markets(series)
            rows = []
            for m in mk:
                p = _mid(m)
                strike = m.get("floor_strike") or m.get("cap_strike")
                if p is None or strike is None:
                    continue
                rows.append({
                    "ticker": m.get("ticker"),
                    "strike": float(strike),
                    "prob": round(p, 4),
                    "oi": m.get("open_interest"),
                    "close_time": m.get("close_time"),
                })
            rows.sort(key=lambda r: r["strike"])
            out[key] = rows
            return rows

        up = ladder("KXBTCMAXMON", "monthly_max_ladder")
        dn = ladder("KXBTCMINMON", "monthly_min_ladder")

        # implied "varhato" szelso ertekek: az a strike, ahol a valoszinuseg 50%
        def implied_at(rows, target=0.5, ascending_prob=False):
            if len(rows) < 2:
                return None
            seq = rows if not ascending_prob else list(reversed(rows))
            for i in range(len(seq) - 1):
                p1, p2 = seq[i]["prob"], seq[i + 1]["prob"]
                if (p1 - target) * (p2 - target) <= 0 and p1 != p2:
                    s1, s2 = seq[i]["strike"], seq[i + 1]["strike"]
                    w = (target - p1) / (p2 - p1)
                    return s1 + w * (s2 - s1)
            return None

        out["implied_month_high_p50"] = implied_at(up)
        out["implied_month_low_p50"] = implied_at(dn)

        if spot_price and out["implied_month_high_p50"] and out["implied_month_low_p50"]:
            up_room = out["implied_month_high_p50"] / spot_price - 1
            dn_room = 1 - out["implied_month_low_p50"] / spot_price
            out["upside_room_pct"] = 100.0 * up_room
            out["downside_room_pct"] = 100.0 * dn_room
            tot = up_room + dn_room
            out["skew"] = (up_room - dn_room) / tot if tot > 0 else 0.0
        out["total_open_interest"] = sum(
            (r.get("oi") or 0) for r in (out.get("monthly_max_ladder", []) + out.get("monthly_min_ladder", []))
        )
        return out

    return _safe(run, "kalshi")


# --------------------------------------------------------------------------
# 5. REDDIT RETAIL SENTIMENT
# --------------------------------------------------------------------------

BULL = {"bull", "bullish", "moon", "pump", "rally", "breakout", "ath", "buy", "long", "up",
        "green", "rip", "send", "accumulate", "bottom", "reversal", "squeeze"}
BEAR = {"bear", "bearish", "dump", "crash", "capitulation", "sell", "short", "down", "red",
        "rekt", "liquidated", "panic", "top", "bubble", "correction", "bleed"}


def fetch_reddit_sentiment() -> dict:
    def run():
        posts = []
        for sub in ("Bitcoin", "CryptoCurrency", "BitcoinMarkets"):
            try:
                d = _get(f"https://www.reddit.com/r/{sub}/hot.json", {"limit": 60}, retries=2,
                         ua=BROWSER_UA)
                for c in d["data"]["children"]:
                    p = c["data"]
                    posts.append({
                        "sub": sub,
                        "title": p.get("title", ""),
                        "score": p.get("score", 0),
                        "comments": p.get("num_comments", 0),
                    })
            except Exception:  # noqa: BLE001,S110
                continue
        if not posts:
            raise RuntimeError("no reddit posts")

        bull_w = bear_w = 0.0
        for p in posts:
            toks = {t.strip(".,!?:;()[]\"'").lower() for t in p["title"].split()}
            w = 1.0 + (p["score"] ** 0.5) / 10.0  # engagement-sulyozas
            bull_w += w * len(toks & BULL)
            bear_w += w * len(toks & BEAR)
        tot = bull_w + bear_w
        score = 50.0 if tot == 0 else 100.0 * bull_w / tot
        return {
            "posts_analyzed": len(posts),
            "bull_weight": round(bull_w, 2),
            "bear_weight": round(bear_w, 2),
            "retail_bullishness": round(score, 1),  # 0..100
            "total_engagement": sum(p["score"] + p["comments"] for p in posts),
        }

    return _safe(run, "reddit")


# --------------------------------------------------------------------------
# osszefogo
# --------------------------------------------------------------------------

def fetch_liquidation(deriv: dict, symbol: str = "BTCUSDT") -> dict:
    """Likvidacios terkep. Ha van COINGLASS_API_KEY, a valos CoinGlass v4
    heatmapet hasznalja; kulonben sajat modellt epit ugyanabbol a
    modszertani osztalybol (OI + tokeattetel-savok).
    """

    def run():
        if not deriv.get("ok"):
            raise RuntimeError("nincs derivativ adat (OI kell hozza)")
        oi = deriv.get("oi_now_usd")
        if not oi:
            raise RuntimeError("nincs open interest")
        cg = fetch_coinglass(_get)
        if cg:
            return cg
        kl = _klines(symbol, "1h", 720)  # ~30 nap
        r = deriv.get("retail_ls_ratio") or 1.0
        long_share = r / (1.0 + r)
        m = build_map(kl, oi, long_share)
        if not m.get("ok"):
            raise RuntimeError(m.get("error", "terkep-epites sikertelen"))
        return m

    return _safe(run, "liquidation")


def collect_all(symbol: str = "BTCUSDT") -> dict:
    tech = fetch_price_and_technical(symbol)
    spot = tech.get("price") if tech.get("ok") else None
    deriv = fetch_derivatives(symbol)
    return {
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "technical": tech,
        "derivatives": deriv,
        "liquidation": fetch_liquidation(deriv, symbol),
        "fear_greed": fetch_fear_greed(),
        "kalshi": fetch_kalshi(spot),
        "reddit": fetch_reddit_sentiment(),
    }

# ===========================================================================
# JELEK ES SULYOZAS
# ===========================================================================

WEIGHT_PROFILE = "v2.0-2026-09"

# ---------------------------------------------------------------------------
# v2: MINDEN faktor kap valodi sulyt, Marcell dontese alapjan (nem varunk
# 15-20 hetet a Kalshira es a socialra).
#
# A faktoronkenti meres ettol fuggetlenul tovabb fut a hatterben: a resolver
# minden hetre kulon rogziti, melyik faktor mit hivott es mi lett belole.
# Ha egy faktor 15-20 het utan 50-55% korul teljesit, EGY SOR atirasaval
# kivehető innen -- a sulyprofil verziozva van, hogy lassuk, mikor valtozott.
# ---------------------------------------------------------------------------
WEIGHTS = {
    "derivatives": 0.22,          # funding + OI kombinaciok -> crowding / squeeze
    "technical": 0.22,            # trend + RSI + strukturalis helyzet
    "liquidation": 0.18,          # likvidacios klaszter-vonzas (modellezett terkep)
    "retail_positioning": 0.15,   # OKX long/short account ratio, contrarian
    "kalshi": 0.13,               # prediction market implied eloszlas
    "fear_greed": 0.05,           # csak extremumban
    "reddit_contrarian": 0.05,    # social retail contrarian
}

# Ures: nincs tobbe 0 sulyu sav. A meres viszont megmaradt.
TRACKED_ONLY: set[str] = set()

# Ezekre a faktorokra meg NINCS sajat merési eredmeny -- a dashboard
# kulon jeloli oket, hogy latszodjon: sulyuk van, de meg bizonyitatlanok.
UNPROVEN = {"kalshi", "reddit_contrarian", "liquidation"}


def _clamp(v, lo=-1.0, hi=1.0):
    return max(lo, min(hi, v))


def _sig(name, value, confidence, why, ok=True):
    """value: -1..+1 (negativ = bearish). confidence: 0..1."""
    return {
        "name": name,
        "value": round(_clamp(value), 3) if ok else None,
        "confidence": round(_clamp(confidence, 0, 1), 2) if ok else 0.0,
        "direction": ("UP" if value > 0.08 else "DOWN" if value < -0.08 else "FLAT") if ok else None,
        "why": why,
        "ok": ok,
        "tracked_only": name in TRACKED_ONLY,
        "unproven": name in UNPROVEN,
        "weight": WEIGHTS.get(name, 0.0),
    }


# ---------------------------------------------------------------------------
# 1. DERIVATIVES — funding x OI x ar kombinaciok
# ---------------------------------------------------------------------------

def signal_derivatives(deriv: dict, tech: dict) -> dict:
    if not deriv.get("ok") or not tech.get("ok"):
        return _sig("derivatives", 0, 0, "adat hianyzik", ok=False)

    f_ann = deriv.get("funding_annualized_pct") or 0.0
    oi7 = deriv.get("oi_chg_7d_pct")
    ret7 = tech.get("ret_7d_pct") or 0.0

    v = 0.0
    notes = []

    # (a) funding extremum -> contrarian. Normal sav kb. -5%..+15% annualizalt.
    if f_ann > 30:
        v -= 0.45; notes.append(f"funding extrem magas ({f_ann:.0f}% ann.) — zsufolt longok")
    elif f_ann > 15:
        v -= 0.20; notes.append(f"funding emelkedett ({f_ann:.0f}% ann.)")
    elif f_ann < -10:
        v += 0.45; notes.append(f"funding extrem negativ ({f_ann:.0f}% ann.) — short squeeze uzemanyag")
    elif f_ann < 0:
        v += 0.18; notes.append(f"funding negativ ({f_ann:.0f}% ann.)")
    else:
        notes.append(f"funding semleges ({f_ann:.0f}% ann.)")

    # (b) ar x OI kombinacio
    if oi7 is not None:
        if ret7 > 1 and oi7 > 5:
            v -= 0.20; notes.append("ar↑ + OI↑ — uj leverage, sebezheto")
        elif ret7 > 1 and oi7 < -5:
            v += 0.25; notes.append("ar↑ + OI↓ — short covering, egeszsegesebb")
        elif ret7 < -1 and oi7 > 5:
            v -= 0.25; notes.append("ar↓ + OI↑ — uj shortok epulnek")
        elif ret7 < -1 and oi7 < -5:
            v += 0.22; notes.append("ar↓ + OI↓ — kimosas, pozicionaltsag tisztult")

    # (c) OI szint extremum
    oi_p = deriv.get("oi_pctile_30d")
    if oi_p is not None and oi_p > 90:
        v -= 0.12; notes.append("OI 30 napos csucson — magas likvidacios kockazat")

    conf = 0.75
    if oi7 is None:
        conf -= 0.2
    if abs(f_ann) < 5 and (oi7 is None or abs(oi7) < 3):
        conf -= 0.25  # minden semleges -> keves informacio
    return _sig("derivatives", v, conf, "; ".join(notes))


# ---------------------------------------------------------------------------
# 2. TECHNICAL
# ---------------------------------------------------------------------------

def signal_technical(tech: dict) -> dict:
    if not tech.get("ok"):
        return _sig("technical", 0, 0, "adat hianyzik", ok=False)

    v = 0.0
    notes = []
    rsi = tech.get("rsi14_d")
    p20 = tech.get("pct_vs_sma20") or 0.0
    p50 = tech.get("pct_vs_sma50") or 0.0
    p200 = tech.get("pct_vs_sma200")

    # trend-struktura
    trend = 0.0
    if p20 > 0: trend += 0.5
    else: trend -= 0.5
    if p50 > 0: trend += 0.3
    else: trend -= 0.3
    if p200 is not None:
        trend += 0.2 if p200 > 0 else -0.2
    v += 0.45 * trend
    notes.append(f"trend: {p20:+.1f}% SMA20 / {p50:+.1f}% SMA50")

    # RSI — csak extremumban contrarian, kozepen momentum
    if rsi is not None:
        if rsi > 72:
            v -= 0.25; notes.append(f"RSI {rsi:.0f} tulvett")
        elif rsi < 28:
            v += 0.30; notes.append(f"RSI {rsi:.0f} tulad va")
        elif rsi > 55:
            v += 0.10; notes.append(f"RSI {rsi:.0f}")
        elif rsi < 45:
            v -= 0.10; notes.append(f"RSI {rsi:.0f}")

    # tulnyujtottsag az SMA20-tol
    if abs(p20) > 8:
        v += -0.18 if p20 > 0 else 0.18
        notes.append(f"tulnyujtott az SMA20-tol ({p20:+.1f}%)")

    conf = 0.7 if rsi is not None else 0.45
    return _sig("technical", v, conf, "; ".join(notes))


# ---------------------------------------------------------------------------
# 3. RETAIL POSITIONING (contrarian) — OKX long/short account ratio
# ---------------------------------------------------------------------------

def signal_retail_positioning(deriv: dict) -> dict:
    if not deriv.get("ok") or deriv.get("retail_ls_ratio") is None:
        return _sig("retail_positioning", 0, 0, "adat hianyzik", ok=False)

    r = deriv["retail_ls_ratio"]
    p = deriv.get("retail_ls_pctile_30d")
    top = deriv.get("top_trader_ls_ratio")

    # 0..100 retail bullishness a 30 napos percentilisbol (relativ, nem abszolut)
    bull = p if p is not None else 50.0
    v = -(bull - 50.0) / 50.0 * 0.55  # contrarian, tompitva
    notes = [f"retail L/S {r:.2f} ({bull:.0f}. percentilis 30 napra)"]

    # ha a top traderek ELLENTETESEN allnak a retail-lel, az erositi a jelet
    if top is not None:
        if (r > 2.0 and top < 1.0) or (r < 1.0 and top > 2.0):
            v *= 1.4
            notes.append(f"top traderek ellentetesen allnak ({top:.2f}) — jel erositve")

    conf = 0.6 if p is not None else 0.3
    return _sig("retail_positioning", v, conf, "; ".join(notes))


# ---------------------------------------------------------------------------
# 4. LIQUIDITY PULL — liquidation proxy strukturabol
# ---------------------------------------------------------------------------

def signal_liquidation(liq: dict, tech: dict) -> dict:
    """Liquidity Attraction Score a modellezett likvidacios terkepbol.

    Ha van CoinGlass kulcs, a valos heatmapbol; kulonben sajat modellbol
    (OI szetosztva a belepesi arak eloszlasan + tokeattetel-savok).
    Tartalek: ha a terkep nem all ossze, a 30 napos swing szintekre esik vissza.
    """

    if liq.get("ok"):
        a = attraction_score(liq)
        if a.get("ok"):
            return _sig("liquidation", a["value"], a["confidence"], a["why"])

    # --- tartalek: swing-szint proxy ---
    if not tech.get("ok"):
        return _sig("liquidation", 0, 0, "adat hianyzik", ok=False)
    dh, dl = tech.get("dist_to_high_pct"), tech.get("dist_to_low_pct")
    if dh is None or dl is None:
        return _sig("liquidation", 0, 0, "nincs swing adat", ok=False)
    eps = 0.4
    pu, pd = 1.0 / max(dh, eps), 1.0 / max(dl, eps)
    v = (pu - pd) / (pu + pd) * 0.6
    return _sig("liquidation", v, 0.3,
                f"terkep nem allt ossze — tartalek: swing high {dh:+.1f}% / low -{dl:.1f}%")


# ---------------------------------------------------------------------------
# 5. FEAR & GREED — csak extremumban
# ---------------------------------------------------------------------------

def signal_fear_greed(fg: dict) -> dict:
    if not fg.get("ok"):
        return _sig("fear_greed", 0, 0, "adat hianyzik", ok=False)
    v_ = fg["value"]
    if v_ >= 80:
        return _sig("fear_greed", -0.55, 0.5, f"F&G {v_} — extreme greed")
    if v_ <= 20:
        return _sig("fear_greed", 0.60, 0.55, f"F&G {v_} — extreme fear")
    if v_ >= 68:
        return _sig("fear_greed", -0.22, 0.3, f"F&G {v_} — greed")
    if v_ <= 32:
        return _sig("fear_greed", 0.25, 0.3, f"F&G {v_} — fear")
    return _sig("fear_greed", 0.0, 0.15, f"F&G {v_} — semleges sav, nincs jel")


# ---------------------------------------------------------------------------
# 6. KALSHI (TRACKED, 0 suly)
# ---------------------------------------------------------------------------

def signal_kalshi(k: dict) -> dict:
    if not k.get("ok"):
        return _sig("kalshi", 0, 0, "adat hianyzik", ok=False)
    skew = k.get("skew")
    if skew is None:
        return _sig("kalshi", 0, 0, "nem sikerult eloszlast rekonstrualni", ok=False)
    up, dn = k.get("upside_room_pct"), k.get("downside_room_pct")
    return _sig("kalshi", skew * 0.8, 0.4,
                f"havi implied: +{up:.1f}% / -{dn:.1f}% tér, skew {skew:+.2f} "
                f"[indirekt — nincs hetvegi Kalshi piac hetfon]")


# ---------------------------------------------------------------------------
# 7. REDDIT CONTRARIAN (TRACKED, 0 suly)
# ---------------------------------------------------------------------------

def signal_reddit(rd: dict) -> dict:
    if not rd.get("ok"):
        return _sig("reddit_contrarian", 0, 0, "adat hianyzik", ok=False)
    b = rd["retail_bullishness"]
    v = -(b - 50.0) / 50.0 * 0.7
    band = ("bullish contrarian" if b < 20 else "enyhen bullish" if b < 40
            else "neutral" if b < 60 else "enyhen bearish" if b < 80 else "bearish contrarian")
    return _sig("reddit_contrarian", v, 0.3,
                f"retail bullishness {b:.0f}/100 ({band}), {rd['posts_analyzed']} poszt")


# ---------------------------------------------------------------------------
# OSSZEGZES
# ---------------------------------------------------------------------------

def build_signals(data: dict) -> list[dict]:
    tech, deriv = data["technical"], data["derivatives"]
    return [
        signal_derivatives(deriv, tech),
        signal_technical(tech),
        signal_retail_positioning(deriv),
        signal_liquidation(data.get("liquidation", {}), tech),
        signal_fear_greed(data["fear_greed"]),
        signal_kalshi(data["kalshi"]),
        signal_reddit(data["reddit"]),
    ]


def aggregate(signals: list[dict], tech: dict) -> dict:
    """Sulyozott score 0..100 + expected range + invalidation."""
    num = den = 0.0
    used, missing = [], []
    for s in signals:
        if s["tracked_only"]:
            continue
        if not s["ok"] or s["weight"] <= 0:
            missing.append(s["name"])
            continue
        w = s["weight"] * s["confidence"]
        num += w * s["value"]
        den += w
        used.append(s["name"])

    raw = (num / den) if den > 0 else 0.0       # -1..+1
    score = 50.0 + raw * 50.0                    # 0..100

    # bizalom: mennyi sulyt tudtunk ténylegesen felhasznalni
    max_den = sum(WEIGHTS[n] for n in WEIGHTS if n not in TRACKED_ONLY)
    coverage = den / max_den if max_den else 0.0
    agreement = _agreement(signals)
    confidence = round(min(1.0, 0.45 * coverage + 0.55 * agreement), 2)

    price = tech.get("price")
    rng = _expected_range(price, raw, confidence, tech)

    if score >= 65:
        bias, emoji = "BULLISH", "🟢"
    elif score >= 56:
        bias, emoji = "ENYHÉN BULLISH", "🟢"
    elif score > 44:
        bias, emoji = "NEUTRAL", "⚪"
    elif score > 35:
        bias, emoji = "ENYHÉN BEARISH", "🔴"
    else:
        bias, emoji = "BEARISH", "🔴"

    return {
        "score": round(score, 1),
        "raw": round(raw, 3),
        "bias": bias,
        "emoji": emoji,
        "direction": "UP" if score > 55 else "DOWN" if score < 45 else "FLAT",
        "confidence": confidence,
        "coverage": round(coverage, 2),
        "agreement": round(agreement, 2),
        "factors_used": used,
        "factors_missing": missing,
        "expected_range": rng,
        "invalidation": rng.get("invalidation"),
        "weight_profile": WEIGHT_PROFILE,
    }


def _agreement(signals: list[dict]) -> float:
    vals = [s["value"] for s in signals if s["ok"] and not s["tracked_only"] and s["weight"] > 0]
    if len(vals) < 2:
        return 0.3
    ups = sum(1 for v in vals if v > 0.08)
    dns = sum(1 for v in vals if v < -0.08)
    if ups + dns == 0:
        return 0.25
    return abs(ups - dns) / (ups + dns)


def _expected_range(price, raw, confidence, tech):
    """A sav szelesseget a REALIZALT hetvegi volatilitas adja (utolso 26 hetvege),
    a kozeppontot a score tolja el. Nem talalgatas -- meresbol skalazott.
    """
    if not price:
        return {}
    wk = tech.get("weekend_move_stats") or {}
    base = wk.get("abs_p80_pct")
    if not base:
        base = (tech.get("atr14_pct_d") or 2.0) * 1.3
    # aktualis vol-regime korrekcio
    atr = tech.get("atr14_pct_d")
    if atr:
        base *= max(0.7, min(1.5, atr / 2.2))

    shift = raw * base * 0.45 * confidence   # a bias eltolja a kozeppontot
    center = price * (1 + shift / 100.0)
    half = base * 0.85
    low = center * (1 - half / 100.0)
    high = center * (1 + half / 100.0)

    inval = price * (1 - base / 100.0) if raw > 0 else price * (1 + base / 100.0)
    return {
        "low": round(low, 0),
        "high": round(high, 0),
        "center": round(center, 0),
        "width_pct": round(2 * half, 2),
        "basis": f"realizalt hetvegi p80 mozgas {wk.get('n', 0)} hetvegebol",
        "invalidation": round(inval, 0),
        "invalidation_side": "below" if raw > 0 else "above",
    }

# ===========================================================================
# PREDIKCIO
# ===========================================================================

def weekend_window(now: datetime) -> tuple[datetime, datetime]:
    """A kovetkezo hetvege: pentek 21:00 UTC -> vasarnap 23:00 UTC (utolso 1h gyertya)."""
    d = now
    while d.weekday() != 4:  # 4 = pentek
        d += timedelta(days=1)
    start = d.replace(hour=21, minute=0, second=0, microsecond=0)
    if start <= now:
        start += timedelta(days=7)
    end = start + timedelta(days=2, hours=2)
    return start, end


def month_end(now: datetime) -> datetime:
    y, m = now.year, now.month
    nxt = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=timezone.utc)
    return nxt - timedelta(hours=1)


def compact_factors(signals: list[dict]) -> list[dict]:
    """Tomor faktor-sor a dashboardnak. A nyers adat NEM kerul be."""
    return [
        {
            "name": s["name"],
            "dir": s["direction"],
            "value": s["value"],
            "conf": s["confidence"],
            "w": s["weight"],
            "tracked_only": s["tracked_only"],
            "unproven": s.get("unproven", False),
            "ok": s["ok"],
            "why": s["why"][:180] if s.get("why") else "",
        }
        for s in signals
    ]


def predict_main() -> int:
    now = datetime.now(timezone.utc)
    DATA.mkdir(parents=True, exist_ok=True)

    data = collect_all()
    signals = build_signals(data)
    agg = aggregate(signals, data["technical"])

    price = data["technical"].get("price")
    if not price:
        print("FATAL: nincs ar, a predikcio nem keszitheto el", file=sys.stderr)
        return 1

    ws, we = weekend_window(now)
    me = month_end(now)

    # havi horizont: ugyanaz a score, szelesebb savval (Kalshi havi implied-bol)
    k = data["kalshi"]
    monthly = {
        "target_time": me.isoformat(timespec="seconds"),
        "direction": agg["direction"],
        "score": agg["score"],
        "kalshi_implied_high": k.get("implied_month_high_p50") if k.get("ok") else None,
        "kalshi_implied_low": k.get("implied_month_low_p50") if k.get("ok") else None,
        "note": "12 adatpont/ev — evekig megfigyeles, nem tanulsag",
    }

    drivers = [s for s in signals if s["ok"] and not s["tracked_only"] and abs(s["value"]) > 0.15]
    drivers.sort(key=lambda s: abs(s["value"]) * s["weight"] * s["confidence"], reverse=True)
    driver_text = ", ".join(
        f"{s['name']} {'↑' if s['value'] > 0 else '↓'}" for s in drivers[:3]
    ) or "nincs kiemelkedo faktor"

    prediction = {
        "id": ws.strftime("%Y-W%V"),
        "made_at": now.isoformat(timespec="seconds"),
        "anchor_price": round(price, 2),
        "weekend_start": ws.isoformat(timespec="seconds"),
        "weekend_end": we.isoformat(timespec="seconds"),
        "score": agg["score"],
        "bias": agg["bias"],
        "emoji": agg["emoji"],
        "direction": agg["direction"],
        "confidence": agg["confidence"],
        "coverage": agg["coverage"],
        "agreement": agg["agreement"],
        "expected_range": agg["expected_range"],
        "invalidation": agg["invalidation"],
        "invalidation_side": agg["expected_range"].get("invalidation_side"),
        "main_drivers": driver_text,
        "weight_profile": agg["weight_profile"],
        "factors": compact_factors(signals),
        "factor_calls": {s["name"]: s["direction"] for s in signals if s["ok"]},
        "monthly": monthly,
        "sources_failed": [k2 for k2, v in data.items()
                           if isinstance(v, dict) and v.get("ok") is False],
        "resolved": False,
    }

    er = agg["expected_range"]
    headline = (
        f"BTC WEEKEND BIAS: {agg['emoji']} {agg['bias']} — {agg['score']:.0f}%\n"
        f"Expected weekend range: ${er.get('low', 0):,.0f}–${er.get('high', 0):,.0f}\n"
        f"Main drivers: {driver_text}.\n"
        f"Invalidation: {er.get('invalidation_side', '')} ${er.get('invalidation', 0):,.0f}."
    )
    prediction["headline"] = headline

    # --- history: felulirjuk az azonos ID-t, ha ujrafutna ---
    hist = load_json(HISTORY, [])
    hist = [h for h in hist if h.get("id") != prediction["id"] or h.get("resolved")]
    hist.append(prediction)
    hist.sort(key=lambda h: h["made_at"])
    HISTORY.write_text(json.dumps(hist, indent=1, ensure_ascii=False), encoding="utf-8")

    current = load_json(CURRENT, {})
    current["current"] = prediction
    current["accuracy"] = current.get("accuracy", {"n": 0, "note": "meres alatt — nincs meg adat"})
    current["updated_at"] = now.isoformat(timespec="seconds")
    CURRENT.write_text(json.dumps(current, indent=1, ensure_ascii=False), encoding="utf-8")

    print(headline)
    if prediction["sources_failed"]:
        print("FIGYELEM — kiesett forras:", ", ".join(prediction["sources_failed"]))
    return 0

# ===========================================================================
# KIERTEKELES
# ===========================================================================

# zaj-kuszob: ez alatt FLAT, nem irany
FLAT_THRESHOLD_PCT = 0.6


def price_at(ts_iso: str) -> float | None:
    """1h zaroar egy adott UTC idopontra, OKX-rol.

    Az OKX "after" parametere a megadott idobelyegnel KORABBI rekordokat adja,
    ujdonsag-eloszor -- ezert after = T+1 ms, es az elso talalat a T-kor nyilo
    gyertya. Ellenorizzuk is, hogy tenyleg az jott-e vissza.
    """
    dt = datetime.fromisoformat(ts_iso)
    ms = int(dt.timestamp() * 1000)
    for path in ("/api/v5/market/history-candles", "/api/v5/market/candles"):
        try:
            d = _okx(path, {"instId": INST_SPOT, "bar": "1H",
                                    "after": ms + 1, "limit": 1})
            if d and int(d[0][0]) == ms:
                return float(d[0][4])
        except Exception:  # noqa: BLE001
            continue
    return None


def actual_direction(pct: float) -> str:
    if pct > FLAT_THRESHOLD_PCT:
        return "UP"
    if pct < -FLAT_THRESHOLD_PCT:
        return "DOWN"
    return "FLAT"


def grade(predicted: str, actual: str) -> str:
    """4 kimenet, sosem 2 — a FLAT nem tevedes es nem talalat."""
    if predicted is None:
        return "n/a"
    if predicted == "FLAT":
        # A range-hivas is hivas: ha semlegest mondtunk es a piac atlepte a
        # zaj-kuszobot, az TEVEDES -- kulonben a FLAT ingyen pont lenne.
        return "HIT" if actual == "FLAT" else "MISS"
    if predicted == actual:
        return "HIT"
    if actual == "FLAT":
        return "FLAT"
    return "MISS"


def resolve_one(p: dict) -> dict | None:
    now = datetime.now(timezone.utc)
    end = datetime.fromisoformat(p["weekend_end"])
    if end > now:
        return None

    p_start = price_at(p["weekend_start"])
    p_end = price_at(p["weekend_end"])
    if p_start is None or p_end is None:
        p["resolve_error"] = "nem sikerult arat szerezni"
        return p

    pct = 100.0 * (p_end / p_start - 1)
    act = actual_direction(pct)

    er = p.get("expected_range") or {}
    in_range = None
    if er.get("low") and er.get("high"):
        in_range = bool(er["low"] <= p_end <= er["high"])

    # anchor (hetfoi ar) -> vasarnapi zaras, masodlagos metrika
    anchor_pct = 100.0 * (p_end / p["anchor_price"] - 1)

    p["outcome"] = {
        "friday_close": round(p_start, 2),
        "sunday_close": round(p_end, 2),
        "weekend_move_pct": round(pct, 2),
        "actual_direction": act,
        "result": grade(p.get("direction"), act),
        "range_hit": in_range,
        "anchor_to_sunday_pct": round(anchor_pct, 2),
        "invalidated": (
            (p_end < p["invalidation"]) if p.get("invalidation_side") == "below"
            else (p_end > p["invalidation"]) if p.get("invalidation_side") == "above"
            else None
        ),
        "resolved_at": now.isoformat(timespec="seconds"),
    }
    # faktoronkenti kulon eredmeny — EZ a rendszer lenyege
    p["factor_results"] = {
        name: grade(call, act) for name, call in (p.get("factor_calls") or {}).items()
    }
    p["resolved"] = True
    p.pop("resolve_error", None)
    return p


def rollup(hist: list[dict]) -> dict:
    done = [h for h in hist if h.get("resolved") and h.get("outcome")]
    if not done:
        return {"n": 0, "hit_rate": None, "range_hit_rate": None, "by_factor": {},
                "maturity": "ELOZETES — meg nincs lezart hetvege"}

    res = [h["outcome"]["result"] for h in done]
    hits = res.count("HIT")
    miss = res.count("MISS")
    flat = res.count("FLAT")
    directional = hits + miss

    rng = [h["outcome"]["range_hit"] for h in done if h["outcome"].get("range_hit") is not None]
    maes = [abs(h["outcome"]["sunday_close"] - (h.get("expected_range") or {}).get("center", h["outcome"]["sunday_close"]))
            for h in done if (h.get("expected_range") or {}).get("center")]

    # faktoronkent
    fac: dict[str, dict] = {}
    for h in done:
        for name, r in (h.get("factor_results") or {}).items():
            b = fac.setdefault(name, {"HIT": 0, "MISS": 0, "FLAT": 0, "n/a": 0})
            b[r] = b.get(r, 0) + 1
    factor_acc = {}
    for name, b in fac.items():
        d = b["HIT"] + b["MISS"]
        factor_acc[name] = {
            "n_directional": d,
            "hit_rate": round(100.0 * b["HIT"] / d, 1) if d else None,
            "flat": b["FLAT"],
            "verdict": ("nincs eleg adat" if d < 15
                        else "nincs edge" if b["HIT"] / d <= 0.55
                        else "igeretes"),
        }

    return {
        "n": len(done),
        "directional_n": directional,
        "hit_rate": round(100.0 * hits / directional, 1) if directional else None,
        "hits": hits, "misses": miss, "flats": flat,
        "range_hit_rate": round(100.0 * sum(rng) / len(rng), 1) if rng else None,
        "mae_usd": round(sum(maes) / len(maes), 0) if maes else None,
        "by_factor": factor_acc,
        "baseline_note": "irany-hivas veletlenszeruen ~50%. 55% alatt a jel ertektelen.",
        "maturity": ("ELOZETES — 15 lezart hetvege alatt megfigyeles, nem tanulsag"
                     if len(done) < 15 else "MERHETO"),
    }


def resolve_main() -> int:
    hist = load_json(HISTORY, [])
    if not hist:
        print("nincs history, nincs mit kiertekelni")
        return 0

    changed = 0
    for i, p in enumerate(hist):
        if p.get("resolved"):
            continue
        out = resolve_one(p)
        if out and out.get("resolved"):
            hist[i] = out
            changed += 1
            o = out["outcome"]
            print(f"{out['id']}: predikcio {out['direction']} -> tény {o['actual_direction']} "
                  f"({o['weekend_move_pct']:+.2f}%) = {o['result']}, range_hit={o['range_hit']}")

    HISTORY.write_text(json.dumps(hist, indent=1, ensure_ascii=False), encoding="utf-8")

    cur = load_json(CURRENT, {})
    cur["accuracy"] = rollup(hist)
    cur["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    DATA.mkdir(parents=True, exist_ok=True)
    CURRENT.write_text(json.dumps(cur, indent=1, ensure_ascii=False), encoding="utf-8")

    a = cur["accuracy"]
    if a["n"]:
        print(f"Osszesitett: {a['n']} hetvege, irany-talalat {a.get('hit_rate')}%, "
              f"range {a.get('range_hit_rate')}% — {a['maturity']}")
    print(f"{changed} predikcio lezarva.")
    return 0


# ===========================================================================
# CLI
# ===========================================================================

def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "predict"
    if cmd == "predict":
        return predict_main()
    if cmd == "resolve":
        return resolve_main()
    print(f"ismeretlen parancs: {cmd} (predict | resolve)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
