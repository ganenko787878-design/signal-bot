"""Индикаторы на обычных списках. Пока данных мало, значение равно NaN, и любое сравнение с ним ложно."""
from collections import deque

NAN = float("nan")


def sma(x, n):
    out, s = [NAN] * len(x), 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(x, n):
    out = [NAN] * len(x)
    if len(x) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(x[:n]) / n
    out[n - 1] = e
    for i in range(n, len(x)):
        e = x[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi(c, n=14):
    out = [NAN] * len(c)
    if len(c) <= n:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        if d > 0:
            g += d
        else:
            l -= d
    g /= n
    l /= n
    out[n] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + (d if d > 0 else 0.0)) / n
        l = (l * (n - 1) + (-d if d < 0 else 0.0)) / n
        out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def atr(h, l, c, n=14):
    out = [NAN] * len(c)
    if len(c) <= n:
        return out
    tr = [0.0] * len(c)
    for i in range(1, len(c)):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def adx(h, l, c, n=14):
    m = len(c)
    out = [NAN] * m
    if m <= 2 * n:
        return out
    tr = [0.0] * m
    pdm = [0.0] * m
    ndm = [0.0] * m
    for i in range(1, m):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    a, p, q = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(ndm[1:n + 1])
    dx = [NAN] * m

    def _dx(a, p, q):
        if a == 0:
            return 0.0
        pdi, ndi = 100 * p / a, 100 * q / a
        return 0.0 if pdi + ndi == 0 else 100 * abs(pdi - ndi) / (pdi + ndi)

    dx[n] = _dx(a, p, q)
    for i in range(n + 1, m):
        a = a - a / n + tr[i]
        p = p - p / n + pdm[i]
        q = q - q / n + ndm[i]
        dx[i] = _dx(a, p, q)
    v = sum(dx[n:2 * n]) / n
    out[2 * n - 1] = v
    for i in range(2 * n, m):
        v = (v * (n - 1) + dx[i]) / n
        out[i] = v
    return out


def _rolling(x, n, better):
    """Экстремум за последние n значений, включая текущее."""
    out, q = [NAN] * len(x), deque()
    for i, v in enumerate(x):
        while q and not better(x[q[-1]], v):
            q.pop()
        q.append(i)
        if q[0] <= i - n:
            q.popleft()
        if i >= n - 1 and v == v:
            out[i] = x[q[0]]
    return out


def rolling_max(x, n):
    return _rolling(x, n, lambda a, b: a > b)


def rolling_min(x, n):
    return _rolling(x, n, lambda a, b: a < b)


def shift(x, k=1):
    """Сдвиг вправо: значение на шаге i равно x[i-k]. Нужен, чтобы уровень не включал текущую свечу."""
    return [NAN] * k + x[:-k] if k else list(x)
