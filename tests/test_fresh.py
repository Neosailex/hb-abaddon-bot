import sys, time, threading, types
sys.path.insert(0, sys.argv[1])
import bot.engine as E
from bot import winapi, config as cfgmod
winapi.left_up = lambda *a, **k: None
winapi.key_up = lambda *a, **k: None
cfgmod.save = lambda *a, **k: None

class W:  # mundo mínimo
    def __init__(s): s.learn = {"dangers": [["aresden city", 50, 170, time.time()]]}

def mk(mode):
    b = object.__new__(E.Bot)
    b.cfg = {"bot_mode": mode}
    b.logs = []; b.log = lambda m: b.logs.append(m)
    b.mouse_held = False; b.mod_held = None
    b.pos = {"map": "Aresden City", "x": 50, "y": 170}
    b.world = W(); b.nav = {"path_cache": ("x",)}
    b.running = True; b.alive = True
    b.goto = {"map": "Aresden City", "x": 169, "y": 196}; b.mission = {"label": "Herrero"}
    b._pit_go = {"x": 1}; b._flee_until = time.time() + 600; b._worn_seen = time.time(); b._town_until = time.time() + 900
    b.target = {"x": 1}; b.state = "attack"; b._rep_last = time.time()
    return b

fails = []
def ok(c, msg):
    print(("OK   " if c else "FAIL ") + msg)
    if not c: fails.append(msg)

# 1) arranque limpio corta el viaje al herrero que venía de Automático
b = mk("auto")
def trip():
    while b.alive and b.running:
        time.sleep(0.3)
b._rep_th = threading.Thread(target=trip, daemon=True); b._rep_th.start()
b.set_mode("manual")
ok(not b._rep_th.is_alive(), "el viaje al herrero de Automático se corta al pasar a Manual")
ok(b.running, "el bot sigue prendido después de limpiar")
ok(b.goto is None and b.mission is None and b._pit_go is None, "sin viaje/encargo/nido pendiente")
ok(not b._fleeing(), "sin huida pendiente")
ok(b.target is None and b.state == "seek", "sin objetivo viejo: busca bicho")
ok(b._town_until == 0 and b._worn_seen == 0, "sin pueblo ni 'equipo roto' pendientes")
ok("path_cache" not in b.nav, "camino viejo borrado")
ok(b.cfg["mode_anchor"]["x"] == 50, "nido = donde está parado")

# 2) lo que ya estaba roto/sin arma al arrancar no lo saca; lo nuevo sí
ok(b._fresh_skip("roto", ["Hauberk (M) (0/400)"]) is True, "Hauberk rota al arrancar: la ignora")
ok(b._fresh_skip("roto", ["Hauberk (M) (0/400)"]) is True, "sigue rota la misma: la sigue ignorando")
ok(b._fresh_skip("roto", ["Hauberk (M) (0/400)", "Battle Axe (0/1000)"]) is False, "se rompe el arma DESPUÉS: va al herrero")
ok(b._fresh_skip("sin arma", ["arma"]) is True, "sin arma al arrancar: pega igual")
c = mk("auto"); c.set_mode("manual")
ok(c._fresh_skip("roto", []) is False, "arranca sano: primera lectura sin nada roto")
ok(c._fresh_skip("roto", ["Battle Axe (0/1000)"]) is False, "arrancó sano y DESPUÉS se rompe: va al herrero")
ok(c._fresh_skip("sin arma", []) is False and c._fresh_skip("sin arma", ["arma"]) is False, "arrancó con arma y la pierde: va")

# 3) Manual ignora zonas peligrosas de Automático
ok(b._is_danger(50, 170) is False, "Manual ignora la zona donde lo mataron en Automático")

# 4) Automático NO cambia (regresión)
a = mk("auto")
ok(a._fresh_skip("roto", ["Hauberk (M) (0/400)"]) is False, "Automático: lo roto sigue mandando al herrero")
ok(a._is_danger(50, 170) is True, "Automático: sigue evitando la zona peligrosa")
a.set_mode("auto")
ok(a.goto is not None and a._town_until > 0, "Automático: no le borro sus pendientes")

# 5) Prender/apagar en Manual: arranca de 0
m = mk("manual"); m._fresh_t = 0
m._fresh_start("prendido")
ok(m._fresh_t > 0 and m._fresh_base == {}, "prender en Manual = foto nueva del estado")
ok(any("Arranque limpio" in l for l in m.logs), "lo avisa en el registro")

# 6) Caza/Quest también limpian; Quest sigue con su reloj
q = mk("auto"); q.set_mode("quest")
ok(q.goto is None and hasattr(q, "_qm_t0"), "Quest arranca limpio y con su hora 50/10")
print("\n" + ("TODO OK" if not fails else f"{len(fails)} FALLAS"))
sys.exit(1 if fails else 0)
