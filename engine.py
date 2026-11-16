"""Прогон стратегии по истории и подсчёт статистики."""
import bisect
import csv
import os

import strategies as st
from bybit import DATA_DIR, TF_MS

FEE = 0.00055    # комиссия тейкера Bybit за одну сторону
SLIP = 0.0002    # допущение о проскальзывании за одну сторону
COST = FEE + SLIP


def load_rows(symbol, tf):
    with open(os.path.join(DATA_DIR, f"{symbol}_{tf}.csv"), newline="") as f:
        return [[int(r[0])] + [float(x) for x in r[1:]] for r in csv.reader(f)]


def load_funding(symbol):
    path = os.path.join(DATA_DIR, f"{symbol}_funding.csv")
    if not os.path.exists(path):
        return [], []
    with open(path, newline="") as f:
        rows = [(int(r[0]), float(r[1])) for r in csv.reader(f)]
    ts = [r[0] for r in rows]
    cum, s = [0.0], 0.0
    for _, r in rows:
        s += r
        cum.append(s)
    return ts, cum


def funding_between(fund, t0, t1):
    """Сумма ставок фандинга, выплаченных в интервале (t0, t1]."""
    ts, cum = fund
    if not ts:
        return 0.0
    return cum[bisect.bisect_right(ts, t1)] - cum[bisect.bisect_right(ts, t0)]


class Market:
    """Свечи одного инструмента: рабочий и старший таймфрейм для каждого горизонта."""

    def __init__(self, symbol):
        self.symbol = symbol
        r15, r240 = load_rows(symbol, "15"), load_rows(symbol, "240")
        self.series = {
            "15": st.Series(r15, "15"), "60": st.Series(st.resample(r15, "60"), "60"),
            "240": st.Series(r240, "240"), "D": st.Series(st.resample(r240, "D"), "D"),
        }
        self.funding = load_funding(symbol)


def run(profile, mkt, cost=COST):
    """Все сделки профиля по одному инструменту. Вход по открытию свечи, следующей за сигнальной."""
    ltf, htf = mkt.series[profile["tf"]], mkt.series[profile["htf"]]
    P = st.prepare(profile, ltf, htf)
    ex, step = profile["ex"], TF_MS[profile["tf"]]
    T, O, H, L, C, A, TR = ltf.t, ltf.o, ltf.h, ltf.l, ltf.c, P["atr"], P["trend"]
    trades, pos, pending = [], None, None
    for i in range(250, len(T)):
        first = False
        if pending is not None:
            side, stop = pending
            pending = None
            if (O[i] - stop) * side > 0:
                pos, first = st.Position(side, O[i], stop, ex, T[i]), True
        if pos is not None:
            pos.on_range(None if first else O[i], H[i], L[i], T[i] + step)
            if not pos.closed:
                pos.on_close(C[i], H[i], L[i], A[i], T[i] + step, TR[i])
            if pos.closed:
                gross = pos.r
                net = gross - st.costs_r(pos, cost, funding_between(mkt.funding, pos.t_entry, pos.t_exit))
                trades.append({
                    "sym": mkt.symbol, "side": pos.side, "t0": pos.t_entry, "t1": pos.t_exit,
                    "entry": pos.entry, "stop_pct": pos.risk / pos.entry * 100,
                    "gross": gross, "r": net, "bars": pos.bars, "reason": pos.reason,
                    "tp1": pos.tp1_done,
                })
                pos = None
        if pos is None:
            sig = st.signal_at(profile, P, i)
            if sig:
                pending = sig
    return trades


def portfolio(trades, max_open):
    """Объединяет сделки по инструментам: не больше max_open позиций одновременно."""
    out, open_until = [], []
    for tr in sorted(trades, key=lambda x: x["t0"]):
        open_until = [t for t in open_until if t > tr["t0"]]
        if len(open_until) >= max_open:
            continue
        open_until.append(tr["t1"])
        out.append(tr)
    return out


def stats(trades):
    n = len(trades)
    if not n:
        return {"n": 0, "win": 0.0, "avg": 0.0, "avg_gross": 0.0, "pf": 0.0, "sum": 0.0, "dd": 0.0, "stop_pct": 0.0, "hold_h": 0.0, "per_month": 0.0}
    rs = [t["r"] for t in trades]
    pos, neg = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    eq = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: x["t1"]):
        eq += t["r"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    span = (max(t["t1"] for t in trades) - min(t["t0"] for t in trades)) / (30.44 * 86_400_000) or 1
    return {
        "n": n, "win": sum(1 for r in rs if r > 0) / n * 100, "avg": sum(rs) / n,
        "avg_gross": sum(t["gross"] for t in trades) / n,
        "pf": pos / neg if neg else float("inf"), "sum": sum(rs), "dd": dd,
        "stop_pct": sorted(t["stop_pct"] for t in trades)[n // 2],
        "hold_h": sum(t["t1"] - t["t0"] for t in trades) / n / 3_600_000,
        "per_month": n / span,
    }


def split(trades, t_split):
    return [t for t in trades if t["t0"] < t_split], [t for t in trades if t["t0"] >= t_split]
