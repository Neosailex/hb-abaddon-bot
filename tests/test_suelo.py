"""Piso desde la memoria (v498, investigado con Juan el 22/11): grilla 51x37 de 44 bytes por baldosa, centrada en
el pj; el objeto de arriba en +22/+24. Dos copias seguidas. Casillas para elegir qué levantar."""
import os
import struct
import tempfile
import time
from _base import mk_bot, T, winapi, cfgmod, E
from bot import memory as M, gamedata as G

t = T("suelo")


def rec(sh=0, fr=0, owner=0, par=0):
    r = bytearray(44)
    r[21] = 0xF0 + par
    struct.pack_into("<HH", r, 22, sh, fr)
    r[33], r[34], r[35] = 0xEE + par, 0x94, 0x01
    struct.pack_into("<I", r, 36, owner)
    return r


def grid(items=None, owners=()):
    items = items or {}
    out = bytearray()
    for i in range(M.GRID_N):
        sh, fr = items.get(i, (0, 0))
        out += rec(sh, fr, 0x1234 if i in owners else 0, i % 2)
    return bytes(out)


idx = lambda dx, dy: (dx + M.GRID_CX) * M.GRID_H + (dy + M.GRID_CY)

# 1) lo que vimos en vivo: poción en el centro; después el pj dio un paso en diagonal (-1,+1) y la poción quedó en 979
g = grid({943: (6, 5)})
t.ok(M.grid_items(g, 23, 141) == [(23, 141, 6, 5)], "poción en la baldosa del pj (índice 943 = centro)")
g2 = grid({979: (6, 5)})
t.ok(M.grid_items(g2, 22, 142) == [(23, 141, 6, 5)], "pj se movió (22,142): la poción sigue en (23,141) (índice 979)")
g3 = grid({idx(-2, -2): (6, 0), idx(0, -2): (15, 7)})
t.ok(sorted(M.grid_items(g3, 27, 145)) == [(25, 143, 6, 0), (27, 143, 15, 7)], "oro y martillo en sus baldosas")
t.ok(M.grid_items(grid(), 10, 10) == [], "piso vacío: nada")

# 2) encontrar la grilla: dos copias separadas 83072 bytes, con basura alrededor
junk = os.urandom(5000)
blob = junk + g3 + bytes(M.GRID_GAP - M.GRID_N * 44) + g3 + junk
runs = M.find_grid_runs(blob)
bases = M.pick_grid_base(runs)
t.ok(bases[:2] == [len(junk), len(junk) + M.GRID_GAP], "encuentra las dos copias donde empiezan")
# con baldosas raras en el medio (bicho encima, borde del mapa) la corrida se corta pero el inicio sigue bien
gb = bytearray(g3)
gb[1376 * 44 + 34] = 0
t.ok(M.pick_grid_base(M.find_grid_runs(bytes(gb)))[:1] == [0], "corrida cortada (como en Eternal Field): igual da el inicio")


class FakeProc:
    def __init__(self, mem):
        self.mem = mem  # {base: bytes}

    def regions(self):
        return [(b, len(d)) for b, d in self.mem.items()]

    def read(self, a, n):
        for b, d in self.mem.items():
            if b <= a < b + len(d):
                return d[a - b:a - b + n]
        return None


base = 0x0B96EF00
fresca = grid({idx(0, 0): (6, 0)}, owners={idx(-3, 0), idx(1, 1)})
vieja = grid({}, owners=())
memb = fresca + bytes(M.GRID_GAP - M.GRID_N * 44) + vieja
gg = M.GroundGrid()
gg.sync = True  # en la prueba espero a que termine de buscar
r = gg.read(FakeProc({base: memb}), 27, 145, mobs=[(24, 145), (28, 146)])
t.ok(gg.bases == [base, base + M.GRID_GAP], "GroundGrid encuentra el par de copias")
t.ok(r == [(27, 145, 6, 0)], "elige la copia al día (la que tiene a los bichos donde están)")
memc = vieja + bytes(M.GRID_GAP - M.GRID_N * 44) + fresca
gg2 = M.GroundGrid()
gg2.sync = True
t.ok(gg2.read(FakeProc({base: memc}), 27, 145, mobs=[(24, 145), (28, 146)]) == [(27, 145, 6, 0)],
     "y si la al día es la segunda, usa la segunda")
g0 = M.GroundGrid(); g0.sync = True
t.ok(g0.read(FakeProc({base: os.urandom(200000)}), 1, 1) is None, "sin grilla en memoria: None (sigue el loot de antes)")

# 3) categorías (casillas del panel)
cat = G.loot_cat
t.ok(cat({"name": "Gold", "type": 5}) == "oro", "Gold = oro")
t.ok(cat({"name": "Superior Mallet", "type": 1}) == "equipo", "martillo = equipo")
t.ok(cat({"name": "Revitalizing Potion", "type": 7}) == "pociones", "poción = pociones")
t.ok(cat({"name": "Orc Meat", "type": 7}) == "comida", "carne = comida")
t.ok(cat({"name": "Orc Teeth", "type": 5}) == "partes", "dientes de orco = partes de bicho")
t.ok(cat({"name": "Zemstone of Sacrifice", "type": 0}) == "misc" and cat({"name": "Ancient Tablet", "type": 3}) == "misc",
     "zemstone y tablet = misc")

# 4) decisión de levantar (bot de mentira)
tmp = tempfile.mkdtemp()
cfgmod.DATA = tmp
E.cfgmod.DATA = tmp
DB = {(6, 0): [("Gold", "oro")], (6, 5): [("Revitalizing Potion", "pociones")], (15, 7): [("Superior Mallet", "equipo")]}


def mk(mode, mem_items, cats=None):
    b = mk_bot("manual")
    b.cfg.update({"loot_mem": mode, "loot_cats": cats or {c: True for c in G.LOOT_CATS}, "server": {"name": "Olympia"}})
    b.pos = {"map": "Aresden City", "x": 50, "y": 170, "t": time.time()}
    b.img = object()
    b.bars = {"hp": 100}
    b._char = lambda: (959, 560)
    b._tile = lambda: (64, 64)
    b.abs_pt = lambda a, c: (a, c)
    b._release = lambda: None
    b._quest_mobs = lambda: set()
    b._loot_note = lambda: []
    b._grid = lambda: None
    b._gcat = (None, DB)
    b.world.game_dir = None
    b.world.learn = {}
    b.mem = type("Mm", (), {"proc": object()})()
    items = [(x, y, sh, fr) for (x, y), (sh, fr) in mem_items.items()]
    b.ground = type("Gg", (), {"read": lambda self, p, px, py, mobs=(): list(items)})()
    clicks = []
    winapi.key_down = lambda *a, **k: None
    winapi.click = lambda a, c, **k: clicks.append((round((a - 959) / 64) + 50, round((c - 560) / 64) + 170))
    return b, clicks


import bot.vision as V
_orig_gl = V.ground_loot

b, cl = mk("activo", {(52, 170): (6, 0)})
V.ground_loot = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no debería mirar colores"))
t.ok(b._loot_pass([]) and b._loot_go["x"] == 52 and cl == [(52, 170)], "activo: ve oro en memoria y va directo")

b, cl = mk("activo", {})
t.ok(not b._loot_pass([]) and not cl, "activo: piso vacío, no va a ningún lado (ni mira colores)")

b, cl = mk("activo", {(52, 170): (15, 7)}, cats={"oro": True, "equipo": False, "pociones": True, "partes": True, "comida": True, "misc": True})
t.ok(not b._loot_pass([]) and not cl, "equipo destildado: no levanta el martillo")

b, cl = mk("activo", {(52, 170): (15, 7), (49, 170): (6, 0)})
b._bag_slots = (50, 50, time.time())
t.ok(b._loot_pass([]) and b._loot_go["x"] == 49, "bolso lleno: va por el oro, no por el martillo")
b, cl = mk("activo", {(52, 170): (15, 7)})
b._bag_slots = (50, 50, time.time())
t.ok(not b._loot_pass([]), "bolso lleno y solo equipo tirado: no va")

# a ciegas (recién murió un bicho al lado)
b, cl = mk("activo", {})
b._kill_pick = (51, 170, time.time() - 3, "Orc")
b._kill_pick_ready = lambda *a: True
t.ok(not b._loot_pass([]) and not cl, "activo: murió un orco y la memoria ve la baldosa vacía: no pierde el paso")
b, cl = mk("activo", {})
kp = (51, 170, time.time() - 0.5, "Orc")
b._kill_pick = kp
b._kill_pick_ready = lambda *a: True
t.ok(not b._loot_pass([]) and b._kill_pick == kp, "recién muerto (0,5 s): espera a que aparezca lo que cae")
b, cl = mk("activo", {(51, 170): (6, 5)})
b._kill_pick = (51, 170, time.time() - 1, "Orc")
b._kill_pick_ready = lambda *a: True
t.ok(b._loot_pass([]) and cl == [(51, 170)], "activo: cayó una poción donde murió: va")

# varias cosas en la misma baldosa: al llegar, si queda algo vuelve enseguida
b, cl = mk("activo", {(50, 170): (6, 0)})
b._loot_pass([])
b._loot_pass([])  # llegó (está parado ahí)
t.ok(b._loot_tried.get((50, 170), 0) - time.time() < 2, "al levantar por memoria, la baldosa queda libre al instante (puede haber más abajo)")

# sombra: hace lo mismo que antes y anota
V.ground_loot = lambda *a, **k: []
b, cl = mk("sombra", {})
b._kill_pick = (51, 170, time.time() - 1, "Orc")
b._kill_pick_ready = lambda *a: True
t.ok(b._loot_pass([]) and cl == [(51, 170)], "sombra: va a ciegas igual que antes (no cambia nada)")
log = open(os.path.join(tmp, "suelo_sombra.log"), encoding="utf-8").read()
t.ok("vacía" in log and "(51,170)" in log, "sombra: anota que la memoria la veía vacía")
b, cl = mk("sombra", {(53, 172): (6, 5)})
b._loot_pass([])
log = open(os.path.join(tmp, "suelo_sombra.log"), encoding="utf-8").read()
t.ok("Revitalizing Potion en (53,172)" in log, "sombra: anota lo que la memoria ve y hoy no fue a buscar")

b, cl = mk("off", {(52, 170): (6, 0)})
t.ok(b._ground_mem([]) is None, "apagado: no lee la memoria")
V.ground_loot = _orig_gl
t.end()
