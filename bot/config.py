"""Configuración del bot: se guarda en data/config.json."""
import copy
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
CONFIG_PATH = os.path.join(DATA, "config.json")
TEMPLATES = os.path.join(DATA, "plantillas")

DEFAULTS = {
    "window_title": "Helbreath",          # parte del título de la ventana del juego
    "mode": "auto",                        # auto | templates | pit
    "background_mode": False,               # clicks directo a la ventana del juego (podés usar la PC)
    "focus_game": True,                     # trae el juego al frente al prender (después nunca roba el foco)
    # --- Calibración (coordenadas relativas al área de juego) ---
    "calib": {
        "hp_bar": None,     # {"x1","y1","x2","y2","color":[b,g,r]}
        "mp_bar": None,
        "sp_bar": None,
        "char_center": None,  # {"x","y"}
        "game_area": None,    # {"x1","y1","x2","y2"} zona donde buscar monstruos (sin HUD)
        "tile": {"w": 72, "h": 72},
        "hp_pot_slot": None,  # {"x","y"} si las pociones se usan con doble click en inventario
        "mp_pot_slot": None,
        "sp_pot_slot": None,
        "food_slot": None,
        "recall_slot": None,
        "exclude": [],       # zonas a ignorar (minimapa, etc.)
        "pos_box": None,     # texto "Mapa (x,y)" de la barra de abajo
        "char_button": None, "char_box": None, "stat_confirm": None,
        "plus_str": None, "plus_dex": None, "plus_vit": None, "plus_int": None, "plus_mag": None, "plus_chr": None,
    },
    "bar_tolerance": 45,
    # --- Pociones / supervivencia ---
    "hp_pot_pct": 55, "mp_pot_pct": 25, "sp_pot_pct": 20,
    "hp_pot_action": {"type": "slot", "key": "F1"},  # slot = doble click en el slot calibrado | key = tecla
    "mp_pot_action": {"type": "off", "key": "F2"},
    "sp_pot_action": {"type": "off", "key": "F3"},
    "pot_cooldown": 1.2,
    "panic_hp_pct": 35,
    "panic_action": "flee",  # flee (escapar a la ciudad y descansar) | recall_and_stop | stop | pots_only
    "rest_until_pct": 75,
    "flee_max_sec": 600,
    "recall_action": {"type": "off", "key": "F5"},
    # --- Comida y acciones con timer ---
    "timers": [
        {"name": "Comer", "every_min": 20, "action": {"type": "slot", "slot": "food_slot", "key": "F4"}, "enabled": False},
    ],
    # --- Combate ---
    "match_threshold": 0.72,
    "match_scale": 0.5,            # compara a media resolución (menos CPU)
    "attack_modifier": "",        # "" | CTRL | SHIFT (tecla que se mantiene al hacer click para atacar)
    "attack_hold": True,           # mantener click apretado sobre el monstruo
    "attack_check_every": 0.6,     # cada cuánto revisa si el monstruo sigue
    "max_target_dist_tiles": 9,
    "target_timeout": 25,          # si no muere en X seg, cambia de objetivo
    "pit_cycle_delay": 0.35,
    "pit_directions": ["N", "NE", "E", "SE", "S", "SW", "W", "NW"],
    "ignore_templates": [],
    # --- Modo automático ---
    "hover_confirm": True,          # pasa el mouse: si aparece un nombre, ataca
    "hover_min_pixels": 40,
    "enemy_min_pixels": 18,
    "hover_wait": 0.09,
    "scan_every": 1.2,
    "scan_rings": [1.0, 1.6, 2.3, 3.0],
    "motion_thresh": 22,
    "settle_time": 0.4,
    "debug": False,
    "self_radius_tiles": 0.8,
    "blob_min_tiles": 0.12,
    "blob_max_tiles": 4.0,
    "track_threshold": 0.5,
    # --- Loot ---
    "loot_enabled": True,
    "loot_delay": 2.0,             # en Abaddon el raro aparece cuando se desvanece el cuerpo
    "loot_radius_tiles": 3,
    "loot_max_clicks": 4,
    # --- Movimiento ---
    "roam_enabled": True,
    "roam_after": 6,                # seg sin objetivo -> camina
    "roam_radius_tiles": 5,
    "leash_tiles": 14,              # no se aleja más que esto del punto de inicio (aprox)
    # --- Stats automáticos ---
    "stats_auto": {"enabled": True, "every_min": 1},
    "memory": {"enabled": True, "pos": None},
    "autostart": True,
    # --- Viaje (etapa 2) ---
    "travel": {"enabled": False, "dest": "auto", "my_level": 1, "explore": True, "home": "aresden",
               "avoid": ["middleland", "promiseland", "icebound", "elvine", "elvine farm", "eldiniel garden", "rocky highland", "death valley", "elvine dungeon"]},
    "hunt_radius_tiles": 25,
    "explore_after_sec": 35,
    "view_tiles": [7, 5],
    "game_dir": "",
    # --- Auto-login ---
    "autologin": {"enabled": False, "account": "", "password_enc": "", "char_name": "", "relaunch": True},
    "login_field_offset": 140,
    # --- Eventos ---
    "pause_on_raid": False,
    # --- Hotkeys globales ---
    "hotkeys": {"toggle": "INSERT", "panic_stop": "END", "calib_point": "PAGEUP", "capture_template": "PAGEDOWN", "auto_calib": "HOME"},
    "template_size": 80,
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    global LAST_SAVE_MTIME
    os.makedirs(DATA, exist_ok=True)
    try:
        LAST_SAVE_MTIME = os.path.getmtime(CONFIG_PATH)
    except OSError:
        LAST_SAVE_MTIME = 1
    for sub in ("monsters", "items"):
        os.makedirs(os.path.join(TEMPLATES, sub), exist_ok=True)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = _merge(DEFAULTS, json.load(f))
            # F12 minimiza Helbreath: pasar las teclas viejas a las nuevas
            hk = cfg.setdefault("hotkeys", {})
            if str(hk.get("toggle", "")).startswith("F") or str(hk.get("calib_point", "")).startswith("F"):
                hk.update({"toggle": "INSERT", "panic_stop": "END", "auto_calib": "HOME",
                           "calib_point": "PAGEUP", "capture_template": "PAGEDOWN"})
            return cfg
        except Exception:
            pass
    return copy.deepcopy(DEFAULTS)


def save(cfg):
    os.makedirs(DATA, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CONFIG_PATH)
    global LAST_SAVE_MTIME
    try:
        LAST_SAVE_MTIME = os.path.getmtime(CONFIG_PATH)
    except OSError:
        pass


LAST_SAVE_MTIME = 0


def changed_outside():
    """True si alguien editó config.json desde afuera (para recargarla sin reiniciar el bot)."""
    try:
        m = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return False
    return LAST_SAVE_MTIME and m > LAST_SAVE_MTIME + 0.5
