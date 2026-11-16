"""Dibujos de los objetos tirados en el piso, sacados de la instalación del juego (sprites/lgn_itemground.opk).

Formato .opk (Olympia): cabecera = u32 tamaño, u32 2, u32 cantidad de hojas; después, por hoja:
u32 256, u32 offset, u32 tamaño comprimido, u32 tamaño sin comprimir, u32 cantidad de cuadros, y por cuadro 6 int16
(x, y, ancho, alto, pivote x, pivote y). Cada hoja es un BMP comprimido con zstd; el color de fondo es el del
píxel de arriba a la izquierda. En items.dat cada objeto dice su hoja (bytes 5-6, empieza en 1) y su cuadro (7-8).
Sirve para cualquier server que use este cliente."""
import os
import struct

import cv2
import numpy as np


def _find(game_dir):
    for sub in ("sprites", "SPRITES", "Sprites"):
        p = os.path.join(game_dir or "", sub, "lgn_itemground.opk")
        if os.path.exists(p):
            return p
    return None


def load_sheets(path):
    """[{'img': BGR, 'bg': (b,g,r), 'frames': [(x, y, w, h, px, py)]}] o [] si no se puede leer."""
    try:
        import zstandard as zstd
        d = open(path, "rb").read()
        _hdr, _two, n = struct.unpack_from("<3I", d, 0)
        off, out = 12, []
        dc = zstd.ZstdDecompressor()
        for _ in range(n):
            _k, o, _cs, _us, nf = struct.unpack_from("<5I", d, off)
            off += 20
            frames = [struct.unpack_from("<6h", d, off + 12 * i) for i in range(nf)]
            off += 12 * nf
            raw = dc.decompressobj().decompress(d[o:])
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                out.append(None)
                continue
            out.append({"img": img, "bg": tuple(int(v) for v in img[0, 0]), "frames": frames})
        return out
    except Exception:
        return []


def item_sprites(game_dir):
    """{id: (hoja, cuadro)} leído de contents/items.dat."""
    out = {}
    try:
        from . import gamedata
        cd = gamedata.contents_dir(game_dir)
        d = open(os.path.join(cd, "items.dat"), "rb").read()
        n = struct.unpack_from("<I", d, 8)[0]
        off = 16
        for _ in range(n):
            iid, ln = struct.unpack_from("<HH", d, off)
            off += 4 + ln
            l2 = struct.unpack_from("<H", d, off)[0]
            off += 2 + l2
            r = d[off:off + 56]
            off += 56
            if len(r) < 56:
                break
            out[iid] = (struct.unpack_from("<H", r, 5)[0], struct.unpack_from("<H", r, 7)[0])
    except Exception:
        pass
    return out


class GroundDB:
    """Base de dibujos del piso: para cada (hoja, cuadro) el recorte, su máscara y qué objetos lo usan."""

    def __init__(self, game_dir):
        self.ok = False
        self.tpl = {}      # (hoja, cuadro) -> {'img','mask','w','h','px','py','items': [nombres]}
        p = _find(game_dir)
        if not p:
            return
        sheets = load_sheets(p)
        if not sheets:
            return
        try:
            from . import gamedata
            items = gamedata.load_items(game_dir)
        except Exception:
            items = {}
        spr = item_sprites(game_dir)
        users = {}
        for iid, (sh, fr) in spr.items():
            nm = (items.get(iid) or {}).get("name")
            if nm:
                users.setdefault((sh, fr), []).append(nm)
        for si, s in enumerate(sheets):
            if not s:
                continue
            H, W = s["img"].shape[:2]
            for fi, (x, y, w, h, px, py) in enumerate(s["frames"]):
                if w <= 2 or h <= 2 or x < 0 or y < 0 or x + w > W or y + h > H:
                    continue
                crop = s["img"][y:y + h, x:x + w].copy()
                mask = (np.abs(crop.astype(int) - np.array(s["bg"], int)).sum(axis=2) > 24).astype(np.uint8)
                if int(mask.sum()) < 12:
                    continue
                key = (si + 1, fi)
                self.tpl[key] = {"img": crop, "mask": mask, "w": w, "h": h, "px": px, "py": py, "items": users.get(key, [])}
        self.ok = bool(self.tpl)

    def used(self):
        """Solo los dibujos que algún objeto de la tabla usa (lo demás son cuadros sueltos)."""
        return {k: v for k, v in self.tpl.items() if v["items"]}
