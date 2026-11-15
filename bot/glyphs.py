"""Lector de números chicos con plantillas que el bot aprende solo.

El OCR de Windows no lee cifras sueltas en la letra pixelada del juego ("4" debajo de una poción). Cada vez que
el OCR sí lee bien un número de 2 o más cifras (el oro "24", los stats "14", "23"...), corto cada cifra y la
guardo como ejemplo de ese dígito. Después, para una cifra suelta, busco el ejemplo más parecido (vecino más
cercano). Es una "red" mínima que se entrena sola con lo que va viendo; vale para cada server por separado.
"""
import json
import os

import numpy as np

W, H = 10, 14  # tamaño normalizado de cada cifra


def _segments(mask):
    """Corta una máscara binaria (texto = True) en columnas de tinta separadas por columnas vacías."""
    cols = mask.any(axis=0)
    segs, start = [], None
    for i, c in enumerate(list(cols) + [False]):
        if c and start is None:
            start = i
        elif not c and start is not None:
            segs.append((start, i))
            start = None
    out = []
    for a, b in segs:
        sub = mask[:, a:b]
        rows = np.nonzero(sub.any(axis=1))[0]
        if len(rows) == 0:
            continue
        out.append(sub[rows.min():rows.max() + 1])
    if out:
        # descarto pedacitos sueltos (bordes del ícono, puntos): las cifras tienen todas casi la misma altura
        hmax = max(g.shape[0] for g in out)
        out = [g for g in out if g.shape[0] >= 0.6 * hmax and g.sum() >= 6]
    return out


def _norm(g):
    import cv2
    g = g.astype(np.uint8) * 255
    g = cv2.resize(g, (W, H), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    return g.reshape(-1)


class GlyphReader:
    def __init__(self, path):
        self.path = path
        self.samples = {}  # dígito -> [vectores]
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            self.samples = {k: [np.array(v, np.float32) for v in vs] for k, vs in raw.items()}
        except Exception:
            pass
        self._dirty = 0

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({k: [np.round(v, 3).tolist() for v in vs] for k, vs in self.samples.items()}, f)
        except Exception:
            pass

    def learn(self, mask, text):
        """mask: binaria con el número que el OCR leyó como 'text'. Guarda cada cifra si el corte coincide."""
        text = "".join(c for c in str(text) if c.isdigit())
        if not text:
            return False
        segs = _segments(mask)
        if len(segs) != len(text):
            return False
        for g, ch in zip(segs, text):
            vs = self.samples.setdefault(ch, [])
            v = _norm(g)
            if not any(np.abs(v - u).mean() < 0.02 for u in vs):  # no repito ejemplos casi iguales
                vs.append(v)
                del vs[:-12]
                self._dirty += 1
        if self._dirty >= 3:
            self._dirty = 0
            self.save()
        return True

    def known(self):
        return sorted(self.samples)

    def read(self, mask, max_dist=0.30, margin=1.4, max_segs=4):
        """Número leído con las plantillas, o None si alguna cifra no se parece lo suficiente a nada."""
        if not self.samples:
            return None
        segs = _segments(mask)
        if not segs or len(segs) > max_segs:
            return None
        out = ""
        for g in segs:
            v = _norm(g)
            dist = {ch: min(float(np.abs(v - u).mean()) for u in vs) for ch, vs in self.samples.items() if vs}
            if not dist:
                return None
            order = sorted(dist, key=dist.get)
            best, bd = order[0], dist[order[0]]
            second = dist[order[1]] if len(order) > 1 else 9.0
            # tiene que parecerse y además ganarle claro a la segunda opción
            if bd > max_dist or second < bd * margin:
                return None
            out += best
        return int(out)
