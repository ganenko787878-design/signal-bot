"""Публичные рыночные данные Bybit (без ключей и без доступа к аккаунту).

Только чтение: свечи, тикеры, фандинг, открытый интерес. Торговых запросов здесь нет.
"""
import csv
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

API_HOSTS = ["https://api.bybit.com", "https://api.bytick.com"]
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

TF_MS = {"1": 60_000, "5": 300_000, "15": 900_000, "60": 3_600_000, "240": 14_400_000, "D": 86_400_000}
TF_NAME = {"1": "1м", "5": "5м", "15": "15м", "60": "1ч", "240": "4ч", "D": "1Д"}


def ssl_context():
    """Системные сертификаты; если их нет (бывает у Python с python.org на Mac), берём запасной набор."""
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0) > 0:
        return ctx
    try:
        import certifi  # noqa: WPS433
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    for path in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    return ctx


_CTX = ssl_context()
_host = 0


def http_json(url, data=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "signal-bot/1.0", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def api(path, params, retries=4):
    """GET /v5/market/<path>. Переключает хост и повторяет при сбое сети."""
    global _host
    qs = urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        host = API_HOSTS[(_host + attempt) % len(API_HOSTS)]
        try:
            j = http_json(f"{host}/v5/market/{path}?{qs}")
            if j.get("retCode") != 0:
                raise RuntimeError(f"Bybit: {j.get('retMsg')} (код {j.get('retCode')})")
            _host = (_host + attempt) % len(API_HOSTS)
            return j["result"]
        except RuntimeError as e:
            last = e
            if "Too many" in str(e) or "10006" in str(e):
                time.sleep(2 + attempt * 2)
                continue
            raise
        except Exception as e:  # сеть, таймаут
            last = e
            time.sleep(0.6 * (attempt + 1))
    raise last


def klines(symbol, interval, limit=1000, end=None):
    """Свечи по возрастанию времени: [t_ms, open, high, low, close, volume]. Последняя может быть незакрытой."""
    p = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    if end is not None:
        p["end"] = int(end)
    rows = api("kline", p)["list"]
    out = [[int(a[0]), float(a[1]), float(a[2]), float(a[3]), float(a[4]), float(a[5])] for a in rows]
    out.reverse()
    return out


def closed_klines(symbol, interval, limit=1000, now_ms=None):
    """Только закрытые свечи."""
    now_ms = now_ms or int(time.time() * 1000)
    rows = klines(symbol, interval, limit)
    step = TF_MS[interval]
    return [r for r in rows if r[0] + step <= now_ms]


def _cache_path(symbol, interval):
    return os.path.join(DATA_DIR, f"{symbol}_{interval}.csv")


def history(symbol, interval, since_ms, log=None):
    """Закрытые свечи с since_ms до текущего момента. Кеш на диске хранит всё скачанное, докачивается только новое."""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _cache_path(symbol, interval)
    cached = []
    if os.path.exists(path):
        with open(path, newline="") as f:
            cached = [[int(r[0])] + [float(x) for x in r[1:]] for r in csv.reader(f)]
    step = TF_MS[interval]
    now = int(time.time() * 1000)
    need_older = not cached or cached[0][0] > since_ms + step
    have_to = cached[-1][0] if cached else None
    fresh, end = [], now
    while True:
        rows = klines(symbol, interval, 1000, end)
        if not rows:
            break
        fresh = rows + fresh
        oldest = rows[0][0]
        if log:
            log(f"{symbol} {TF_NAME[interval]}: до {time.strftime('%Y-%m-%d', time.gmtime(oldest / 1000))}")
        if not need_older and oldest <= have_to:
            break
        if oldest <= since_ms or len(rows) < 1000:
            break
        end = oldest - 1
    merged = {r[0]: r for r in cached}
    merged.update({r[0]: r for r in fresh if r[0] + step <= now})
    out = [merged[t] for t in sorted(merged)]
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(out)
    return [r for r in out if r[0] >= since_ms]


def funding_history(symbol, since_ms):
    """Выплаты фандинга [(t_ms, rate)] по возрастанию. Кеш на диске."""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{symbol}_funding.csv")
    cached = []
    if os.path.exists(path):
        with open(path, newline="") as f:
            cached = [(int(r[0]), float(r[1])) for r in csv.reader(f)]
    need_older = not cached or cached[0][0] > since_ms + 9 * 3_600_000
    have_to = cached[-1][0] if cached else None
    fresh, end = [], int(time.time() * 1000)
    while True:
        rows = api("funding/history", {"category": "linear", "symbol": symbol, "limit": 200, "endTime": end})["list"]
        if not rows:
            break
        part = [(int(x["fundingRateTimestamp"]), float(x["fundingRate"])) for x in rows]
        fresh += part
        oldest = min(p[0] for p in part)
        if (not need_older and oldest <= have_to) or oldest <= since_ms or len(rows) < 200:
            break
        end = oldest - 1
    merged = dict(cached)
    merged.update(dict(fresh))
    out = sorted(merged.items())
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(out)
    return [x for x in out if x[0] >= since_ms]


def ticker(symbol):
    return api("tickers", {"category": "linear", "symbol": symbol})["list"][0]


def instrument(symbol):
    it = api("instruments-info", {"category": "linear", "symbol": symbol})["list"][0]
    tick = it["priceFilter"]["tickSize"]
    step = it["lotSizeFilter"]["qtyStep"]
    dec = lambda s: len(s.split(".")[1].rstrip("0")) if "." in s else 0  # noqa: E731
    return {
        "tick": float(tick), "price_dp": dec(tick),
        "qty_step": float(step), "qty_dp": dec(step),
        "min_qty": float(it["lotSizeFilter"]["minOrderQty"]),
        "base": it["baseCoin"],
    }


def market_context(symbol):
    """Справочный контекст для сообщения: фандинг, изменение открытого интереса, доля лонгов. Ошибки не критичны."""
    out = {}
    try:
        t = ticker(symbol)
        out["funding"] = float(t["fundingRate"])
        out["last"] = float(t["lastPrice"])
        out["turnover24h"] = float(t["turnover24h"])
        out["chg24h"] = float(t["price24hPcnt"]) * 100
    except Exception:
        pass
    try:
        oi = api("open-interest", {"category": "linear", "symbol": symbol, "intervalTime": "15min", "limit": 20})["list"]
        if len(oi) >= 17:
            out["oi_chg_4h"] = (float(oi[0]["openInterest"]) / float(oi[16]["openInterest"]) - 1) * 100
    except Exception:
        pass
    try:
        ra = api("account-ratio", {"category": "linear", "symbol": symbol, "period": "1h", "limit": 1})["list"]
        if ra:
            out["long_share"] = float(ra[0]["buyRatio"]) * 100
    except Exception:
        pass
    return out
