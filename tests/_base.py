"""Base del banco de pruebas: un bot de mentira (sin juego, sin Windows) para probar la lógica.
Uso: python tests/run_all.py  (corre todo; si algo falla, la versión NO sale)."""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import bot.engine as E  # noqa: E402
from bot import winapi, config as cfgmod  # noqa: E402

winapi.left_up = lambda *a, **k: None
winapi.key_up = lambda *a, **k: None
cfgmod.save = lambda *a, **k: None


class World:
    def __init__(self):
        self.learn = {"dangers": [["aresden city", 50, 170, time.time()]]}

    def save(self):
        pass


def mk_bot(mode="auto"):
    b = object.__new__(E.Bot)
    b.cfg = {"bot_mode": mode}
    b.logs = []
    b.log = lambda m: b.logs.append(m)
    b.mouse_held = False
    b.mod_held = None
    b.pos = {"map": "Aresden City", "x": 50, "y": 170}
    b.world = World()
    b.nav = {"path_cache": ("x",)}
    b.running = True
    b.alive = True
    b.goto = {"map": "Aresden City", "x": 169, "y": 196}
    b.mission = {"label": "Herrero"}
    b._pit_go = {"x": 1}
    b._flee_until = time.time() + 600
    b._worn_seen = time.time()
    b._town_until = time.time() + 900
    b.target = {"x": 1}
    b.state = "attack"
    b._rep_last = time.time()
    b.stats = {"state": "", "target": "", "kills": 0}
    return b


class T:
    def __init__(self, name):
        self.name, self.fails, self.n = name, [], 0

    def ok(self, cond, msg):
        self.n += 1
        if not cond:
            self.fails.append(msg)
        print(("  OK   " if cond else "  FALLA ") + msg)

    def end(self):
        print(f"{self.name}: {self.n - len(self.fails)}/{self.n}")
        sys.exit(1 if self.fails else 0)
