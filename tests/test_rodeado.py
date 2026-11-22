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

# 2) barrido 3x3 con el mouse: pega solo donde aparece la barra, sigue con el mismo y pasa al siguiente
def setup(vivos):
    x = mk_bot("manual")
    x.rect = (0, 0, 1919, 1080)
    x.cfg.update({"sweep_hover_s": 0.0})
    x._char = lambda: (959, 560)
    x._tile = lambda: (64, 64)
    x.abs_pt = lambda a, b_: (a, b_)
    x.pos = {"map": "Aresden City", "x": 50, "y": 170, "t": time.time()}
    clicks, cursor = [], [None]
    def tile_of(px_, py_):
        return (round((px_ - 959) / 64), round((py_ + 64 * 0.3 - 560) / 64))
    x._bar_read = lambda px_, py_: 1.0 if tile_of(px_, py_) in vivos else None
    winapi.move = lambda a, b_: cursor.__setitem__(0, (a, b_))
    winapi.click = lambda a, b_, button="left", **k: clicks.append((tile_of(a, b_), button))
    return x, clicks

vivos = {(1, 0), (-1, 1)}
x, clicks = setup(vivos)
t.ok(x._sweep_step(100.0, ents=[]), "rodeado: encuentra a uno con el mouse y le pega")
t.ok(clicks[-1][0] in vivos and clicks[-1][1] == "right", "le pega con clic derecho (sin caminar) donde hay bicho")
primero = clicks[-1][0]
x._sweep_step(100.3, ents=[])
t.ok(len(clicks) == 1, "no repite el golpe antes de tiempo (0,9 s)")
x._sweep_step(101.0, ents=[])
t.ok(clicks[-1][0] == primero, "mientras su barra esté, sigue con el MISMO bicho")
vivos.discard(primero)
x._sweep_step(102.0, ents=[])
t.ok(clicks[-1][0] in vivos, "muerto ese, pasa al siguiente de alrededor")
vivos.clear()
t.ok(not x._sweep_step(103.0, ents=[]), "alrededor no queda nadie: vuelve a cazar de a uno")
t.ok(x._sweep_draw is None, "sin nadie alrededor no dibuja el cuadrado grande")

# primero donde la memoria ve bichos
vivos2 = {(0, 1), (-1, -1)}
y, clicks2 = setup(vivos2)
y._sweep_step(200.0, ents=[(49, 169, {"name": "Orc"}, 1)])
t.ok(clicks2[-1][0] == (-1, -1), "empieza por donde la memoria dice que hay un bicho")

# 3) dibujo: cuadrado grande rojo + el de adentro
frame = np.zeros((1080, 1919, 3), np.uint8)
ov = vision.draw_overlay(frame, {"calib": {}}, sweep={"t": time.time(), "cx": 959, "cy": 541, "tw": 64, "th": 64, "cur": (1, 0)})
red = (ov[:, :, 2] > 200) & (ov[:, :, 1] < 50) & (ov[:, :, 0] < 50)
t.ok(red[541 - 96, 959].any() or red[541 - 96 - 1:541 - 96 + 2, 959].any(), "dibuja el cuadrado rojo grande de 3x3")
t.ok(red[541, 959 + 64 - 28:959 + 64 + 28].any(), "dibuja el cuadrado de adentro en el bicho al que le pega")
t.end()
