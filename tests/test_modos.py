"""Modos de orden fija: arranque limpio, nido, pueblo, ritmo de golpe (v486-v491)."""
import threading
import time
from _base import mk_bot, T

t = T("modos")

# arranque limpio al pasar de Automático a Manual con un viaje al herrero en curso
b = mk_bot("auto")
def trip():
    while b.alive and b.running:
        time.sleep(0.2)
b._rep_th = threading.Thread(target=trip, daemon=True)
b._rep_th.start()
b.set_mode("manual")
t.ok(not b._rep_th.is_alive(), "Manual corta el viaje al herrero que venía de Automático")
t.ok(b.running, "sigue prendido")
t.ok(b.goto is None and b.mission is None and b._pit_go is None, "sin viaje / encargo / nido pendiente")
t.ok(not b._fleeing(), "sin huida pendiente")
t.ok(b.target is None and b.state == "seek", "sin objetivo viejo")
t.ok(b._town_until == 0 and b._worn_seen == 0, "sin pueblo ni 'roto' pendientes")
t.ok("path_cache" not in b.nav, "camino viejo borrado")
t.ok(b.cfg["mode_anchor"]["x"] == 50 and b.cfg["mode_anchor"]["y"] == 170, "nido = donde está parado")
t.ok(b._anchored(), "Manual tiene nido fijo")

# lo que ya estaba roto al arrancar no lo saca; lo nuevo sí
t.ok(b._fresh_skip("roto", ["Hauberk (M) (0/400)"]), "roto al arrancar: lo ignora")
t.ok(b._fresh_skip("roto", ["Hauberk (M) (0/400)"]), "sigue roto lo mismo: lo sigue ignorando")
t.ok(not b._fresh_skip("roto", ["Hauberk (M) (0/400)", "Battle Axe (0/1000)"]), "se rompe otra cosa después: va al herrero")
t.ok(b._fresh_skip("sin arma", ["arma"]), "sin arma al arrancar: pega igual")
c = mk_bot("auto"); c.set_mode("manual")
t.ok(not c._fresh_skip("roto", []), "arranca sano: primera lectura sin nada roto")
t.ok(not c._fresh_skip("roto", ["Battle Axe (0/1000)"]), "arrancó sano y se rompe: va al herrero")

# Manual ignora zonas peligrosas de Automático; Automático no
t.ok(b._is_danger(50, 170) is False, "Manual ignora la zona donde lo mataron")
a = mk_bot("auto")
t.ok(a._is_danger(50, 170) is True, "Automático sigue evitando la zona peligrosa")
t.ok(not a._fresh_skip("roto", ["Hauberk (M) (0/400)"]), "Automático: lo roto sigue mandando al herrero")
a.set_mode("auto")
t.ok(a.goto is not None and a._town_until > 0, "Automático no pierde sus pendientes")

# pueblo según el modo (_town_need)
started = []
for mode in ("manual", "cartografo", "investigar", "quest", "auto"):
    x = mk_bot(mode)
    x._rep_th = None
    x._oly_repair_trip = lambda why="": started.append((mode, why))
    x._quest_maint = lambda: False
    x._town_until = 0
    x._town_need("bolso lleno")
    t.ok((x._town_until > 0) == (mode == "auto"), f"'bolso lleno' en {mode}: {'va al pueblo' if mode == 'auto' else 'NO va al pueblo'}")
x = mk_bot("manual"); x._rep_th = None; started.clear()
x._oly_repair_trip = lambda why="": started.append(why)
x._town_need("equipo roto"); time.sleep(0.2)
t.ok(len(started) == 1, "Manual con equipo roto: solo herrero")
q = mk_bot("quest"); q._quest_maint = lambda: False; q._town_until = 0
q._town_need("sin arma")
t.ok(q._town_until > 0, "Quest sin arma: sí va al pueblo")

# ritmo de golpe no saca a nadie en modos fijos
for mode in ("manual", "caza", "cartografo", "investigar"):
    x = mk_bot(mode); x._town_until = 0
    x._town_need = lambda why: setattr(x, "_tn", why)
    x._hit_pace("Orc", 30.0, time.time())
    t.ok(not getattr(x, "_tn", None), f"'pego más lento' no saca al pj en {mode}")

# cada modo arranca como debe
q = mk_bot("auto"); q.set_mode("quest")
t.ok(q.goto is None and hasattr(q, "_qm_t0"), "Quest arranca limpio y con su hora 50/10")
k = mk_bot("manual"); k.cfg["mode_anchor"] = {"x": 1}; k.set_mode("caza")
t.ok(k.cfg.get("mode_anchor") is None, "Caza elige su nido sola (borra el de Manual)")
g = mk_bot("auto"); g.set_mode("cartografo")
t.ok(g.cfg.get("carto") is None and g.bot_mode() == "cartografo", "Cartógrafo arranca desde donde está")
z = mk_bot("auto"); z.set_mode("cualquiera")
t.ok(z.bot_mode() == "auto", "modo desconocido = Automático")

# nido DENTRO de la ciudad: es nido, no 'viaje' (antes solo le pegaba a lo pegado y no perseguía a nadie)
n = mk_bot("auto"); n.set_mode("manual")
n._is_travelling = lambda: False
n.travel_dest = lambda: "Promiseland"
n.pos = {"map": "Aresden City", "x": 52, "y": 172}
t.ok(n._in_town(), "(Aresden City cuenta como ciudad)")
t.ok(n._grid_mode() == "nido", "Manual en el nido de orcos de la ciudad: modo nido (persigue a los de alrededor)")
n.pos = {"map": "Aresden City", "x": 150, "y": 190}
t.ok(n._grid_mode() == "viaje", "cruzando la ciudad lejos del nido: modo viaje")
au = mk_bot("auto"); au.goto = None; au.mission = None; au._pit_go = None
au._is_travelling = lambda: False; au.travel_dest = lambda: "Promiseland"
au.world.learn["oly_hot"] = {"aresden city": {"x": 55, "y": 176}}
au.pos = {"map": "Aresden City", "x": 57, "y": 178}
t.ok(au._grid_mode() == "nido", "Automático en la zona buena de la ciudad: nido")

# en el nido de la ciudad se CAZA (antes solo peleaba si le pegaban y nunca salía a buscar: 14:00 del 22/11)
c2 = mk_bot("auto"); c2.set_mode("manual")
c2.pos = {"map": "Aresden City", "x": 52, "y": 172}
t.ok(not c2._town_no_hunt(), "Manual en el nido de la ciudad: SÍ caza (no es 'pueblo')")
c2.pos = {"map": "Aresden City", "x": 150, "y": 190}
t.ok(c2._town_no_hunt(), "cruzando la ciudad lejos del nido: no caza")
import inspect
src = inspect.getsource(type(c2)._run_loop) if hasattr(type(c2), "_run_loop") else ""
import bot.engine as _E
code = open(_E.__file__, encoding="utf-8").read()
t.ok("in_town = self._town_no_hunt()" in code, "la decisión de cazar usa la regla del nido (no 'toda la ciudad')")
t.ok(code.count("self._in_town()") <= 2, "no quedan otros lugares que traten al nido de la ciudad como pueblo")

# Manual: nada de 'mantenimiento' (v491 lo mandaba al herrero al minuto de prender)
mm = mk_bot("auto"); mm.set_mode("manual")
t.ok(time.time() - mm._rep_last < 5, "arranque limpio: el reloj de mantenimiento arranca ahora")
t.ok(mm._man_rep_t == 0, "arranque limpio: si se rompe algo después, puede ir al herrero")
t.end()
