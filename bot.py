"""Сигнальный бот для теста стратегий на Bybit (бессрочные контракты USDT).

Что делает: следит за рынком, по правилам из profiles.json присылает сигналы лонг/шорт
с оценкой риска, ведёт каждую сделку «на бумаге» и подсказывает, когда фиксировать прибыль.
Чего не делает: не подключается к аккаунту и не открывает сделки. Решение всегда за человеком.

Запуск:
  python3 bot.py              работа с Telegram (нужен токен в config.json)
  python3 bot.py --replay 30  прогон последних 30 дней в ускоренном режиме, вывод в это окно
  python3 bot.py --check      проверка связи с Bybit и Telegram
"""
import argparse
import bisect
import html
import json
import os
import random
import re
import sys
import time
import traceback

import bybit
import strategies as st
from bybit import TF_MS, TF_NAME
from engine import COST

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
RESULTS_PATH = os.path.join(HERE, "results.json")

DEFAULT_CONFIG = {
    "telegram_token": "",
    "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT"],
    "deposit_usdt": 1000,
    "profiles_on": {"id_mod": True, "id_high": True, "sw_mod": True, "sw_high": True},
}
OPEN_BOT = os.environ.get("OPEN_BOT", "").strip().lower() in ("1", "yes", "true", "да")
ADMINS = {int(x) for x in os.environ.get("ALLOWED_CHATS", "").replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()}
RISK_WORD = {"moderate": "умеренный", "high": "высокий"}
CLOSE_WORDS = {
    "stop": "Стоп", "be": "Стоп в безубытке", "trail": "Трейлинг-стоп", "tp2": "Тейк 2",
    "flip": "Тренд развернулся", "time": "Выход по времени",
}


# ---------- форматирование ----------
def fnum(x, dp=2):
    return f"{x:,.{dp}f}".replace(",", " ").replace(".", ",")


def fpct(x, dp=2):
    return ("+" if x > 0 else "−" if x < 0 else "") + fnum(abs(x), dp) + "%"


def fr(x):
    return ("+" if x > 0 else "−" if x < 0 else "") + fnum(abs(x), 2) + "R"


def ftime(ms):
    return time.strftime("%d.%m %H:%M", time.localtime(ms / 1000))


def deals(n):
    n = int(n)
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} сделка"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} сделки"
    return f"{n} сделок"


def log(msg):
    print(time.strftime("[%d.%m %H:%M:%S] ") + msg, flush=True)


def plain(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# ---------- источники данных ----------
class Live:
    grace = 5000   # ждём 5 с после закрытия свечи, чтобы биржа её отдала

    def now(self):
        return int(time.time() * 1000)

    def closed(self, sym, tf, limit=1000):
        return bybit.closed_klines(sym, tf, limit)

    def price(self, sym):
        return float(bybit.ticker(sym)["lastPrice"])

    def ranges(self, sym, t_from, now):
        """Свечи, покрывающие (t_from, now]: [(конец, макс, мин)], и время, с которого проверять в следующий раз."""
        gap = max(1, (now - t_from) / 60_000)
        tf = "1" if gap <= 900 else ("5" if gap <= 4500 else "60")
        step = TF_MS[tf]
        rows = bybit.klines(sym, tf, min(1000, int(gap * 60_000 / step) + 3))
        out = [(r[0] + step, r[2], r[3]) for r in rows if r[0] + step > t_from]
        return out, (rows[-1][0] if rows else now)

    def context(self, sym):
        return bybit.market_context(sym)


class Replay:
    """Проигрывает историю из кеша: бот думает, что это живой рынок."""
    grace = 0

    def __init__(self, symbols, start_ms):
        self.t = start_ms
        self.rows, self.ts = {}, {}
        for s in symbols:
            r15 = bybit.history(s, "15", start_ms - 60 * 86_400_000)
            r240 = bybit.history(s, "240", start_ms - 420 * 86_400_000)
            for tf, rows in (("15", r15), ("60", st.resample(r15, "60")), ("240", r240), ("D", st.resample(r240, "D"))):
                self.rows[s, tf] = rows
                self.ts[s, tf] = [r[0] for r in rows]

    def now(self):
        return self.t

    def closed(self, sym, tf, limit=1000):
        i = bisect.bisect_right(self.ts[sym, tf], self.t - TF_MS[tf])
        return self.rows[sym, tf][max(0, i - limit):i]

    def price(self, sym):
        i = bisect.bisect_left(self.ts[sym, "15"], self.t)
        rows = self.rows[sym, "15"]
        return rows[i][1] if i < len(rows) else rows[-1][4]

    def ranges(self, sym, t_from, now):
        ts, rows = self.ts[sym, "15"], self.rows[sym, "15"]
        a = bisect.bisect_right(ts, t_from - TF_MS["15"])
        b = bisect.bisect_right(ts, now - TF_MS["15"])
        return [(r[0] + TF_MS["15"], r[2], r[3]) for r in rows[a:b]], now

    def context(self, sym):
        return {}


# ---------- доставка сообщений ----------
class Console:
    def __init__(self, quiet=False):
        self.quiet, self.count = quiet, 0

    def send(self, chat, text, buttons=None, reply_to=None, silent=False):
        self.count += 1
        if not self.quiet:
            print("\n" + "─" * 60 + "\n" + plain(text), flush=True)
        return None


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, payload=None, timeout=20):
        data = json.dumps(payload or {}).encode("utf-8")
        j = bybit.http_json(self.base + method, data=data, timeout=timeout)
        if not j.get("ok"):
            raise RuntimeError(f"Telegram: {j.get('description')}")
        return j["result"]

    def send(self, chat, text, buttons=None, reply_to=None, silent=False):
        p = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True, "disable_notification": bool(silent)}
        if buttons:
            p["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
        if reply_to:
            p["reply_to_message_id"] = reply_to
            p["allow_sending_without_reply"] = True
        try:
            return self.call("sendMessage", p)["message_id"]
        except Exception as e:  # noqa: BLE001
            log(f"Не удалось отправить сообщение: {e}")
            return None


# ---------- бот ----------
class Bot:
    def __init__(self, cfg, provider, messenger, state_path=STATE_PATH):
        self.cfg, self.data, self.out, self.state_path = cfg, provider, messenger, state_path
        self.profiles = st.load_profiles()
        self.results = {}
        if os.path.exists(RESULTS_PATH):
            with open(RESULTS_PATH, encoding="utf-8") as f:
                self.results = json.load(f).get("profiles", {})
        self.state = {
            "chats": [], "pair_code": f"{random.randint(0, 999999):06d}", "tg_offset": 0, "last_bar": {},
            "open": [], "closed": [], "seq": 0, "deposit": cfg.get("deposit_usdt", 1000),
            "risk_pct": {}, "on": dict(cfg.get("profiles_on", {})),
        }
        if state_path and os.path.exists(state_path):
            with open(state_path, encoding="utf-8") as f:
                self.state.update(json.load(f))
        for x in os.environ.get("ALLOWED_CHATS", "").replace(";", ",").split(","):
            x = x.strip()
            if x.lstrip("-").isdigit() and int(x) not in self.state["chats"]:
                self.state["chats"].append(int(x))   # чаты из настроек хостинга подключаются без /start
        self.pos = {r["id"]: st.Position.from_dict(r["pos"]) for r in self.state["open"]}
        self._inst = {}
        self.auto_take = False   # в режиме --replay все сделки считаются взятыми, чтобы показать сопровождение

    # --- служебное
    def save(self):
        if not self.state_path:
            return
        for r in self.state["open"]:
            r["pos"] = self.pos[r["id"]].to_dict()
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    def inst(self, sym):
        if sym not in self._inst:
            try:
                self._inst[sym] = bybit.instrument(sym)
            except Exception:  # noqa: BLE001
                self._inst[sym] = {"tick": 0.01, "price_dp": 2, "qty_step": 0.001, "qty_dp": 3, "min_qty": 0.001, "base": sym.replace("USDT", "")}
        return self._inst[sym]

    def is_on(self, key):
        return self.state["on"].get(key, True)

    def risk_pct(self, key):
        return self.state["risk_pct"].get(key, self.profiles[key].get("risk_pct", 0.5))

    def fp(self, sym, x):
        return fnum(x, self.inst(sym)["price_dp"])

    def broadcast(self, text, buttons=None, replies=None, silent=False):
        ids = {}
        chats = self.state["chats"] or ([None] if isinstance(self.out, Console) else [])
        for chat in chats:
            mid = self.out.send(chat, text, buttons, (replies or {}).get(str(chat)), silent)
            if mid:
                ids[str(chat)] = mid
        return ids

    def open_of(self, key, sym=None):
        return [r for r in self.state["open"] if r["profile"] == key and (sym is None or r["sym"] == sym)]

    # --- основной цикл
    def tick(self):
        now = self.data.now()
        try:
            self.check_prices(now)
        except Exception as e:  # noqa: BLE001
            log(f"Проверка цен: {e!r}")
        tfs = sorted({self.profiles[k]["tf"] for k in st.ORDER if self.is_on(k) or self.open_of(k)}, key=lambda x: TF_MS[x])
        for tf in tfs:
            step = TF_MS[tf]
            bar_t = now // step * step - step
            if now - (bar_t + step) < self.data.grace:
                continue
            fresh = now - (bar_t + step) <= 180_000
            for sym in self.cfg["symbols"]:
                if self.state["last_bar"].get(f"{sym}|{tf}", 0) >= bar_t:
                    continue
                try:
                    self.on_bar(sym, tf, bar_t, fresh, now)
                    self.state["last_bar"][f"{sym}|{tf}"] = bar_t
                except Exception as e:  # noqa: BLE001
                    log(f"{sym} {TF_NAME[tf]}: {e!r}")
        self.save()

    def on_bar(self, sym, tf, bar_t, fresh, now):
        keys = [k for k in st.ORDER if (self.is_on(k) or self.open_of(k, sym)) and self.profiles[k]["tf"] == tf]
        if not keys:
            return
        rows = self.data.closed(sym, tf, 1000)
        if not rows or rows[-1][0] < bar_t:
            raise RuntimeError("биржа ещё не отдала закрытую свечу")
        ltf, htf_cache, step = st.Series(rows, tf), {}, TF_MS[tf]
        new, why = [], []
        for k in keys:
            prof = self.profiles[k]
            h = prof["htf"]
            if h not in htf_cache:
                htf_cache[h] = st.Series(self.data.closed(sym, h, 1000), h)
            P = st.prepare(prof, ltf, htf_cache[h])
            last = len(ltf) - 1
            for rec in list(self.open_of(k, sym)):
                pos = self.pos[rec["id"]]
                for j in range(max(0, last - 400), last + 1):
                    if ltf.t[j] <= rec["last_bar"] or pos.closed:
                        continue
                    ev = pos.on_close(ltf.c[j], ltf.h[j], ltf.l[j], P["atr"][j], ltf.t[j] + step, P["trend"][j])
                    self.handle(rec, ev, ltf.t[j] + step)
                rec["last_bar"] = ltf.t[last]
            if fresh and self.is_on(k) and not self.open_of(k, sym) and len(self.open_of(k)) < prof["max_open"]:
                sig = st.signal_at(prof, P, last)
                rec = self.open_position(k, sym, sig, ltf.t[last], now) if sig else None
                if rec:
                    new.append(rec)
                    for w in st.explain(prof, P, last, sig[0], lambda x: self.fp(sym, x)):
                        if w not in why:
                            why.append(w)
        if new:
            try:
                ctx = self.data.context(sym)
            except Exception:  # noqa: BLE001
                ctx = {}
            if len(new) == 1:
                buttons = [[("Вошёл", f"in:{new[0]['id']}"), ("Пропустил", f"skip:{new[0]['id']}")]]
            else:
                buttons = [[(f"Вошёл: {RISK_WORD[self.profiles[r['profile']]['risk']]}", f"in:{r['id']}") for r in new],
                           [("Пропустил", "skip:" + ",".join(str(r["id"]) for r in new))]]
            ids = self.broadcast(self.signal_text(new, why, ctx), buttons)
            for r in new:
                r["msgs"] = ids
                if self.auto_take:
                    r["taken"] = True

    def check_prices(self, now):
        by_sym = {}
        for rec in self.state["open"]:
            by_sym.setdefault(rec["sym"], []).append(rec)
        for sym, recs in by_sym.items():
            rows, nxt = self.data.ranges(sym, min(r["chk"] for r in recs), now)
            for rec in recs:
                mine = [r for r in rows if r[0] > rec["chk"]]
                if not mine:
                    continue
                pos = self.pos[rec["id"]]
                hi, lo = max(r[1] for r in mine), min(r[2] for r in mine)
                ev = pos.on_range(None, hi, lo, now)
                rec["chk"] = nxt
                if not pos.closed and not pos.tp1 and not pos.tp2:
                    best = (hi if pos.side > 0 else lo)
                    m = int((best - pos.entry) * pos.side / pos.risk)
                    if m > rec.get("milestone", 0):
                        rec["milestone"] = m
                        ev.append(("milestone", best))
                self.handle(rec, ev, now)

    # --- сделки
    def open_position(self, key, sym, sig, bar_t, now):
        side, stop = sig
        entry = self.data.price(sym)
        if (entry - stop) * side <= 0:
            return None
        self.state["seq"] += 1
        pid = self.state["seq"]
        pos = st.Position(side, entry, stop, self.profiles[key]["ex"], now)
        self.pos[pid] = pos
        rec = {"id": pid, "profile": key, "sym": sym, "chk": now, "last_bar": bar_t, "msgs": {},
               "muted": False, "taken": None, "stop_note": stop, "milestone": 0, "pos": pos.to_dict()}
        self.state["open"].append(rec)
        return rec

    def sizing(self, key, sym, pos):
        inst = self.inst(sym)
        risk_usd = self.state["deposit"] * self.risk_pct(key) / 100
        qty = risk_usd / (pos.risk + (pos.entry + pos.stop0) * COST)
        qty = int(qty / inst["qty_step"] + 1e-9) * inst["qty_step"]
        stop_frac = pos.risk / pos.entry
        lev = max(1, min(50, int(1 / (1.5 * stop_frac + 0.005))))
        return risk_usd, qty, qty * pos.entry, lev, inst

    def exposure(self):
        """Сколько позиций в каждую сторону и какой убыток, если сработают все стопы, % депозита."""
        longs = shorts = 0
        worst = 0.0
        for r in self.state["open"]:
            pos = self.pos[r["id"]]
            if pos.side > 0:
                longs += 1
            else:
                shorts += 1
            at_stop = pos.frac * (pos.stop - pos.entry) * pos.side / pos.risk
            if at_stop < 0:
                worst += -at_stop * self.risk_pct(r["profile"])
        return longs, shorts, worst

    def signal_text(self, recs, why, ctx):
        sym = recs[0]["sym"]
        first = self.pos[recs[0]["id"]]
        f = lambda x: self.fp(sym, x)  # noqa: E731
        horizon = "внутри дня" if self.profiles[recs[0]["profile"]]["horizon"] == "intraday" else "среднесрок"
        L = [f"{'🟢' if first.side > 0 else '🔴'} <b>{'ЛОНГ' if first.side > 0 else 'ШОРТ'} · {sym}</b> · {horizon}",
             f"Вход: <b>{f(first.entry)}</b> по рынку"]
        for rec in recs:
            key = rec["profile"]
            prof, pos, ex = self.profiles[key], self.pos[rec["id"]], self.profiles[key]["ex"]
            L += ["", f"<b>{RISK_WORD[prof['risk']].capitalize()} риск</b> ({prof['code']})",
                  f"Стоп: {f(pos.stop0)} (убыток {fpct(pos.pnl_pct(pos.stop0))} от входа)"]
            if pos.tp1:
                L.append(f"Тейк 1: {f(pos.tp1)} (прибыль {fpct(pos.pnl_pct(pos.tp1))}): закрыть {ex['tp1_frac'] * 100:.0f}%" + (", стоп в безубыток" if ex.get("be") else ""))
            if pos.tp2:
                L.append(f"Тейк 2: {f(pos.tp2)} (прибыль {fpct(pos.pnl_pct(pos.tp2))}): закрыть остаток")
            if not pos.tp1 and not pos.tp2:
                one = pos.risk / pos.entry * 100
                L.append(f"Тейка нет, прибыль забирает трейлинг-стоп ({ex['trail_atr']:g} ATR от лучшей цены). Ориентиры: +1R это {fpct(one)} от входа, +2R это {fpct(2 * one)}")
            elif ex.get("trail_atr"):
                L.append(f"После тейка 1 стоп идёт на {ex['trail_atr']:g} ATR от лучшей цены")
            if ex.get("max_bars"):
                L.append(f"Выход по времени: через {st.span_text(ex['max_bars'], prof['tf'])}")
            risk_usd, qty, notional, lev, inst = self.sizing(key, sym, pos)
            if qty >= inst["min_qty"]:
                size = f"объём {fnum(qty, inst['qty_dp'])} {inst['base']} на {fnum(notional, 0)} USDT"
            else:
                size = f"объём меньше минимального ({inst['min_qty']:g} {inst['base']})"
            L.append(f"Риск {fnum(self.risk_pct(key), 2)}% депозита ({fnum(risk_usd, 2)} USDT), {size}, плечо не выше {lev}x")
            res = self.results.get(key)
            if res:
                te = res["test"]
                L.append(f"На отложенной истории: {deals(te['n'])}, {te['win']:.0f}% прибыльных, в среднем {fr(te['avg'])}. {res['verdict'].capitalize()}.")
        L += ["", "<b>Почему сигнал</b>"] + ["• " + html.escape(w) for w in why]
        c = []
        if "funding" in ctx:
            fr_ = ctx["funding"] * 100
            c.append(f"фандинг {fpct(fr_, 4)} ({'лонги платят шортам' if fr_ > 0 else 'шорты платят лонгам' if fr_ < 0 else 'без выплат'})")
        if "oi_chg_4h" in ctx:
            c.append(f"открытый интерес за 4 ч {fpct(ctx['oi_chg_4h'], 1)}")
        if "long_share" in ctx:
            c.append(f"в лонге {fnum(ctx['long_share'], 1)}% аккаунтов")
        if c:
            L += ["", "<b>Контекст</b> (в правила не входит)", "; ".join(c)]
        longs, shorts, worst = self.exposure()
        L += ["", "<b>Общий риск</b>",
              f"Тестовых позиций у бота: лонгов {longs}, шортов {shorts}. Если сработают все стопы: {fpct(-worst)} депозита."]
        if max(longs, shorts) >= 4:
            L.append("Много позиций в одну сторону. Криптовалюты обычно движутся вместе, поэтому это почти одна большая ставка.")
        L += ["", "Нажмите «Вошёл», и бот будет вести сделку: тейки, перенос стопа, выход. Без нажатия пришлёт только итог.",
              "Это тестовый сигнал по формальным правилам, не инвестиционная рекомендация. Бот сделки не открывает."]
        return "\n".join(L)

    def handle(self, rec, events, now):
        if not events:
            return
        key, sym, pos = rec["profile"], rec["sym"], self.pos[rec["id"]]
        prof = self.profiles[key]
        f = lambda x: self.fp(sym, x)  # noqa: E731
        side = "лонг" if pos.side > 0 else "шорт"
        head = f"<b>{sym} {side}</b> ({prof['code']}, вход {f(pos.entry)})"
        for kind, px in events:
            text = brief = None
            if kind == "trail_move":
                if abs(px - rec["stop_note"]) >= 0.5 * pos.risk:
                    rec["stop_note"] = px
                    locked = pos.pnl_pct(px)
                    text = f"{head}\nСтоп подтянуть до {f(px)}: {'фиксирует прибыль' if locked > 0 else 'убыток не больше'} {fpct(abs(locked)).lstrip('+')} от входа."
            elif kind == "milestone":
                text = f"{head}\nПрибыль {fpct(pos.pnl_pct(px))} от входа, это +{rec['milestone']}R. По правилам профиля держим, стоп сейчас {f(pos.stop)}."
            elif kind == "tp1":
                be = f", стоп перенести в безубыток ({f(pos.entry)})" if prof["ex"].get("be") else ""
                text = f"✅ {head}\nТейк 1 на {f(px)}: прибыль {fpct(pos.pnl_pct(px))} от входа.\nПо правилам: закрыть {prof['ex']['tp1_frac'] * 100:.0f}% позиции{be}."
                rec["stop_note"] = pos.stop
            if pos.closed and kind in CLOSE_WORDS:
                net = pos.r - st.costs_r(pos, COST)
                act = "закрыть остаток" if pos.tp1_done else "закрыть позицию"
                text = (f"{'✅' if net > 0 else '⛔'} {head}\n{CLOSE_WORDS[kind]} на {f(px)}: {fpct(pos.pnl_pct(px))} от входа. По правилам: {act}.\n"
                        f"Итог сделки с комиссиями: {fr(net)}, это {fpct(net * self.risk_pct(key))} депозита.")
                brief = (f"Тест без вашего входа: {sym} {side} ({prof['code']}) закрыт. {CLOSE_WORDS[kind]} на {f(px)}, {fpct(pos.pnl_pct(px))} от входа"
                         f"{', до этого был тейк 1' if pos.tp1_done and kind != 'tp2' else ''}. Итог {fr(net)}.")
                self.state["closed"].append({
                    "id": rec["id"], "profile": key, "sym": sym, "side": pos.side, "t0": pos.t_entry, "t1": now,
                    "entry": pos.entry, "exit": px, "r": net, "reason": kind, "taken": rec.get("taken"),
                })
                self.state["open"] = [r for r in self.state["open"] if r["id"] != rec["id"]]
                self.pos.pop(rec["id"], None)
            if rec.get("muted"):
                continue
            if rec.get("taken") and text:
                self.broadcast(text, replies=rec.get("msgs"))
            elif not rec.get("taken") and brief:
                self.broadcast(brief, replies=rec.get("msgs"), silent=True)

    # --- тексты команд
    def status_text(self):
        if not self.state["open"]:
            return "Открытых тестовых позиций нет."
        L = ["<b>Открытые тестовые позиции</b>"]
        for rec in self.state["open"]:
            pos, sym = self.pos[rec["id"]], rec["sym"]
            try:
                px = self.data.price(sym)
            except Exception:  # noqa: BLE001
                px = pos.entry
            nxt = f"тейк 1 {self.fp(sym, pos.tp1)}" if (pos.tp1 and not pos.tp1_done) else (f"тейк 2 {self.fp(sym, pos.tp2)}" if pos.tp2 else "трейлинг")
            tag = " · вы в сделке" if rec.get("taken") else (" · пропущена" if rec.get("muted") else " · только тест")
            L.append(f"\n{'🟢' if pos.side > 0 else '🔴'} <b>{sym}</b> {self.profiles[rec['profile']]['code']}{tag}\n"
                     f"вход {self.fp(sym, pos.entry)} ({ftime(pos.t_entry)}), сейчас {self.fp(sym, px)}: {fpct(pos.pnl_pct(px))}\n"
                     f"стоп {self.fp(sym, pos.stop)}, дальше {nxt}, в позиции {pos.frac * 100:.0f}%")
        return "\n".join(L)

    def stats_text(self):
        L = ["<b>Тест на живом рынке</b> (все сигналы бота, на бумаге)"]
        closed = self.state["closed"]
        if not closed:
            return L[0] + "\nЗакрытых сделок пока нет."
        for k in st.ORDER:
            tr = [t for t in closed if t["profile"] == k]
            if not tr:
                L.append(f"\n{self.profiles[k]['title']}: сделок нет")
                continue
            rs = [t["r"] for t in tr]
            L.append(f"\n<b>{self.profiles[k]['title']}</b>\n{deals(len(tr))}, прибыльных {sum(1 for r in rs if r > 0) / len(rs) * 100:.0f}%, "
                     f"в среднем {fr(sum(rs) / len(rs))}, итог {fr(sum(rs))} = {fpct(sum(rs) * self.risk_pct(k))} депозита")
        first = min(t["t0"] for t in closed)
        L.append(f"\nС {ftime(first)}. Открыто сейчас: {len(self.state['open'])}.")
        return "\n".join(L)

    def test_text(self):
        if not self.results:
            return "Нет файла results.json. Запустите: python3 backtest.py"
        L = ["<b>Проверка правил на истории</b>", "Комиссия тейкера и проскальзывание учтены. «Проверка» — период, который не участвовал в подборе."]
        for k in st.ORDER:
            r = self.results.get(k)
            if not r:
                continue
            L.append(f"\n<b>{r['title']}</b>")
            for name, s in (("Обучение", r["train"]), ("Проверка", r["test"])):
                L.append(f"{name}: {deals(s['n'])}, {s['win']:.0f}% прибыльных, в среднем {fr(s['avg'])}, итог {fr(s['sum'])}, просадка {fnum(s['dd'], 1)}R")
            L.append(f"Вывод: {r['verdict']}.")
        return "\n".join(L)

    def rules_text(self):
        L = ["<b>Правила профилей</b>"]
        for k in st.ORDER:
            p = self.profiles[k]
            L.append(f"\n<b>{p['title']}</b> ({p['code']}) · {'включён' if self.is_on(k) else 'выключен'} · риск {fnum(self.risk_pct(k), 2)}% · /{'off' if self.is_on(k) else 'on'}_{k}")
            L += ["• " + html.escape(x) for x in st.describe(p)]
        return "\n".join(L)

    def market_text(self):
        L = ["<b>Рынок сейчас</b>"]
        for sym in self.cfg["symbols"]:
            try:
                ctx = self.data.context(sym)
                marks = []
                for tf in ("240", "D"):
                    s = st.Series(self.data.closed(sym, tf, 1000), tf)
                    P = st.prepare({"family": "breakout", "p": {"htf_fast": 50, "htf_slow": 200, "n": 2}}, s, s)
                    tr = P["trend"][-1]
                    marks.append(f"{TF_NAME[tf]} {'↑' if tr > 0 else '↓' if tr < 0 else '→'}")
                L.append(f"\n<b>{sym}</b> {self.fp(sym, ctx.get('last', 0))} ({fpct(ctx.get('chg24h', 0))} за 24 ч)\n"
                         f"тренд: {', '.join(marks)}; фандинг {fpct(ctx.get('funding', 0) * 100, 4)}; в лонге {fnum(ctx.get('long_share', 0), 0)}% аккаунтов")
            except Exception as e:  # noqa: BLE001
                L.append(f"\n{sym}: нет данных ({e})")
        L.append("\nТренд: ↑ цена > EMA50 > EMA200, ↓ наоборот, → смешанно.")
        return "\n".join(L)

    HELP = ("<b>Команды</b>\n"
            "/status — открытые тестовые позиции и результат по каждой\n"
            "/stats — итоги теста на живом рынке\n"
            "/test — результаты проверки на истории\n"
            "/rules — правила всех профилей, включение и выключение\n"
            "/market — тренды, фандинг и перекос по инструментам\n"
            "/deposit 5000 — депозит для расчёта объёма\n"
            "/risk id_mod 0.3 — риск на сделку в % для профиля (id_mod, id_high, sw_mod, sw_high)\n\n"
            "Под каждым сигналом кнопки. «Вошёл»: бот ведёт сделку и пишет про тейки, перенос стопа и выход. «Пропустил»: бот считает сделку для статистики молча. Без нажатия пришлёт только итог.\n\n"
            "Бот не подключается к аккаунту биржи и сам сделки не открывает.")

    # --- Telegram
    def on_update(self, u, tg):
        cb = u.get("callback_query")
        if cb:
            chat = cb["message"]["chat"]["id"]
            if chat not in self.state["chats"]:
                return
            m = re.match(r"(in|skip):([\d,]+)$", cb.get("data", ""))
            note = "Сделка уже закрыта"
            if m:
                ids = [int(x) for x in m.group(2).split(",") if x]
                mid = str(cb["message"]["message_id"])
                group = [r for r in self.state["open"] if r["id"] in ids or mid in [str(v) for v in r.get("msgs", {}).values()]]
                hit = [r for r in group if r["id"] in ids]
                if hit and m.group(1) == "in":
                    for r in group:
                        if r["id"] in ids:
                            r["taken"], r["muted"] = True, False
                        elif not r.get("taken"):
                            r["taken"], r["muted"] = False, True
                    note = "Отметил: вы в сделке. Буду вести её до выхода."
                elif hit:
                    for r in hit:
                        r["taken"], r["muted"] = False, True
                    note = "Пропущена. Писать по ней не буду, в статистику пойдёт."
            try:
                tg.call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": note})
                tg.call("editMessageReplyMarkup", {"chat_id": chat, "message_id": cb["message"]["message_id"], "reply_markup": {"inline_keyboard": []}})
            except Exception:  # noqa: BLE001
                pass
            return
        msg = u.get("message") or {}
        text, chat = (msg.get("text") or "").strip(), (msg.get("chat") or {}).get("id")
        if not text or chat is None:
            return
        cmd, *args = text.split()
        cmd = cmd.split("@")[0].lower()
        if chat not in self.state["chats"]:
            if cmd == "/start" and OPEN_BOT:
                self.state["chats"].append(chat)
                self.save()
                tg.send(chat, "Готово, этот чат подключён. Сигналы будут приходить сюда.\n\n" + self.HELP)
            elif cmd == "/start" and args and args[0] == self.state["pair_code"]:
                self.state["chats"].append(chat)
                self.save()
                tg.send(chat, "Готово, этот чат подключён. Сигналы будут приходить сюда.\n\n" + self.HELP)
            else:
                tg.send(chat, "Бот приватный. Пришлите /start и код из окна, где запущен бот. Например: /start 123456")
            return
        reply = None
        if ADMINS and chat not in ADMINS and (cmd in ("/deposit", "/risk") or re.match(r"/(on|off)", cmd)):
            tg.send(chat, "Менять настройки бота может только владелец.")
            return
        if cmd in ("/start", "/help"):
            reply = self.HELP
        elif cmd == "/status":
            reply = self.status_text()
        elif cmd == "/stats":
            reply = self.stats_text()
        elif cmd == "/test":
            reply = self.test_text()
        elif cmd == "/rules":
            reply = self.rules_text()
        elif cmd == "/market":
            reply = self.market_text()
        elif cmd == "/deposit":
            try:
                v = float(args[0].replace(",", "."))
                assert v > 0
                self.state["deposit"] = v
                reply = f"Депозит для расчёта объёма: {fnum(v, 0)} USDT."
            except Exception:  # noqa: BLE001
                reply = f"Сейчас {fnum(self.state['deposit'], 0)} USDT. Чтобы изменить: /deposit 5000"
        elif cmd == "/risk":
            try:
                key, v = args[0], float(args[1].replace(",", "."))
                assert key in self.profiles and 0 < v <= 5
                self.state["risk_pct"][key] = v
                reply = f"{self.profiles[key]['title']}: риск {fnum(v, 2)}% на сделку."
            except Exception:  # noqa: BLE001
                reply = "Формат: /risk id_mod 0.3. Профили: " + ", ".join(f"{k} ({fnum(self.risk_pct(k), 2)}%)" for k in st.ORDER)
        else:
            m = re.match(r"/(on|off)_?(\w+)?$", cmd)
            key = (m.group(2) if m and m.group(2) else (args[0] if args else "")) if m else ""
            if m and key in self.profiles:
                self.state["on"][key] = m.group(1) == "on"
                reply = f"{self.profiles[key]['title']}: {'включён' if self.state['on'][key] else 'выключен. Открытые позиции по нему бот доведёт'}."
            else:
                reply = "Не знаю такую команду.\n\n" + self.HELP
        if reply:
            tg.send(chat, reply)
        self.save()


# ---------- запуск ----------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    env_token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    elif env_token:
        pass   # на хостинге: настройки берутся из переменных окружения
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        log("Создал config.json. Впишите в него токен бота из @BotFather (поле telegram_token).")
    if env_token:
        cfg["telegram_token"] = env_token
    if os.environ.get("DEPOSIT_USDT"):
        cfg["deposit_usdt"] = float(os.environ["DEPOSIT_USDT"].replace(",", "."))
    return cfg


def run_live(cfg):
    token = (cfg.get("telegram_token") or "").strip()
    tg = Telegram(token) if token else None
    bot = Bot(cfg, Live(), tg or Console())
    if tg:
        me = tg.call("getMe")
        log(f"Бот @{me['username']} запущен. Инструменты: {', '.join(cfg['symbols'])}")
        if not bot.state["chats"]:
            log(f"Откройте бота в Telegram и отправьте ему: /start {bot.state['pair_code']}")
        else:
            log(f"Подключено чатов: {len(bot.state['chats'])}. Код для ещё одного: /start {bot.state['pair_code']}")
        bot.save()
    else:
        log("Токена нет: сигналы будут печататься в это окно. Чтобы получать их в Telegram, впишите токен в config.json.")
    last_tick = 0.0
    while True:
        try:
            if tg:
                ups = tg.call("getUpdates", {"offset": bot.state["tg_offset"], "timeout": 8, "allowed_updates": ["message", "callback_query"]}, timeout=20)
                for u in ups:
                    bot.state["tg_offset"] = u["update_id"] + 1
                    try:
                        bot.on_update(u, tg)
                    except Exception:  # noqa: BLE001
                        log("Ошибка в команде:\n" + traceback.format_exc())
            else:
                time.sleep(5)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"Telegram недоступен: {e!r}")
            time.sleep(5)
        if time.time() - last_tick >= 20:
            last_tick = time.time()
            try:
                bot.tick()
            except Exception:  # noqa: BLE001
                log("Ошибка в цикле:\n" + traceback.format_exc())


def run_replay(cfg, days, quiet=False):
    end = int(time.time() * 1000) // TF_MS["15"] * TF_MS["15"]
    start = end - days * 86_400_000
    log(f"Загружаю историю и проигрываю последние {days} дн. Это тест на прошлом, а не прогноз.")
    data = Replay(cfg["symbols"], start)
    bot = Bot(cfg, data, Console(quiet), state_path=None)
    bot.auto_take = True
    t = start
    while t <= end:
        data.t = t
        bot.tick()
        t += TF_MS["15"]
    print("\n" + "═" * 60 + "\n" + plain(bot.stats_text()))
    print(f"\nСообщений отправлено бы: {bot.out.count}")
    return bot


def run_check(cfg):
    for sym in cfg["symbols"]:
        t = bybit.ticker(sym)
        print(f"Bybit {sym}: {t['lastPrice']}")
    token = (cfg.get("telegram_token") or "").strip()
    if token:
        print("Telegram: бот @" + Telegram(token).call("getMe")["username"])
    else:
        print("Telegram: токен не задан в config.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Сигнальный бот для теста стратегий на Bybit")
    ap.add_argument("--replay", type=int, metavar="ДНЕЙ", help="проиграть последние N дней в ускоренном режиме")
    ap.add_argument("--quiet", action="store_true", help="в режиме --replay печатать только итог")
    ap.add_argument("--check", action="store_true", help="проверить связь с Bybit и Telegram")
    a = ap.parse_args()
    config = load_config()
    try:
        if a.check:
            run_check(config)
        elif a.replay:
            run_replay(config, a.replay, a.quiet)
        else:
            run_live(config)
    except KeyboardInterrupt:
        print("\nОстановлен.")
        sys.exit(0)
