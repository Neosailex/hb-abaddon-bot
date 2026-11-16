"""La grilla: una sola foto, en tiempo real, de lo que rodea al personaje. Cada cosa está en una baldosa del mapa:
bichos vivos (con seguimiento: cada uno conserva su número aunque se mueva), jugadores, NPCs y lo tirado en el piso.
El bot decide en qué MODO está (nido, viaje, local, zona nueva, recolección, descanso) y el modo decide cómo se lee
esta misma grilla. Todo clic sale de una baldosa de acá. No depende de ningún server: solo de listas de (x, y, qué)."""
import time

PLAYER_TYPES = {1, 2, 3, 4, 5, 6}
MODES = ("nido", "viaje", "local", "zona nueva", "recolección", "descanso")


class Track:
    __slots__ = ("id", "name", "typ", "x", "y", "t0", "seen", "adj_since", "near_t", "cr", "n", "attack", "moved")

    def __init__(self, tid, e, now):
        self.id, self.name, self.typ, self.x, self.y = tid, e["name"], e.get("typ"), e["x"], e["y"]
        self.t0 = self.seen = now
        self.adj_since = None
        self.near_t = 0.0
        self.cr = e.get("cr")
        self.n = 0          # número en la cola (1 = al que le pego ahora)
        self.attack = False  # entra en los que enfrento (los demás solo los vigilo)
        self.moved = now

    def dist(self, me):
        return max(abs(self.x - me[0]), abs(self.y - me[1]))

    def as_dict(self):
        return {"id": self.id, "name": self.name, "x": self.x, "y": self.y, "n": self.n, "attack": self.attack}


class Grid:
    def __init__(self, follow=2, lost_after=0.8):
        self.tracks = {}
        self._next = 1
        self.me = None
        self.map = None
        self.players, self.npcs, self.loot = [], [], []
        self.queue = []
        self.lost = []       # bichos que dejaron de estar vivos en esta lectura (para contar muertes y botín)
        self.t = 0.0
        self.mode = "nido"
        self.target = None   # id del bicho número 1
        self.follow, self.lost_after = follow, lost_after

    # ------------------------------------------------------------------ lectura
    def update(self, me, mp, live, now=None):
        """me=(x, y); live=[{'x','y','name','typ','kind': 'mob'|'player'|'npc', 'cr'}]. Mantiene el seguimiento:
        cada bicho vivo conserva su número mientras no se aleje más de `follow` baldosas entre dos lecturas."""
        now = now or time.time()
        if mp != self.map:
            self.tracks, self.target = {}, None  # cambié de mapa: nada de lo anterior vale
        self.me, self.map, self.t = me, mp, now
        mobs = [e for e in live if e.get("kind") == "mob"]
        self.players = [e for e in live if e.get("kind") == "player"]
        self.npcs = [e for e in live if e.get("kind") == "npc"]
        pairs = []
        for tid, tr in self.tracks.items():
            for i, e in enumerate(mobs):
                if e["name"] == tr.name:
                    d = max(abs(e["x"] - tr.x), abs(e["y"] - tr.y))
                    if d <= self.follow:
                        pairs.append((d, tid, i))
        pairs.sort()
        got, used = set(), set()
        for d, tid, i in pairs:
            if tid in got or i in used:
                continue
            got.add(tid)
            used.add(i)
            tr, e = self.tracks[tid], mobs[i]
            if (tr.x, tr.y) != (e["x"], e["y"]):
                tr.moved = now
            tr.x, tr.y, tr.seen, tr.cr = e["x"], e["y"], now, e.get("cr")
        for i, e in enumerate(mobs):
            if i not in used:
                self.tracks[self._next] = Track(self._next, e, now)
                self._next += 1
        self.lost = []
        for tid in [t for t, tr in self.tracks.items() if now - tr.seen > self.lost_after]:
            self.lost.append(self.tracks.pop(tid))
            if self.target == tid:
                self.target = None
        for tr in self.tracks.values():
            d = tr.dist(me)
            if d <= 2:
                tr.near_t = now
            if d <= 1:
                tr.adj_since = tr.adj_since or now
            else:
                tr.adj_since = None
        return self

    def set_loot(self, items):
        """items=[{'x','y','kind'}] en baldosas del mapa."""
        self.loot = list(items)

    # ------------------------------------------------------------------ consultas
    def at(self, x, y):
        return [tr for tr in self.tracks.values() if (tr.x, tr.y) == (x, y)]

    def adjacent(self):
        return [tr for tr in self.tracks.values() if tr.dist(self.me) <= 1]

    def crowd(self, tr, r=3):
        return sum(1 for o in self.tracks.values() if o is not tr and max(abs(o.x - tr.x), abs(o.y - tr.y)) <= r)

    def player_near(self, x, y, r=3):
        return any(max(abs(p["x"] - x), abs(p["y"] - y)) <= r for p in self.players)

    # ------------------------------------------------------------------ cola de objetivos
    def rank(self, cap=2, usable=None, quest=(), reach=12, mode=None, taken=None):
        """Numera los bichos por prioridad. usable(tr)->bool descarta los que no se pueden (élite, pared, nido de más).
        taken(tr)->bool: otro jugador ya le está pegando (lo dejo para el final). Los primeros `cap` son los que
        enfrento; el resto los vigilo. En modo 'viaje'/'local'/'descanso' solo cuentan los que tengo pegados."""
        mode = mode or self.mode
        me = self.me
        out = []
        for tr in self.tracks.values():
            d = tr.dist(me)
            if d > reach:
                continue
            adj = d <= 1
            if mode in ("viaje", "local", "descanso", "recolección") and not adj:
                continue
            if not adj and usable and not usable(tr):
                continue
            s = float(d)
            if adj:
                s -= 6            # el que tengo encima va primero
            if tr.id == self.target:
                s -= 2.5          # no cambio de objetivo por nada
            if tr.name in quest and d <= 14:
                s -= 2
            s += 1.5 * max(0, self.crowd(tr) - cap)
            if taken and taken(tr):
                s += 8            # ya es de otro jugador
            out.append((s, tr))
        out.sort(key=lambda p: (p[0], p[1].id))
        for tr in self.tracks.values():
            tr.n, tr.attack = 0, False
        self.queue = [tr for _s, tr in out]
        for i, tr in enumerate(self.queue):
            tr.n = i + 1
            tr.attack = i < max(1, cap)
        self.target = self.queue[0].id if self.queue else None
        return self.queue

    def snapshot(self):
        return {"t": self.t, "mode": self.mode, "me": self.me, "map": self.map, "target": self.target,
                "mobs": [tr.as_dict() for tr in sorted(self.tracks.values(), key=lambda t: t.n or 999)],
                "players": [{"x": p["x"], "y": p["y"]} for p in self.players],
                "npcs": [{"x": p["x"], "y": p["y"], "name": p.get("name")} for p in self.npcs],
                "loot": list(self.loot)}
