"""Visión: captura de la ventana, lectura de barras HP/MP/SP y búsqueda de monstruos/ítems por plantilla."""
import base64
import os

import cv2
import numpy as np

try:
    import mss
except Exception:  # permite tests sin mss
    mss = None


class Screen:
    def __init__(self):
        self._sct = None

    hwnd = None
    use_printwindow = False  # PrintWindow devuelve imágenes viejas con este juego: se usa la pantalla

    def grab(self, rect):
        """rect = (x, y, w, h) en pantalla -> imagen BGR.
        Primero intenta copiar la ventana del juego directo (aunque esté tapada); si no, saca foto de la pantalla."""
        if self.hwnd and self.use_printwindow:
            from . import winapi
            img = winapi.capture_window(self.hwnd)
            if img is not None and rect and img.shape[1] == rect[2] and img.shape[0] == rect[3]:
                return img
        if mss is None or rect is None:
            return None
        if self._sct is None:
            self._sct = mss.mss()
        x, y, w, h = rect
        shot = self._sct.grab({"left": x, "top": y, "width": w, "height": h})
        return np.ascontiguousarray(np.array(shot)[:, :, :3])


def bar_percent(img, bar, tol=45):
    """Porcentaje lleno de una barra horizontal.

    bar = {"x1","y1","x2","y2","color":[b,g,r]} (inicio y final de la barra, color de la barra llena).
    Mira una franja de 3 px de alto sobre la línea y busca hasta dónde llega el color.
    """
    if img is None or not bar:
        return None
    h, w = img.shape[:2]
    x1, x2 = sorted((int(bar["x1"]), int(bar["x2"])))
    y = int(round((bar["y1"] + bar["y2"]) / 2))
    x1, x2 = max(0, x1), min(w - 1, x2)
    if x2 - x1 < 3 or not (0 <= y < h):
        return None
    ys = slice(max(0, y - 1), min(h, y + 2))
    strip = img[ys, x1:x2 + 1].astype(np.int16)
    color = np.array(bar["color"], dtype=np.int16).reshape(1, 1, 3)
    ok = (np.abs(strip - color).max(axis=2) <= tol).any(axis=0)
    if not ok.any():
        return 0.0
    # La barra se vacía de derecha a izquierda: el último píxel con color marca el nivel.
    # Se toleran huecos chicos (números escritos encima de la barra).
    filled = np.where(ok)[0]
    last = filled.max()
    density = ok[: last + 1].mean()
    if density < 0.35:  # demasiado ruido: usar proporción simple
        return round(100.0 * ok.mean(), 1)
    return round(100.0 * (last + 1) / len(ok), 1)


class Templates:
    """Plantillas de monstruos e ítems capturadas por el usuario (PNG en data/templates/<tipo>/)."""

    def __init__(self, root):
        self.root = root
        self.items = {"monsters": {}, "items": {}}
        self.reload()

    def reload(self):
        for kind in self.items:
            self.items[kind] = {}
            folder = os.path.join(self.root, kind)
            os.makedirs(folder, exist_ok=True)
            for fn in sorted(os.listdir(folder)):
                if fn.lower().endswith(".png"):
                    img = cv2.imread(os.path.join(folder, fn), cv2.IMREAD_COLOR)
                    if img is not None and img.shape[0] >= 8 and img.shape[1] >= 8:
                        self.items[kind][fn[:-4]] = img

    def save(self, kind, name, img):
        folder = os.path.join(self.root, kind)
        os.makedirs(folder, exist_ok=True)
        safe = "".join(c for c in name if c.isalnum() or c in " _-()'").strip() or "plantilla"
        base, n = safe, 2
        while os.path.exists(os.path.join(folder, safe + ".png")):
            safe = f"{base} {n}"
            n += 1
        cv2.imwrite(os.path.join(folder, safe + ".png"), img)
        self.reload()
        return safe

    def delete(self, kind, name):
        p = os.path.join(self.root, kind, name + ".png")
        if os.path.exists(p):
            os.remove(p)
        self.reload()

    def thumbs(self):
        out = {}
        for kind, d in self.items.items():
            out[kind] = [{"name": n, "img": to_data_url(im, 70)} for n, im in d.items()]
        return out


_scaled_cache = {}


def _scaled(tpl, scale):
    key = (id(tpl), scale)
    t = _scaled_cache.get(key)
    if t is None:
        t = cv2.resize(tpl, (max(4, int(tpl.shape[1] * scale)), max(4, int(tpl.shape[0] * scale))), interpolation=cv2.INTER_AREA)
        if len(_scaled_cache) > 400:
            _scaled_cache.clear()
        _scaled_cache[key] = t
    return t


def find(img, templates, threshold=0.72, roi=None, ignore=(), max_hits=30, scale=1.0):
    """Busca todas las plantillas. Devuelve [{name,x,y,w,h,score}] (x,y = centro, coords de img).
    scale < 1 achica imagen y plantillas antes de comparar: mucho menos CPU."""
    if img is None or not templates:
        return []
    ox = oy = 0
    area = img
    if roi:
        x1, y1, x2, y2 = (int(roi[k]) for k in ("x1", "y1", "x2", "y2"))
        x1, x2 = sorted((max(0, x1), min(img.shape[1], x2)))
        y1, y2 = sorted((max(0, y1), min(img.shape[0], y2)))
        if x2 - x1 > 10 and y2 - y1 > 10:
            area, ox, oy = img[y1:y2, x1:x2], x1, y1
    scale = float(scale or 1.0)
    if scale < 0.99:
        area = cv2.resize(area, (max(1, int(area.shape[1] * scale)), max(1, int(area.shape[0] * scale))), interpolation=cv2.INTER_AREA)
    hits = []
    for name, tpl in templates.items():
        if name in ignore:
            continue
        H, W = tpl.shape[:2]
        if scale < 0.99:
            tpl = _scaled(tpl, scale)
        th, tw = tpl.shape[:2]
        if th > area.shape[0] or tw > area.shape[1]:
            continue
        res = cv2.matchTemplate(area, tpl, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= threshold)
        if len(ys) > 400:  # demasiados: quedarse con los mejores
            idx = np.argsort(res[ys, xs])[-400:]
            ys, xs = ys[idx], xs[idx]
        for y, x in zip(ys, xs):
            hits.append({"name": name, "x": int((x + tw / 2) / scale + ox), "y": int((y + th / 2) / scale + oy),
                         "w": int(W), "h": int(H), "score": float(res[y, x])})
    return nms(hits)[:max_hits]


def nms(hits, overlap=0.4):
    """Saca detecciones repetidas del mismo monstruo."""
    hits = sorted(hits, key=lambda h: -h["score"])
    keep = []
    for h in hits:
        dup = False
        for k in keep:
            dx = abs(h["x"] - k["x"]) / max(1, (h["w"] + k["w"]) / 2)
            dy = abs(h["y"] - k["y"]) / max(1, (h["h"] + k["h"]) / 2)
            if dx < (1 - overlap) and dy < (1 - overlap):
                dup = True
                break
        if not dup:
            keep.append(h)
    return keep


def crop_around(img, x, y, size):
    if img is None:
        return None
    h, w = img.shape[:2]
    half = size // 2
    x1, y1 = max(0, int(x) - half), max(0, int(y) - half)
    x2, y2 = min(w, x1 + size), min(h, y1 + size)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    return img[y1:y2, x1:x2].copy()


def to_data_url(img, quality=60, max_w=None):
    if img is None:
        return ""
    if max_w and img.shape[1] > max_w:
        s = max_w / img.shape[1]
        img = cv2.resize(img, (int(img.shape[1] * s), int(img.shape[0] * s)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


def draw_overlay(img, cfg, monsters=(), items=(), target=None, bars=None):
    """Dibuja lo que ve el bot sobre una copia de la pantalla (para la vista previa)."""
    if img is None:
        return None
    out = img.copy()
    c = cfg.get("calib", {})
    ga = c.get("game_area")
    if ga:
        cv2.rectangle(out, (int(ga["x1"]), int(ga["y1"])), (int(ga["x2"]), int(ga["y2"])), (90, 90, 90), 1)
    for key, col in (("hp_bar", (60, 60, 255)), ("mp_bar", (255, 140, 40)), ("sp_bar", (60, 220, 60))):
        b = c.get(key)
        if b:
            cv2.line(out, (int(b["x1"]), int(b["y1"])), (int(b["x2"]), int(b["y2"])), col, 2)
    cc = c.get("char_center")
    if cc:
        cv2.circle(out, (int(cc["x"]), int(cc["y"])), 6, (0, 215, 255), 2)
    for m in monsters:
        col = (0, 0, 255) if target and m is target else (0, 165, 255)
        cv2.rectangle(out, (m["x"] - m["w"] // 2, m["y"] - m["h"] // 2), (m["x"] + m["w"] // 2, m["y"] + m["h"] // 2), col, 2)
        cv2.putText(out, f'{m["name"]} {m["score"]:.2f}', (m["x"] - m["w"] // 2, m["y"] - m["h"] // 2 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, col, 1, cv2.LINE_AA)
    for it in items:
        cv2.rectangle(out, (it["x"] - it["w"] // 2, it["y"] - it["h"] // 2), (it["x"] + it["w"] // 2, it["y"] + it["h"] // 2), (0, 220, 0), 2)
    return out


# ---------------------------------------------------------------- modo automático
def small_gray(img, roi, scale=0.5):
    """Recorte de la zona de juego en gris y achicado (para detectar movimiento rápido)."""
    if img is None:
        return None
    x1, y1, x2, y2 = (int(roi[k]) for k in ("x1", "y1", "x2", "y2"))
    a = img[max(0, y1):y2, max(0, x1):x2]
    if a.size == 0:
        return None
    g = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (max(1, int(g.shape[1] * scale)), max(1, int(g.shape[0] * scale))), interpolation=cv2.INTER_AREA)


def motion_blobs(prev, cur, thresh=22, min_area=30, max_area=12000, max_changed=0.25):
    """Cosas que se mueven entre dos cuadros (monstruos animados). Devuelve [] si se movió toda la pantalla."""
    if prev is None or cur is None or prev.shape != cur.shape:
        return None
    d = cv2.absdiff(prev, cur)
    _, m = cv2.threshold(d, thresh, 255, cv2.THRESH_BINARY)
    if m.mean() / 255.0 > max_changed:
        return None  # la pantalla se desplazó: no sirve este cuadro
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    m = cv2.dilate(m, np.ones((5, 5), np.uint8), iterations=2)
    n, _, stats, cent = cv2.connectedComponentsWithStats(m)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if min_area <= area <= max_area:
            out.append({"x": float(cent[i][0]), "y": float(cent[i][1]), "w": int(w), "h": int(h), "area": int(area)})
    return out


def _name_like(a):
    """Píxeles que parecen letras de nombre: blancas, rojas o amarillas bien saturadas."""
    B, G, R = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    white = (mn > 205) & (mx - mn < 40)
    red = (R > 170) & (G < 95) & (B < 95)
    yellow = (R > 190) & (G > 170) & (B < 90)
    return white | red | yellow


def new_text_pixels(before, after, box, cursor=None):
    """Cuenta píxeles claros que aparecieron en `box` (ej. el nombre del monstruo al pasar el mouse).
    Ignora la zona del cursor del juego."""
    if before is None or after is None or before.shape != after.shape:
        return 0
    h, w = before.shape[:2]
    x1, y1, x2, y2 = (int(v) for v in box)
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return 0
    b = before[y1:y2, x1:x2].astype(np.int16)
    a = after[y1:y2, x1:x2].astype(np.int16)
    # letras nuevas: blancas o de color (en Abaddon los nombres de monstruos salen en rojo)
    changed = np.abs(a - b).max(axis=2) > 70
    mask = changed & _name_like(a)
    if cursor:
        cx, cy = cursor
        mask[max(0, cy - 12 - y1):max(0, cy + 70 - y1), max(0, cx - 12 - x1):max(0, cx + 70 - x1)] = False
    return int(mask.sum())


def bright_text_pixels(img, box, cursor=None):
    """Cuenta píxeles casi blancos (letras del nombre) en `box`, sin contar el cursor."""
    if img is None:
        return 0
    h, w = img.shape[:2]
    x1, y1, x2, y2 = (int(v) for v in box)
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return 0
    a = img[y1:y2, x1:x2].astype(np.int16)
    mask = _name_like(a)
    if cursor:
        cx, cy = cursor
        mask[max(0, cy - 12 - y1):max(0, cy + 70 - y1), max(0, cx - 12 - x1):max(0, cx + 70 - x1)] = False
    return int(mask.sum())


def enemy_pixels(img, x, y):
    """Píxeles rojos del cartel '(Enemy)' que el juego dibuja al lado del cursor cuando está sobre un monstruo."""
    if img is None:
        return 0
    h, w = img.shape[:2]
    x1, y1, x2, y2 = max(0, int(x) - 90), max(0, int(y) - 35), min(w, int(x) + 110), min(h, int(y) + 80)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return 0
    a = img[y1:y2, x1:x2].astype(np.int16)
    B, G, R = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    return int(((R > 160) & (G < 90) & (B < 90) & (R - G > 90)).sum())


_LVL_TPL = None


def _lvl_masks(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    b, gg, r = (img[:, :, k].astype(np.int16) for k in range(3))
    dark = ((g < 22) * 255).astype(np.uint8)
    yellow = (((r > 110) & (gg > 110) & (b < 90) & (np.abs(r - gg) < 60)) * 255).astype(np.uint8)
    return {"dark": dark, "yellow": yellow}


def find_level_up(img, thr=0.62):
    """Busca el cartel 'Level Up!' abajo a la derecha. Titila entre amarillo y oscuro:
    se prueba con las dos plantillas. Devuelve (x, y, score) o None."""
    global _LVL_TPL
    if img is None:
        return None
    if _LVL_TPL is None:
        import os
        d = os.path.join(os.path.dirname(__file__), "assets")
        _LVL_TPL = {k: cv2.imread(os.path.join(d, f), cv2.IMREAD_GRAYSCALE)
                    for k, f in (("dark", "levelup_mask.png"), ("yellow", "levelup_yellow.png"))}
    H, W = img.shape[:2]
    s = W / 1280.0
    y1, y2, x1, x2 = int(H * 0.60), int(H * 0.895), int(W * 0.70), W  # sin tocar la botonera de abajo
    masks = _lvl_masks(img[y1:y2, x1:x2])
    k = np.ones((3, 3), np.uint8)
    best = None
    for name, t in _LVL_TPL.items():
        if t is None:
            continue
        if abs(s - 1) > 0.02:
            t = cv2.resize(t, None, fx=s, fy=s, interpolation=cv2.INTER_NEAREST)
        m = masks[name]
        if m.shape[0] < t.shape[0] or m.shape[1] < t.shape[1]:
            continue
        r = cv2.matchTemplate(cv2.dilate(m, k).astype(np.float32), cv2.dilate(t, k).astype(np.float32), cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(r)
        if mx >= thr and (best is None or mx > best[2]):
            best = (x1 + loc[0] + t.shape[1] // 2, y1 + loc[1] + t.shape[0] // 2, float(mx))
    return best


def find_stat_buttons(img):
    """Botones naranjas '+' de la ventana 'Level up Setting'. Devuelve [(x, y)] de arriba hacia abajo (hasta 6)."""
    if img is None:
        return []
    H, W = img.shape[:2]
    sc = W / 1280.0
    b, g, r = (img[:, :, k].astype(np.int16) for k in range(3))
    m = ((r > 145) & (r < 230) & (g > 80) & (g < 145) & (b > 25) & (b < 90) & (r - b > 80)).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, _, st, cen = cv2.connectedComponentsWithStats(m)
    # el '+' y el '-' quedan pegados en una sola mancha de ~43x19
    plus = [(float(c[0]) - 12 * sc, float(c[1])) for s_, c in zip(st[1:], cen[1:])
            if 34 * sc <= s_[2] <= 54 * sc and 12 * sc <= s_[3] <= 27 * sc]
    if not plus:
        return []
    # la columna con más botones
    best = max(plus, key=lambda p: sum(1 for q in plus if abs(q[0] - p[0]) < 6 * sc))
    col = sorted([q for q in plus if abs(q[0] - best[0]) < 6 * sc], key=lambda q: q[1])
    out = [col[0]]
    for q in col[1:]:
        if 22 * sc < q[1] - out[-1][1] < 40 * sc:
            out.append(q)
    if len(out) < 3:
        return []  # menos de 3 renglones alineados: no es la ventana (madera, antorchas, etc.)
    gaps = [out[i + 1][1] - out[i][1] for i in range(len(out) - 1)]
    if max(gaps) - min(gaps) > 9 * sc:
        return []
    return [(int(round(x)), int(round(y))) for x, y in out[:6]]


def find_bag_items(img, box):
    """Pociones rojas y carne dentro del bolso abierto. box = (x1, y1, x2, y2). Devuelve (pociones, carnes) como [(x, y)]."""
    if img is None:
        return [], []
    H, W = img.shape[:2]
    x1, y1, x2, y2 = [int(v) for v in box]
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
    c = img[y1:y2, x1:x2]
    if c.size == 0:
        return [], []
    b, g, r = (c[:, :, k].astype(np.int16) for k in range(3))
    red = ((r > 150) & (g < 70) & (b < 70)).astype(np.uint8)
    n, _, st, cen = cv2.connectedComponentsWithStats(red)
    pots = [(x1 + int(cc[0]), y1 + int(cc[1])) for s_, cc in zip(st[1:], cen[1:]) if 20 <= s_[4] <= 140 and s_[2] <= 18 and s_[3] <= 22]
    # carne: punta de hueso clara (gris/blanca) con marrón abajo; van en fila, así que exijo vecinos
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    white = ((mn > 150) & (mx - mn < 40)).astype(np.uint8)
    n, _, st, cen = cv2.connectedComponentsWithStats(white)
    cand = []
    hh, ww = white.shape
    for s_, cc in zip(st[1:], cen[1:]):
        if not (6 <= s_[4] <= 90 and s_[2] <= 12 and s_[3] <= 14):
            continue
        cx, cy = int(cc[0]), int(cc[1])
        wy1, wy2, wx1, wx2 = min(hh, cy + 8), min(hh, cy + 24), max(0, cx - 14), min(ww, cx + 3)
        if wy2 <= wy1 or wx2 <= wx1:
            continue
        rr, gg, bb = r[wy1:wy2, wx1:wx2], g[wy1:wy2, wx1:wx2], b[wy1:wy2, wx1:wx2]
        brown = (rr > 95) & (rr < 190) & (gg > 60) & (gg < rr) & (bb < gg) & (bb < 110) & (rr - bb > 45)
        if brown.mean() > 0.15:
            cand.append((cx, cy, max(0, cx - 6), min(hh - 1, cy + 16)))
    meats = []
    for cx, cy, xx, yy in cand:
        if any(o is not None and 8 <= abs(o[0] - cx) <= 20 and abs(o[1] - cy) <= 4 for o in cand):
            meats.append((x1 + xx, y1 + yy))
    meats = sorted(meats, reverse=True)  # de derecha a izquierda (lejos de otros items)
    return sorted(pots, key=lambda p: (p[1], p[0])), meats


def neutral_pixels(img, x, y):
    """Píxeles azules del cartel 'Neutral' que sale debajo del nombre de un NPC al pasarle el mouse."""
    if img is None:
        return 0
    h, w = img.shape[:2]
    x1, y1, x2, y2 = max(0, int(x) - 110), max(0, int(y) - 40), min(w, int(x) + 130), min(h, int(y) + 90)
    c = img[y1:y2, x1:x2].astype(np.int16)
    if c.size == 0:
        return 0
    b, g, r = c[:, :, 0], c[:, :, 1], c[:, :, 2]
    return int(((b > 170) & (r < 90) & (g < 120) & (b - r > 110)).sum())


_SHOP_TPL = None


def find_shop_header(img, thr=0.5):
    """Encabezado 'ITEM ... PRICE' de la lista de venta. Devuelve el centro de la palabra PRICE (x, y) o None."""
    global _SHOP_TPL
    if img is None:
        return None
    if _SHOP_TPL is None:
        import os
        _SHOP_TPL = cv2.imread(os.path.join(os.path.dirname(__file__), "assets", "shop_header.png"), cv2.IMREAD_GRAYSCALE)
        if _SHOP_TPL is None:
            return None
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    sc = img.shape[1] / 1280.0
    t = _SHOP_TPL if abs(sc - 1) < 0.02 else cv2.resize(_SHOP_TPL, None, fx=sc, fy=sc, interpolation=cv2.INTER_NEAREST)
    m = ((g < 75) * 255).astype(np.uint8)
    k = np.ones((3, 3), np.uint8)
    r = cv2.matchTemplate(cv2.dilate(m, k).astype(np.float32), cv2.dilate(t, k).astype(np.float32), cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(r)
    if mx < thr:
        return None
    return (loc[0] + 200 * sc, loc[1] + 13 * sc, float(mx))
