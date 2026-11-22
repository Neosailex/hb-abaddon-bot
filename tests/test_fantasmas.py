"""Filtro de bichos fantasma de la memoria (bug v489: un reloj basura hacía que no viera ningún orco)."""
from _base import E, T

t = T("fantasmas")
# foto real del 21/11 21:50 (herrería de Aresden): x, y, tipo, estado, reloj
GTC = 933341796
RAW = [[44, 32, 2, 14, 933341797], [43, 34, 2, 14, 933341797], [43, 32, 4, 14, 933341797],
       [40, 36, 6, 14, 933341797], [44, 30, 4, 31, 933294810], [46, 35, 2, 14, 933341797],
       [55, 44, 0, 17430, 1030881280],  # basura: reloj 27 h adelantado
       [0, 0, 0, 0, 672253626], [43, 168, 65527, 65535, 5],
       [59, 169, 14, 3, 932147224], [45, 174, 14, 7, 932148557], [46, 196, 17, 3, 932150486]]  # ciudad, 20 min viejos
near = [r for r in RAW if r[4] == 933341797]

# 1) con la cuenta envenenada que tenía el bot (off de la basura), igual ve a los vivos
tk = {"off": 4196819422}
f = E.Bot.ghost_filter(RAW, GTC, tk)
t.ok(f is not None, "decide aunque la cuenta vieja esté mal")
t.ok(all(f(r[4]) for r in near), "los bichos de al lado están VIVOS (antes los tiraba todos)")
t.ok(not f(1030881280), "el reloj basura adelantado = fantasma")
t.ok(not any(f(r[4]) for r in RAW if 932000000 < r[4] < 932200000), "los de la ciudad de hace 20 min = fantasmas")
t.ok(-60000 < tk["off"] < 600000, "la cuenta se corrige sola")

# 2) arrancando de cero
tk = {"off": None}
f = E.Bot.ghost_filter(RAW, GTC, tk)
t.ok(all(f(r[4]) for r in near), "de cero: los de al lado vivos")
t.ok(not f(1030881280), "de cero: basura = fantasma")

# 3) solo basura: no decido (no tiro a nadie por una cuenta inventada)
tk = {"off": None}
t.ok(E.Bot.ghost_filter([[1, 1, 2, 0, 1030881280]], GTC, tk) is None, "solo datos basura: no descarto a nadie")

# 4) el reloj de Windows da la vuelta (49 días)
tk = {"off": None}
g2 = 5000
f = E.Bot.ghost_filter([[1, 1, 2, 0, (g2 - 2000) & 0xFFFFFFFF], [2, 2, 2, 0, (g2 - 9000) & 0xFFFFFFFF]], g2, tk)
t.ok(f is not None and f((g2 - 2000) & 0xFFFFFFFF), "con el reloj recién dado vuelta sigue andando")
t.end()
