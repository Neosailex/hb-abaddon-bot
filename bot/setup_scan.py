"""Puesta a punto: el bot abre una por una las ventanas del juego, saca foto, lee sus textos y las vuelve a
cerrar con click derecho. Con eso se arma la guía de ventanas de un cliente nuevo (dónde está cada botón,
cómo se suman stats, cómo se abre y cierra cada cosa). No toca nada que gaste o cambie algo:
no aprieta '+' de stats, ni Aceptar, Comprar, Vender, Borrar, etc."""
import json
import os
import time
import zipfile

import cv2
import numpy as np

from . import ocr, winapi

# palabras de botones que NO se tocan nunca (pueden gastar, borrar, confirmar o desconectar)
DENY = ("borrar", "eliminar", "delete", "salir", "exit", "logout", "desconect", "vender", "sell", "comprar", "buy",
        "aceptar", "accept", "confirmar", "confirm", "ok", "si", "sí", "yes", "tirar", "drop", "reiniciar", "restart",
        "subir", "upgrade", "mejorar", "craft", "fabricar", "manufact", "cambiar", "change", "password", "contraseña",
        "clan", "guild", "grupo", "party", "invit", "expuls", "kick", "abandon", "retir", "reset", "aplicar", "apply",
        "guardar", "save", "enviar", "send", "trade", "comerci", "intercamb", "+", "-", "x")


def _norm(t):
    return "".join(c for c in (t or "").lower() if c.isalnum())


def _changed_box(base, img, thr=40, min_area=4000):
    """Rectángulo de lo que cambió entre dos cuadros (la ventana que se abrió). None si casi nada cambió."""
    if base is None or img is None or base.shape != img.shape:
        return None
    d = cv2.absdiff(cv2.cvtColor(base, cv2.COLOR_BGR2GRAY), cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    _, m = cv2.threshold(d, thr, 255, cv2.THRESH_BINARY)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    n, _, st, _ = cv2.connectedComponentsWithStats(m)
    boxes = [st[i] for i in range(1, n) if st[i][4] >= min_area]
    if not boxes:
        return None
    b = max(boxes, key=lambda s: s[4])
    return [int(b[0]), int(b[1]), int(b[0] + b[2]), int(b[1] + b[3])]


class SetupScan:
    def __init__(self, bot, out_dir):
        self.bot = bot
        self.out = out_dir
        self.report = {"t": time.strftime("%Y-%m-%d %H:%M"), "windows": []}
        self.n = 0

    # -- utilidades
    def grab(self):
        time.sleep(0.15)
        return self.bot.screen.grab(self.bot.rect)

    def click(self, x, y, button="left"):
        winapi.click(*self.bot.abs_pt(x, y), button=button)

    def words(self, img, box=None):
        H, W = img.shape[:2]
        b = box or [0, 0, W, H]
        try:
            return ocr.read_words(img, {"x1": b[0], "y1": b[1], "x2": b[2], "y2": b[3]}, scale=1.0)
        except Exception:
            return []

    def save(self, name, img):
        self.n += 1
        fn = f"{self.n:02d}_{name}.png"
        cv2.imwrite(os.path.join(self.out, fn), img)
        return fn

    def close(self, box, base):
        """Cierro con click derecho sobre la ventana; si no se cierra, ESC."""
        if box:
            self.click((box[0] + box[2]) // 2, box[1] + 20, button="right")
            time.sleep(0.6)
            img = self.grab()
            if _changed_box(base, img) is None:
                return "click derecho"
        winapi.press("ESC")
        time.sleep(0.6)
        img = self.grab()
        return "ESC" if _changed_box(base, img) is None else "no se cerró"

    def probe(self, name, how, do_open, base, depth=1):
        """Abre algo, saca foto, lee textos, explora sus botones (un nivel) y lo cierra."""
        do_open()
        time.sleep(1.0)
        img = self.grab()
        box = _changed_box(base, img)
        rec = {"name": name, "open": how, "box": box, "img": self.save(name, img)}
        if not box:
            rec["result"] = "no se abrió nada"
            self.report["windows"].append(rec)
            return rec
        ws = self.words(img, box)
        rec["words"] = [[w[0], int(w[1]), int(w[2])] for w in ws]
        rec["buttons"] = []
        if depth > 0:
            seen = set()
            for w in ws:
                t = _norm(w[0])
                if len(t) < 4 or t in seen or any(t.startswith(_norm(d)) for d in DENY if len(_norm(d)) >= 2):
                    continue
                seen.add(t)
                if len(rec["buttons"]) >= 10:
                    break
                before = self.grab()
                self.click(w[1], w[2])
                time.sleep(0.9)
                after = self.grab()
                sub = _changed_box(before, after)
                b = {"label": w[0], "at": [int(w[1]), int(w[2])], "changed": sub}
                if sub:
                    b["img"] = self.save(f"{name}_{t[:12]}", after)
                    b["words"] = [[x[0], int(x[1]), int(x[2])] for x in self.words(after, sub)][:60]
                    # cierro lo que abrió el botón (sin cerrar la ventana madre)
                    self.click((sub[0] + sub[2]) // 2, sub[1] + 20, button="right")
                    time.sleep(0.6)
                    if _changed_box(before, self.grab()) is not None:
                        winapi.press("ESC")
                        time.sleep(0.5)
                rec["buttons"].append(b)
        rec["close"] = self.close(box, base)
        self.report["windows"].append(rec)
        return rec

    def run(self, log):
        bot = self.bot
        os.makedirs(self.out, exist_ok=True)
        base = self.grab()
        if base is None:
            return None
        H, W = base.shape[:2]
        self.save("base", base)
        allw = self.words(base)
        self.report["screen_words"] = [[w[0], int(w[1]), int(w[2])] for w in allw]
        # 1) textos-botón que ya se ven (ej. "Subir de Nivel")
        for w in allw:
            t = _norm(w[0])
            if t.startswith("subir") or t.startswith("levelup") or t == "nivel":
                pt = (w[1], w[2])
                log("Puesta a punto: abro la ventana de subir de nivel.")
                rec = self.probe("subir_nivel", f"click en '{w[0]}'", lambda: self.click(*pt), base, depth=0)
                rec["note"] = "No toco los '+': solo miro dónde están."
                base = self.grab()
                break
        # 2) los íconos de la barra de abajo (de a uno)
        y = int(self.bot.cfg["calib"].get("bar_icons_y", 1034))
        xs = self.bot.cfg["calib"].get("bar_icons_x") or [1074, 1156, 1224, 1290, 1358, 1426, 1492]
        for i, x in enumerate(xs):
            log(f"Puesta a punto: ícono {i + 1} de {len(xs)} de la barra.")
            self.probe(f"icono{i + 1}", f"click en ({x},{y})", lambda x=x: self.click(x, y), base)
            base = self.grab()
        # 3) teclas clásicas de ventanas (solo las que en Helbreath abren ventanas, no usan cosas)
        for k in ("F5", "F6", "F7", "F8", "F9"):  # F1-F3 y F10-F12 son atajos de ítems/magias: no
            log(f"Puesta a punto: pruebo la tecla {k}.")
            self.probe(f"tecla_{k}", f"tecla {k}", lambda k=k: winapi.press(k), base)
            base = self.grab()
        with open(os.path.join(self.out, "reporte.json"), "w", encoding="utf-8") as f:
            json.dump(self.report, f, ensure_ascii=False, indent=1)
        zp = self.out + ".zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for fn in os.listdir(self.out):
                z.write(os.path.join(self.out, fn), fn)
        return zp
