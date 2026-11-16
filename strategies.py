"""Правила стратегий и сопровождение позиции.

Один и тот же код используют бэктест (backtest.py) и бот (bot.py), поэтому
сигналы в боте совпадают с тем, что проверено на истории.

Четыре профиля: внутри дня и среднесрок, у каждого умеренный и высокий риск.
Параметры лежат в profiles.json: его пишет backtest.py после отбора на истории.
"""
import json
import os

import indicators as ind
from bybit import TF_MS, TF_NAME

NAN = float("nan")
HERE = os.path.dirname(os.path.abspath(__file__))

# Исходные параметры. Они заданы до тестов; backtest.py может уточнить их на обучающем отрезке.
DEFAULT_PROFILES = {
    "id_mod": {
        "title": "Внутри дня · умеренный риск", "code": "ВД-У", "horizon": "intraday", "risk": "moderate",
        "tf": "15", "htf": "60", "family": "pullback",
        "p": {"htf_fast": 50, "htf_slow": 200, "adx_min": 0, "ema": 20, "rsi_dip": 40, "dip_look": 8,
              "swing": 8, "stop_buf": 0.2, "min_stop": 1.0, "max_stop": 3.0},
        "ex": {"tp1_r": 1.0, "tp1_frac": 0.5, "be": True, "tp2_r": 2.5, "trail_atr": None, "max_bars": 48, "flip": True},
        "risk_pct": 0.5, "max_open": 3,
    },
    "id_high": {
        "title": "Внутри дня · высокий риск", "code": "ВД-В", "horizon": "intraday", "risk": "high",
        "tf": "15", "htf": "60", "family": "breakout",
        "p": {"htf_fast": 50, "htf_slow": 200, "adx_min": 0, "n": 32, "vol": 1.8, "stop_atr": 1.5, "htf_filter": 0},
        "ex": {"tp1_r": 1.5, "tp1_frac": 0.5, "be": True, "tp2_r": None, "trail_atr": 3.0, "max_bars": 48, "flip": False},
        "risk_pct": 1.5, "max_open": 3,
    },
    "sw_mod": {
        "title": "Среднесрок · умеренный риск", "code": "СС-У", "horizon": "swing", "risk": "moderate",
        "tf": "240", "htf": "D", "family": "pullback",
        "p": {"htf_fast": 20, "htf_slow": 50, "adx_min": 0, "ema": 20, "rsi_dip": 40, "dip_look": 6,
              "swing": 10, "stop_buf": 0.2, "min_stop": 1.0, "max_stop": 3.0},
        "ex": {"tp1_r": 1.5, "tp1_frac": 0.5, "be": True, "tp2_r": None, "trail_atr": 3.0, "max_bars": 90, "flip": True},
        "risk_pct": 1.0, "max_open": 3,
    },
    "sw_high": {
        "title": "Среднесрок · высокий риск", "code": "СС-В", "horizon": "swing", "risk": "high",
        "tf": "240", "htf": "D", "family": "breakout",
        "p": {"htf_fast": 20, "htf_slow": 50, "adx_min": 0, "n": 30, "vol": 1.5, "stop_atr": 1.5, "htf_filter": 0},
        "ex": {"tp1_r": 2.0, "tp1_frac": 0.3, "be": True, "tp2_r": None, "trail_atr": 3.0, "max_bars": None, "flip": False},
        "risk_pct": 2.0, "max_open": 3,
    },
}
ORDER = ["id_mod", "id_high", "sw_mod", "sw_high"]


def load_profiles():
    """Профили с параметрами после отбора (profiles.json), иначе исходные."""
    path = os.path.join(HERE, "profiles.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return json.loads(json.dumps(DEFAULT_PROFILES))


class Series:
    __slots__ = ("t", "o", "h", "l", "c", "v", "tf")

    def __init__(self, rows, tf):
        self.tf = tf
        self.t = [r[0] for r in rows]
        self.o = [r[1] for r in rows]
        self.h = [r[2] for r in rows]
        self.l = [r[3] for r in rows]
        self.c = [r[4] for r in rows]
        self.v = [r[5] for r in rows]

    def __len__(self):
        return len(self.t)


def resample(rows, tf_to):
    """Собирает старший таймфрейм из младших свечей (границы по UTC, как у Bybit)."""
    step = TF_MS[tf_to]
    out, cur = [], None
    for t, o, h, l, c, v in rows:
        b = t - t % step
        if cur is None or cur[0] != b:
            cur = [b, o, h, l, c, v]
            out.append(cur)
        else:
            if h > cur[2]:
                cur[2] = h
            if l < cur[3]:
                cur[3] = l
            cur[4] = c
            cur[5] += v
    return out


def prepare(profile, ltf, htf):
    """Считает всё, что нужно правилам. Старший таймфрейм берётся только по закрытым свечам."""
    p = profile["p"]
    P = {"ltf": ltf, "atr": ind.atr(ltf.h, ltf.l, ltf.c, 14)}
    # --- тренд старшего таймфрейма, привязанный к каждой свече младшего без заглядывания вперёд
    hf, hs = ind.ema(htf.c, p["htf_fast"]), ind.ema(htf.c, p["htf_slow"])
    hadx = ind.adx(htf.h, htf.l, htf.c, 14) if p.get("adx_min") else None
    htrend = [0] * len(htf)
    for j in range(len(htf)):
        if hadx is not None and not (hadx[j] >= p["adx_min"]):
            continue
        c = htf.c[j]
        if c > hf[j] > hs[j]:
            htrend[j] = 1
        elif c < hf[j] < hs[j]:
            htrend[j] = -1
    lms, hms = TF_MS[ltf.tf], TF_MS[htf.tf]
    trend, hidx, j = [0] * len(ltf), [-1] * len(ltf), -1
    for i in range(len(ltf)):
        close_t = ltf.t[i] + lms
        while j + 1 < len(htf) and htf.t[j + 1] + hms <= close_t:
            j += 1
        hidx[i] = j
        trend[i] = htrend[j] if j >= 0 else 0
    P.update(trend=trend, hidx=hidx, htf=htf, hf=hf, hs=hs)
    if profile["family"] == "pullback":
        r = ind.rsi(ltf.c, 14)
        P.update(
            ema=ind.ema(ltf.c, p["ema"]), rsi=r,
            rsi_min=ind.rolling_min(r, p["dip_look"]), rsi_max=ind.rolling_max(r, p["dip_look"]),
            sw_lo=ind.rolling_min(ltf.l, p["swing"]), sw_hi=ind.rolling_max(ltf.h, p["swing"]),
        )
    else:
        P.update(
            hh=ind.shift(ind.rolling_max(ltf.h, p["n"])), ll=ind.shift(ind.rolling_min(ltf.l, p["n"])),
            vsma=ind.shift(ind.sma(ltf.v, 20)),
        )
    return P


def signal_at(profile, P, i):
    """Сигнал на закрытии свечи i: (сторона, цена стопа) или None. Сторона: +1 лонг, -1 шорт."""
    p, s = profile["p"], P["ltf"]
    a, c = P["atr"][i], s.c[i]
    if not a > 0:
        return None
    tr = P["trend"][i]
    if profile["family"] == "pullback":
        e, e1 = P["ema"][i], P["ema"][i - 1]
        if tr == 1 and P["rsi_min"][i] <= p["rsi_dip"] and c > e and c > s.o[i] and s.c[i - 1] <= e1:
            stop = P["sw_lo"][i] - p["stop_buf"] * a
            if c - stop > p["max_stop"] * a:
                return None
            return 1, min(stop, c - p["min_stop"] * a)
        if tr == -1 and P["rsi_max"][i] >= 100 - p["rsi_dip"] and c < e and c < s.o[i] and s.c[i - 1] >= e1:
            stop = P["sw_hi"][i] + p["stop_buf"] * a
            if stop - c > p["max_stop"] * a:
                return None
            return -1, max(stop, c + p["min_stop"] * a)
        return None
    # breakout
    if not s.v[i] > p["vol"] * P["vsma"][i]:
        return None
    f = p.get("htf_filter", 0)   # 0: без фильтра, 1: не против тренда, 2: только по тренду
    if c > P["hh"][i] and (f == 0 or (f == 1 and tr >= 0) or (f == 2 and tr == 1)):
        return 1, c - p["stop_atr"] * a
    if c < P["ll"][i] and (f == 0 or (f == 1 and tr <= 0) or (f == 2 and tr == -1)):
        return -1, c + p["stop_atr"] * a
    return None


def explain(profile, P, i, side, fmt):
    """Причины сигнала словами. fmt форматирует цену."""
    p, s = profile["p"], P["ltf"]
    tf, htf = TF_NAME[profile["tf"]], TF_NAME[profile["htf"]]
    j = P["hidx"][i]
    out = []
    tr = P["trend"][i]
    if j >= 0 and tr != 0:
        sign = ">" if tr > 0 else "<"
        out.append(f"Тренд {htf} {'вверх' if tr > 0 else 'вниз'}: цена {fmt(P['htf'].c[j])} {sign} EMA{p['htf_fast']} {fmt(P['hf'][j])} {sign} EMA{p['htf_slow']} {fmt(P['hs'][j])}")
    elif j >= 0:
        out.append(f"Тренд {htf} смешанный, сигнал без фильтра тренда")
    if profile["family"] == "pullback":
        ext = P["rsi_min"][i] if side > 0 else P["rsi_max"][i]
        out.append(f"Откат закончился: RSI {tf} {'опускался до' if side > 0 else 'поднимался до'} {ext:.0f}, сейчас {P['rsi'][i]:.0f}")
        out.append(f"Свеча {tf} закрылась {'выше' if side > 0 else 'ниже'} EMA{p['ema']} ({fmt(P['ema'][i])})")
    else:
        lvl = P["hh"][i] if side > 0 else P["ll"][i]
        out.append(f"Закрытие {tf} {'выше максимума' if side > 0 else 'ниже минимума'} за {span_text(p['n'], profile['tf'])}: уровень {fmt(lvl)}")
        if P["vsma"][i] > 0:
            out.append("Объём свечи: " + f"{s.v[i] / P['vsma'][i]:.1f}".replace(".", ",") + " от среднего")
    return out


class Position:
    """Сопровождение одной позиции по правилам профиля. R = расстояние от входа до исходного стопа."""

    def __init__(self, side, entry, stop, ex, t):
        self.side, self.entry, self.stop0, self.stop = side, entry, stop, stop
        self.risk = abs(entry - stop)
        self.ex = ex
        self.tp1 = entry + side * ex["tp1_r"] * self.risk if ex.get("tp1_r") else None
        self.tp2 = entry + side * ex["tp2_r"] * self.risk if ex.get("tp2_r") else None
        self.frac, self.r, self.tp1_done = 1.0, 0.0, False
        self.extreme, self.bars = entry, 0
        self.t_entry, self.t_exit = t, None
        self.closed, self.reason = False, None
        self.fills = []   # (время, цена, доля, причина)

    # --- сериализация для бота
    def to_dict(self):
        return {k: getattr(self, k) for k in (
            "side", "entry", "stop0", "stop", "risk", "ex", "tp1", "tp2", "frac", "r", "tp1_done",
            "extreme", "bars", "t_entry", "t_exit", "closed", "reason", "fills")}

    @classmethod
    def from_dict(cls, d):
        o = cls.__new__(cls)
        for k, v in d.items():
            setattr(o, k, v)
        o.fills = [tuple(f) for f in o.fills]
        return o

    def _fill(self, price, frac, reason, t):
        self.r += frac * (price - self.entry) * self.side / self.risk
        self.frac -= frac
        self.fills.append((t, price, frac, reason))
        if self.frac <= 1e-9:
            self.frac, self.closed, self.reason, self.t_exit = 0.0, True, reason, t

    def pnl_pct(self, price):
        return (price / self.entry - 1) * 100 * self.side

    def on_range(self, o, h, l, t):
        """Проверка уровней по диапазону цен. Если в одной свече задеты и стоп, и тейк, считаем, что стоп был первым."""
        ev = []
        if self.closed:
            return ev
        s = self.side
        if (l <= self.stop) if s > 0 else (h >= self.stop):
            px = self.stop
            if o is not None and ((s > 0 and o < px) or (s < 0 and o > px)):
                px = o   # гэп через стоп: исполняемся по цене открытия
            kind = "stop" if self.stop == self.stop0 else ("be" if abs(self.stop - self.entry) < 1e-9 * self.entry else "trail")
            self._fill(px, self.frac, kind, t)
            ev.append((kind, px))
            return ev
        if self.tp1 is not None and not self.tp1_done and ((h >= self.tp1) if s > 0 else (l <= self.tp1)):
            self.tp1_done = True
            self._fill(self.tp1, min(self.frac, self.ex["tp1_frac"]), "tp1", t)
            if self.ex.get("be") and not self.closed and (self.stop - self.entry) * s < 0:
                self.stop = self.entry
            ev.append(("tp1", self.tp1))
        if not self.closed and self.tp2 is not None and ((h >= self.tp2) if s > 0 else (l <= self.tp2)):
            px = self.tp2
            self._fill(px, self.frac, "tp2", t)
            ev.append(("tp2", px))
        return ev

    def on_close(self, c, h, l, atr, t, trend):
        """Закрытие свечи рабочего таймфрейма: трейлинг, выход по смене тренда и по времени."""
        ev = []
        if self.closed:
            return ev
        s, ex = self.side, self.ex
        self.bars += 1
        self.extreme = max(self.extreme, h) if s > 0 else min(self.extreme, l)
        if ex.get("flip") and trend == -s:
            self._fill(c, self.frac, "flip", t)
            return [("flip", c)]
        if ex.get("max_bars") and self.bars >= ex["max_bars"]:
            self._fill(c, self.frac, "time", t)
            return [("time", c)]
        if ex.get("trail_atr") and (self.tp1_done or ex.get("trail_start")) and atr > 0:
            new = self.extreme - s * ex["trail_atr"] * atr
            if (new - self.stop) * s > 0 and (c - new) * s > 0:
                self.stop = new
                ev.append(("trail_move", new))
        return ev


def costs_r(pos, cost_rate, funding_sum=0.0):
    """Комиссии, проскальзывание и фандинг в единицах R."""
    paid = pos.entry * cost_rate + sum(f[2] * f[1] * cost_rate for f in pos.fills)
    return (paid + pos.side * funding_sum * pos.entry) / pos.risk


def span_text(bars, tf):
    hours = bars * TF_MS[tf] / 3_600_000
    return f"{hours:.0f} ч" if hours < 48 else f"{hours / 24:.0f} дн"


def _g(x):
    return f"{x:g}".replace(".", ",")


def describe(profile):
    """Правила профиля словами: для бота и отчёта."""
    p, ex = profile["p"], profile["ex"]
    tf, htf = TF_NAME[profile["tf"]], TF_NAME[profile["htf"]]
    out = []
    if profile["family"] == "breakout":
        vol = f" Объём сигнальной свечи выше среднего за 20 свечей минимум в {_g(p['vol'])} раза." if p.get("vol") else ""
        out.append(f"Вход: свеча {tf} закрылась выше максимума (лонг) или ниже минимума (шорт) за {p['n']} свечей ({span_text(p['n'], profile['tf'])}).{vol}")
        flt = p.get("htf_filter", 0)
        if flt == 2:
            out.append(f"Фильтр: только по тренду {htf}. Лонг, если цена > EMA{p['htf_fast']} > EMA{p['htf_slow']}; шорт, если цена < EMA{p['htf_fast']} < EMA{p['htf_slow']}.")
        elif flt == 1:
            out.append(f"Фильтр: не против тренда {htf}.")
        else:
            out.append("Фильтра тренда нет: сигналы в обе стороны.")
        out.append(f"Стоп: {_g(p['stop_atr'])} ATR(14) на {tf} от цены сигнала.")
    else:
        out.append(f"Вход: по тренду {htf} (цена и EMA{p['htf_fast']} по одну сторону от EMA{p['htf_slow']}) после отката. RSI(14) на {tf} уходил за {p['rsi_dip']} (лонг) или {100 - p['rsi_dip']} (шорт), затем свеча закрылась обратно за EMA{p['ema']}.")
        out.append(f"Стоп: за экстремумом последних {p['swing']} свечей, от {_g(p['min_stop'])} до {_g(p['max_stop'])} ATR.")
    if ex.get("tp1_r"):
        out.append(f"Тейк 1: +{_g(ex['tp1_r'])}R, закрыть {ex['tp1_frac'] * 100:.0f}% позиции" + (", стоп в безубыток." if ex.get("be") else "."))
    if ex.get("tp2_r"):
        out.append(f"Тейк 2: +{_g(ex['tp2_r'])}R, закрыть остаток.")
    if ex.get("trail_atr"):
        when = "сразу после входа" if ex.get("trail_start") else "после тейка 1"
        out.append(f"Трейлинг: {when} стоп идёт на {_g(ex['trail_atr'])} ATR от лучшей цены.")
    if not ex.get("tp1_r") and not ex.get("tp2_r"):
        out.append("Фиксированного тейка нет: прибыль забирает трейлинг-стоп.")
    if ex.get("max_bars"):
        out.append(f"Выход по времени: через {span_text(ex['max_bars'], profile['tf'])}, если стоп не сработал.")
    if ex.get("flip"):
        out.append(f"Досрочный выход: тренд {htf} развернулся против позиции.")
    out.append("R — расстояние от входа до исходного стопа.")
    return out
