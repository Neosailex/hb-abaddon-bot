"""Mundo: mapas del cliente (.amd), por dónde se camina, A*, y portales que el bot va aprendiendo."""
import heapq
import json
import os
import re
import time

import numpy as np

# Nombre que muestra el juego -> archivo .amd (si alguno está mal, el bot lo corrige solo comparando posiciones)
NAME_GUESS = {
    "beginner zone": "default", "bleeding island": "bisle",
    "aresden": "ARESDEN", "aresden city": "ARESDEN", "aresden farm": "arefarm", "aresien garden": "areuni",
    "eternal field": "huntzone2", "silent wood": "huntzone4", "aresden dungeon": "aresdend1",
    "elvine": "ELVINE", "elvine city": "ELVINE", "elvine farm": "elvfarm", "eldiniel garden": "elvuni",
    "rocky highland": "huntzone1", "death valley": "huntzone3", "elvine dungeon": "elvined1",
    "middleland": "middleland", "promiseland": "2ndmiddle", "icebound": "icebound",
    "middleland mine": "middled1x", "middleland dungeon": "middled1n",
    "dungeon l2": "dglv2", "dungeon l3": "dglv3", "dungeon l4": "dglv4",
    "tower of hell 1": "Toh1", "tower of hell 2": "Toh2", "tower of hell 3": "Toh3", "tower of hell 4": "Toh4",
    "shop": "gshop_1", "blacksmith": "bsmith_1", "city hall": "cityhall_1", "warehouse": "wrhus_1",
    "wizard tower": "wzdtwr_1", "magic tower": "wzdtwr_1", "cathedral": "cath_1", "church": "cath_1",
    "guild hall": "gldhall_1", "command hall": "Cmdhall_1", "infernia": "inferniaA", "procella": "procella",
    "aresden shop": "gshop_1", "aresden blacksmith": "bsmith_1", "aresden city hall": "cityhall_1",
    "aresden warehouse": "wrhus_1", "aresden wizard tower": "wzdtwr_1", "aresden magic tower": "wzdtwr_1",
    "aresden cathedral": "cath_1", "aresden church": "cath_1", "aresden guild hall": "gldhall_1",
    "aresden command hall": "Cmdhall_1", "aresden barracks": "arebrk11",
    "elvine shop": "gshop_2", "elvine blacksmith": "bsmith_2", "elvine city hall": "cityhall_2",
}

# Archivo -> nombre que muestra el juego (para el mapa del mundo base)
FILE_NAME = {  # nombres exactos: tabla de mapas que el servidor de Abaddon le manda al juego (carpeta CACHE)
    "default": "beginner zone", "inferniab": "infernia b", "inferniaa": "infernia a", "maze": "maze",
    "druncncity": "druncnian city", "procella": "procella", "abaddon": "abaddon", "aresden": "aresden city",
    "arefarm": "aresden farm", "aresdend1": "aresden dungeon", "arebrk11": "aresden barrack 1", "arebrk12": "aresden barrack 1",
    "arebrk21": "aresden barrack 2", "arebrk22": "aresden barrack 2", "wrhus_1": "aresden warehouse", "cityhall_1": "aresden cityhall",
    "arewrhus": "aresden warehouse", "resurr1": "revival zone", "gshop_1": "aresden shop", "arejail": "aresden jail",
    "cath_1": "aresien church", "wzdtwr_1": "magic tower", "gshop_1f": "aresden shop", "wrhus_1f": "aresden warehouse",
    "bsmith_1f": "aresden blacksmith", "bsmith_1": "aresden blacksmith", "gldhall_1": "aresden guildhall", "cmdhall_1": "command hall",
    "dglv2": "dungeon l2", "dglv3": "dungeon l3", "dglv4": "dungeon l4", "elvine": "elvine city",
    "elvfarm": "elvine farm", "elvbrk11": "elvine barrack 1", "elvbrk12": "elvine barrack 1", "elvbrk21": "elvine barrack 2",
    "elvbrk22": "elvine barrack 2", "elvined1": "elvine dungeon", "elvjail": "elvine jail", "elvwrhus": "elvine warehouse",
    "gldhall_2": "elvine guildhall", "gshop_2": "elvine shop", "gshop_2f": "elvine shop", "resurr2": "revival zone",
    "wrhus_2": "elvine warehouse", "wrhus_2f": "elvine warehouse", "wzdtwr_2": "magic tower", "bsmith_2": "elvine blacksmith",
    "bsmith_2f": "elvine blacksmith", "cath_2": "eldiniel church", "cityhall_2": "elvine cityhall", "cmdhall_2": "command hall",
    "btfield": "battle field", "godh": "god's heldenian", "hrampart": "heldenian rampart", "areuni": "aresien garden",
    "elvuni": "eldiniel garden", "huntzone1": "rocky highland", "huntzone2": "eternal field", "huntzone3": "death valley",
    "huntzone4": "silent wood", "middleland": "middleland", "2ndmiddle": "promiseland", "middled1n": "middleland dungeon",
    "middled1x": "middleland mine", "bisle": "bleeding island", "fightzone1": "arena 1", "fightzone2": "arena 2",
    "fightzone3": "arena 3", "fightzone4": "arena 4", "fightzone5": "arena 5", "fightzone6": "arena 6",
    "fightzone7": "arena 7", "fightzone8": "arena 8", "fightzone9": "arena 9", "toh1": "tower of hell 1",
    "toh2": "tower of hell 2", "toh3": "tower of hell 3", "icebound": "ice map", "toh4": "tower of hell 4",
}
INDOOR = ("shop", "blacksmith", "city hall", "cityhall", "warehouse", "wizard tower", "magic tower", "cathedral", "church", "guild hall", "guildhall", "command hall", "barracks", "barrack 1", "barrack 2", "jail", "revival zone")


def file_display(key):
    return FILE_NAME.get(str(key).lower(), str(key).lower())


# nombres exactos del servidor -> archivo (los edificios repetidos quedan con el primero; el bot corrige comparando posiciones)
for _k, _v in FILE_NAME.items():
    NAME_GUESS.setdefault(_v, _k)

DEFAULT_GAME_DIRS = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Helbreath Abaddon", "game"),
    r"C:\Program Files\Helbreath Abaddon\game",
    r"C:\Program Files (x86)\Helbreath Abaddon\game",
]


def _helbreath_dirs(server=None):
    """Carpetas de juegos Helbreath instalados (cualquier servidor), con el del servidor elegido primero."""
    roots = [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"), os.environ.get("APPDATA", ""),
             os.environ.get("LOCALAPPDATA", ""), r"C:\Program Files", r"C:\Program Files (x86)",
             r"C:\Games", "C:\\"]
    out = []
    for r in roots:
        try:
            names = os.listdir(r)
        except OSError:
            continue
        for n in names:
            if "helbreath" in n.lower() or (server and server.lower() in n.lower()):
                for sub in ("", "game", "Game", "client"):
                    d = os.path.join(r, n, sub)
                    if os.path.isdir(os.path.join(d, "MAPDATA")):
                        out.append(d)
    if server:
        out.sort(key=lambda d: 0 if server.lower() in d.lower() else 1)
    return out


def find_game_dir(cfg_dir=None, server=None):
    if cfg_dir and os.path.isdir(os.path.join(cfg_dir, "MAPDATA")):
        return cfg_dir
    if not server or server.lower() == "abaddon":
        for d in DEFAULT_GAME_DIRS:
            if os.path.isdir(os.path.join(d, "MAPDATA")):
                return d
    found = _helbreath_dirs(server)
    return found[0] if found else None


def _zstd(data):
    try:
        import zstandard
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=64 << 20)
    except ImportError:
        from compression import zstd  # Python 3.14+
        return zstd.decompress(data)


class MapData:
    def __init__(self, path):
        d = open(path, "rb").read()
        if d[256:260] == b"\x28\xb5\x2f\xfd":  # cuerpo comprimido con zstd (cliente de Olympia)
            d = d[:256] + _zstd(d[256:])
        h = d[:256].decode("latin1", "ignore")
        self.w = int(re.search(r"MAPSIZEX\s*=\s*(\d+)", h).group(1))
        self.h = int(re.search(r"MAPSIZEY\s*=\s*(\d+)", h).group(1))
        t = np.frombuffer(d[256:256 + self.w * self.h * 10], np.uint8).reshape(self.h, self.w, 10)
        flags = t[:, :, 8]
        self.walk = (flags & 0x80) == 0          # [y, x]
        self.tele = (flags & 0x40) != 0
        ys, xs = np.where(self.tele)
        self.teleports = list(zip(xs.tolist(), ys.tolist()))
        self.name = os.path.splitext(os.path.basename(path))[0]
        if not self.teleports:
            self._borrow_portals()

    def _borrow_portals(self):
        """Algunos clientes (Olympia) sacaron la marca de portales del archivo del mapa: si hay instalado un
        Helbreath clásico con el mismo mapa (mismo tamaño), uso sus portales."""
        for gd in DEFAULT_GAME_DIRS:
            try:
                md = os.path.join(gd, "MAPDATA")
                fn = next((f for f in os.listdir(md) if os.path.splitext(f)[0].lower() == self.name.lower()), None)
                if not fn:
                    continue
                d = open(os.path.join(md, fn), "rb").read()
                if d[256:260] == b"\x28\xb5\x2f\xfd":
                    continue
                h = d[:256].decode("latin1", "ignore")
                w = int(re.search(r"MAPSIZEX\s*=\s*(\d+)", h).group(1))
                hh = int(re.search(r"MAPSIZEY\s*=\s*(\d+)", h).group(1))
                if (w, hh) != (self.w, self.h):
                    continue
                t = np.frombuffer(d[256:256 + w * hh * 10], np.uint8).reshape(hh, w, 10)
                self.tele = (t[:, :, 8] & 0x40) != 0
                ys, xs = np.where(self.tele)
                self.teleports = list(zip(xs.tolist(), ys.tolist()))
                return
            except Exception:
                continue

    def ok(self, x, y):
        return 0 <= x < self.w and 0 <= y < self.h and bool(self.walk[y, x])

    def portal_groups(self):
        """Agrupa baldosas de portal vecinas: un portal = un grupo."""
        left = set(self.teleports)
        groups = []
        while left:
            s = left.pop()
            g, st = [s], [s]
            while st:
                x, y = st.pop()
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        n = (x + dx, y + dy)
                        if n in left:
                            left.remove(n)
                            g.append(n)
                            st.append(n)
            cx = sum(p[0] for p in g) / len(g)
            cy = sum(p[1] for p in g) / len(g)
            groups.append({"tiles": g, "center": (int(round(cx)), int(round(cy)))})
        return groups

    def nearest_walkable(self, x, y, r=6):
        best = None
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if self.ok(x + dx, y + dy) or (0 <= x + dx < self.w and 0 <= y + dy < self.h and self.tele[y + dy, x + dx]):
                    d = abs(dx) + abs(dy)
                    if best is None or d < best[0]:
                        best = (d, (x + dx, y + dy))
        return best[1] if best else None

    def near_tele(self, x, y, r=2):
        """True si hay una puerta/portal a r baldosas o menos."""
        if getattr(self, "_tn", None) is None or self._tn[0] != r:
            import numpy as _np
            t = self.tele.astype(bool)
            m = t.copy()
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    m |= _np.roll(_np.roll(t, dy, 0), dx, 1)
            self._tn = (r, m)
        return 0 <= x < self.w and 0 <= y < self.h and bool(self._tn[1][y, x])

    def los(self, a, b):
        """True si se puede ir derecho de a hasta b sin chocar: todas las baldosas de la línea se pueden pisar
        y no corta esquinas. Sirve para saber hasta dónde apuntar el mouse en pasillos y laberintos."""
        x0, y0 = a
        x1, y1 = b
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        sx = 1 if x1 > x0 else -1
        sy = 1 if y1 > y0 else -1
        err = dx - dy
        x, y = x0, y0
        while (x, y) != (x1, y1):
            e2 = 2 * err
            nx, ny = x, y
            if e2 > -dy:
                err -= dy
                nx += sx
            if e2 < dx:
                err += dx
                ny += sy
            if nx != x and ny != y and not (self.ok(nx, y) and self.ok(x, ny)):
                return False
            x, y = nx, ny
            if not self.ok(x, y) and (x, y) != (x1, y1):
                return False
        return True

    def astar(self, start, goal, max_nodes=60000, allow_tele_goal=True):
        """Camino de baldosas (8 direcciones). Devuelve lista de (x, y) o None.
        Esquiva las puertas (2 baldosas alrededor) salvo cerca del inicio o del destino,
        así no se mete sin querer en edificios."""
        sx, sy = start
        gx, gy = goal
        if not (0 <= gx < self.w and 0 <= gy < self.h):
            return None

        def passable(x, y):
            if not (0 <= x < self.w and 0 <= y < self.h):
                return False
            if self.walk[y, x]:
                if (self.tele.any() and self.near_tele(x, y)
                        and max(abs(x - sx), abs(y - sy)) > 3 and max(abs(x - gx), abs(y - gy)) > 3):
                    return False
                return True
            return allow_tele_goal and (x, y) == (gx, gy) and self.tele[y, x]

        openh = [(0, 0, (sx, sy))]
        came = {(sx, sy): None}
        g = {(sx, sy): 0}
        n = 0
        while openh and n < max_nodes:
            _, cost, cur = heapq.heappop(openh)
            n += 1
            if cur == (gx, gy):
                path = []
                while cur:
                    path.append(cur)
                    cur = came[cur]
                return path[::-1]
            cx, cy = cur
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if not dx and not dy:
                        continue
                    nx, ny = cx + dx, cy + dy
                    if not passable(nx, ny):
                        continue
                    if dx and dy and not (passable(cx + dx, cy) or passable(cx, cy + dy)):
                        continue  # no cortar esquinas
                    nc = cost + (1.414 if dx and dy else 1.0)
                    if nc < g.get((nx, ny), 1e18):
                        g[(nx, ny)] = nc
                        came[(nx, ny)] = cur
                        h = max(abs(gx - nx), abs(gy - ny))
                        heapq.heappush(openh, (nc + h, nc, (nx, ny)))
        return None


class World:
    """Todos los mapas + lo aprendido (qué nombre es qué mapa, adónde lleva cada portal)."""

    def __init__(self, game_dir, store_path):
        self.game_dir = game_dir
        self.store_path = store_path
        self.maps = {}
        self.files = {}
        if game_dir:
            md = os.path.join(game_dir, "MAPDATA")
            for fn in os.listdir(md):
                if fn.lower().endswith(".amd"):
                    self.files[os.path.splitext(fn)[0]] = os.path.join(md, fn)
        self.learn = {"names": {}, "links": {}, "anchors": {}}
        try:
            with open(store_path, encoding="utf-8") as f:
                self.learn.update(json.load(f))
        except Exception:
            pass
        self._obs = {}  # nombre -> [(x, y)] posiciones vistas
        self.base = {}
        self.load_base(os.path.join(os.path.dirname(store_path), "mundo_base.json"))
        # semilla: en el Beginner Zone el portal de la izquierda va a Aresden
        self.learn["links"].setdefault("beginner zone|81,75", {"to": "aresden", "x": None, "y": None, "t": 0, "seed": True})
        self.learn["links"].setdefault("beginner zone|128,78", {"to": "elvine", "x": None, "y": None, "t": 0, "seed": True})

    def load_base(self, path):
        """Mapa del mundo base (portales clásicos de Helbreath): se suma a lo aprendido sin pisarlo.
        No uso los edificios como paso: sus salidas dependen de por dónde se entró."""
        try:
            with open(path, encoding="utf-8") as f:
                self.base = json.load(f).get("maps", {})
        except Exception:
            self.base = {}
            return 0
        learned = {}
        for k, v in self.learn["links"].items():
            if not v.get("base"):
                m, xy = k.split("|")
                learned.setdefault(m, []).append(tuple(map(int, xy.split(","))))
        for k in [k for k, v in self.learn["links"].items() if v.get("base")]:
            del self.learn["links"][k]  # la base se rearma en cada arranque
        n = 0
        for key, groups in self.base.items():
            frm = file_display(key)
            if any(frm.endswith(s) for s in INDOOR):
                continue
            for g in groups:
                tx, ty = g["tile"]
                if any(max(abs(tx - x), abs(ty - y)) <= 8 for x, y in learned.get(frm, [])):
                    continue  # ese portal ya lo crucé: vale lo que vi
                self.learn["links"][f"{frm}|{tx},{ty}"] = {"to": file_display(g["to"]), "x": g.get("x"), "y": g.get("y"), "t": 0, "base": True}
                n += 1
        return n

    def save(self):
        try:
            tmp = self.store_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                keep = dict(self.learn, links={k: v for k, v in self.learn["links"].items() if not v.get("base")})
                json.dump(keep, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.store_path)
        except Exception:
            pass

    def get(self, file_key):
        if not file_key:
            return None
        k = self._key(file_key)
        if k is None:
            return None
        if k not in self.maps:
            try:
                self.maps[k] = MapData(self.files[k])
            except Exception:
                return None
        return self.maps[k]

    def _key(self, name):
        if name in self.files:
            return name
        low = {k.lower(): k for k in self.files}
        return low.get(str(name).lower())

    # --- identificar qué archivo es el mapa que muestra el juego
    def map_for(self, display_name):
        if not display_name:
            return None
        dn = display_name.strip().lower()
        if dn.startswith("\\"):
            dn = "a" + dn[1:]  # el OCR a veces lee la A inicial como una barra
        if dn in self.learn["names"]:
            return self.get(self.learn["names"][dn])
        g = NAME_GUESS.get(dn)
        if not g:  # el OCR a veces lee mal una letra ("\\resden"): busco el nombre más parecido
            import difflib
            clean = "".join(c for c in dn if c.isalnum() or c == " ").strip()
            best = difflib.get_close_matches(clean, list(NAME_GUESS), n=1, cutoff=0.85)
            g = NAME_GUESS[best[0]] if best else None
        if g and self._key(g):
            return self.get(g)
        return None

    def observe(self, display_name, x, y):
        """Anota posiciones; si el mapa adivinado no cierra (posición en una pared), busca el archivo correcto."""
        if not display_name:
            return
        dn = display_name.strip().lower()
        obs = self._obs.setdefault(dn, [])
        if (x, y) not in obs:
            obs.append((x, y))
            del obs[:-40]
        cur = self.map_for(display_name)
        bad = cur is None or sum(1 for p in obs if not cur.ok(*p)) > max(1, len(obs) // 5)
        if bad and len(obs) >= 5:
            best = None
            for k in self.files:
                m = self.get(k)
                if not m:
                    continue
                score = sum(1 for p in obs if m.ok(*p)) / len(obs)
                if score >= 0.95 and (best is None or score > best[0] or (score == best[0] and m.w * m.h < best[1].w * best[1].h)):
                    best = (score, m)
            if best:
                self.learn["names"][dn] = best[1].name
                self.save()

    # --- portales aprendidos: "mapa|x,y" -> {"to": nombre, "x":, "y":}
    def add_link(self, from_name, tile, to_name, to_xy):
        key = f"{from_name.strip().lower()}|{tile[0]},{tile[1]}"
        p = from_name.strip().lower() + "|"
        for k in [k for k, v in self.learn["links"].items() if v.get("base") and k.startswith(p)]:
            bx, by = map(int, k[len(p):].split(","))
            if max(abs(bx - tile[0]), abs(by - tile[1])) <= 8:
                del self.learn["links"][k]
        self.learn["links"][key] = {"to": to_name.strip().lower(), "x": to_xy[0], "y": to_xy[1], "t": int(time.time())}
        self.save()

    def links_from(self, name):
        p = name.strip().lower() + "|"
        out = []
        for k, v in self.learn["links"].items():
            if k.startswith(p):
                x, y = map(int, k[len(p):].split(","))
                out.append(((x, y), v))
        return out

    def route(self, from_name, to_name, forbidden=(), cost=None):
        """Camino por portales conocidos, eligiendo el MÁS SEGURO y no el más corto: cada mapa que piso suma
        cost(mapa) (1 si no se pasa función). Devuelve [(mapa, portal_xy)], [] si ya estoy, None si no sé."""
        a, b = from_name.strip().lower(), to_name.strip().lower()
        if a == b:
            return []
        cost = cost or (lambda name: 1.0)
        dist = {a: 0.0}
        prev = {a: None}
        heap = [(0.0, a)]
        while heap:
            d, cur = heapq.heappop(heap)
            if d > dist.get(cur, 1e18):
                continue
            if cur == b:
                steps = []
                n = b
                while prev[n]:
                    m, t = prev[n]
                    steps.append((m, t))
                    n = m
                return steps[::-1]
            for tile, v in self.links_from(cur):
                nxt = v["to"]
                if nxt in forbidden:
                    continue
                nd = d + max(0.1, float(cost(nxt))) + (0.3 if v.get("base") else 0.0)
                if nd < dist.get(nxt, 1e18):
                    dist[nxt] = nd
                    prev[nxt] = (cur, tile)
                    heapq.heappush(heap, (nd, nxt))
        return None


# ---------- mapa del mundo para la pantalla del bot ----------
# Lugar de cada mapa en la grilla del mundo (columna, fila). Lo que no está acá se acomoda solo abajo.
LAYOUT = {
    "aresden": (2, 2), "arefarm": (3, 2), "huntzone2": (2, 3), "areuni": (1, 3), "huntzone4": (2, 4), "aresdend1": (1, 2),
    "dglv2": (0, 2), "dglv3": (0, 3), "dglv4": (0, 4), "default": (1, 1),
    "middleland": (4, 1), "2ndmiddle": (4, 2), "middled1n": (4, 3), "middled1x": (5, 1), "icebound": (4, 0),
    "toh1": (5, 0), "toh2": (6, 0), "toh3": (7, 0), "bisle": (3, 0),
    "elvine": (6, 2), "elvfarm": (5, 2), "huntzone1": (6, 3), "elvuni": (7, 3), "huntzone3": (6, 4), "elvined1": (7, 2),
    "druncncity": (3, 5), "maze": (4, 5), "inferniaa": (5, 5), "inferniab": (6, 5), "procella": (7, 5), "abaddon": (7, 4),
}


# Minimapas reales del juego: sprites/interface/telescope.pak (1 píxel por baldosa). Qué dibujo es cada mapa
# lo saqué comparando cada imagen con el plano .amd (coinciden los 31 sin dudas).
TELESCOPE = {
    "huntzone3": 1, "huntzone1": 2, "elvuni": 3, "elvine": 4, "middleland": 5, "elvfarm": 6, "arefarm": 7, "default": 8,
    "huntzone4": 9, "huntzone2": 10, "areuni": 11, "aresden": 12, "dglv2": 13, "dglv3": 14, "dglv4": 15, "elvined1": 16,
    "aresdend1": 17, "bisle": 18, "toh1": 19, "toh2": 20, "toh3": 21, "middled1x": 22, "middled1n": 23, "2ndmiddle": 24,
    "icebound": 25, "druncncity": 26, "inferniaa": 27, "inferniab": 28, "maze": 29, "procella": 30, "abaddon": 31,
}


def telescope_images(game_dir):
    """Devuelve {mapa: 'data:image/png;base64,...'} con el arte del minimapa del propio juego, o {} si no está."""
    import base64
    import struct
    out = {}
    try:
        p = None
        for sub in ("sprites", "SPRITES", "Sprites"):
            c = os.path.join(game_dir or "", sub, "interface", "telescope.pak")
            if os.path.isfile(c):
                p = c
                break
        if not p:
            return out
        d = open(p, "rb").read()
        n = struct.unpack("<I", d[20:24])[0]
        ents = [struct.unpack("<II", d[24 + i * 8:32 + i * 8]) for i in range(n)]
        for key, idx in TELESCOPE.items():
            if idx >= n:
                continue
            off, size = ents[idx]
            s = d[off:off + size]
            q = s.find(b"\x89PNG")
            if q >= 0:
                out[key] = "data:image/png;base64," + base64.b64encode(s[q:]).decode()
    except Exception:
        pass
    return out


def render_map_png(m, size=220):
    """Dibujo chico del mapa desde el .amd: pasto donde se camina, oscuro donde no, amarillo las puertas."""
    import base64
    import cv2
    img = np.zeros((m.h, m.w, 3), np.uint8)
    img[:] = (38, 30, 26)
    img[m.walk] = (62, 110, 58)
    k = max(1, max(m.w, m.h) // size)
    small = cv2.resize(img, (max(1, m.w // k), max(1, m.h // k)), interpolation=cv2.INTER_AREA)
    for x, y in m.teleports:
        cv2.circle(small, (x // k, y // k), 2, (60, 200, 240), -1)
    ok, buf = cv2.imencode(".png", small)
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode() if ok else None


def world_view(world):
    """Todo lo que la pantalla necesita para dibujar el mundo: mapas (con dibujo), lugar en la grilla y portales."""
    low = {k.lower(): k for k in world.files}
    keys = [k for k in low if not any(file_display(k).endswith(s) for s in INDOOR)]
    seen = {str(v).lower() for v in world.learn.get("names", {}).values()}
    keys = [k for k in keys if k in LAYOUT or k in seen]
    used = set(LAYOUT.values())
    extra = [k for k in sorted(keys) if k not in LAYOUT]
    pos = dict(LAYOUT)
    col, row = 0, 6
    for k in extra:
        pos[k] = (col, row)
        col += 1
        if col > 7:
            col, row = 0, row + 1
    art = telescope_images(world.game_dir)
    maps = []
    for k in keys:
        m = world.get(low[k])
        if not m:
            continue
        maps.append({"key": k, "name": file_display(k).title(), "w": m.w, "h": m.h, "gx": pos[k][0], "gy": pos[k][1],
                     "img": art.get(k) or render_map_png(m), "art": k in art})
    names = {mp["name"].lower(): mp["key"] for mp in maps}
    links = []
    for kk, v in world.learn["links"].items():
        frm, xy = kk.split("|")
        x, y = map(int, xy.split(","))
        links.append({"from": names.get(frm), "from_name": frm.title(), "x": x, "y": y, "to": names.get(v["to"]), "to_name": v["to"].title(),
                      "tx": v.get("x"), "ty": v.get("y"), "seen": not v.get("base")})
    return {"maps": maps, "links": links}
