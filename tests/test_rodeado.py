"""Rodeado (idea de Juan): cuadrado rojo grande 3x3 desde el pj, mouse para confirmar cada bicho, y la barra de vida
que el juego muestra al pasar el mouse (foto real del 22/11 con 'Orco' y su barra)."""
import os
import time
import cv2
import numpy as np
from _base import mk_bot, E, T, winapi
from bot import vision

t = T("rodeado")
FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "rodeado_orcos.jpg")
img = cv2.imread(FX) if os.path.isfile(FX) else None  # la foto real vive solo en la PC (no se sube al repo)

# 1) la barra de vida del bicho bajo el cursor se lee en la foto real
if img is not None:
    b = mk_bot("manual")
    b.rect = (0, 0, img.shape[1], img.shape[0])
    b.screen = type("S", (), {"grab": lambda self, r: img})()
    t.ok(b._bar_read(1000, 500) is not None, "foto real: con el mouse sobre el orco veo su barra de vida")
    t.ok(b._bar_read(600, 250) is None, "foto real: en el pasto vacío no hay barra")

# 2) cuadrado grande (regla de Juan): entra rodeado; no sale hasta que quede 1 solo alrededor; si se suman, sigue
def setup(vivos):
    x = mk_bot("manual")
    x.rect = (0, 0, 1919, 1080)
    x.cfg.update({"sweep_hover_s": 0.0})
    x._char = lambda: (959, 560)
    x._tile = lambda: (64, 64)
    x.abs_pt = lambda a, b_: (a, b_)
    x.pos = {"map": "Aresden City", "x": 50, "y": 170, "t": time.time()}
    clicks = []
    def tile_of(px_, py_):
        return (round((px_ - 959) / 64), round((py_ + 64 * 0.3 - 560) / 64))
    x._bar_read = lambda px_, py_: 1.0 if tile_of(px_, py_) in vivos else None
    winapi.move = lambda a, b_: None
    winapi.click = lambda a, b_, button="left", **k: clicks.append((tile_of(a, b_), button))
    return x, clicks

RING = [(0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1)]
vivos = set(RING)  # rodeado por 8, como en la foto de Juan
x, clicks = setup(vivos)
t.ok(not x._sweep_step(100.0, ents=[]), "sin entrar, el cuadrado grande no hace nada")
t.ok(not x._sweep_on(100.0, 2), "con 2 pegados no entra")
t.ok(x._sweep_on(100.0, 8), "rodeado por 8: entra al cuadrado grande")
tt = 100.0
muertos = 0
while len(vivos) > 1 and tt < 200:
    x._sweep_step(tt, ents=[])
    tt += 1.0
    if clicks and clicks[-1][0] in vivos:
        vivos.discard(clicks[-1][0])      # ese murió
        x.stats["kills"] += 1
        muertos += 1
    t_ok = x._sweep_state()["active"]
    if len(vivos) >= 2 and not t_ok:
        break
t.ok(muertos == 7 and len(vivos) == 1, f"mata 7 de 8 sin salir (mató {muertos})")
t.ok(all(c[1] == "right" for c in clicks), "siempre clic derecho (pega sin caminar)")
t.ok(not x._sweep_step(tt, ents=[]), "queda 1 solo alrededor: sale al cuadrado chico")
t.ok(not x._sweep_state()["active"] and x._sweep_draw is None, "sin cuadrado grande dibujado")
t.ok(not x._sweep_on(tt + 1, 3), "recién salido no vuelve a entrar enseguida (no rebota)")
t.ok(x._sweep_on(tt + 4, 3), "pasados unos segundos, si lo vuelven a rodear, entra")

# se suman bichos mientras pelea: sigue
vivos = {(0, -1), (1, 0), (0, 1)}
y, clicks2 = setup(vivos)
y._sweep_on(300.0, 3)
y._sweep_step(300.0, ents=[]); vivos.discard(clicks2[-1][0]); y.stats["kills"] += 1
vivos |= {(-1, 0), (-1, -1), (1, 1)}   # llegan 3 más
seguia = True
tt = 301.0
while len(vivos) > 1 and tt < 400:
    if not y._sweep_step(tt, ents=[]):
        seguia = False
        break
    if clicks2[-1][0] in vivos:
        vivos.discard(clicks2[-1][0]); y.stats["kills"] += 1
    tt += 1.0
t.ok(seguia and len(vivos) == 1, "se suman bichos: sigue hasta que vuelve a quedar 1")

# anti-loop: pega y no muere nadie → a los 15 s sale y no vuelve enseguida
vivos = {(0, -1), (1, 0), (0, 1)}
z, clicks3 = setup(vivos)
z._sweep_on(500.0, 3)
t.ok(z._sweep_step(505.0, ents=[]), "pegando a uno que no muere (5 s): sigue")
t.ok(not z._sweep_step(516.0, ents=[]), "15 s sin matar a nadie: sale (no queda en loop)")
t.ok(not z._sweep_on(520.0, 3), "después de trabarse espera más antes de volver a entrar")
t.ok(z._sweep_on(525.0, 3), "y después sí puede volver a entrar")

# primero donde la memoria ve bichos
vivos2 = {(0, 1), (-1, -1), (1, 1)}
w, clicks4 = setup(vivos2)
w._sweep_on(600.0, 3)
w._sweep_step(600.0, ents=[(49, 169, {"name": "Orc"}, 1)])
t.ok(clicks4[-1][0] == (-1, -1), "empieza por donde la memoria dice que hay un bicho")

# 3) dibujo: cuadrado grande rojo + el de adentro
frame = np.zeros((1080, 1919, 3), np.uint8)
ov = vision.draw_overlay(frame, {"calib": {}}, sweep={"t": time.time(), "cx": 959, "cy": 541, "tw": 64, "th": 64, "cur": (1, 0)})
red = (ov[:, :, 2] > 200) & (ov[:, :, 1] < 50) & (ov[:, :, 0] < 50)
t.ok(red[541 - 96, 959].any() or red[541 - 96 - 1:541 - 96 + 2, 959].any(), "dibuja el cuadrado rojo grande de 3x3")
t.ok(red[541, 959 + 64 - 28:959 + 64 + 28].any(), "dibuja el cuadrado de adentro en el bicho al que le pega")
t.end()
