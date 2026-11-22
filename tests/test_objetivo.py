"""Objetivo fijo (Juan 22/11): con bichos que no mueren de un golpe, no cambiar de objetivo hasta matarlo."""
import time
from _base import mk_bot, T, winapi

t = T("objetivo")

def setup(vivos):
    x = mk_bot("manual")
    x.rect = (0, 0, 1919, 1080)
    x._char = lambda: (959, 560)
    x._tile = lambda: (64, 64)
    x.abs_pt = lambda a, b_: (a, b_)
    x.pos = {"map": "Aresden City", "x": 50, "y": 170, "t": time.time()}
    x._oly_ents = lambda: []
    clicks = []
    def tile_of(px_, py_):
        return (round((px_ - 959) / 64), round((py_ + 64 * 0.3 - 560) / 64))
    x._bar_read = lambda px_, py_: 1.0 if tile_of(px_, py_) in vivos else None
    winapi.move = lambda a, b_: None
    winapi.click = lambda a, b_, button="left", **k: clicks.append((tile_of(a, b_), button))
    return x, clicks

scr = lambda d: (int(959 + d[0] * 64), int(560 + d[1] * 64 - 64 * 0.3))
ciervo = (99, 0, {"name": "Deer"}, 7)

vivos = {(1, 0), (0, 1)}
x, clicks = setup(vivos)
x._lock_set(ciervo, *scr((1, 0)), 100.0)
t.ok(x._tlock and x._tlock["d"] == (1, 0), "le pegué y el mouse vio su barra: queda fijo")
for i in range(5):
    x._lock_step(101.0 + i)
t.ok(len(clicks) == 5 and all(c[0] == (1, 0) for c in clicks), "5 golpes seguidos al MISMO bicho (no salta al otro pegado)")
t.ok(all(c[1] == "right" for c in clicks), "pega sin caminar (clic derecho)")
x.stats["kills"] += 1
t.ok(not x._lock_step(107.0) and x._tlock is None, "murió: suelta y pasa al próximo")

y, c2 = setup({(1, 0)})
y._lock_set(ciervo, *scr((1, 0)), 200.0)
t.ok(y._lock_step(210.0), "a los 10 s sin matar sigue pegándole")
t.ok(not y._lock_step(216.0) and y._tlock is None, "15 s sin bajarle NADA de vida: lo suelta (no queda en loop)")
t.ok(y._oly_bad.get(7, 0) > 216.0, "y lo deja un rato")

# bicho con mucha vida (rana, esqueleto, Rudolph): mientras la barra baja, le sigue pegando aunque tarde 1 minuto
q, c6 = setup({(1, 0)})
vida = [1.0]
q._bar_read = lambda px_, py_: vida[0] if (round((px_ - 959) / 64), round((py_ + 64 * 0.3 - 560) / 64)) == (1, 0) else None
q._lock_set(ciervo, *scr((1, 0)), 600.0)
seguia = True
for i in range(60):
    vida[0] -= 0.015
    if not q._lock_step(601.0 + i):
        seguia = False
        break
t.ok(seguia and q._tlock is not None, "bicho de mucha vida: 60 s pegándole sin soltarlo (su vida baja)")
t.ok(len(c6) >= 30 and all(c[0] == (1, 0) for c in c6), "todos los golpes al mismo bicho")

vv = {(1, 0)}
z, c3 = setup(vv)
z._lock_set(ciervo, *scr((1, 0)), 300.0)
vv.clear()
z._lock_step(301.0); z._lock_step(302.0)
t.ok(z._tlock is None, "su barra ya no está (se movió): lo suelta y elige de nuevo")

w, c4 = setup(set())
w._lock_set(ciervo, *scr((1, 0)), 400.0)
t.ok(getattr(w, "_tlock", None) is None, "si el mouse no ve su barra, no lo fija (no se casa con el pasto)")

r, c5 = setup({(1, 0), (0, 1), (-1, 0)})
r._lock_set(ciervo, *scr((1, 0)), 500.0)
r._oly_ents = lambda: [(51, 170, {}, 1), (50, 171, {}, 2), (49, 170, {}, 3)]
t.ok(not r._lock_step(501.0) and r._sweep_state()["active"], "si lo rodean mientras pega: entra el cuadrado grande")
t.end()
