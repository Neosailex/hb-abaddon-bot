"""Lectura del bolso (Olympia): cantidades en verde debajo de cada frasco, oro en blanco, y los objetos sueltos
aunque estén encimados (un frasco arriba de un escudo). Trabaja solo con la imagen: se puede probar sin el juego."""
import re

import cv2
import numpy as np

NUM_RE = re.compile(r"(\d+(?:\.\d+)?)([km]?)")


def _groups(mask, kx, ky):
    n, lab, st, cen = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    cs = [[int(st[i, 0]), int(st[i, 1]), int(st[i, 0] + st[i, 2]), int(st[i, 1] + st[i, 3])] for i in range(1, n)
          if st[i, 4] >= 12 and 9 * ky <= st[i, 3] <= 20 * ky and 3 * kx <= st[i, 2] <= 60 * kx]
    cs.sort()
    gs = []
    for c in cs:
        for o in gs:
            if -2 <= c[0] - o[2] <= 5 * kx and abs(c[1] - o[1]) <= 3 and abs(c[3] - o[3]) <= 3:
                o[2], o[1], o[3] = max(o[2], c[2]), min(o[1], c[1]), max(o[3], c[3])
                break
        else:
            gs.append(c)
    return gs


def _colors(px):
    """Cuántos píxeles de cada color de frasco (y de oro) hay en un recorte."""
    b, g, r = px[:, 0], px[:, 1], px[:, 2]
    return {"vida": int(((r > 140) & (g < 70) & (b < 80)).sum()),
            "maná": int(((b > 120) & (r < 110) & (g < 110)).sum()),
            "energía": int(((g > 110) & (r < 90) & (b < 90)).sum()),
            "oro": int(((r > 150) & (g > 140) & (b < 130)).sum())}


def scan(img, pw, words=(), readnum=None):
    """img: captura del juego; pw: (x, y) de la palabra 'Peso:' del bolso; words: palabras del OCR de la ventana
    [(texto, x, y)]; readnum(img, x1, y1, x2, y2) -> número o None.
    Devuelve {'gold', 'gold_approx', 'pots': {vida, maná, energía}, 'unknown', 'items': [{x, y, area, w, h}]}."""
    H, W = img.shape[:2]
    kx, ky = W / 1920.0, H / 1080.0
    x1, y1 = max(0, int(pw[0] - 40 * kx)), int(pw[1] + 15 * ky)
    x2, y2 = min(W, int(pw[0] + 440 * kx)), min(H, int(pw[1] + 330 * ky))
    roi = img[y1:y2, x1:x2]
    b, g, r = [roi[:, :, i].astype(int) for i in range(3)]
    mx = np.maximum(np.maximum(r, g), b)
    green = (g > 115) & (g - r > 40) & (g - b > 40)
    out = {"gold": None, "gold_approx": False, "pots": {"vida": 0, "maná": 0, "energía": 0}, "unknown": False,
           "items": [], "box": (x1, y1, x2, y2)}
    taken = np.zeros(roi.shape[:2], np.uint8)  # lo que ya sé qué es (frascos, números, oro): no es un objeto suelto
    centers = []
    orphans = []
    # 1) cantidades en verde: cada una pertenece al frasco que tiene arriba a la izquierda
    for o in _groups(green, kx, ky):
        bb = roi[max(0, o[1] - int(36 * ky)):max(0, o[1] - int(4 * ky)), max(0, o[0] - int(30 * kx)):o[0] + int(4 * kx)]
        col = _colors(bb.reshape(-1, 3).astype(int))
        kind = max(("vida", "maná", "energía"), key=lambda k: col[k])
        if col[kind] < 25:
            # no veo el frasco (puede estar tapado por otro objeto): guardo la cantidad para decidir al final
            if (o[2] - o[0]) >= 6 * kx:
                orphans.append(o)
            continue
        n = readnum(img, x1 + o[0] - 3, y1 + o[1] - 3, x1 + o[2] + 3, y1 + o[3] + 3) if readnum else None
        if n is None:
            # mi lector de cifras no pudo: uso el número que leyó el OCR de la ventana en ese mismo lugar
            for w in words:
                t = str(w[0]).strip()
                if t.isdigit() and 1 <= int(t) <= 999 and x1 + o[0] - 8 * kx <= w[1] <= x1 + o[2] + 8 * kx \
                        and y1 + o[1] - 8 * ky <= w[2] <= y1 + o[3] + 8 * ky:
                    n = int(t)
                    break
        if n is None:
            out["unknown"] = True
            n = 2  # con número a la vista hay al menos dos
        out["pots"][kind] += n
        c = (o[0] - int(10 * kx), o[1] - int(20 * ky))
        centers.append(c)
        out.setdefault("pot_kinds", []).append(kind)
        cv2.circle(taken, c, int(15 * kx), 1, -1)
        cv2.rectangle(taken, (o[0] - 2, o[1] - 2), (o[2] + 2, o[3] + 2), 1, -1)
    # 1b) cantidad verde sin frasco a la vista: las cantidades verdes son siempre de pociones. Si no conté ninguna
    #     de vida, la más grande es la de vida (el frasco rojo quedó tapado por una espada o un escudo).
    if orphans and out["pots"]["vida"] == 0 and readnum:
        vals = [(readnum(img, x1 + o[0] - 3, y1 + o[1] - 3, x1 + o[2] + 3, y1 + o[3] + 3), o) for o in orphans]
        vals = [(v, o) for v, o in vals if v is not None and 2 <= v <= 99]
        if vals:
            v, o = max(vals, key=lambda t: t[0])
            out["pots"]["vida"] = v
            out["vida_hidden"] = True
            out["vida_hidden_pt"] = (int(x1 + o[0] - 10 * kx), int(y1 + o[1] - 20 * ky))
            cv2.rectangle(taken, (o[0] - 2, o[1] - 2), (o[2] + 2, o[3] + 2), 1, -1)
    # 2) oro: el número en blanco que está pegado al montón de oro (el juego abrevia: '774', '2.6k', '1.2m').
    #    Los otros números blancos son cantidades de objetos apilados: no son oro.
    inbox = sorted([w for w in words if x1 <= w[1] <= x2 and y1 <= w[2] <= y2], key=lambda w: (round(w[2] / (8 * ky)), w[1]))
    toks = []
    for w in inbox:
        t = w[0].strip().lower().replace(",", ".").replace(" ", "")
        if toks and abs(toks[-1][2] - w[2]) < 6 * ky and 0 <= w[1] - toks[-1][1] < 34 * kx and re.fullmatch(r"[\d.km]+", t) and re.fullmatch(r"[\d.]+", toks[-1][0]):
            toks[-1] = (toks[-1][0] + t, (toks[-1][1] + w[1]) / 2.0, toks[-1][2])  # '8.' + '4k' partido por el OCR
        else:
            toks.append((t, w[1], w[2]))
    cands = []
    for t, wx, wy in toks:
        m = NUM_RE.fullmatch(t)
        if not m:
            continue
        px = img[max(0, int(wy - 9 * ky)):int(wy + 9 * ky), max(0, int(wx - 20 * kx)):int(wx + 20 * kx)].reshape(-1, 3).astype(int)
        br = px[px.max(axis=1) > 110]
        if len(br) < 10 or ((br[:, 1] - br[:, 2]) > 40).mean() > 0.3:
            continue  # verde: es una cantidad de frascos, no el oro
        pile = img[max(0, int(wy - 50 * ky)):max(1, int(wy - 2 * ky)), max(0, int(wx - 62 * kx)):max(1, int(wx + 10 * kx))].reshape(-1, 3).astype(int)
        cands.append((_colors(pile)["oro"], bool(m.group(2)), m, wx, wy))
    if cands:
        best = max(cands, key=lambda c: (c[0] >= 40, c[1], c[0]))
        if best[0] >= 40 or best[1] or len(cands) == 1:
            m, wx, wy = best[2], best[3], best[4]
            out["gold"] = int(round(float(m.group(1)) * {"": 1, "k": 1000, "m": 1000000}[m.group(2)]))
            out["gold_approx"] = bool(m.group(2))
            out["gold_xy"] = (int(wx), int(wy))
            cv2.rectangle(taken, (int(wx - x1 - 26 * kx), int(wy - y1 - 10 * ky)), (int(wx - x1 + 26 * kx), int(wy - y1 + 10 * ky)), 1, -1)
    # 3) lo demás: montón de oro, frascos sueltos sin número (hay uno solo) y objetos
    solid = ((mx > 90) & (taken == 0)).astype(np.uint8)
    solid = cv2.morphologyEx(solid, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    n, lab, st, cen = cv2.connectedComponentsWithStats(solid, connectivity=8)
    for i in sorted(range(1, n), key=lambda i: -st[i, 4]):
        a, w_, h_ = int(st[i, 4]), int(st[i, 2]), int(st[i, 3])
        if a < 70 * kx * ky or h_ < 10 * ky:
            continue
        comp = lab == i
        col = _colors(roi[comp].astype(int))
        gxy = out.get("gold_xy")
        by_num = gxy and -60 * kx < (x1 + cen[i][0]) - gxy[0] < 8 * kx and -50 * ky < (y1 + cen[i][1]) - gxy[1] < 4 * ky and w_ <= 40 * kx
        # el montón de oro es el que está pegado al número de oro; otra cosa dorada (el pan: Baguette) es un objeto
        if by_num or (col["oro"] > 0.25 * a and not gxy and not out.get("gold_pile")):
            out["gold_pile"] = (x1 + int(cen[i][0]), y1 + int(cen[i][1]))
            continue
        kind = max(("vida", "maná", "energía"), key=lambda k: col[k])
        if col[kind] > 0.3 * a and w_ <= 30 * kx and h_ <= 34 * ky:
            if any(abs(cen[i][0] - c[0]) < 16 * kx and abs(cen[i][1] - c[1]) < 18 * ky for c in centers):
                continue  # resto de un frasco ya contado
            out["pots"][kind] += 1
            centers.append((int(cen[i][0]), int(cen[i][1])))
            out.setdefault("pot_kinds", []).append(kind)
            continue
        if any(abs(cen[i][0] - c[0]) < 12 * kx and abs(cen[i][1] - c[1]) < 12 * ky for c in centers) and a < 220 * kx * ky:
            continue
        # punto para hacerle click: el más metido adentro del objeto (lejos de frascos encimados y de los bordes)
        dt = cv2.distanceTransform(comp.astype(np.uint8), cv2.DIST_L2, 3)
        py, px_ = np.unravel_index(int(dt.argmax()), dt.shape)
        out["items"].append({"x": x1 + int(px_), "y": y1 + int(py), "area": a, "w": w_, "h": h_,
                             "x0": x1 + int(st[i, 0]), "y0": y1 + int(st[i, 1]),
                             "cx": x1 + int(cen[i][0]), "cy": y1 + int(cen[i][1])})
    out["pot_xy"] = [(x1 + c[0], y1 + c[1]) for c in centers]
    return out


def crowded(sc, kx=1.0, ky=1.0):
    """Objetos encimados con otra cosa (un frasco o el oro arriba, o dos objetos pegados): hay que separarlos
    para poder hacerles doble click. Devuelve la lista de objetos a mover."""
    out = []
    things = list(sc.get("pot_xy") or []) + ([sc["gold_pile"]] if sc.get("gold_pile") else [])
    for it in sc["items"]:
        m = 8 * kx
        hit = any(it["x0"] - m <= t[0] <= it["x0"] + it["w"] + m and it["y0"] - m <= t[1] <= it["y0"] + it["h"] + m for t in things)
        if hit or it["w"] > 56 * kx or it["h"] > 60 * ky:
            out.append(it)
    return out


def free_spot(sc, kx=1.0, ky=1.0):
    """Un lugar vacío del bolso, bien adentro (nunca cerca del borde: soltar afuera sería tirar el objeto)."""
    bx1, by1, bx2, by2 = sc["box"]
    occ = list(sc.get("pot_xy") or []) + ([sc["gold_pile"]] if sc.get("gold_pile") else [])
    for it in sc["items"]:
        occ += [(it["cx"], it["cy"]), (it["x0"], it["y0"]), (it["x0"] + it["w"], it["y0"] + it["h"])]
    ys = range(int(by2 - 70 * ky), int(by1 + 110 * ky), -int(70 * ky))
    xs = range(int(bx1 + 90 * kx), int(bx2 - 90 * kx), int(75 * kx))
    for y in ys:
        for x in xs:
            if all(abs(x - o[0]) > 60 * kx or abs(y - o[1]) > 60 * ky for o in occ):
                return x, y
    return None
