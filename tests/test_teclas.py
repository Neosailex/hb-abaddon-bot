"""Teclas del bot sin choque con las del juego (Insert=poción roja, Home=ataque seguro, End=chat)."""
import json
import os
import tempfile
from _base import cfgmod, T

t = T("teclas")
CHOQUE = {"INSERT", "DELETE", "HOME", "END", "PAGEUP", "1", "2", "3", "4", "5", "TAB"}
hk = cfgmod.DEFAULTS["hotkeys"]
t.ok(not (set(v for v in hk.values() if v) & CHOQUE), "las teclas de fábrica no chocan con el juego")
t.ok(hk["toggle"] == "CTRL+ALT+B" and hk["panic_stop"] == "CTRL+ALT+X", "Ctrl+Alt+B prende/apaga, Ctrl+Alt+X frena")
from _base import E
t.ok(E.hotkey_vks("CTRL+ALT+B") == [0x11, 0x12, 0x42], "Ctrl+Alt+B se entiende como combinación")
t.ok(E.hotkey_vks("INSERT") == [0x2D] and E.hotkey_vks("") is None and E.hotkey_vks("CTRL+ÑÑ") is None, "teclas solas, vacías y raras")
JUEGO_CTRL_ALT = {"CTRL+ALT+F", "CTRL+ALT+P"}
t.ok(not ({hk["toggle"], hk["panic_stop"]} & JUEGO_CTRL_ALT), "no pisa Ctrl+Alt+F ni Ctrl+Alt+P del juego")

d = tempfile.mkdtemp()
old_path = cfgmod.CONFIG_PATH
try:
    cfgmod.CONFIG_PATH = os.path.join(d, "config.json")
    for vieja, esperado in (({"toggle": "INSERT", "panic_stop": "END"}, "CTRL+ALT+B"),
                            ({"toggle": "F12", "panic_stop": "F11"}, "CTRL+ALT+B"),
                            ({"toggle": "PAUSE", "panic_stop": "SCROLLLOCK"}, "CTRL+ALT+B"),
                            ({"toggle": "CTRL+ALT+K", "panic_stop": "END"}, "CTRL+ALT+K")):
        json.dump({"hotkeys": vieja}, open(cfgmod.CONFIG_PATH, "w"))
        c = cfgmod.load()
        t.ok(c["hotkeys"]["toggle"] == esperado, f"config con {vieja['toggle']}/{vieja['panic_stop']} → prende con {esperado}")
finally:
    cfgmod.CONFIG_PATH = old_path
t.end()
