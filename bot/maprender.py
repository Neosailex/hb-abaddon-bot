"""Dibujo lindo de cada mapa para la sección Mapa, hecho con los dibujos del propio juego (sprites/maps y
sprites/objects de la instalación). Se arma en la PC de Juan y queda en data/maprender/ (no va al repo).

.amd: cabecera de 256 bytes ('MAPSIZEX = w MAPSIZEY = h TILESIZE = 10') y por baldosa 10 bytes: int16 dibujo del
piso, int16 cuadro, int16 dibujo del objeto, int16 cuadro, byte atributos, byte libre.
.opk: ver groundsprites.py (hojas BMP comprimidas con zstd; cada cuadro x, y, ancho, alto, pivote x, pivote y).
Qué hoja es qué número de dibujo: el que dice el nombre (Tile363-366 = 363..366) o la tabla de abajo (cliente clásico)."""
import glob
import os
import re
import struct
import threading
import time

import cv2
import numpy as np

BASE = {"maptiles1": 0, "structures1": 51, "sinside1": 70, "trees1": 100, "objects1": 200, "objects2": 211,
        "objects3": 216, "objects4": 220, "objects5": 230, "objects6": 238, "objects7": 242, "maptiles2": 300,
        "maptiles4": 320, "maptiles5": 330, "maptiles6": 349, "lgn_maptiles": 546}
PX = 4          # píxeles por baldosa en el dibujo final
MAXD = 2048     # lado máximo del dibujo


def _opk(path):
    import zstandard as zstd
    d = open(path, "rb").read()
    _h, _t, n = struct.unpack_from("<3I", d, 0)
    off, out, dc = 12, [], zstd.ZstdDecompressor()
    for _ in range(n):
        _k, o, _cs, _us, nf = struct.unpack_from("<5I", d, off)
        off += 20
        fr = [struct.unpack_from("<6h", d, off + 12 * i) for i in range(nf)]
        off += 12 * nf
        try:
            raw = dc.decompressobj().decompress(d[o:])
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            img = None
        out.append((img, fr))
    return out


def _sprites_dir(game_dir):
    for sub in ("sprites", "SPRITES", "Sprites"):
        p = os.path.join(game_dir or "", sub)
        if os.path.isdir(p):
            return p
    return None


class Renderer:
    def __init__(self, game_dir, out_dir, log=None):
        self.game_dir, self.out_dir, self.log = game_dir, out_dir, log or (lambda *_: None)
        self.tab = None
        self.busy = False

    def _table(self):
        if self.tab is not None:
            return self.tab
        tab = {}
        sd = _sprites_dir(self.game_dir)
        for sub in ("maps", "objects"):
            for p in sorted(glob.glob(os.path.join(sd or "", sub, "*.opk"))):
                nm = os.path.splitext(os.path.basename(p))[0].lower()
                if "shadow" in nm or "dynamic" in nm:
                    continue
                m = re.match(r"(?:tile|maptiles)(\d+)-(\d+)", nm)
                b = int(m.group(1)) if m else BASE.get(nm)
                if b is None:
                    continue
                try:
                    for i, sh in enumerate(_opk(p)):
                        tab.setdefault(b + i, sh)
                except Exception:
                    pass
        self.tab = tab
        return tab

    @staticmethod
    def _amd(path):
        d = open(path, "rb").read()
        w, h = map(int, re.findall(rb"(\d+)", d[:256])[:2])
        dt = np.dtype([("ts", "<i2"), ("tf", "<i2"), ("os", "<i2"), ("of", "<i2"), ("a", "u1"), ("b", "u1")])
        body = d[256:256 + 10 * w * h]
        if len(body) < 10 * w * h:
            body = body + b"\x00" * (10 * w * h - len(body))  # archivo más corto (algunos fightzone): relleno vacío
        return np.frombuffer(body, dtype=dt).reshape(h, w)

    def render(self, amd_path):
        tab = self._table()
        a = self._amd(amd_path)
        h, w = a.shape
        px = max(1, min(PX, MAXD // max(w, h)))
        s = px / 32.0
        img = np.zeros((h * px, w * px, 3), np.uint8)
        cache = {}
        for y in range(h):
            row = a[y]
            for x in range(w):
                k = (int(row[x]["ts"]), int(row[x]["tf"]))
                t = cache.get(k)
                if t is None:
                    t = False
                    sh = tab.get(k[0])
                    if sh and sh[0] is not None and 0 <= k[1] < len(sh[1]):
                        fx, fy, fw, fh = sh[1][k[1]][:4]
                        if fw > 0 and fh > 0 and fx >= 0 and fy >= 0:
                            t = cv2.resize(sh[0][fy:fy + fh, fx:fx + fw], (px, px), interpolation=cv2.INTER_AREA)
                    cache[k] = t
                if t is not False:
                    img[y * px:(y + 1) * px, x * px:(x + 1) * px] = t
        # objetos (árboles, casas, escaleras, flores), de arriba hacia abajo para que se tapen bien
        H, W = img.shape[:2]
        oc = {}
        ys, xs = np.nonzero(a["os"] > 0)
        for y, x in sorted(zip(ys.tolist(), xs.tolist())):
            k = (int(a[y, x]["os"]), int(a[y, x]["of"]))
            c = oc.get(k)
            if c is None:
                c = False
                sh = tab.get(k[0])
                if sh and sh[0] is not None and 0 <= k[1] < len(sh[1]):
                    fx, fy, fw, fh, pvx, pvy = sh[1][k[1]]
                    if fw > 0 and fh > 0 and fx >= 0 and fy >= 0:
                        spr = sh[0][fy:fy + fh, fx:fx + fw]
                        bg = sh[0][0, 0].astype(int)
                        m = (np.abs(spr.astype(int) - bg).sum(axis=2) > 24).astype(np.uint8)
                        nw, nh = max(1, int(round(fw * s))), max(1, int(round(fh * s)))
                        c = (cv2.resize(spr, (nw, nh), interpolation=cv2.INTER_AREA),
                             cv2.resize(m, (nw, nh), interpolation=cv2.INTER_NEAREST).astype(bool), pvx * s, pvy * s)
                oc[k] = c
            if c is False:
                continue
            spr, m, ox, oy = c
            X, Y = int(round(x * px + ox)), int(round(y * px + oy))
            x0, y0, x1, y1 = max(0, X), max(0, Y), min(W, X + spr.shape[1]), min(H, Y + spr.shape[0])
            if x1 <= x0 or y1 <= y0:
                continue
            mm = m[y0 - Y:y1 - Y, x0 - X:x1 - X]
            img[y0:y1, x0:x1][mm] = spr[y0 - Y:y1 - Y, x0 - X:x1 - X][mm]
        return img

    def path_for(self, key):
        return os.path.join(self.out_dir, f"{key.lower()}.jpg")

    def ensure(self, key, amd_path):
        p = self.path_for(key)
        try:
            if os.path.exists(p) and os.path.getmtime(p) >= os.path.getmtime(amd_path):
                return p
            img = self.render(amd_path)
            if img is None or img.size == 0 or not img.any():
                return None
            os.makedirs(self.out_dir, exist_ok=True)
            cv2.imwrite(p, img, [cv2.IMWRITE_JPEG_QUALITY, 82])
            return p
        except Exception as e:
            self.fails = getattr(self, "fails", 0) + 1
            if self.fails <= 1:
                self.log(f"🗺 Algunos mapas no se pueden dibujar ({key}: {e!r}); quedan con el dibujo simple.")
            return None

    def render_all(self, files, keys=None):
        """En segundo plano: dibuja los mapas que falten (tarda un par de minutos la primera vez)."""
        if self.busy or not _sprites_dir(self.game_dir):
            return
        self.busy = True

        def go():
            n = 0
            try:
                for k, fp in sorted(files.items()):
                    if keys is not None and k.lower() not in keys:
                        continue
                    p = self.path_for(k)
                    if os.path.exists(p) and os.path.getmtime(p) >= os.path.getmtime(fp):
                        continue
                    if self.ensure(k, fp):
                        n += 1
                    time.sleep(0.05)
                if n:
                    self.log(f"🗺 Dibujé {n} mapa/s con los gráficos del juego (sección Mapa).")
            finally:
                self.busy = False
        threading.Thread(target=go, daemon=True).start()

    def data_url(self, key):
        import base64
        p = self.path_for(key)
        if not os.path.exists(p):
            return None
        return "data:image/jpeg;base64," + base64.b64encode(open(p, "rb").read()).decode()
