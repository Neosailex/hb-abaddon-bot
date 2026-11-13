"""Motor del bot: loop de captura, supervivencia, combate, loot y movimiento."""
import json
import math
import os
import random
import re
import threading
import time
from collections import deque

from . import config as cfgmod
from . import events as evmod
from . import memory, ocr, secret, vision, winapi
from . import world as worldmod
import difflib
import numpy as np

VK = {f"F{i}": 0x6F + i for i in range(1, 13)}
VK.update({"INSERT": 0x2D, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22, "PAUSE": 0x13, "SCROLLLOCK": 0x91})
DIRS = {"N": (0, -1), "NE": (1, -1), "E": (1, 0), "SE": (1, 1), "S": (0, 1), "SW": (-1, 1), "W": (-1, 0), "NW": (-1, -1)}
POINT_SLOTS = {"char_center", "hp_pot_slot", "mp_pot_slot", "sp_pot_slot", "food_slot", "recall_slot",
               "char_button", "plus_str", "plus_dex", "plus_vit", "plus_int", "plus_mag", "plus_chr", "stat_confirm"}
TWO_POINT_SLOTS = {"hp_bar", "mp_bar", "sp_bar", "game_area", "pos_box", "char_box"}


class Bot:
    def __init__(self):
        self.cfg = cfgmod.load()
        self.db = self._load_json("hb_db.json")
        self.templates = vision.Templates(cfgmod.TEMPLATES)
        self.screen = vision.Screen()
        self.lock = threading.RLock()
        self.running = False
        self.alive = True
        self.hwnd = None
        self.win_title = None
        self.rect = None
        self.img = None
        self.bars = {"hp": None, "mp": None, "sp": None}
        self.log_lines = deque(maxlen=250)
        self.preview = ""
        self.preview_on = True
        self.frame_id = 0
        self.calib = {"slot": None, "points": [], "msg": ""}
        self.progress = {"exp": None, "rate": None, "eta_min": None, "levelups": 0}
        self.capture = {"kind": "monsters", "name": "monstruo"}
        gd = worldmod.find_game_dir(self.cfg.get("game_dir"))
        self.world = worldmod.World(gd, os.path.join(cfgmod.DATA, "mundo.json"))
        self.pos = {"map": None, "x": None, "y": None, "t": 0, "text": ""}
        mp = (self.cfg.get("memory") or {}).get("pos")
        self.mem = memory.PosFinder(self.log, saved=mp)
        self._mem_hold = False
        self._last_ocr_xy = None
        self.ents = memory.Entities(self.log)
        self._ent_bl = {}
        self._last_ocr = 0
        self.nav = {"status": "", "dest": None, "last_click": 0, "last_pos": None, "still_since": 0, "target_tile": None}
        self._reset_session()
        self._logfile = os.path.join(cfgmod.DATA, "bot.log")
        self.threads = [threading.Thread(target=self._loop, daemon=True),
                        threading.Thread(target=self._hotkeys, daemon=True),
                        threading.Thread(target=self._preview_loop, daemon=True),
                        threading.Thread(target=self._control_loop, daemon=True)]
        self.on_restart = None
        for t in self.threads:
            t.start()
        hk = self.cfg.get("hotkeys", {})
        self.log(f"Bot listo. {hk.get('toggle')} prende/apaga, {hk.get('panic_stop')} frena todo, {hk.get('auto_calib')} autocalibra.")
        if winapi.IS_WIN and not winapi.is_admin():
            self.log("Tip: si el juego no responde a los clicks, abrí el bot como administrador.")

    # ------------------------------------------------------------------ util
    def _load_json(self, name):
        try:
            with open(os.path.join(cfgmod.DATA, name), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _reset_session(self):
        self.stats = {"kills": 0, "hp_pots": 0, "mp_pots": 0, "sp_pots": 0, "loot_clicks": 0, "started": None,
                      "runtime": 0, "state": "apagado", "target": "", "roams": 0}
        self.state = "seek"
        self.target = None
        self.target_since = 0
        self.last_seen_target = 0
        self.last_attack_click = 0
        self.mouse_held = False
        self.mod_held = None
        self.last_target_found = time.time()
        self.loot_at = None
        self.loot_pos = None
        self.blacklist = []  # [(x, y, hasta)]
        self.disp = [0.0, 0.0]  # desplazamiento aproximado en tiles desde el inicio
        self.pit_i = 0
        self.last_pot = {"hp": 0, "mp": 0, "sp": 0}
        self.timer_last = {}
        self.panic_count = 0
        self._last_mons = []
        self._prev_small = None
        self._prev_t = 0
        self._last_move = time.time()
        self._track = None       # recorte del monstruo que estoy atacando
        self._hover_fail = 0
        self._name_base = 0
        self._seen_alive = False

    def log(self, msg):
        line = time.strftime("%H:%M:%S ") + msg
        self.log_lines.append(line)
        try:
            with open(self._logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def save_config(self, new_cfg):
        with self.lock:
            self.cfg = cfgmod._merge(cfgmod.DEFAULTS, new_cfg)
            cfgmod.save(self.cfg)
        self.log("Configuración guardada.")

    # ----------------------------------------------------------- on / off
    def start(self):
        with self.lock:
            if self.running:
                return
            if not self._ensure_window():
                self.log(f"No encuentro la ventana del juego ('{self.cfg['window_title']}'). Abrí Helbreath o corregí el título en Config.")
                return
            c = self.cfg["calib"]
            if not c.get("char_center"):
                self.log("Falta calibrar el centro del personaje (pestaña Calibración).")
                return
            if self.cfg["mode"] == "templates" and not self.templates.items["monsters"]:
                self.log("Modo plantillas sin monstruos capturados: capturá alguno con Av Pág o usá el modo Pit.")
                return
            if not c.get("hp_bar"):
                self.log("Ojo: sin barra de HP calibrada no tomo pociones ni hago pánico.")
            self._reset_session()
            self.stats["started"] = time.time()
            self.running = True
            self.stats["state"] = "buscando"
            bg = bool(self.cfg.get("background_mode"))
            winapi.set_background(self.hwnd if bg else None)
            self.screen.use_printwindow = bg
            self.log("▶ Bot prendido." + (" (segundo plano: podés usar la PC)" if bg else ""))
            if self.cfg.get("focus_game") and not bg:
                winapi.focus(self.hwnd)

    def stop(self, reason="manual"):
        if reason in ("se perdió la ventana del juego",) and self.running:
            self._resume_when_game = True
        with self.lock:
            was = self.running
            self.running = False
            self._release()
            winapi.set_background(None)
            self.stats["state"] = "apagado"
            self.stats["target"] = ""
            if was:
                self.log(f"■ Bot apagado ({reason}).")

    def toggle(self):
        if self.running:
            self.stop("manual")
        else:
            self.start()

    # ------------------------------------------------- control remoto (consola)
    CODE_DIRS = ("bot", "bot/assets", "ui")

    def _code_mtimes(self):
        out = {}
        for d in self.CODE_DIRS:
            p = os.path.join(cfgmod.BASE, d)
            try:
                for f in os.listdir(p):
                    fp = os.path.join(p, f)
                    if os.path.isfile(fp) and f.endswith((".py", ".html", ".png", ".js", ".css")):
                        out[fp] = os.path.getmtime(fp)
            except OSError:
                pass
        fp = os.path.join(cfgmod.BASE, "app.py")
        if os.path.exists(fp):
            out[fp] = os.path.getmtime(fp)
        return out

    def _control_loop(self):
        """Funciona solo, sin tocar la PC:
        - arranca a cazar apenas aparece el juego (y retoma si se reinició),
        - si se actualiza el código, se reinicia solo y sigue donde estaba,
        - lee órdenes de data/control.json (start, stop, calibrate, restart, diag, shot)."""
        import json
        base = self._code_mtimes()
        changed_at = 0
        resume_p = os.path.join(cfgmod.DATA, "resume.json")
        ctl_p = os.path.join(cfgmod.DATA, "control.json")
        done_p = os.path.join(cfgmod.DATA, "control_done.json")
        want_start = bool(self.cfg.get("autostart", True))
        try:
            with open(resume_p, encoding="utf-8") as f:
                r = json.load(f)
            os.remove(resume_p)
            want_start = want_start or bool(r.get("running"))
        except Exception:
            pass
        last_id = None
        try:
            with open(ctl_p, encoding="utf-8") as f:
                last_id = json.load(f).get("id")  # no repetir la última orden vieja
        except Exception:
            pass
        t_launch = time.time()
        while self.alive:
            time.sleep(1.0)
            try:
                # 1) arrancar solo cuando aparece el juego
                if getattr(self, "_resume_when_game", False) and not self.running:
                    want_start = True
                    self._resume_when_game = False
                if want_start and not self.running and time.time() - t_launch > 3:
                    if self._ensure_window():
                        self.log("Arranco solo (modo consola).")
                        self.start()
                        if self.running:
                            want_start = False
                # 2) órdenes remotas
                try:
                    with open(ctl_p, encoding="utf-8") as f:
                        cmd = json.load(f)
                except Exception:
                    cmd = None
                if cmd and cmd.get("id") != last_id:
                    last_id = cmd.get("id")
                    res = self._run_cmd(cmd.get("cmd", ""), cmd)
                    with open(done_p, "w", encoding="utf-8") as f:
                        json.dump({"id": last_id, "cmd": cmd.get("cmd"), "result": res, "t": time.strftime("%H:%M:%S")}, f, ensure_ascii=False, default=str)
                # 2b) cada tanto busco si hay una versión nueva en GitHub (si la hay, la bajo y el paso 3 me reinicia)
                if (self.cfg.get("auto_update", True) and time.time() - getattr(self, "_upd_t", t_launch - 1080) > 1200
                        and not getattr(self, "_busy_ui", False) and not getattr(self, "mission", None)):
                    self._upd_t = time.time()

                    def _upd():
                        try:
                            import importlib.util
                            sp = importlib.util.spec_from_file_location("actualizar", os.path.join(cfgmod.BASE, "actualizar.py"))
                            mod = importlib.util.module_from_spec(sp)
                            sp.loader.exec_module(mod)
                            quiet = lambda t: self.log("⬇ " + t) if not t.startswith("No pude") else None
                            mod.check_and_update(quiet)
                        except Exception:
                            pass
                    threading.Thread(target=_upd, daemon=True).start()
                # 3) código nuevo: me reinicio solo
                now = self._code_mtimes()
                if now != base:
                    if not changed_at:
                        changed_at = time.time()
                    elif time.time() - changed_at > 8 and not getattr(self, "_busy_ui", False):
                        self.log("Hay una versión nueva del bot: me reinicio solo.")
                        self.restart_app()
                        return
            except Exception as e:
                self.log(f"Control: {e!r}")

    def _run_cmd(self, c, cmd):
        c = (c or "").lower()
        if c == "start":
            self.start()
            return {"running": self.running}
        if c == "stop":
            self.stop("orden remota")
            return {"running": self.running}
        if c == "calibrate":
            return self.auto_calibrate()
        if c == "restart":
            threading.Thread(target=lambda: (time.sleep(0.5), self.restart_app()), daemon=True).start()
            return "reiniciando"
        if c == "diag":
            return self.mem_diag()
        if c == "shot":
            import cv2
            img = self.screen.grab(self.rect) if self._ensure_window() else None
            if img is not None:
                d = os.path.join(cfgmod.DATA, "debug")
                os.makedirs(d, exist_ok=True)
                cv2.imwrite(os.path.join(d, "shot.png"), img)
                return "debug/shot.png"
            return "sin juego"
        if c in ("scan", "peek", "regions", "memstatus"):
            return self._mem_cmd(c, cmd)
        if c == "py":
            # análisis a medida (lo manda Claude desde la conversación de Juan)
            import numpy as np
            env = {"bot": self, "memory": memory, "np": np, "os": os, "time": time, "vision": vision,
                   "proc": self.mem.proc, "DATA": cfgmod.DATA}
            exec(cmd.get("code", ""), env)
            return env.get("result")
        if c == "set":
            with self.lock:
                for k, v in (cmd.get("values") or {}).items():
                    self.cfg[k] = v
                cfgmod.save(self.cfg)
            return "ok"
        return f"orden desconocida: {c}"

    def _mem_cmd(self, c, cmd):
        """Herramientas para investigar la memoria del juego a distancia (solo lectura)."""
        if not self._ensure_window() or not memory.IS_WIN:
            return "sin juego"
        p = self.mem.proc or memory.Proc(self.hwnd)
        d = os.path.join(cfgmod.DATA, "debug")
        os.makedirs(d, exist_ok=True)
        if c == "memstatus":
            return {"status": self.mem.status, "addr": list(self.mem.addr) if self.mem.addr else None, "error": self.mem.error,
                    "cands": len(self.mem.cands), "pos": [self.pos.get("map"), self.pos.get("x"), self.pos.get("y")],
                    "bars": self.bars, "pid": getattr(p, "pid", None)}
        if c == "regions":
            regs = p.regions()
            return {"n": len(regs), "mb": round(sum(r[1] for r in regs) / 2**20, 1), "regs": [[hex(a), n] for a, n in regs[:400]]}
        if c == "peek":
            out = {}
            for i, (addr, size) in enumerate(cmd.get("ranges") or []):
                addr = int(addr, 16) if isinstance(addr, str) else int(addr)
                data = p.read(addr, int(size)) or b""
                name = f"peek_{cmd.get('tag', 'x')}_{i}.bin"
                with open(os.path.join(d, name), "wb") as f:
                    f.write(data)
                out[name] = [hex(addr), len(data)]
            out["pos"] = [self.pos.get("map"), self.pos.get("x"), self.pos.get("y")]
            out["bars"] = self.bars
            return out
        if c == "scan":
            res = memory.scan_value(p, cmd.get("type", "i32"), cmd.get("value"), limit=int(cmd.get("limit", 3000)),
                                    within=cmd.get("within"))
            return {"n": len(res), "addrs": [hex(a) for a in res], "pos": [self.pos.get("map"), self.pos.get("x"), self.pos.get("y")]}

    def restart_app(self):
        import json
        try:
            with open(os.path.join(cfgmod.DATA, "resume.json"), "w", encoding="utf-8") as f:
                json.dump({"running": self.running}, f)
        except Exception:
            pass
        self.stop("reinicio")
        if self.on_restart:
            self.on_restart()

    def shutdown(self):
        self.stop("cerrando")
        self.alive = False

    # -------------------------------------------------------------- window
    def _ensure_window(self):
        if self.hwnd:
            r = winapi.client_rect(self.hwnd)
            if r:
                self.rect = r
                try:
                    self.screen.hwnd = self.hwnd
                except Exception:
                    pass
                return True
        hwnd, title = winapi.find_window(self.cfg.get("window_title"))
        self.hwnd, self.win_title = hwnd, title
        self.rect = winapi.client_rect(hwnd) if hwnd else None
        try:
            self.screen.hwnd = hwnd
        except Exception:
            pass
        return self.rect is not None

    def windows(self):
        return [t for _, t in winapi.list_windows()]

    def abs_pt(self, x, y):
        return (self.rect[0] + int(x), self.rect[1] + int(y))

    # ---------------------------------------------------------------- loops
    def _loop(self):
        last_preview = 0
        last_find = 0
        while self.alive:
            t0 = time.time()
            try:
                if time.time() - getattr(self, "_last_cfg_check", 0) > 2:
                    self._last_cfg_check = time.time()
                    if cfgmod.changed_outside():
                        with self.lock:
                            self.cfg = cfgmod.load()
                        self.log("Configuración recargada (cambió el archivo).")
                if time.time() - last_find > 2 or not self.rect:
                    self._ensure_window()
                    last_find = time.time()
                self.screen.use_printwindow = bool(self.cfg.get("background_mode"))
                idle = not self.running
                if idle and time.time() - getattr(self, "_last_grab", 0) < 1.0:
                    time.sleep(0.1)
                    continue
                self._last_grab = time.time()
                self.img = self.screen.grab(self.rect) if self.rect else None
                self._read_bars()
                self._mem_step()
                ocr_every = (2.5 if self.mem.learned else 0.8) if self.running else 2.5
                if (self.cfg["calib"].get("pos_box") and time.time() - self._last_ocr > ocr_every
                        and not getattr(self, "_ocr_busy", False)):
                    self._last_ocr = time.time()
                    self._ocr_busy = True
                    snap = self.mem.snapshot() if self._mem_on() else None
                    threading.Thread(target=self._read_pos_bg, args=(self.img, snap, self.mouse_held), daemon=True).start()
                if self.running:
                    if self._autologin_step():
                        pass
                    elif self.img is None:
                        self.stop("se perdió la ventana del juego")
                    else:
                        self._tick()
                        self.stats["runtime"] = int(time.time() - self.stats["started"])
            except Exception as e:  # nunca matar el loop
                self.log(f"Error: {e!r}")
                self._release()
                time.sleep(0.5)
            time.sleep(max(0.03, 0.12 - (time.time() - t0)))

    def _hotkeys(self):
        prev = {}
        while self.alive:
            hk = self.cfg.get("hotkeys", {})
            for action, key in hk.items():
                vk = VK.get(str(key).upper())
                if not vk:
                    continue
                down = winapi.key_pressed(vk)
                if down and not prev.get(action):
                    try:
                        self._on_hotkey(action)
                    except Exception as e:
                        self.log(f"Error hotkey: {e!r}")
                prev[action] = down
            time.sleep(0.03)

    def _on_hotkey(self, action):
        if action == "toggle":
            self.toggle()
        elif action == "panic_stop":
            self.stop("F11 freno de emergencia")
        elif action == "calib_point":
            self._calib_point()
        elif action == "capture_template":
            self._capture_template()
        elif action == "auto_calib":
            self.auto_calibrate()

    # ----------------------------------------------------------- calibration
    def calib_begin(self, slot):
        if slot not in POINT_SLOTS | TWO_POINT_SLOTS:
            return
        self.calib = {"slot": slot, "points": [], "msg": ""}
        if slot in TWO_POINT_SLOTS:
            first = ("el INICIO (izquierda) de la barra, con la barra llena" if slot.endswith("_bar")
                     else "la esquina de ARRIBA a la IZQUIERDA del texto 'Mapa (x,y)' de la barra de abajo" if slot == "pos_box"
                     else "la esquina de ARRIBA a la IZQUIERDA del área de juego (sin la barra de abajo)")
            self.calib["msg"] = f"Poné el mouse sobre {first} y apretá Re Pág (Page Up)."
        else:
            self.calib["msg"] = "Poné el mouse sobre el punto y apretá Re Pág (Page Up)."
        self.log(f"Calibrando {slot}: {self.calib['msg']}")

    def _calib_point(self):
        slot = self.calib.get("slot")
        if not slot:
            self.log("Re Pág: primero elegí qué calibrar en la pestaña Calibración.")
            return
        if not self._ensure_window():
            self.log("No encuentro la ventana del juego.")
            return
        cx, cy = winapi.cursor_pos()
        x, y = cx - self.rect[0], cy - self.rect[1]
        if not (0 <= x < self.rect[2] and 0 <= y < self.rect[3]):
            self.log("El mouse está fuera de la ventana del juego.")
            return
        c = self.cfg["calib"]
        if slot in POINT_SLOTS:
            c[slot] = {"x": x, "y": y}
            self._calib_done(slot)
            return
        pts = self.calib["points"]
        pts.append((x, y))
        if len(pts) == 1:
            if slot.endswith("_bar"):
                img = self.screen.grab(self.rect)
                self._bar_color = [int(v) for v in img[y, min(img.shape[1] - 1, x + 2)]] if img is not None else [0, 0, 255]
                self.calib["msg"] = "Ahora el FINAL (derecha) de la barra y Re Pág."
            else:
                self.calib["msg"] = "Ahora la esquina de ABAJO a la DERECHA y Re Pág."
            self.log(self.calib["msg"])
            return
        (x1, y1), (x2, y2) = pts[0], pts[1]
        if slot.endswith("_bar"):
            c[slot] = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "color": self._bar_color}
        else:
            c[slot] = {"x1": min(x1, x2), "y1": min(y1, y2), "x2": max(x1, x2), "y2": max(y1, y2)}
        self._calib_done(slot)

    def _calib_done(self, slot):
        cfgmod.save(self.cfg)
        self.calib = {"slot": None, "points": [], "msg": f"✔ {slot} calibrado."}
        self.log(f"✔ {slot} calibrado.")

    def set_tile(self, w, h):
        self.cfg["calib"]["tile"] = {"w": int(w), "h": int(h)}
        cfgmod.save(self.cfg)

    def set_capture(self, kind, name):
        self.capture = {"kind": "items" if kind == "items" else "monsters", "name": name or "plantilla"}
        self.log(f"Av Pág va a capturar: {self.capture['name']} ({'ítem' if self.capture['kind'] == 'items' else 'monstruo'}). Poné el mouse encima y apretá Av Pág (Page Down).")

    def _capture_template(self):
        if not self._ensure_window():
            self.log("No encuentro la ventana del juego.")
            return
        cx, cy = winapi.cursor_pos()
        x, y = cx - self.rect[0], cy - self.rect[1]
        size = int(self.cfg.get("template_size", 80))
        # el juego dibuja su propio cursor: lo corro afuera de la foto antes de sacarla
        away = (cx + size * 2, cy) if x + size * 2 < self.rect[2] else (cx - size * 2, cy)
        winapi.move(*away)
        time.sleep(0.12)
        img = self.screen.grab(self.rect)
        winapi.move(cx, cy)
        crop = vision.crop_around(img, x, y, size)
        if crop is None:
            self.log("No pude recortar ahí (¿mouse fuera del juego?).")
            return
        # control de calidad: si la foto aparece en muchos lados es pasto/piso, no un monstruo
        hits = vision.find(img, {"t": crop}, self.cfg["match_threshold"], self._game_roi(), scale=self.cfg.get("match_scale", 0.5))
        if len(hits) > 3:
            self.log(f"Foto descartada: aparece en {len(hits)} lugares (salió mucho piso). Apuntá justo al centro del monstruo.")
            return
        if float(crop.std()) < 12:
            self.log("Foto descartada: es casi todo del mismo color. Apuntá al monstruo.")
            return
        name = self.templates.save(self.capture["kind"], self.capture["name"], crop)
        self.log(f"✔ Plantilla guardada: {name}")

    # ------------------------------------------------------------- reading
    def _read_bars(self):
        c = self.cfg["calib"]
        tol = int(self.cfg.get("bar_tolerance", 45))
        for k in ("hp", "mp", "sp"):
            self.bars[k] = vision.bar_percent(self.img, c.get(f"{k}_bar"), tol) if self.img is not None else None
        eb = c.get("exp_bar")
        if eb and self.img is not None:
            import cv2
            y = int(eb["y1"])
            if 0 <= y < self.img.shape[0]:
                row = cv2.cvtColor(self.img[y:y + 1, int(eb["x1"]):int(eb["x2"])], cv2.COLOR_BGR2HSV)[0]
                g = (row[:, 0] > 40) & (row[:, 0] < 85) & (row[:, 1] > 120) & (row[:, 2] > 90)
                if g.any():
                    self._exp_update(round(100.0 * (np.where(g)[0].max() + 1) / len(g), 1))
        hp = self.bars.get("hp")
        if hp is not None:
            h = getattr(self, "_hp_hist", [])
            h.append((time.time(), hp))
            self._hp_hist = [x for x in h if time.time() - x[0] < 4]

    def _exp_update(self, pct):
        """Sigue la barra de experiencia: % actual, ritmo por hora, cuándo sube y detecta los level up."""
        now = time.time()
        p = self.progress
        last = p.get("exp")
        if last is not None and pct + 40 < last:  # la barra se vació: subió de nivel
            t = self.cfg.setdefault("travel", {})
            lm = self._level_mem()
            t["my_level"] = lm if lm else int(t.get("my_level", 1)) + 1
            cfgmod.save(self.cfg)
            self.log(f"⬆ ¡Subiste a nivel {t['my_level']}!")
            p["hist"] = []
            p["levelups"] = p.get("levelups", 0) + 1
            self._last_stats = 0  # repartir los puntos ya
        p["exp"] = pct
        h = p.setdefault("hist", [])
        if not h or now - h[-1][0] > 20:
            h.append((now, pct))
            del h[:-90]
        if len(h) >= 2 and h[-1][0] - h[0][0] > 60:
            rate = (h[-1][1] - h[0][1]) / ((h[-1][0] - h[0][0]) / 3600)
            p["rate"] = round(rate, 1)
            p["eta_min"] = int((100 - pct) / rate * 60) if rate > 0.1 else None

    def _game_roi(self):
        ga = self.cfg["calib"].get("game_area")
        if ga:
            return ga
        if self.img is None:
            return None
        h, w = self.img.shape[:2]
        return {"x1": 0, "y1": 0, "x2": w, "y2": int(h * 0.82)}

    def _char(self):
        cc = self.cfg["calib"].get("char_center")
        if cc:
            return cc["x"], cc["y"]
        h, w = self.img.shape[:2]
        return w // 2, int(h * 0.41)

    def _tile(self):
        t = self.cfg["calib"].get("tile") or {}
        return max(8, int(t.get("w", 32))), max(8, int(t.get("h", 32)))

    # ----------------------------------------------------------------- tick
    def _tick(self):
        if getattr(self, "_busy_ui", False):
            # otro hilo está usando ventanas del juego (comprando): no tocar nada
            self.stats["state"] = "comprando"
            return
        cfg = self.cfg
        if cfg.get("pause_on_raid") and evmod.raid_active(self.db.get("events", [])):
            if self.stats["state"] != "pausa (raid)":
                self._release()
                self.log("Raid en curso: pausa hasta que termine.")
            self.stats["state"] = "pausa (raid)"
            return
        if not winapi.is_foreground(self.hwnd) and not self.cfg.get("background_mode"):
            # no roba el foco: si mirás otra ventana (el bot, el navegador) se queda quieto
            if self.stats["state"] != "pausa (juego en 2º plano)":
                self._release()
                self.log("Juego en segundo plano: pausa hasta que vuelvas al juego.")
            self.stats["state"] = "pausa (juego en 2º plano)"
            self.last_target_found = time.time()
            return
        if self.stats["state"] == "pausa (juego en 2º plano)":
            self.stats["state"] = "buscando"
        if self._survival():
            return
        self._timers()
        sa = cfg.get("stats_auto", {})
        if sa.get("enabled") and time.time() - getattr(self, "_lvl_check", 0) > 8:
            self._lvl_check = time.time()
            lu_pts = self._lu_mem()
            mn_ = (self.pos.get("map") or "").lower()
            indoors = any(k in mn_ for k in ("shop", "blacksmith", "cityhall", "city hall", "warehouse", "guild", "church", "magic tower", "barrack", "jail", "command hall"))
            wait_ = 20 if getattr(self, "_stats_fail", 0) < 2 else 180
            if (not indoors and not getattr(self, "mission", None) and (lu_pts or vision.find_level_up(self.img))
                    and time.time() - getattr(self, "_stats_done", 0) > wait_):
                self._last_stats = 0
        if sa.get("enabled") and self.state != "attack" and getattr(self, "_last_stats", 1) == 0:
            self._last_stats = time.time()
            self._stats_done = time.time()
            self._do_stats()
            return
        if self._fleeing() and self.ents.center and self.mem.learned and self._flee_mem():
            return
        if self._fleeing():
            if not self._travel(force_dest=cfg.get("travel", {}).get("home", "aresden")):
                self._release()
                self.stats["state"] = "descansando"
            return
        self._maybe_shop()
        self._maybe_repair()
        self._brain_step()
        if self._mission_step():
            return
        mem_ok = (cfg["mode"] not in ("pit", "templates") and cfg.get("memory", {}).get("hunt", True)
                  and self.ents.center and self.mem.learned)
        mname0 = (self.pos.get("map") or "").lower()
        in_building = any(k in mname0 for k in ("shop", "blacksmith", "cityhall", "city hall", "warehouse", "guild", "cathedral", "church", "wizard", "magic tower", "barrack", "jail", "command hall"))
        if in_building and time.time() - getattr(self, "_park_t", 0) > 6:
            self._park_t = time.time()
            self._park_shop_dialog(self.img)
        if in_building and self.state != "seek":
            self._release()
            self.state, self.target = "seek", None
        # Si estoy yendo a algún lado (otro mapa), no me distraigo con cada bicho: solo peleo si me pegan.
        focused = self._is_travelling() and not self._attacked_recently()
        if focused and self.state != "seek":
            self._release()
            self.state, self.target = "seek", None
        if mem_ok and not in_building and not focused:
            if self.state in ("attack", "loot") or self._mem_near():
                self._auto()
                return
            if not self._fleeing() and self._goto_spot():
                return
        # mirar alrededor aunque esté viajando o explorando
        if (not mem_ok and not focused and self.state == "seek" and self.nav.get("walking") and self.cfg.get("hover_confirm", True)
                and time.time() - getattr(self, "_last_scan", 0) > self.cfg.get("scan_while_walking_every", 2.0)):
            self._last_scan = time.time()
            self._release()
            self.nav["walking"] = False
            time.sleep(0.25)
            self.img = self.screen.grab(self.rect)
            hit = self._scan_ring()
            if hit:
                self._engage(hit)
                return
        if self._travel():
            return
        mname = (self.pos.get("map") or "").lower()
        if any(k in mname for k in ("shop", "blacksmith", "cityhall", "city hall", "warehouse", "guild", "cathedral", "church", "wizard", "magic tower", "barrack", "jail", "command hall")):
            self.stats["state"] = "en un edificio"
            return
        if cfg["mode"] == "pit":
            self._pit()
        elif cfg["mode"] == "templates":
            self._hunt()
        else:
            self._auto()

    # ------------------------------------------------------------- survival
    def _do_action(self, action, slot_key=None):
        if not action or action.get("type") == "off":
            return False
        if action.get("type") == "key":
            self._release()
            winapi.press(action.get("key", "F1"))
            return True
        slot = self.cfg["calib"].get(action.get("slot") or slot_key)
        if not slot:
            return False
        self._release()
        x, y = self.abs_pt(slot["x"], slot["y"])
        winapi.click(x, y, double=True)
        return True

    def _fleeing(self):
        return time.time() < getattr(self, "_flee_until", 0)

    # ------------------------------------------------------------- muerte
    def _dangers(self, mapname=None):
        d = self.world.learn.setdefault("dangers", [])
        mk = (mapname or self.pos.get("map") or "").strip().lower()
        return [z for z in d if z[0] == mk]

    def _is_danger(self, x, y, r=None, mapname=None):
        r = r or self.cfg.get("danger_radius_tiles", 35)
        return any(max(abs(x - z[1]), abs(y - z[2])) <= r for z in self._dangers(mapname))

    def _hp_mem(self):
        """Vida exacta desde la memoria (al lado de la posición). None si no se sabe."""
        try:
            if self.mem.learned and self.mem.addr[0] == "i16":
                d = self.mem.proc.read(self.mem.addr[1] + 12, 4)
                if d:
                    return int.from_bytes(d, "little", signed=True)
        except Exception:
            pass
        return None

    def _death_check(self):
        """True si el pj está muerto (y ya se ocupó de revivir)."""
        now = time.time()
        b = self.bars
        hm = self._hp_mem()
        dead = (hm == 0) if hm is not None else (b.get("hp") == 0 and ((b.get("mp") or 0) > 0 or (b.get("sp") or 0) > 0))
        if not dead:
            self._dead_since = 0
            return False
        if not getattr(self, "_dead_since", 0):
            self._dead_since = now
            return True
        if now - self._dead_since < 3 or now - getattr(self, "_restart_click", 0) < 6:
            return True
        self._release()
        if not getattr(self, "_death_logged", 0) or now - self._death_logged > 60:
            self._death_logged = now
            self.stats["deaths"] = self.stats.get("deaths", 0) + 1
            mk = (self.pos.get("map") or "").strip().lower()
            if mk and self.pos.get("x") is not None:
                self.world.learn.setdefault("dangers", []).append([mk, self.pos["x"], self.pos["y"], int(now)])
                a = self.world.learn["anchors"].get(mk)
                if a and self._is_danger(a[0], a[1]):
                    del self.world.learn["anchors"][mk]
                self.world.save()
            self.log(f"☠ Me mataron en {self.pos.get('map')} ({self.pos.get('x')},{self.pos.get('y')}). Anoto la zona como peligrosa y revivo.")
        # botón "Restart" (abajo a la derecha, donde sale Level Up)
        img = self.img
        pt = None
        try:
            H, W = img.shape[:2]
            words = ocr.read_words(img, {"x1": int(W * 0.6), "y1": int(H * 0.7), "x2": W, "y2": int(H * 0.92)}, scale=1.0)
            w = self._find_word(words, "restart")
            if w:
                pt = (w[1], w[2])
        except Exception:
            pass
        if not pt and self.rect:
            pt = (int(self.rect[2] * 0.943), int(self.rect[3] * 0.85))
        winapi.click(*self.abs_pt(*pt))
        self._restart_click = now
        self.state, self.target = "seek", None
        self._explore_goal = None
        return True

    def _survival(self):
        if self._death_check():
            self.stats["state"] = "muerto: reviviendo"
            return True
        cfg, b, now = self.cfg, self.bars, time.time()
        hp = b.get("hp")
        hm = self._hp_mem()
        if hm is not None and hm > 0:
            # vida exacta de la memoria (la barra en pantalla a veces parpadea)
            self._hp_max = max(getattr(self, "_hp_max", 0), hm)
            hp = min(100.0, 100.0 * hm / self._hp_max)
            b = dict(b, hp=hp)
            # comer y tomar pociones del bolso
            hg = self._hunger_mem()
            if hg is not None and hg <= cfg.get("eat_below", 12) and now - getattr(self, "_last_eat", 0) > 8:
                self._last_eat = now
                if self._consume("meat"):
                    self.log("Comí (tenía hambre).")
            if hp <= cfg.get("hp_pot_pct", 55) and now - getattr(self, "_last_pot_t", 0) > cfg.get("pot_cooldown", 1.5):
                self._last_pot_t = now
                if self._consume("pot"):
                    hm2 = self._hp_mem() or hm
                    hp = min(100.0, 100.0 * hm2 / self._hp_max)
                    b = dict(b, hp=hp)
        if hp == 0 and not b.get("mp") and not b.get("sp"):
            hp = None  # todo en 0: la imagen todavía no cargó, no es que me morí
        # vida baja tiene que durar un ratito (al entrar/salir de edificios la pantalla parpadea)
        if hp is not None and hp <= cfg["panic_hp_pct"]:
            if not getattr(self, "_low_since", 0):
                self._low_since = now
            if now - self._low_since < 0.6:
                hp = None
        else:
            self._low_since = 0
        if self._fleeing():
            if hp is not None and hp >= cfg.get("rest_until_pct", 75):
                self._flee_until = 0
                self.log(f"Vida {hp:.0f}%: vuelvo a cazar.")
            return False
        if hp is not None and hp <= cfg["panic_hp_pct"] and cfg.get("panic_action") == "flee":
            self._do_action(cfg["hp_pot_action"], "hp_pot_slot")
            self._flee_until = now + cfg.get("flee_max_sec", 600)
            self._release()
            tt = (self.target or {}).get("etype")
            if tt is not None:
                hard = self.world.learn.setdefault("hard_types", {})
                hard[str(tt)] = int(cfg.get("travel", {}).get("my_level", 1))
                self.world.save()
                self.log(f"Ese bicho (tipo {tt}) me pega demasiado para mi nivel: lo esquivo por ahora.")
            self.state, self.target = "seek", None
            self.log(f"¡Vida {hp:.0f}%! Me alejo a descansar.")
            return False
        if hp is not None and hp <= cfg["panic_hp_pct"]:
            self.panic_count += 1
            self._do_action(cfg["hp_pot_action"], "hp_pot_slot")
            self.stats["hp_pots"] += 1
            if cfg["panic_action"] != "pots_only" and self.panic_count >= 3:
                self.log(f"¡PÁNICO! HP {hp:.0f}%.")
                if cfg["panic_action"] == "recall_and_stop":
                    if self._do_action(cfg["recall_action"], "recall_slot"):
                        self.log("Recall usado.")
                self.stop("pánico por HP baja")
                return True
            time.sleep(0.25)
            return True
        self.panic_count = 0
        for k in ("hp", "mp", "sp"):
            v = b.get(k)
            if v is None or v >= cfg[f"{k}_pot_pct"]:
                continue
            if now - self.last_pot[k] < cfg["pot_cooldown"]:
                continue
            if self._do_action(cfg[f"{k}_pot_action"], f"{k}_pot_slot"):
                self.last_pot[k] = now
                self.stats[f"{k}_pots"] += 1
        return False

    def _timers(self):
        now = time.time()
        for i, t in enumerate(self.cfg.get("timers", [])):
            if not t.get("enabled"):
                continue
            last = self.timer_last.setdefault(i, now)
            if now - last >= float(t.get("every_min", 20)) * 60:
                if self._do_action(t.get("action"), t.get("action", {}).get("slot")):
                    self.log(f"Timer: {t.get('name', 'acción')}.")
                self.timer_last[i] = now

    # ---------------------------------------------------------------- mouse
    def _hold_on(self, x, y):
        if not self.running:
            return
        ax, ay = self.abs_pt(x, y)
        mod = (self.cfg.get("attack_modifier") or "").upper() or None
        if mod and self.mod_held != mod:
            winapi.key_down(mod)
            self.mod_held = mod
        if self.cfg.get("attack_hold", True):
            winapi.move(ax, ay)
            if not self.mouse_held:
                time.sleep(0.02)
                winapi.left_down()
                self.mouse_held = True
        else:
            winapi.click(ax, ay)

    def _release(self):
        if self.mouse_held:
            winapi.left_up()
            self.mouse_held = False
        if self.mod_held:
            winapi.key_up(self.mod_held)
            self.mod_held = None

    # ----------------------------------------------------------------- hunt
    def _dist_tiles(self, x, y):
        cx, cy = self._char()
        tw, th = self._tile()
        return math.hypot((x - cx) / tw, (y - cy) / th)

    def _blacklisted(self, m):
        now = time.time()
        self.blacklist = [b for b in self.blacklist if b[2] > now]
        return any(abs(m["x"] - bx) < m["w"] and abs(m["y"] - by) < m["h"] for bx, by, _ in self.blacklist)

    def _monsters(self, roi=None):
        return vision.find(self.img, self.templates.items["monsters"], self.cfg["match_threshold"],
                           roi or self._game_roi(), self.cfg.get("ignore_templates", []), scale=self.cfg.get("match_scale", 0.5))

    def _hunt(self):
        now = time.time()
        if self.state == "loot":
            self._loot_step()
            return
        if self.state == "attack" and self.target:
            if now - self.last_attack_click < self.cfg["attack_check_every"]:
                return
            t = self.target
            pad = max(t["w"], t["h"])
            roi = {"x1": t["x"] - pad * 1.5, "y1": t["y"] - pad * 1.5, "x2": t["x"] + pad * 1.5, "y2": t["y"] + pad * 1.5}
            near = [m for m in self._monsters(roi) if m["name"] == t["name"]] or self._monsters(roi)
            if near:
                m = min(near, key=lambda m: math.hypot(m["x"] - t["x"], m["y"] - t["y"]))
                self.target = m
                self.last_seen_target = now
                if now - self.target_since > self.cfg["target_timeout"]:
                    self.log(f"{m['name']} no muere en {self.cfg['target_timeout']}s: cambio de objetivo.")
                    self.blacklist.append((m["x"], m["y"], now + 15))
                    self._release()
                    self.state, self.target = "seek", None
                    return
                if self.mouse_held and math.hypot(m["x"] - t["x"], m["y"] - t["y"]) > 12:
                    self._release()
                self._hold_on(m["x"], m["y"])
                self.last_attack_click = now
                return
            # desapareció el objetivo
            if now - self.last_seen_target > 0.5:
                self._release()
                if now - self.target_since > 0.3:  # un golpe fuerte puede matar rápido
                    self.stats["kills"] += 1
                    self.log(f"✝ {t['name']} (kill #{self.stats['kills']}).")
                    if self.cfg.get("loot_enabled") and self.templates.items["items"]:
                        self.state, self.loot_at, self.loot_pos = "loot", now + self.cfg["loot_delay"], (t["x"], t["y"])
                        self.stats["state"] = "esperando loot"
                        return
                self.state, self.target = "seek", None
            return

        # seek
        mons = [m for m in self._monsters() if not self._blacklisted(m)
                and self._dist_tiles(m["x"], m["y"]) <= self.cfg["max_target_dist_tiles"]]
        self._last_mons = mons
        if mons:
            m = min(mons, key=lambda m: self._dist_tiles(m["x"], m["y"]))
            self.target, self.state = m, "attack"
            self.target_since = self.last_seen_target = self.last_target_found = now
            self.stats["state"], self.stats["target"] = "atacando", m["name"]
            self._hold_on(m["x"], m["y"])
            self.last_attack_click = now
            return
        self.stats["state"], self.stats["target"] = "buscando", ""
        if self.cfg.get("roam_enabled") and now - self.last_target_found > self.cfg["roam_after"]:
            self._roam()
            self.last_target_found = now

    def _loot_step(self):
        if time.time() < self.loot_at:
            return
        lx, ly = self.loot_pos
        tw, th = self._tile()
        r = self.cfg["loot_radius_tiles"]
        roi = {"x1": lx - r * tw, "y1": ly - r * th, "x2": lx + r * tw, "y2": ly + r * th}
        items = vision.find(self.img, self.templates.items["items"], self.cfg["match_threshold"], roi, scale=self.cfg.get("match_scale", 0.5))
        for it in items[: self.cfg["loot_max_clicks"]]:
            x, y = self.abs_pt(it["x"], it["y"])
            winapi.click(x, y)
            self.stats["loot_clicks"] += 1
            self.log(f"Loot: {it['name']}")
            time.sleep(0.6)
        self.state, self.target = "seek", None

    def _roam(self):
        tw, th = self._tile()
        cx, cy = self._char()
        r = self.cfg["roam_radius_tiles"]
        if math.hypot(*self.disp) > self.cfg["leash_tiles"]:
            ang = math.atan2(-self.disp[1], -self.disp[0]) + random.uniform(-0.5, 0.5)  # volver
        else:
            ang = random.uniform(0, 2 * math.pi)
        dist = random.uniform(r * 0.5, r)
        dx, dy = math.cos(ang) * dist, math.sin(ang) * dist
        roi = self._game_roi()
        x = min(max(cx + dx * tw, roi["x1"] + tw), roi["x2"] - tw)
        y = min(max(cy + dy * th, roi["y1"] + th), roi["y2"] - th)
        t = self._tile_of(x, y)
        if t and self._near_door(3, t):
            x, y = cx - (x - cx), cy - (y - cy)  # para el otro lado
        self._release()
        winapi.click(*self.abs_pt(x, y))
        self.disp[0] += (x - cx) / tw
        self.disp[1] += (y - cy) / th
        self.stats["roams"] += 1
        self.stats["state"] = "caminando"
        self._last_move = time.time()




    # ------------------------------------------------------- autocalibración
    def auto_calibrate(self):
        """Encuentra solo: zona de juego, barras, texto del mapa, personaje, baldosa y botón del personaje."""
        import cv2
        import numpy as np
        if not self._ensure_window():
            return {"ok": False, "msg": "No encuentro la ventana del juego."}
        # el juego tiene que estar adelante (salvo en segundo plano, que se ve igual tapado)
        self.screen.use_printwindow = bool(self.cfg.get("background_mode"))
        if not self.cfg.get("background_mode") and not winapi.is_foreground(self.hwnd):
            winapi.focus(self.hwnd)
            time.sleep(0.8)
            self._ensure_window()
        a = self.screen.grab(self.rect)
        time.sleep(0.8)
        b = self.screen.grab(self.rect)
        if a is None or b is None:
            return {"ok": False, "msg": "No pude sacar foto del juego."}
        frozen = float(cv2.absdiff(a, b).max()) == 0
        H, W = b.shape[:2]
        g = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
        cols = np.where(g.mean(axis=0) > 12)[0]
        rows = np.where(g.mean(axis=1) > 12)[0]
        if len(cols) < 50 or len(rows) < 50:
            return {"ok": False, "msg": "La imagen está negra: poné el juego en ventana (no pantalla completa)."}
        gx1, gx2, gy1, gy2 = int(cols[0]), int(cols[-1]), int(rows[0]), int(rows[-1])
        gw = gx2 - gx1
        hsv = cv2.cvtColor(b, cv2.COLOR_BGR2HSV)
        y0 = gy1 + int((gy2 - gy1) * 0.80)
        def bar(mask_fn, name, xs=None):
            xs = xs if xs is not None else gx1
            m = mask_fn(hsv[y0:gy2, xs:gx1 + int(gw * 0.6)])
            m = cv2.morphologyEx(m.astype(np.uint8) * 255, cv2.MORPH_OPEN, np.ones((3, 9), np.uint8))
            n, _, st, _ = cv2.connectedComponentsWithStats(m)
            best = None
            for i in range(1, n):
                x, y, w, h, ar = st[i]
                if gw * 0.06 < w < gw * 0.45 and h >= max(6, int(gw / 220)) and w > 3 * h and (best is None or w * h > best[2] * best[3]):
                    best = (x, y, w, h)
            if not best:
                return None
            x, y, w, h = (int(v) for v in best)
            # unir pedazos en la misma fila (el número escrito encima corta la barra)
            for i in range(1, n):
                x2_, y2_, w2_, h2_, _ = (int(v) for v in st[i])
                if abs((y2_ + h2_ / 2) - (y + h / 2)) < h and w2_ > 6 and min(abs(x2_ + w2_ - x), abs(x + w - x2_)) < gw * 0.08:
                    nx1, nx2 = min(x, x2_), max(x + w, x2_ + w2_)
                    x, w = nx1, nx2 - nx1
            yy = y0 + y + int(h * 0.65)
            xx1, xx2 = xs + x + 2, xs + x + w - 3
            row = b[yy, xx1:xx2]
            sel = mask_fn(hsv[yy:yy + 1, xx1:xx2])[0]
            col = [int(v) for v in (np.median(row[sel], axis=0) if sel.any() else row[len(row) // 2])]
            return {"x1": xx1, "y1": yy, "x2": xx2, "y2": yy, "color": col}
        red = lambda h: ((h[:, :, 0] < 8) | (h[:, :, 0] > 170)) & (h[:, :, 1] > 150) & (h[:, :, 2] > 120)
        blue = lambda h: (h[:, :, 0] > 100) & (h[:, :, 0] < 130) & (h[:, :, 1] > 150) & (h[:, :, 2] > 90)
        green = lambda h: (h[:, :, 0] > 45) & (h[:, :, 0] < 80) & (h[:, :, 1] > 150) & (h[:, :, 2] > 100)
        c = self.cfg["calib"]
        found = []
        for key, fn in (("hp_bar", red), ("mp_bar", blue), ("sp_bar", green)):
            r = bar(fn, key, xs=(c["hp_bar"]["x2"] + 5) if key == "sp_bar" and "hp_bar" in found else None)
            if r:
                c[key] = r
                found.append(key)
        hud_top = min([c[k]["y1"] for k in ("hp_bar", "sp_bar") if c.get(k)] or [gy1 + int((gy2 - gy1) * 0.91)]) - int((gy2 - gy1) * 0.03)
        # barra de experiencia: la línea verde finita arriba de la barra de abajo
        best_row = None
        for yy in range(max(gy1, hud_top - int((gy2 - gy1) * 0.06)), min(gy2, hud_top + int((gy2 - gy1) * 0.04))):
            r = hsv[yy, gx1:gx2]
            g = (r[:, 0] > 40) & (r[:, 0] < 85) & (r[:, 1] > 120) & (r[:, 2] > 90)
            if g.sum() > gw * 0.03 and (best_row is None or g.sum() > best_row[1]):
                best_row = (yy, int(g.sum()))
        if best_row:
            yy = best_row[0]
            c["exp_bar"] = {"x1": gx1 + 2, "y1": yy, "x2": gx2 - 2, "y2": yy, "green": True}
            found.append("exp_bar")
        tile = max(16, int(round(gw / 20)))
        c["tile"] = {"w": tile, "h": tile}
        c["game_area"] = {"x1": gx1 + 2, "y1": gy1 + 2, "x2": gx2 - 2, "y2": hud_top - 4}
        cx = (gx1 + gx2) // 2
        cy = int((gy1 + hud_top) / 2 + tile * 0.75)
        c["char_center"] = {"x": cx, "y": cy}
        c["exclude"] = [{"x1": gx2 - int(gw * 0.17), "y1": gy1, "x2": gx2, "y2": gy1 + int((gy2 - gy1) * 0.23)}]
        c["char_button"] = {"x": gx1 + int(gw * 0.638), "y": gy1 + int((gy2 - gy1) * 0.958)}
        found += ["game_area", "char_center", "tile", "char_button"]
        # texto del mapa
        words = ocr.read_words(b, {"x1": gx1, "y1": hud_top, "x2": gx2, "y2": gy2})
        coord = [w for w in words if re.search(r"\(?\d{1,3},\d{1,3}\)?", w[0])]
        if coord:
            cw = coord[0]
            line = [w for w in words if abs(w[2] - cw[2]) < cw[4] and w[1] <= cw[1] and cw[1] - w[1] < gw * 0.35]
            xl = min(w[1] - w[3] / 2 for w in line)
            xr = cw[1] + cw[3] / 2
            c["pos_box"] = {"x1": int(xl - 12), "y1": int(cw[2] - cw[4]), "x2": int(xr + 14), "y2": int(cw[2] + cw[4])}
            found.append("pos_box")
        cfgmod.save(self.cfg)
        msg = "Calibré: " + ", ".join(found) + f". Baldosa {tile}px."
        if frozen:
            msg += " ⚠ La imagen no cambia entre fotos: el juego en pantalla completa no deja ver. Pasalo a ventana."
        self.log(msg)
        return {"ok": True, "msg": msg, "frozen": frozen}


    # ------------------------------------------------------------ auto-login
    def set_login(self, account, password, char_name, enabled, relaunch):
        al = self.cfg.setdefault("autologin", {})
        al.update({"account": account.strip(), "char_name": char_name.strip(), "enabled": bool(enabled), "relaunch": bool(relaunch)})
        if password:
            al["password_enc"] = secret.protect(password)
        cfgmod.save(self.cfg)
        self.log("Auto-login guardado." + (" Contraseña cifrada ✓" if al.get("password_enc") else ""))
        return {"saved": True, "has_password": bool(al.get("password_enc"))}

    def _game_exe(self):
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Helbreath Abaddon")
        for p in (os.path.join(base, "HelbreathAbaddon.exe"), os.path.join(base, "game", "Helbreath.exe")):
            if os.path.exists(p):
                return p
        return None

    def _screen_words(self):
        if self.img is None:
            return [], ""
        h, w = self.img.shape[:2]
        words = ocr.read_words(self.img, {"x1": 0, "y1": 0, "x2": w, "y2": h}, scale=1.0)
        return words, " ".join(x[0].lower() for x in words)

    def _autologin_step(self):
        """True si está ocupado reconectando (no cazar). Sale solo cuando vuelve a leer la posición en el mapa."""
        al = self.cfg.get("autologin", {})
        if not al.get("enabled"):
            return False
        now = time.time()
        in_game = (not self.cfg["calib"].get("pos_box")) or (self.pos.get("t") and now - self.pos["t"] < 20)
        if self.img is not None and not self.cfg["calib"].get("pos_box"):
            return False
        if self.img is not None and in_game:
            if self.stats["state"].startswith("reconectando"):
                self.log("✔ De vuelta en el juego.")
                self.stats["state"] = "buscando"
            return False
        if now - getattr(self, "_al_last", 0) < 6:
            return self.stats["state"].startswith("reconectando")
        self._al_last = now
        # 1) el juego no está abierto
        if self.img is None or not self.hwnd:
            self._release()
            self.stats["state"] = "reconectando"
            if al.get("relaunch", True) and now - getattr(self, "_al_launch", 0) > 120:
                exe = self._game_exe()
                if exe:
                    self._al_launch = now
                    self.log("El juego no está abierto: lo abro.")
                    os.startfile(exe)  # noqa
            return True
        if not winapi.is_foreground(self.hwnd) and not self.cfg.get("background_mode"):
            winapi.focus(self.hwnd)
            time.sleep(0.5)
        words, txt = self._screen_words()
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        # 2) pantalla de login
        if any(k in txt for k in ("password", "contraseña", "account", "login", "log in")):
            self._release()
            self.stats["state"] = "reconectando: login"
            if now - getattr(self, "_al_login_try", 0) < 25:
                return True
            self._al_login_try = now
            pwd = secret.unprotect(al.get("password_enc", ""))
            if not al.get("account") or not pwd:
                self.log("Auto-login: falta cuenta o contraseña en Configuración.")
                return True
            lab = next((w for w in words if norm(w[0]) in ("account", "accountname", "id", "user", "username", "login")), None)
            if lab:
                winapi.click(*self.abs_pt(lab[1] + lab[3] / 2 + self.cfg.get("login_field_offset", 140), lab[2]))
                time.sleep(0.3)
            winapi.press("CTRL+A")
            winapi.type_text(al["account"])
            plab = next((w for w in words if norm(w[0]).startswith("password")), None)
            if plab:
                winapi.click(*self.abs_pt(plab[1] + plab[3] / 2 + self.cfg.get("login_field_offset", 140), plab[2]))
            else:
                winapi.press("TAB")
            time.sleep(0.3)
            winapi.press("CTRL+A")
            winapi.type_text(pwd)
            pwd = None
            winapi.press("ENTER")
            self.log("Auto-login: entré cuenta y contraseña.")
            return True
        # 3) elegir personaje
        cn = norm(al.get("char_name", ""))
        target = next((w for w in words if cn and norm(w[0]) == cn), None)
        if target or any(k in txt for k in ("select character", "start", "enter game", "play")):
            self._release()
            self.stats["state"] = "reconectando: personaje"
            if now - getattr(self, "_al_char_try", 0) < 20:
                return True
            self._al_char_try = now
            if target:
                winapi.click(*self.abs_pt(target[1], target[2]), double=True)
                time.sleep(0.8)
            btn = next((w for w in words if norm(w[0]) in ("start", "play", "enter", "connect", "ok")), None)
            if btn:
                winapi.click(*self.abs_pt(btn[1], btn[2]))
            else:
                winapi.press("ENTER")
            self.log(f"Auto-login: elegí el personaje {al.get('char_name') or ''}.")
            return True
        # 4) cartel de desconexión
        if any(k in txt for k in ("disconnect", "connection lost", "desconect")):
            self._release()
            self.stats["state"] = "reconectando"
            winapi.press("ENTER")
            self.log("Auto-login: cartel de desconexión, lo cierro.")
            return True
        if self.stats["state"].startswith("reconectando"):
            self.stats["state"] = "buscando"
        return False  # no es pantalla de login: sigo normal


    # ---------------------------------------------------- prueba 2º plano
    def test_background(self):
        """Prueba si el juego deja verlo tapado y recibir clicks sin el mouse real."""
        import cv2
        if self.running:
            return {"ok": False, "msg": "Apagá el bot primero."}
        if not self._ensure_window() or not self.cfg["calib"].get("pos_box"):
            return {"ok": False, "msg": "Abrí el juego y autocalibrá primero."}
        out = []
        a = winapi.capture_window(self.hwnd)
        time.sleep(1.0)
        b = winapi.capture_window(self.hwnd)
        if a is None or b is None:
            return {"ok": False, "msg": "✘ No puedo ver el juego tapado (sale negro). El segundo plano no va a andar."}
        diff = float(cv2.absdiff(a, b).mean())
        see = float(cv2.absdiff(a, b).max()) > 0
        out.append(("✔ Veo el juego aunque esté tapado" if see else "✘ La imagen del juego tapado sale congelada") + f" (cambio {diff:.2f})")
        try:
            d = os.path.join(cfgmod.DATA, "debug")
            os.makedirs(d, exist_ok=True)
            cv2.imwrite(os.path.join(d, "bg_capture.png"), b)
            fg = self.screen.grab(self.rect) if False else None
        except Exception:
            pass
        p1 = ocr.read_position(b, self.cfg["calib"]["pos_box"])
        out.append(f"(tamaño {b.shape[1]}x{b.shape[0]}, ventana {self.rect[2]}x{self.rect[3]}, leí '{(p1[3] if p1 else '') or ''}')")
        if not p1 or p1[0] is None:
            out.append("✘ No pude leer la posición desde la imagen tapada")
            return {"ok": False, "msg": " · ".join(out)}
        tw, th = self._tile()
        cx, cy = self._char()
        winapi.set_background(self.hwnd)
        try:
            winapi.move(*self.abs_pt(cx + 3 * tw, cy))
            winapi.left_down()
            time.sleep(1.2)
            winapi.left_up()
        finally:
            winapi.set_background(None)
        time.sleep(1.0)
        c = winapi.capture_window(self.hwnd)
        p2 = ocr.read_position(c, self.cfg["calib"]["pos_box"]) if c is not None else None
        moved = bool(p2 and p2[0] and (p2[1], p2[2]) != (p1[1], p1[2]))
        out.append(f"{'✔' if moved else '✘'} Click sin mouse: {p1[1]},{p1[2]} → {p2[1] if p2 else '?'},{p2[2] if p2 else '?'}")
        ok = see and moved
        if ok:
            self.cfg["background_mode"] = True
            cfgmod.save(self.cfg)
            out.append("Activé el modo segundo plano.")
        else:
            out.append("El juego no acepta el modo segundo plano: el bot va a usar el mouse real.")
        msg = " · ".join(out)
        self.log(msg)
        return {"ok": ok, "msg": msg}

    # ------------------------------------------------------------- posición
    def _read_pos_bg(self, img, snap=None, moving=False):
        try:
            self._read_pos_bg2(img, snap, moving)
        except Exception as e:
            self.log(f"Error leyendo posición: {e!r}")
        finally:
            self._ocr_busy = False

    def _read_pos_bg2(self, img, snap=None, moving=False):
        if True:
            self._read_pos(img)
            if self._mem_on() and self.pos.get("t") and time.time() - self.pos["t"] < 3:
                xy = (self.pos["x"], self.pos["y"])
                stable = xy == self._last_ocr_xy
                self._last_ocr_xy = xy
                self.mem.feed(xy[0], xy[1], snap, stable)
                self._mem_hold = False

    # ---------------------------------------------------------------- memoria
    def _mem_on(self):
        return bool((self.cfg.get("memory") or {}).get("enabled", True)) and memory.IS_WIN and bool(self.hwnd)

    def _mem_step(self):
        """Engancha el proceso y, si ya sabe dónde está la posición, la lee directo (instantáneo)."""
        if not self._mem_on():
            return
        if self.mem.hwnd != self.hwnd and not getattr(self, "_mem_fail", False):
            if not self.mem.attach(self.hwnd):
                self._mem_fail = True
                self.log(f"Memoria: {self.mem.error}. Sigo con la pantalla.")
            else:
                self.log(f"🧠 Memoria: conectado al juego (proceso {getattr(self.mem.proc, 'pid', '?')}). Busco tu posición.")
        # guardar lo aprendido
        mc = self.cfg.setdefault("memory", {})
        sv = list(self.mem.saved) if self.mem.saved else None
        if sv != mc.get("pos"):
            mc["pos"] = sv
            try:
                cfgmod.save(self.cfg)
            except Exception:
                pass
        if self.mem.learned and self.mem.addr[0] == "i16" and not self.ents.center and not self.ents.finding \
                and time.time() - getattr(self, "_ent_try", 0) > 20:
            self._ent_try = time.time()
            threading.Thread(target=self.ents.find, args=(self.mem.proc, self.mem.addr[1]), daemon=True).start()
        if not self.mem.learned or self._mem_hold:
            return
        v = self.mem.read_pos()
        if not v or self.pos.get("x") is None:
            return
        if max(abs(v[0] - self.pos["x"]), abs(v[1] - self.pos["y"])) > 8:
            # salto grande = portal/teleport: espero al OCR para saber el mapa nuevo
            self._mem_hold = True
            self._last_ocr = 0
            return
        self.pos.update({"x": v[0], "y": v[1], "t": time.time()})

    # ---------- investigación: el bot recorre y saca fotos para poder programar lo que falta ----------
    def investigate(self):
        th = getattr(self, "_inv_th", None)
        if th is not None and th.is_alive():
            return {"ok": False, "msg": "Ya estoy investigando."}
        if not self.running:
            return {"ok": False, "msg": "Prendé el bot primero (tiene que estar cazando en la Farm)."}
        if not self._same(self.pos.get("map") or "", "Aresden Farm"):
            return {"ok": False, "msg": "Llevá al pj a Aresden Farm y probá de nuevo."}
        self._inv_th = threading.Thread(target=self._investigate, daemon=True)
        self._inv_th.start()
        return {"ok": True, "msg": "Investigando: unos 6 minutos. No toques el juego. Al final queda data\\investigacion.zip"}

    def _investigate(self):
        import cv2
        import ctypes
        import json as _json
        import zipfile
        d = os.path.join(cfgmod.DATA, "investigacion")
        try:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        sc = self.rect[2] / 1280.0
        P = lambda x, y: self.abs_pt(x * sc, y * sc)
        info = {"pasos": []}

        def note(t):
            info["pasos"].append(time.strftime("%H:%M:%S ") + t)
            self.log("🔎 " + t)

        def snap(name, crop=None):
            try:
                im = self.screen.grab(self.rect)
                if crop:
                    x1, y1, x2, y2 = [int(v * sc) for v in crop]
                    im = im[y1:y2, x1:x2]
                cv2.imwrite(os.path.join(d, name + ".jpg"), im, [cv2.IMWRITE_JPEG_QUALITY, 82])
            except Exception as ex:
                info["pasos"].append(f"foto {name}: {ex!r}")

        def wheel(x, y, n):
            ax, ay = P(x, y)
            winapi.move(ax, ay)
            time.sleep(0.15)
            for _ in range(abs(n)):
                dl = -120 if n < 0 else 120
                ctypes.windll.user32.PostMessageW(self.hwnd, 0x020A, (dl & 0xFFFF) << 16, ((int(ay) & 0xFFFF) << 16) | (int(ax) & 0xFFFF))
                time.sleep(0.12)

        def go_in(tile, word, label):
            self._busy_ui = False
            self.mission_result = None
            self.mission = {"map": "Aresden Farm", "goal": list(tile), "label": label, "until": time.time() + 400}
            t0 = time.time()
            while time.time() - t0 < 400 and self.alive and self.running:
                if word in (self.pos.get("map") or "").lower():
                    self._busy_ui = True
                    self._release()
                    time.sleep(1.5)
                    return True
                time.sleep(0.1)
            return False

        def go_out():
            try:
                self._park_shop_dialog()
            except Exception:
                pass
            self._busy_ui = False
            t0 = time.time()
            while time.time() - t0 < 150 and self.alive and self.running:
                if self._same(self.pos.get("map") or "", "Aresden Farm"):
                    time.sleep(2.5)
                    return True
                time.sleep(0.2)
            return False

        def list_photos(tag):
            h_ = None
            for _ in range(3):
                h_ = vision.find_shop_header(self.screen.grab(self.rect))
                if h_:
                    break
                time.sleep(0.5)
            if not h_:
                note(f"{tag}: no veo la lista de venta.")
                snap(tag + "_sin_lista")
                return
            pw = (h_[0] / sc, h_[1] / sc)
            for _ in range(2):
                if abs(pw[0] - 435) <= 12 and abs(pw[1] - 159) <= 12:
                    break
                gx, gy = pw[0] - 92, pw[1]
                self._drag_bg(gx * sc, gy * sc, (gx + 435 - pw[0]) * sc, (gy + 159 - pw[1]) * sc)
                h2 = vision.find_shop_header(self.screen.grab(self.rect))
                pw = (h2[0] / sc, h2[1] / sc) if h2 else pw
            wheel(300, 300, 90)
            time.sleep(0.5)
            for i in range(14):
                snap(f"{tag}_lista_{i:02d}", (60, 90, 600, 720))
                wheel(300, 300, -6)
                time.sleep(0.5)
            # ficha de las primeras filas de la última página y de la primera, para ver el formato
            note(f"{tag}: lista fotografiada.")

        try:
            # 1) oro en memoria: qué números suben de a poco mientras cazo
            note("Paso 1/5: busco el oro en la memoria (100 s cazando).")
            try:
                a = self.mem.addr[1]
                LO, HI = 16384, 32768
                shots = []
                for _ in range(5):
                    b = self.mem.proc.read(a - LO, LO + HI)
                    if b:
                        shots.append(np.frombuffer(b, dtype="<i4").astype(np.int64))
                    time.sleep(25)
                m = np.stack(shots)
                df, tot = np.diff(m, axis=0), m[-1] - m[0]
                mask = (df >= 0).all(axis=0) & (tot >= 10) & (tot <= 5000) & (m[0] >= 0) & (m[0] < 50_000_000)
                info["oro_candidatos"] = [[int(i * 4 - LO), [int(v) for v in m[:, i]]] for i in np.nonzero(mask)[0][:60]]
            except Exception as ex:
                info["oro_error"] = repr(ex)
            # 2) bolso: foto y cartel de cada cosa manteniendo el click
            note("Paso 2/5: leo el bolso (click sostenido sobre cada cosa).")
            self._busy_ui = True
            self._release()
            time.sleep(1.0)
            pots, meats = vision.find_bag_items(self.screen.grab(self.rect), self._bag_box())
            if not (pots or meats):
                winapi.click(*P(875, 918))
                time.sleep(1.2)
            snap("bolso")
            pts = [(x, y) for y in range(330, 540, 26) for x in range(870, 1170, 26)]
            pts = pts[0::2] + pts[1::2]  # salteado: nunca dos clicks seguidos sobre lo mismo (sería doble click)
            for i, (x, y) in enumerate(pts):
                if not (self.alive and self.running):
                    break
                winapi.move(*P(x, y))
                time.sleep(0.15)
                winapi.left_down()
                time.sleep(0.45)
                snap(f"item_{i:03d}_{x}_{y}")
                winapi.left_up()
                time.sleep(0.45)
            winapi.click(*P(816, 918))
            time.sleep(1.0)
            snap("personaje")
            winapi.click(*P(816, 918))
            time.sleep(0.5)
            winapi.click(*P(875, 918))
            time.sleep(0.5)
            # nombres de items en memoria (para leer el inventario sin mouse)
            try:
                hits = memory.find_strings(self.mem.proc, ["Agile Main Gauche", "Gradius +1", "Special Magic Wand", "Scale Mail", "Gradius+1"], limit=30)
                info["nombres_en_memoria"] = {}
                for wd, addrs in hits.items():
                    info["nombres_en_memoria"][wd] = [[hex(ad), (self.mem.proc.read(ad - 64, 320) or b"").hex()] for ad in addrs[:12]]
            except Exception as ex:
                info["nombres_error"] = repr(ex)
            # 3) herrero: ventana de vender
            note("Paso 3/5: voy al herrero a ver cómo se vende.")
            if go_in((75, 86), "smith", "Blacksmith (investigar)"):
                for _ in range(14):
                    if abs((self.pos.get("x") or 0) - 47) <= 1 and abs((self.pos.get("y") or 0) - 35) <= 1:
                        break
                    winapi.click(*P(1000, 470))
                    time.sleep(0.7)
                time.sleep(1.0)
                info["herrero_pos"] = [self.pos.get("x"), self.pos.get("y")]
                snap("herrero_0")
                winapi.click(*P(745, 345))
                time.sleep(2.5)
                snap("herrero_1_menu")
                winapi.click(*P(735, 364))  # Sell
                time.sleep(2.0)
                snap("herrero_2_sell")
                winapi.click(*P(875, 918))
                time.sleep(1.2)
                snap("herrero_3_sell_bolso")
                winapi.click(*P(875, 918))
                time.sleep(0.6)
                winapi.press("ESC")
                time.sleep(0.8)
                snap("herrero_4_despues_esc")
                winapi.click(*P(745, 345))
                time.sleep(2.5)
                winapi.click(*P(625, 364))  # Trade
                time.sleep(2.0)
                list_photos("herrero")
                go_out()
            else:
                note("No llegué al herrero.")
            # 4) shop: lista completa (capa)
            note("Paso 4/5: voy a la Shop a fotografiar toda la lista.")
            if go_in((63, 70), "shop", "Shop (investigar)"):
                npc = None
                pts = [(x, y) for y in range(420, 720, 48) for x in range(700, 1250, 40)]
                pts += [(x, y) for y in range(120, 800, 56) for x in range(60, 1240, 44) if not (x >= 700 and 420 <= y < 720)]
                for x, y in pts:
                    winapi.move(*P(x, y))
                    time.sleep(0.11)
                    if vision.neutral_pixels(self.screen.grab(self.rect), x * sc, y * sc) >= 25:
                        npc = [x, y]
                        break
                info["shop_npc"] = npc
                if npc:
                    winapi.click(*P(*npc))
                    time.sleep(2.4)
                    snap("shop_1_menu")
                    winapi.click(*P(npc[0] - 122, npc[1] + 18))
                    time.sleep(1.8)
                    list_photos("shop")
                else:
                    note("No encontré al vendedor de la Shop.")
                    snap("shop_sin_vendedor")
                go_out()
            else:
                note("No llegué a la Shop.")
            note("Paso 5/5: armo el zip.")
        except Exception as ex:
            info["error"] = repr(ex)
            self.log(f"🔎 Error investigando: {ex!r}")
        finally:
            self._busy_ui = False
            self._shop_last = time.time()
            self._rep_last = time.time()
            try:
                info["nivel"] = self._level_mem()
                info["stats"] = self._stats_mem()
                with open(os.path.join(d, "info.json"), "w", encoding="utf-8") as f:
                    _json.dump(info, f, ensure_ascii=False, indent=1)
                zp = os.path.join(cfgmod.DATA, "investigacion.zip")
                with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
                    for fn in sorted(os.listdir(d)):
                        z.write(os.path.join(d, fn), fn)
                self.log("🔎 Listo. Mandale a Claude el archivo data\\investigacion.zip")
            except Exception as ex:
                self.log(f"🔎 No pude armar el zip: {ex!r}")

    def mem_diag(self):
        """Diagnóstico: guarda data/debug/memoria.json para analizar (bichos, tu pj)."""
        import json
        if not self._ensure_window() or not memory.IS_WIN:
            return {"ok": False, "msg": "No encuentro el juego."}
        out = {"status": self.mem.status, "error": self.mem.error, "pos": self.pos.get("x") and [self.pos["x"], self.pos["y"]],
               "addr": list(self.mem.addr) if self.mem.addr else None}
        try:
            p = self.mem.proc or memory.Proc(self.hwnd)
            regs = p.regions()
            out["regions"] = len(regs)
            out["mb"] = round(sum(r[1] for r in regs) / 2**20, 1)
            name = (self.cfg.get("autologin") or {}).get("char_name") or "Sailex"
            words = [name, "Slime", "Rabbit", "Orc", "Skeleton", "Giant-Ant", "Scorpion", "Zombie", "Aresden", "aresden"]
            hits = memory.find_strings(p, words, limit=200)
            base = self.mem.addr[1] if self.mem.addr else None
            out["strings"] = {w: {"n": len(a), "addrs": [hex(x) for x in a[:60]],
                                  "rel": [x - base for x in a[:60]] if base else None} for w, a in hits.items()}
            ok = True
        except Exception as e:
            out["exc"] = repr(e)
            ok = False
        d = os.path.join(cfgmod.DATA, "debug")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "memoria.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
        return {"ok": ok, "msg": "Guardado en data/debug/memoria.json", "status": self.mem.status}

    def _canon_map(self, name):
        """Corrige nombres de mapa mal leídos usando los que ya conozco."""
        known = getattr(self, "_known_maps", None)
        if known is None or time.time() - getattr(self, "_known_t", 0) > 60:
            known = {}
            for k in list(worldmod.NAME_GUESS) + [k.split("|")[0] for k in self.world.learn["links"]] + \
                    [v["to"] for v in self.world.learn["links"].values()]:
                if all(c.isalnum() or c == " " for c in k):
                    known[k.lower()] = True
            for extra in ("aresden cityhall", "warehouse", "promiseland", "beginner zone", "aresden city", "aresden farm",
                          "aresden shop", "aresden blacksmith", "eternal field", "silent wood", "aresden dungeon"):
                known[extra] = True
            self._known_maps, self._known_t = known, time.time()
        low = name.strip().lower()
        if low in known:
            return name.strip()
        best = difflib.get_close_matches(low, list(known), n=1, cutoff=0.84)
        if best:
            return " ".join(w.capitalize() for w in best[0].split())
        return name.strip()

    def _read_pos(self, img=None):
        r = ocr.read_position(self.img if img is None else img, self.cfg["calib"].get("pos_box"))
        if not r:
            return
        name, x, y, txt = r
        self.pos["text"] = txt or ""
        if name is None:
            return
        mv = self.mem.read_pos() if self.mem.learned else None
        if mv:
            x, y = mv  # la memoria es exacta; el OCR solo para el nombre del mapa
        else:
            mp0 = self.world.map_for(name)
            if mp0 is not None:
                # el OCR a veces pega el ")" como un 1 al final: "(34,38)" -> 381
                if y >= mp0.h and y // 10 < mp0.h:
                    y //= 10
                if x >= mp0.w or y >= mp0.h:
                    return
        if name.startswith("\\"):
            name = "A" + name[1:]
        name = self._canon_map(name)
        # un cambio de mapa tiene que leerse dos veces seguidas (el OCR a veces lee mal una letra)
        if self.pos.get("map") and name.lower() != self.pos["map"].lower():
            if getattr(self, "_map_pending", None) != name.lower():
                self._map_pending = name.lower()
                return
        self._map_pending = None
        prev = dict(self.pos)
        self.pos.update({"map": name, "x": x, "y": y, "t": time.time()})
        self.world.observe(name, x, y)
        # cambió de mapa: aprender el portal que usé
        if prev.get("map") and prev["map"].lower() != name.lower() and prev.get("x") is not None:
            m = self.world.map_for(prev["map"])
            tile = None
            if m:
                near = [(abs(tx - prev["x"]) + abs(ty - prev["y"]), (tx, ty)) for tx, ty in m.teleports]
                near = [n for n in near if n[0] <= 4]
                if near:
                    tile = min(near)[1]
            if tile:
                self.world.add_link(prev["map"], tile, name, (x, y))
                self.log(f"Aprendí un portal: {prev['map']} ({tile[0]},{tile[1]}) → {name}.")
                # la vuelta: la puerta más cercana a donde aparecí lleva de regreso (hasta que se confirme)
                try:
                    back = [v for _, v in self.world.links_from(name) if self._same(v["to"], prev["map"])]
                    m2 = self.world.map_for(name)
                    if not back and m2:
                        nb = sorted(m2.teleports, key=lambda t: abs(t[0] - x) + abs(t[1] - y))
                        if nb and abs(nb[0][0] - x) + abs(nb[0][1] - y) <= 8:
                            self.world.add_link(name, nb[0], prev["map"], (prev["x"], prev["y"]))
                except Exception:
                    pass
            else:
                self.log(f"Cambio de mapa: {prev['map']} → {name}.")

    def test_pos(self):
        if not self._ensure_window():
            return {"ok": False, "msg": "No encuentro el juego."}
        self.img = self.screen.grab(self.rect)
        self._read_pos()
        err = ocr.last_error()
        m = self.world.map_for(self.pos["map"]) if self.pos["map"] else None
        return {"ok": self.pos["map"] is not None, "text": self.pos["text"], "map": self.pos["map"], "x": self.pos["x"], "y": self.pos["y"],
                "file": m.name if m else None, "error": err, "game_dir": self.world.game_dir}


    # ----------------------------------------------------------------- stats
    STAT_RE = __import__("re").compile(r"\b(STR|DEX|VIT|INT|MAG|CHR)[^0-9A-Za-z]{0,6}(\d{1,3})", __import__("re").I)
    LVL_RE = __import__("re").compile(r"(?:level|lv|lvl|nivel)[^0-9]{0,5}(\d{1,3})", __import__("re").I)
    PTS_RE = __import__("re").compile(r"(?:point|points|pts|puntos)[^0-9]{0,8}(\d{1,3})", __import__("re").I)

    def read_char(self, img=None):
        box = self.cfg["calib"].get("char_box")
        img = self.img if img is None else img
        if not box or img is None:
            return None
        x1, y1, x2, y2 = (int(box[k]) for k in ("x1", "y1", "x2", "y2"))
        txt = ocr.read_text(img[max(0, y1):y2, max(0, x1):x2]) or ""
        stats = {k.upper(): int(v) for k, v in self.STAT_RE.findall(txt)}
        lv = self.LVL_RE.search(txt)
        pts = self.PTS_RE.search(txt)
        return {"text": txt, "stats": stats, "level": int(lv.group(1)) if lv else None, "points": int(pts.group(1)) if pts else None}

    def _next_stat(self, stats):
        plan = self._load_json("training_path.json").get("stat_plan", [])
        for st in plan:
            cur = stats.get(st["stat"])
            if cur is None or cur < st["target"]:
                return st["stat"]
        return "VIT"

    LABELS = {"STR": "strength", "VIT": "vitality", "DEX": "dexterity", "INT": "intelligence", "MAG": "magic", "CHR": "charisma"}

    def _words_text(self, words):
        return " ".join(w[0] for w in sorted(words, key=lambda w: (round(w[2] / 12), w[1])))

    def _find_word(self, words, *parts):
        """Busca una secuencia de palabras (ej 'level', 'set') y devuelve el centro."""
        ws = sorted(words, key=lambda w: (round(w[2] / 12), w[1]))
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        for i, w in enumerate(ws):
            if norm(w[0]).startswith(parts[0]):
                if len(parts) == 1:
                    return w
                nxt = ws[i + 1] if i + 1 < len(ws) else None
                if nxt and norm(nxt[0]).startswith(parts[1]) and abs(nxt[2] - w[2]) < 15:
                    return ((w[0] + " " + nxt[0]), (w[1] + nxt[1]) / 2, w[2], w[3] + nxt[3], w[4])
        return None

    def _dbg_img(self, name, img):
        if self.cfg.get("debug") and img is not None:
            try:
                import cv2
                d = os.path.join(cfgmod.DATA, "debug")
                os.makedirs(d, exist_ok=True)
                cv2.imwrite(os.path.join(d, name), img)
            except Exception:
                pass

    def _hunger_mem(self):
        try:
            if self.mem.learned and self.mem.addr[0] == "i16":
                d = self.mem.proc.read(self.mem.addr[1] + 24, 4)
                v = int.from_bytes(d, "little", signed=True) if d else -1
                if 0 <= v <= 200:
                    return v
        except Exception:
            pass
        return None

    def _bag_box(self):
        sc = self.rect[2] / 1280.0
        bx = self.cfg["calib"].get("bag_box") or [820, 270, 1225, 640]
        return [v * sc for v in bx]

    def _dbl(self, x, y):
        winapi.move(*self.abs_pt(x, y))
        time.sleep(0.25)
        for _ in range(2):
            winapi.left_down()
            time.sleep(0.03)
            winapi.left_up()
            time.sleep(0.14)
        time.sleep(0.5)

    def _pot_key(self):
        """Poción de vida con la tecla del juego (1), sin abrir el bolso. Si la vida no sube, no era esa tecla
        en este server (o no hay pociones): dejo de probar un rato y vuelvo al método del bolso."""
        key = self.cfg.get("potion_key", "1")
        if not key or time.time() < getattr(self, "_potkey_off", 0):
            return False
        hp0 = self._hp_mem()
        if hp0 is None:
            return False
        winapi.press(str(key))
        t0 = time.time()
        while time.time() - t0 < 1.6:
            time.sleep(0.2)
            hp = self._hp_mem()
            if hp is not None and hp >= hp0 + 8:
                if not getattr(self, "_potkey_ok", False):
                    self._potkey_ok = True
                    self.log(f"🧪 Las pociones andan con la tecla {key}: ya no abro el bolso para tomarlas.")
                self._potkey_fail = 0
                self.stats["hp_pots"] = self.stats.get("hp_pots", 0) + 1
                lf = dict(getattr(self, "_left", {}))
                if "pot" in lf:
                    lf["pot"] = max(0, lf["pot"] - 1)
                    self._left = lf
                self._last_move = time.time()
                return True
        self._potkey_fail = getattr(self, "_potkey_fail", 0) + 1
        if self._potkey_fail >= 2:
            self._potkey_fail = 0
            self._potkey_off = time.time() + (600 if getattr(self, "_potkey_ok", False) else 3600)
        return False

    def _consume(self, kind):
        """Abre el bolso, usa una poción roja ('pot') o come carne ('meat') con doble click y lo cierra."""
        if not self.rect:
            return False
        if kind == "pot" and self._pot_key():
            return True
        no = getattr(self, "_no_item", {})
        if time.time() - no.get(kind, 0) < 300:
            return False
        W, H = self.rect[2], self.rect[3]
        sc = W / 1280.0
        inv = (875 * sc, 918 * sc)
        self._release()
        grab = lambda: self.screen.grab(self.rect)

        def find():
            p, m = vision.find_bag_items(grab(), self._bag_box())
            return (p if kind == "pot" else m), (len(p) + len(m))

        items, total = find()
        opened = False
        if not total:
            winapi.click(*self.abs_pt(*inv))
            opened = True
            time.sleep(0.8)
            items, total = find()
        ok = False
        if items:
            self._dbl(*items[0])
            ok = True
            n = len(items) - 1
            self._left = dict(getattr(self, "_left", {}), **{kind: n})
            self.stats["hp_pots" if kind == "pot" else "food"] = self.stats.get("hp_pots" if kind == "pot" else "food", 0) + 1
            if n <= 2 and time.time() - getattr(self, "_low_warn", 0) > 300:
                self._low_warn = time.time()
                self.log(f"Me quedan {n} {'pociones' if kind == 'pot' else 'carnes'}.")
        else:
            no[kind] = time.time()
            self._no_item = no
            self._left = dict(getattr(self, "_left", {}), **{kind: 0})
            self.log("No me quedan pociones de vida." if kind == "pot" else "No me queda comida.")
        if opened or total:
            winapi.click(*self.abs_pt(*inv))  # cerrar el bolso (tapa parte de la pantalla)
            time.sleep(0.4)
        self._last_move = time.time()
        return ok

    def _lu_mem(self):
        """Puntos de nivel sin repartir (memoria). None si no se sabe."""
        try:
            if self.mem.learned and self.mem.addr[0] == "i16":
                d = self.mem.proc.read(self.mem.addr[1] + 80, 4)
                v = int.from_bytes(d, "little", signed=True) if d else -1
                if 0 <= v <= 600:
                    return v
        except Exception:
            pass
        return None

    def _stats_mem(self):
        """STR/VIT/DEX/INT/MAG/CHR desde la memoria (al lado de la posición)."""
        try:
            if self.mem.learned and self.mem.addr[0] == "i16":
                d = self.mem.proc.read(self.mem.addr[1] + 32, 24)
                v = [int.from_bytes(d[i:i + 4], "little", signed=True) for i in range(0, 24, 4)]
                if all(1 <= x <= 400 for x in v):
                    return dict(zip(("STR", "VIT", "DEX", "INT", "MAG", "CHR"), v))
        except Exception:
            pass
        return {}

    def _drag_real(self, x1, y1, x2, y2):
        """Arrastra con el mouse de verdad (las ventanas del juego no se mueven con clicks en segundo plano)."""
        bg = winapi.BG_HWND
        winapi.set_background(None)
        try:
            winapi.focus(self.hwnd)
            time.sleep(0.6)
            winapi.move(*self.abs_pt(x1, y1))
            time.sleep(0.25)
            winapi.left_down()
            time.sleep(0.25)
            for i in range(1, 16):
                winapi.move(*self.abs_pt(x1 + (x2 - x1) * i // 15, y1 + (y2 - y1) * i // 15))
                time.sleep(0.04)
            time.sleep(0.25)
            winapi.left_up()
            time.sleep(0.6)
        finally:
            winapi.set_background(bg)

    def _drag_bg(self, x1, y1, x2, y2):
        """Arrastra dentro del juego (mensajes a la ventana, sin tocar el mouse real)."""
        winapi.move(*self.abs_pt(x1, y1))
        time.sleep(0.2)
        winapi.left_down()
        time.sleep(0.2)
        for i in range(1, 13):
            winapi.move(*self.abs_pt(x1 + (x2 - x1) * i / 12, y1 + (y2 - y1) * i / 12))
            time.sleep(0.05)
        time.sleep(0.2)
        winapi.left_up()
        time.sleep(0.7)

    def _do_stats(self):
        """Abre 'Level up Setting' (cartel Level Up!), reparte los puntos según el plan y confirma con Ok."""
        if not self.rect:
            return
        W, H = self.rect[2], self.rect[3]
        sc = W / 1280.0
        self._release()
        click = lambda x, y: winapi.click(*self.abs_pt(x, y))
        grab = lambda: self.screen.grab(self.rect)
        img = grab()
        btn = vision.find_stat_buttons(img)
        if not btn:
            hit = vision.find_level_up(img)
            if not hit:
                if self._lu_mem():
                    self.log("Stats: hay puntos para repartir pero no veo ni el cartel ni la ventana.")
                return
            self.log("Vi el cartel 'Level Up!': reparto los puntos.")
            click(hit[0], hit[1])
            time.sleep(1.2)
            img = grab()
            btn = vision.find_stat_buttons(img)
        self._dbg_img("stats_1.png", img)
        if not btn:
            self.log("Stats: toqué 'Level Up!' pero no veo la ventana de puntos.")
            return

        def fits(bt):
            return (len(bt) == 6 and bt[0][1] + 290 * sc < H * 0.90 and bt[0][1] - 190 * sc > 0
                    and bt[0][0] - 300 * sc > 0 and bt[0][0] + 60 * sc < W)

        for _ in range(3):
            if fits(btn):
                break
            # la ventana quedó medio afuera de la pantalla: la arrastro al medio
            x0, y0 = btn[0]
            sx = min(max(x0 - 70 * sc, 8), W - 8)
            self._drag_bg(sx, y0, sx + (700 * sc - x0), 330 * sc)
            btn = vision.find_stat_buttons(grab())
            if not btn:
                break
        if not btn or not fits(btn):
            if getattr(self, "_stats_real_drag", 0) < 2 and btn:
                self._stats_real_drag = getattr(self, "_stats_real_drag", 0) + 1
                x0, y0 = btn[0]
                self.log("Stats: muevo la ventana de puntos con el mouse.")
                self._drag_real(min(max(x0 - 70 * sc, 8), W - 8), y0, 630 * sc, 330 * sc)
                btn = vision.find_stat_buttons(grab())
            if not btn or not fits(btn):
                self._stats_fail = getattr(self, "_stats_fail", 0) + 1
                self.log("Stats: no pude acomodar la ventana de puntos.")
                return
        stats = self._stats_mem()
        lm = self._lu_mem()
        pts = lm if lm is not None else 3
        keys = list(self.LABELS)
        added = {}
        for _ in range(min(max(pts, 0), 60)):
            k = self._next_stat(stats)
            x, y = btn[keys.index(k)]
            click(x, y)
            time.sleep(0.25)
            stats[k] = stats.get(k, 0) + 1
            added[k] = added.get(k, 0) + 1
        click(btn[0][0] - 17 * sc, btn[0][1] + 269 * sc)  # Ok
        time.sleep(0.7)
        self._last_move = time.time()
        self._dbg_img("stats_2.png", grab())
        left = self._lu_mem()
        if added and (left is None or left < pts):
            self._stats_fail = 0
            self.log("Stats: " + ", ".join(f"+{v} {k}" for k, v in added.items()))
        elif added:
            self.log("Stats: toqué los botones pero los puntos no bajaron.")
        self._progress(self._level_mem(), stats)

    def _progress(self, lvl, stats):
        p = os.path.join(cfgmod.DATA, "progreso.json")
        try:
            hist = json.load(open(p, encoding="utf-8"))
        except Exception:
            hist = []
        hist.append({"t": int(time.time()), "level": lvl, "stats": stats, "kills": self.stats["kills"]})
        hist = hist[-2000:]
        try:
            json.dump(hist, open(p, "w", encoding="utf-8"))
        except Exception:
            pass

    # ----------------------------------------------------------------- viaje
    def _same(self, a, b):
        if not a or not b:
            return False
        na = "".join(c for c in a.lower() if c.isalnum())
        nb = "".join(c for c in b.lower() if c.isalnum())
        for suf in ("city",):  # "Aresden City" == "Aresden"
            na, nb = (na[:-len(suf)] if na.endswith(suf) else na), (nb[:-len(suf)] if nb.endswith(suf) else nb)
        return na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= 0.9

    def travel_dest(self):
        t = self.cfg.get("travel", {})
        d = t.get("dest") or "auto"
        if d != "auto":
            return d
        lvl = int(t.get("my_level", 1))
        path = self._load_json("training_path.json")
        phases = [ph for ph in path.get("phases", []) if ph["from"] <= lvl]
        if not phases:
            return None
        cur = self.pos.get("map") or ""
        avoid = self._avoid()
        # el mapa ideal para mi nivel si sé llegar; si no, el de la etapa anterior que conozco
        for i, ph in enumerate(reversed(phases)):
            for name in ph["maps"]:
                n = name.split(" (")[0]
                if self._same(cur, n) or self._known_route(cur, n, avoid):
                    if i > 0:
                        self.nav["want"] = phases[-1]["maps"][0].split(" (")[0]
                    else:
                        self.nav.pop("want", None)
                    return n
        self.nav["want"] = phases[-1]["maps"][0].split(" (")[0]
        return phases[-1]["maps"][0].split(" (")[0]

    def _avoid(self):
        """Mapas que todavía no me conviene pisar según nivel y equipo (se recalcula cada minuto)."""
        now = time.time()
        if now - getattr(self, "_avoid_t", 0) > 60:
            try:
                from . import brain
                pj = self._load_json("pj.json") or self._load_json("pj.default.json")
                lvl = self._level_mem() or int(self.cfg.get("travel", {}).get("my_level", 1))
                self._avoid_v = brain.avoid_maps(self.db, lvl, pj.get("owned", []), self._load_json("tiendas.json"), self.cfg.get("comfort", {}))
            except Exception:
                self._avoid_v = [a.lower() for a in self.cfg.get("travel", {}).get("avoid", [])]
            self._avoid_t = now
        return self._avoid_v

    def _known_route(self, cur, dest, avoid=()):
        if not cur:
            return False
        known = {v["to"] for v in self.world.learn["links"].values()}
        dk = next((k for k in known if self._same(k, dest)), None)
        return bool(dk and self.world.route(cur, dk, forbidden=avoid))

    def _walk_to(self, m, goal, label):
        now = time.time()
        px, py = self.pos["x"], self.pos["y"]
        if (px, py) != self.nav.get("last_pos"):
            self.nav["last_pos"], self.nav["still_since"] = (px, py), now
        if now - self.nav["last_click"] < 0.35:
            return
        if max(abs(goal[0] - px), abs(goal[1] - py)) <= 1 and not m.tele[goal[1], goal[0]]:
            self._release()
            return
        path = m.astar((px, py), goal)
        if not path:
            g2 = m.nearest_walkable(*goal)
            path = m.astar((px, py), g2) if g2 else None
        if not path:
            self.nav["status"] = f"sin camino a {label}"
            self._roam()
            self.nav["last_click"] = now
            return
        vx, vy = self.cfg.get("view_tiles", [7, 5])
        goal_is_door = bool(m.tele[goal[1], goal[0]])
        wp = path[min(1, len(path) - 1)]
        for p in path[1:]:
            if abs(p[0] - px) > vx or abs(p[1] - py) > vy:
                break
            if not (goal_is_door and max(abs(p[0] - goal[0]), abs(p[1] - goal[1])) <= 3) and self._line_hits_door(m, (px, py), p):
                break
            wp = p
        stuck = now - self.nav["still_since"] > 4
        if stuck:
            for _ in range(10):
                wp = (px + random.randint(-3, 3), py + random.randint(-3, 3))
                if m.ok(*wp) and not m.near_tele(wp[0], wp[1], 1):
                    break
            self.nav["still_since"] = now
            self.log("Trabado: pruebo para otro lado.")
        tw, th = self._tile()
        cx, cy = self._char()
        # en Helbreath se camina manteniendo el click: muevo el cursor hacia el próximo punto y lo dejo apretado
        ax, ay = self.abs_pt(cx + (wp[0] - px) * tw, cy + (wp[1] - py) * th)
        if self.mod_held:
            winapi.key_up(self.mod_held)
            self.mod_held = None
        winapi.move(ax, ay)
        if not self.mouse_held:
            winapi.left_down()
            self.mouse_held = True
        self.nav["walking"] = True
        self.nav["last_click"] = now
        self._last_move = now
        self.nav["status"] = f"caminando a {label} ({len(path)} pasos)"
        self.stats["state"] = "viajando"

    def _exit_building(self):
        """Adentro de un edificio: camino derecho a la salida con clicks simples en el piso."""
        cur = self.pos.get("map") or ""
        low = cur.lower()
        if not any(k in low for k in ("shop", "blacksmith", "cityhall", "city hall", "warehouse", "guild", "church", "magic tower", "barrack", "jail", "command hall")):
            return False
        if getattr(self, "mission", None) and self._same(cur, self.mission.get("map", "")):
            return False
        m = self.world.map_for(cur)
        x, y = self.pos.get("x"), self.pos.get("y")
        if not m or x is None:
            return False
        outs = [tuple(t_) for t_, _ in self.world.links_from(cur)] or list(m.teleports)
        if not outs:
            return False
        goal = min(outs, key=lambda t_: abs(t_[0] - x) + abs(t_[1] - y))
        path = m.astar((x, y), goal)
        if not path:
            return False
        wp = path[min(3, len(path) - 1)]
        tw, th = self._tile()
        cx, cy = self._char()
        self._release()
        winapi.move(*self.abs_pt(cx + (wp[0] - x) * tw, cy + (wp[1] - y) * th))
        time.sleep(0.05)
        winapi.left_down()
        time.sleep(0.6)
        winapi.left_up()
        time.sleep(0.1)
        self.nav["status"] = "saliendo del edificio"
        self.stats["state"] = "saliendo"
        self._last_move = time.time()
        return True

    def _travel(self, force_dest=None):
        """True si está viajando (no cazar todavía). force_dest: escapar a ese mapa sin pelear."""
        t = self.cfg.get("travel", {})
        if (not t.get("enabled") and not force_dest) or not self.pos.get("map") or time.time() - self.pos["t"] > 5:
            return False
        if self._exit_building():
            return True
        if force_dest:
            pass
        elif (self.state in ("attack", "loot") or time.time() - getattr(self, "_last_enemy", 0) < 6) and not self._is_travelling():
            return False  # ya estoy en mi mapa de caza: si está peleando o acaba de ver un monstruo, que siga cazando
        elif self._under_attack():
            self._defend_until = time.time() + 8
        if not force_dest and time.time() < getattr(self, "_defend_until", 0):
            self.nav["status"] = "me atacan: peleo primero"
            if self.nav.get("walking"):
                self._release()
                self.nav["walking"] = False
            return False
        dest = force_dest or self.travel_dest()
        self.nav["dest"] = dest
        cur = self.pos["map"]
        m = self.world.map_for(cur)
        if not dest or not m:
            if not m:
                self.nav["status"] = f"no conozco el mapa '{cur}' todavía"
            return False
        if force_dest and self._same(cur, dest):
            self.nav["status"] = f"descansando en {dest}"
            if self.nav.get("walking"):
                self._release()
                self.nav["walking"] = False
            return False
        if self._same(cur, dest):
            anchors = self.world.learn["anchors"]
            key = dest.lower()
            if key not in anchors or (self._near_door(6, anchors[key]) and not self._near_door(6)):
                new = key not in anchors
                anchors[key] = [self.pos["x"], self.pos["y"]]
                self.world.save()
                if new:
                    self.log(f"Llegué a {dest}. Cazo por acá.")
            ax, ay = anchors[key]
            now = time.time()
            idle = now - max(getattr(self, "_last_enemy", 0), getattr(self, "_arrived_t", 0))
            if not getattr(self, "_arrived_t", 0) or getattr(self, "_arrived_map", None) != key:
                self._arrived_t, self._arrived_map = now, key
            want = self.nav.get("want")
            if (want and t.get("explore_portals", False) and idle > self.cfg.get("explore_after_sec", 35)
                    and now - getattr(self, "_portal_explore_t", 0) > 60 * float(t.get("portal_explore_min", 15))):
                tried = {tuple(map(int, k.split('|')[1].split(','))) for k in self.world.learn["links"] if k.startswith(cur.lower() + '|')}
                groups = [g for g in m.portal_groups() if len(g["tiles"]) >= 4
                          and not any(tuple(tl) in tried for tl in g["tiles"])]
                if groups:
                    px, py = self.pos["x"], self.pos["y"]
                    g = min(groups, key=lambda g: abs(g["center"][0] - px) + abs(g["center"][1] - py))
                    self._portal_explore_t = now
                    self._portal_goal = min(g["tiles"], key=lambda p: abs(p[0] - px) + abs(p[1] - py))
                    self._portal_map = cur
                    self.log(f"Busco el camino a {want}: pruebo la salida ({self._portal_goal[0]},{self._portal_goal[1]}).")
            pg = getattr(self, "_portal_goal", None)
            if pg and getattr(self, "_portal_map", None) == cur and now - getattr(self, "_portal_explore_t", 0) < 120:
                self._walk_to(m, pg, f"una salida nueva (buscando {want})")
                return True
            self._portal_goal = None
            # sin monstruos hace rato: salgo a explorar otra parte del mapa
            if idle > self.cfg.get("explore_after_sec", 35):
                goal = getattr(self, "_explore_goal", None)
                if not goal or max(abs(self.pos["x"] - goal[0]), abs(self.pos["y"] - goal[1])) <= 2 or now - getattr(self, "_explore_t", 0) > 60:
                    goal = None
                    for _ in range(60):
                        gx = self.pos["x"] + random.randint(-30, 30)
                        gy = self.pos["y"] + random.randint(-30, 30)
                        if (m.ok(gx, gy) and not m.near_tele(gx, gy, 4) and not self._is_danger(gx, gy)
                                and max(abs(gx - self.pos["x"]), abs(gy - self.pos["y"])) >= 12):
                            goal = (gx, gy)
                            break
                    self._explore_goal, self._explore_t = goal, now
                    if goal:
                        self.log(f"Acá no hay monstruos: exploro hacia ({goal[0]},{goal[1]}).")
                if goal:
                    self._walk_to(m, goal, "otra zona del mapa")
                    return True
            far = max(abs(self.pos["x"] - ax), abs(self.pos["y"] - ay)) > self.cfg.get("hunt_radius_tiles", 25)
            if self._is_danger(self.pos["x"], self.pos["y"]) and not self._is_danger(ax, ay):
                self._walk_to(m, (ax, ay), "fuera de la zona peligrosa")
                return True
            if far and idle > 10:
                self._walk_to(m, (ax, ay), "la zona de caza")
                return True
            self.nav["status"] = f"cazando en {dest}"
            if self.state != "attack" and self.mouse_held and self.nav.get("walking"):
                self._release()
            self.nav["walking"] = False
            return False
        avoid = self._avoid()
        # nombre "real" del destino según lo aprendido
        known = {v["to"] for _, v in [(k, v) for k, v in self.world.learn["links"].items()]}
        dest_key = next((k for k in known if self._same(k, dest)), dest.lower())
        route = self.world.route(cur, dest_key, forbidden=avoid)
        home = t.get("home", "aresden")
        if not route and not self._same(cur, home):
            # no sé llegar al destino: primero voy a mi ciudad y exploro desde ahí
            route = self.world.route(cur, home, forbidden=avoid)
        if route:
            _, tile = route[0]
            self._walk_to(m, tile, f"portal hacia {dest}")
            return True
        outs = self.world.links_from(cur)
        if not outs and m.w * m.h <= 120 * 120 and m.teleports:
            # edificio sin salida conocida: voy a la puerta más cercana
            px, py = self.pos["x"], self.pos["y"]
            tl = min(m.teleports, key=lambda t_: abs(t_[0] - px) + abs(t_[1] - py))
            self._walk_to(m, tl, "la salida del edificio")
            return True
        if outs and m.w * m.h <= 120 * 120:
            # estoy adentro de un edificio y no sé llegar: salgo por donde entré
            tile, v = outs[0]
            self._walk_to(m, tile, f"la salida ({v['to']})")
            return True
        if not t.get("explore_portals", False):
            self.nav["status"] = f"no sé cómo llegar a {dest}: cazo acá"
            return False
        tried = {tuple(map(int, k.split('|')[1].split(','))) for k in self.world.learn["links"] if k.startswith(cur.lower() + '|')}
        groups = [g for g in m.portal_groups() if not any(tuple(tl) in tried for tl in g["tiles"])]
        if not groups:
            self.nav["status"] = f"no encuentro el camino a {dest}"
            return False
        px, py = self.pos["x"], self.pos["y"]
        g = min(groups, key=lambda g: abs(g["center"][0] - px) + abs(g["center"][1] - py))
        tile = min(g["tiles"], key=lambda p: abs(p[0] - px) + abs(p[1] - py))
        self._walk_to(m, tile, "un portal nuevo (explorando)")
        return True

    # ----------------------------------------------------------------- auto
    def _excluded(self, x, y):
        for r in self.cfg["calib"].get("exclude") or []:
            if r["x1"] <= x <= r["x2"] and r["y1"] <= y <= r["y2"]:
                return True
        return False

    def _auto_candidates(self):
        """Busca cosas que se mueven alrededor (monstruos). Coordenadas de la ventana."""
        roi = self._game_roi()
        sc = 0.5
        cur = vision.small_gray(self.img, roi, sc)
        now = time.time()
        if now - self._last_move < self.cfg.get("settle_time", 0.9):
            self._prev_small, self._prev_t = cur, now
            return None
        if self._prev_small is None or now - self._prev_t < 0.25:
            if self._prev_small is None:
                self._prev_small, self._prev_t = cur, now
            return None
        tw, th = self._tile()
        min_area = int((tw * sc) * (th * sc) * self.cfg.get("blob_min_tiles", 0.12))
        max_area = int((tw * sc) * (th * sc) * self.cfg.get("blob_max_tiles", 4.0))
        blobs = vision.motion_blobs(self._prev_small, cur, self.cfg.get("motion_thresh", 22), min_area, max_area)
        if self.cfg.get("debug") and now - getattr(self, "_dbg_m", 0) > 5:
            self._dbg_m = now
            try:
                import cv2
                d = os.path.join(cfgmod.DATA, "debug")
                os.makedirs(d, exist_ok=True)
                diff = cv2.absdiff(self._prev_small, cur)
                cv2.imwrite(os.path.join(d, "mov_a.png"), self._prev_small)
                cv2.imwrite(os.path.join(d, "mov_b.png"), cur)
                cv2.imwrite(os.path.join(d, "mov_diff.png"), cv2.normalize(diff, None, 0, 255, cv2.NORM_MINMAX))
                cv2.imwrite(os.path.join(d, "frame.png"), self.img)
                self.log(f"debug: cambio medio entre cuadros {float(diff.mean()):.2f}, máx {int(diff.max())}, manchas {'pantalla movida' if blobs is None else len(blobs)}")
            except Exception as e:
                self.log(f"debug: {e!r}")
        self._prev_small, self._prev_t = cur, now
        if blobs is None:
            self._last_move = now  # se movió la pantalla
            return None
        cx, cy = self._char()
        out = []
        for b in blobs:
            x = roi["x1"] + b["x"] / sc
            y = roi["y1"] + b["y"] / sc
            d = self._dist_tiles(x, y)
            if d < self.cfg.get("self_radius_tiles", 0.8) or d > self.cfg["max_target_dist_tiles"]:
                continue  # soy yo, o está muy lejos
            if self._excluded(x, y):
                continue
            m = {"name": "monstruo", "x": int(x), "y": int(y), "w": int(b["w"] / sc), "h": int(b["h"] / sc), "score": 1.0}
            if self._blacklisted(m):
                continue
            out.append((d, m))
        out.sort(key=lambda t: t[0])
        return [m for _, m in out]

    def _reacquire(self, t):
        """El cartel desapareció: paso el cursor alrededor de donde estaba el monstruo."""
        tw, th = self._tile()
        self._release()
        before = self.screen.grab(self.rect)
        for dx, dy in [(0, 0), (0.5, 0), (-0.5, 0), (0, 0.5), (0, -0.5), (0.5, 0.5), (-0.5, 0.5), (0.5, -0.5), (-0.5, -0.5), (1, 0), (-1, 0), (0, 1), (0, -1)]:
            x, y = int(t["x"] + dx * tw), int(t["y"] + dy * th)
            if self._enemy_at(x, y, None):
                return (x, y)
        return None

    def _enemy_at(self, x, y, before=None):
        """Pongo el cursor en (x, y): ¿aparece '(Enemy)'?"""
        winapi.move(*self.abs_pt(x, y))
        time.sleep(self.cfg.get("hover_wait", 0.09))
        after = self.screen.grab(self.rect)
        if after is None:
            return False
        n = vision.enemy_pixels(after, x, y)
        base = vision.enemy_pixels(before, x, y) if before is not None else 0
        if self.cfg.get("debug") and n:
            self.log(f"debug: cartel enemigo {n}px en {x},{y}")
        self.img = after
        return n - base >= self.cfg.get("enemy_min_pixels", 18)

    def _hover_ok(self, m):
        if not self.cfg.get("hover_confirm", True):
            return True
        return self._enemy_at(m["x"], m["y"], self.img)

    def _tile_of(self, sx, sy):
        tw, th = self._tile()
        cx, cy = self._char()
        if self.pos.get("x") is None:
            return None
        return (self.pos["x"] + int(round((sx - cx) / tw)), self.pos["y"] + int(round((sy - cy) / th)))

    @staticmethod
    def _line_hits_door(m, a, b):
        """El pj camina en línea recta hacia el cursor: ¿esa línea pasa al lado de una puerta?"""
        n = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
        for i in range(1, n + 1):
            x = a[0] + round((b[0] - a[0]) * i / n)
            y = a[1] + round((b[1] - a[1]) * i / n)
            if max(abs(x - a[0]), abs(y - a[1])) > 2 and m.near_tele(x, y, 1):
                return True
        return False

    def _near_door(self, r=2, tile=None):
        m = self.world.map_for(self.pos["map"]) if self.pos.get("map") else None
        if not m:
            return False
        t = tile or (self.pos.get("x"), self.pos.get("y"))
        if t[0] is None:
            return False
        return m.near_tele(int(t[0]), int(t[1]), r)

    def _engage(self, hit):
        t = self._tile_of(*hit)
        if t and self._near_door(2, t):
            self._door_skip = getattr(self, "_door_skip", 0) + 1
            return
        tw, th = self._tile()
        m = {"name": "monstruo", "x": hit[0], "y": hit[1], "w": tw, "h": th, "score": 1.0}
        self._seen_alive = True
        self._track = vision.crop_around(self.img, m["x"], m["y"], int(max(tw, th) * 1.1))
        self.target, self.state = m, "attack"
        self._last_enemy = time.time()
        self.target_since = self.last_seen_target = self.last_target_found = time.time()
        self.stats["state"], self.stats["target"] = "atacando", "monstruo"
        self._hold_on(m["x"], m["y"])
        self.last_attack_click = time.time()

    def _scan_ring(self):
        """Paso el cursor en anillos alrededor del PJ buscando '(Enemy)'. Devuelve (x, y) o None."""
        tw, th = self._tile()
        cx, cy = self._char()
        before = self.img
        pts = []
        for r in self.cfg.get("scan_rings", [1.0, 1.6, 2.3, 3.0]):
            n = max(8, int(r * 8))
            for i in range(n):
                a = 2 * math.pi * i / n + (r * 0.37)
                pts.append((int(cx + math.cos(a) * r * tw), int(cy - th * 0.3 + math.sin(a) * r * th)))
        roi = self._game_roi()
        for x, y in pts:
            if not (roi["x1"] < x < roi["x2"] and roi["y1"] < y < roi["y2"]) or self._excluded(x, y):
                continue
            m = {"x": x, "y": y, "w": tw, "h": th}
            if self._blacklisted(m):
                continue
            if self._enemy_at(x, y, before):
                return (x, y)
        return None

    def _is_travelling(self):
        """True si tengo que ir a otro mapa y sé el camino (se recalcula cada 2 s)."""
        now = time.time()
        if now - getattr(self, "_trav_t", 0) > 2:
            self._trav_t = now
            v = False
            try:
                t = self.cfg.get("travel", {})
                cur = self.pos.get("map") or ""
                if t.get("enabled") and cur:
                    dest = self.travel_dest()
                    v = bool(dest and not self._same(cur, dest) and self._known_route(cur, dest, self._avoid()))
            except Exception:
                v = False
            self._trav_v = v
        return getattr(self, "_trav_v", False)

    def _attacked_recently(self):
        """Me están pegando (o me pegaron hace menos de 8 s): ahí sí corresponde frenar y pelear."""
        if self._under_attack():
            self._defend_until = time.time() + 8
        return time.time() < getattr(self, "_defend_until", 0)

    def _under_attack(self):
        """True si la vida bajó en los últimos 3 segundos."""
        h = getattr(self, "_hp_hist", [])
        if len(h) < 2:
            return False
        recent = [v for t, v in h if time.time() - t < 3]
        return bool(recent) and max(recent) - recent[-1] >= 2

    def _defend(self):
        """Me pegan y no veo a quién: pego alrededor (las 8 casillas)."""
        self._release()
        self.stats["state"], self.stats["target"] = "defendiéndome", ""
        for _ in range(4):
            self._pit()
        self._last_move = time.time()

    # ---------------------------------------------------- lugares de caza
    def _spots(self):
        mk = (self.pos.get("map") or "").strip().lower()
        return self.world.learn.setdefault("spots", {}).setdefault(mk, [])

    def _spot_kill(self, w=1):
        """Anota dónde maté: así aprende dónde salen los bichos (los 'pits')."""
        x, y = self.pos.get("x"), self.pos.get("y")
        if x is None or self._near_door(6) or self._is_danger(x, y):
            return
        sp = self._spots()
        for s_ in sp:
            if max(abs(s_[0] - x), abs(s_[1] - y)) <= 8:
                s_[2] += w
                s_[0] = int(round((s_[0] * 3 + x) / 4))
                s_[1] = int(round((s_[1] * 3 + y) / 4))
                break
        else:
            sp.append([x, y, w])
        if self.stats["kills"] % 5 == 0:
            self.world.save()

    def _goto_spot(self):
        """Sin bichos a la vista: voy al mejor lugar de caza conocido y los voy rotando."""
        x, y = self.pos.get("x"), self.pos.get("y")
        mp = self.world.map_for(self.pos["map"]) if self.pos.get("map") else None
        if x is None or not mp or time.time() - self.pos.get("t", 0) > 5:
            return False
        dest = self.travel_dest()
        if dest and not self._same(self.pos["map"], dest):
            return False  # todavía estoy viajando a otro mapa
        now = time.time()
        vis = getattr(self, "_spot_vis", {})
        sp = [s_ for s_ in self._spots() if s_[2] >= 2 and not self._is_danger(s_[0], s_[1])]
        for s_ in sp:
            if max(abs(s_[0] - x), abs(s_[1] - y)) <= 4:
                vis[(s_[0] // 8, s_[1] // 8)] = now  # ya estoy acá y no hay nada
        self._spot_vis = vis
        todo = [s_ for s_ in sp if now - vis.get((s_[0] // 8, s_[1] // 8), 0) > self.cfg.get("spot_revisit_sec", 75)]
        if not todo:
            return False
        # el más rendidor, penalizando la distancia
        best = max(todo, key=lambda s_: s_[2] - 0.15 * max(abs(s_[0] - x), abs(s_[1] - y)))
        self._walk_to(mp, (best[0], best[1]), f"lugar de caza ({best[0]},{best[1]})")
        self.stats["state"] = "yendo a cazar"
        return True

    # ------------------------------------------------------------- compras
    def _park_shop_dialog(self, img=None):
        """La lista de venta no se puede cerrar y tapa el camino: la corro a la esquina de abajo a la derecha."""
        try:
            sc = self.rect[2] / 1280.0
            h_ = vision.find_shop_header(img if img is not None else self.screen.grab(self.rect), thr=0.7)
            if not h_:
                return False
            x, y = h_[0] / sc, h_[1] / sc
            if x > 1050 and y > 760:
                return False  # ya está en la esquina
            gx, gy = x - 92, y
            self._release()
            self._drag_bg(gx * sc, gy * sc, (gx + 1170 - x) * sc, (gy + 835 - y) * sc)
            return True
        except Exception:
            return False

    def _maybe_shop(self):
        lf = getattr(self, "_left", {})
        need = lf.get("meat", 99) <= self.cfg.get("buy_meat_below", 1) or lf.get("pot", 99) <= self.cfg.get("buy_pots_below", 3)
        if not need or getattr(self, "mission", None):
            return
        for a_ in ("_shop_th", "_rep_th", "_inv_th"):
            th = getattr(self, a_, None)
            if th is not None and th.is_alive():
                return
        if time.time() - getattr(self, "_shop_last", 0) < 900:
            return
        if not self._same(self.pos.get("map") or "", "Aresden Farm"):
            return
        self._shop_last = time.time()
        self._shop_th = threading.Thread(target=self._shop_trip, daemon=True)
        self._shop_th.start()

    def _gold_mem(self):
        """Oro del pj desde la memoria (config memory.gold_off = distancia desde la posición). None si no se sabe."""
        try:
            off = self.cfg.get("memory", {}).get("gold_off")
            if off is not None and self.mem.learned and self.mem.addr[0] == "i16":
                d = self.mem.proc.read(self.mem.addr[1] + int(off), 4)
                v = int.from_bytes(d, "little", signed=True) if d else -1
                if 0 <= v <= 500_000_000:
                    return v
        except Exception:
            pass
        return None

    def _brain_step(self):
        """Cada tanto arma el plan (dónde cazar, qué comprar, para qué juntar oro) y lo anota cuando cambia."""
        now = time.time()
        if now - getattr(self, "_brain_t", 0) < 60:
            return
        self._brain_t = now
        try:
            from . import brain
            cat = self._load_json("tiendas.json")
            pj = self._load_json("pj.json") or self._load_json("pj.default.json")
            lvl = self._level_mem() or int(self.cfg.get("travel", {}).get("my_level", 1))
            cur = self.pos.get("map") or ""
            avoid = self._avoid()
            reach = lambda n: self._same(cur, n) or self._known_route(cur, n, avoid)
            p = brain.plan(self.db, self._load_json("training_path.json"), cat, lvl, self._stats_mem(),
                           self._gold_mem(), pj.get("owned", []), reach, cur)
            self.plan = p
            key = "|".join(p["lines"][:-1])
            if key != getattr(self, "_brain_key", None):
                self._brain_key = key
                for ln in p["lines"]:
                    self.log("🧭 " + ln)
        except Exception as ex:
            if not getattr(self, "_brain_err", False):
                self._brain_err = True
                self.log(f"🧭 Error armando el plan: {ex!r}")

    def _maybe_repair(self):
        """El equipo se gasta y cuando se rompe se desequipa solo (queda pegando a mano): voy al herrero cada tanto,
        o enseguida si de golpe dejo de matar."""
        if getattr(self, "mission", None) or getattr(self, "_busy_ui", False):
            return
        for a in ("_shop_th", "_rep_th", "_inv_th"):
            th = getattr(self, a, None)
            if th is not None and th.is_alive():
                return
        if not self._same(self.pos.get("map") or "", "Aresden Farm"):
            return
        now = time.time()
        if not hasattr(self, "_rep_last"):
            self._rep_last = now - self.cfg.get("repair_every_min", 60) * 60 + 600  # primera pasada a los 10 min
        stalled = len([t for t in getattr(self, "_nokill", []) if now - t < 240]) >= 5 and now - getattr(self, "_last_kill_t", 0) > 150
        due = now - self._rep_last > self.cfg.get("repair_every_min", 60) * 60
        if not (due or (stalled and now - self._rep_last > 600)):
            return
        self._rep_last = now
        self._nokill = []
        self._rep_th = threading.Thread(target=self._repair_trip, args=(stalled,), daemon=True)
        self._rep_th.start()

    def _repair_trip(self, reequip=False):
        """Va al Blacksmith (puerta 75,86 de la Farm), BlackSmith Keeper -> Repair All -> Repair, y vuelve a cazar."""
        try:
            sc = self.rect[2] / 1280.0
            P = lambda x, y: self.abs_pt(x * sc, y * sc)
            self.log("🔧 Voy al herrero a reparar el equipo." + (" (dejé de matar: capaz se rompió el arma)" if reequip else ""))
            self.mission_result = None
            self.mission = {"map": "Aresden Farm", "goal": [75, 86], "label": "Blacksmith (reparar)", "until": time.time() + 480}
            t0 = time.time()
            inside = False
            while time.time() - t0 < 480 and self.alive and self.running:
                if "smith" in (self.pos.get("map") or "").lower():
                    inside = True
                    break
                time.sleep(0.1)
            if not inside:
                self.log("🔧 No llegué al herrero.")
                self._rep_last = time.time() - self.cfg.get("repair_every_min", 60) * 60 + 600
                return
            self._busy_ui = True
            self._release()
            time.sleep(1.5)
            # el herrero está fijo al fondo: camino hasta (47,35) y queda en pantalla en (745,345)
            for _ in range(14):
                if not (self.alive and self.running):
                    return
                if abs((self.pos.get("x") or 0) - 47) <= 1 and abs((self.pos.get("y") or 0) - 35) <= 1:
                    break
                winapi.click(*P(1000, 470))
                time.sleep(0.7)
            time.sleep(1.0)
            if not (abs((self.pos.get("x") or 0) - 47) <= 1 and abs((self.pos.get("y") or 0) - 35) <= 1):
                self.log(f"🔧 No pude acercarme al herrero (quedé en {self.pos.get('x')},{self.pos.get('y')}).")
                self._rep_last = time.time() - self.cfg.get("repair_every_min", 60) * 60 + 600
                return
            dx, dy = (self.pos.get("x") - 47) * 64, (self.pos.get("y") - 35) * 64
            winapi.click(*P(745 - dx, 345 - dy))
            time.sleep(2.5)
            winapi.click(*P(845 - dx, 312 - dy))  # Repair All
            time.sleep(2.0)
            winapi.click(*P(902, 574))  # Repair
            time.sleep(1.5)
            winapi.press("ESC")
            time.sleep(0.5)
            self.log("🔧 Equipo reparado.")
            self._want_equip = bool(reequip)
        except Exception as e:
            self.log(f"🔧 Error reparando: {e!r}")
        finally:
            self._busy_ui = False
        if getattr(self, "_want_equip", False):
            self._want_equip = False
            t0 = time.time()
            while time.time() - t0 < 120 and self.alive and self.running:
                if self._same(self.pos.get("map") or "", "Aresden Farm"):
                    break
                time.sleep(0.2)
            else:
                return
            time.sleep(2.5)
            self._reequip()

    def _reequip(self):
        """Se vuelve a poner el equipo arrastrando cada cosa del bolso al muñeco (posiciones guardadas en config)."""
        try:
            sc = self.rect[2] / 1280.0
            P = lambda x, y: self.abs_pt(x * sc, y * sc)
            self._busy_ui = True
            self._release()
            time.sleep(1.0)
            pots, meats = vision.find_bag_items(self.screen.grab(self.rect), self._bag_box())
            if not (pots or meats):
                winapi.click(*P(875, 918))
                time.sleep(1.2)
            winapi.click(*P(816, 918))
            time.sleep(1.2)
            for x, y in self.cfg.get("equip_slots", [[1065, 405], [1108, 392], [1090, 378], [1122, 385], [1095, 390], [978, 462]]):
                self._drag_bg(x * sc, y * sc, 155 * sc, 345 * sc)
                time.sleep(0.9)
            winapi.click(*P(816, 918))
            time.sleep(0.6)
            winapi.click(*P(875, 918))
            time.sleep(0.6)
            self.log("🛡 Me volví a poner el equipo.")
        except Exception as e:
            self.log(f"🛡 Error equipando: {e!r}")
        finally:
            self._busy_ui = False

    def _shop_trip(self):
        """Va a la Shop de Aresden, compra carne y pociones de vida y vuelve a cazar."""
        try:
            sc = self.rect[2] / 1280.0
            P = lambda x, y: self.abs_pt(x * sc, y * sc)
            self.log("🛒 Me quedan pocas provisiones: voy a la Shop.")
            self.mission_result = None
            self.mission = {"map": "Aresden Farm", "goal": [63, 70], "label": "Shop (provisiones)", "until": time.time() + 480}
            t0 = time.time()
            inside = False
            while time.time() - t0 < 480 and self.alive and self.running:
                if "shop" in (self.pos.get("map") or "").lower():
                    inside = True
                    break
                time.sleep(0.1)
            if not inside:
                self.log("🛒 No llegué a la Shop.")
                return
            self._busy_ui = True
            self._release()
            time.sleep(1.5)
            c = self.cfg.get("shop", {})
            # el vendedor camina: lo busco pasando el mouse hasta que aparezca el cartel azul "Neutral"
            def find_npc():
                W0, H0 = 1280, 960
                pts = [(x, y) for y in range(420, 720, 48) for x in range(700, 1250, 40)]
                pts += [(x, y) for y in range(120, 800, 56) for x in range(60, 1240, 44) if not (x >= 700 and 420 <= y < 720)]
                base = vision.neutral_pixels(self.screen.grab(self.rect), 0, 0)
                for x, y in pts:
                    if not (self.alive and self.running):
                        return None
                    winapi.move(*P(x, y))
                    time.sleep(0.11)
                    img = self.screen.grab(self.rect)
                    if vision.neutral_pixels(img, x * sc, y * sc) >= 25:
                        return [x, y]
                return None

            npc = find_npc()
            if npc:
                before = (self.pos.get("x"), self.pos.get("y"))
                winapi.click(*P(*npc))
                time.sleep(2.2)
                if (self.pos.get("x"), self.pos.get("y")) != before:
                    time.sleep(2.0)
                    npc = find_npc()
                    if npc:
                        winapi.click(*P(*npc))
                        time.sleep(2.2)
            if not npc:
                self.log("🛒 No encontré al vendedor.")
                self._shop_last = time.time() - 300
                return
            def shot(n):
                try:
                    import cv2
                    d = os.path.join(cfgmod.DATA, "debug")
                    os.makedirs(d, exist_ok=True)
                    cv2.imwrite(os.path.join(d, f"shop_{n}.jpg"), self.screen.grab(self.rect), [cv2.IMWRITE_JPEG_QUALITY, 70])
                except Exception:
                    pass

            shot(0)
            self._shop_dbg = {"npc": npc, "pos": [self.pos.get("x"), self.pos.get("y")]}
            # abrir Trade y acomodar la lista arriba a la izquierda (la ventana recuerda dónde quedó)
            winapi.click(*P(npc[0] - 122, npc[1] + 18))
            time.sleep(1.8)
            shot(1)
            area = {"x1": 0, "y1": 0, "x2": self.rect[2], "y2": self.rect[3]}

            def price_word():
                for _ in range(3):
                    h_ = vision.find_shop_header(self.screen.grab(self.rect))
                    if h_:
                        return h_[0] / sc, h_[1] / sc
                    time.sleep(0.5)
                return None

            pw = price_word()
            if not pw:
                self.log("🛒 No veo la lista de venta. Pruebo más tarde.")
                self._shop_last = time.time() - 300
                return
            for _ in range(2):
                if abs(pw[0] - 435) <= 12 and abs(pw[1] - 159) <= 12:
                    break
                gx, gy = pw[0] - 92, pw[1]  # zona vacía del encabezado, entre ITEM y PRICE
                self._drag_bg(gx * sc, gy * sc, (gx + 435 - pw[0]) * sc, (gy + 159 - pw[1]) * sc)
                pw = price_word() or pw
            if abs(pw[0] - 435) > 14 or abs(pw[1] - 159) > 14:
                self.log(f"🛒 No pude acomodar la lista de venta (quedó en {int(pw[0])},{int(pw[1])}).")
                self._shop_last = time.time() - 300
                return
            shot(2)
            steps = []
            meat_n, pot_n = int(c.get("meat_tens", 1)), int(c.get("pot_tens", 2))
            steps += [(c.get("meat_row", [272, 421]), 1.2)] + [([361, 431], 0.35)] * meat_n + [([220, 561], 1.4)]
            steps += [(c.get("pot_row", [272, 191]), 1.2)] + [([361, 431], 0.35)] * pot_n + [([220, 561], 1.4)]
            p_before = (self.pos.get("x"), self.pos.get("y"))
            for i, ((x, y), w) in enumerate(steps):
                if not (self.alive and self.running):
                    break
                winapi.move(*P(x, y))
                time.sleep(0.15)
                winapi.click(*P(x, y))
                time.sleep(w)
                if (self.pos.get("x"), self.pos.get("y")) != p_before:
                    self.log("🛒 Algo salió mal comprando (el pj caminó). Pruebo más tarde.")
                    self._shop_last = time.time() - 300
                    return
            # verifico abriendo el bolso
            inv = P(875, 918)
            winapi.click(*inv)
            time.sleep(0.9)
            pots, meats = vision.find_bag_items(self.screen.grab(self.rect), self._bag_box())
            winapi.click(*inv)
            time.sleep(0.5)
            self._left = {"pot": len(pots), "meat": len(meats)}
            self._no_item = {}
            self.log(f"🛒 Después de comprar tengo {len(meats)} carnes y {len(pots)} pociones.")
        except Exception as e:
            self.log(f"🛒 Error comprando: {e!r}")
        finally:
            self._park_shop_dialog()
            self._busy_ui = False

    def _mission_step(self):
        """Encargo puntual: ir a una baldosa de este mapa (una salida, una tienda). Termina al cambiar de mapa o llegar."""
        ms = getattr(self, "mission", None)
        if not ms:
            return False
        cur = (self.pos.get("map") or "")
        if time.time() > ms.get("until", 0):
            self.log(f"Encargo vencido: {ms.get('label')}.")
            self.mission = None
            return False
        if not self._same(cur, ms["map"]):
            self.log(f"Encargo: {ms.get('label')} → llegué a {cur} ({self.pos.get('x')},{self.pos.get('y')}).")
            self._release()
            self.mission_result = {"map": cur, "x": self.pos.get("x"), "y": self.pos.get("y"), "label": ms.get("label")}
            self.mission = None
            return False
        if self.state != "seek":
            self._release()
            self.state, self.target = "seek", None
        mp = self.world.map_for(cur)
        if not mp or self.pos.get("x") is None or time.time() - self.pos.get("t", 0) > 5:
            return True
        g = tuple(ms["goal"])
        if max(abs(g[0] - self.pos["x"]), abs(g[1] - self.pos["y"])) <= 1 and not mp.tele[g[1], g[0]]:
            self._release()
            self.log(f"Encargo: llegué a {g}.")
            self.mission_result = {"map": cur, "x": self.pos["x"], "y": self.pos["y"], "label": ms.get("label"), "arrived": True}
            self.mission = None
            return False
        self._walk_to(mp, g, ms.get("label", "encargo"))
        self.stats["state"] = "encargo"
        return True

    def _flee_mem(self):
        """Me alejo de los bichos y descanso donde no haya ninguno cerca."""
        ents = self.ents.read(self.mem.proc)
        x, y = self.pos.get("x"), self.pos.get("y")
        mp = self.world.map_for(self.pos["map"]) if self.pos.get("map") else None
        if ents is None or x is None or not mp:
            return False
        mons = [e for e in ents if e[2].startswith("_") and e[3] == 0 and max(abs(e[0]), abs(e[1])) <= 9]
        if not mons:
            self._release()
            self.stats["state"] = "descansando"
            return True
        ax = sum(e[0] for e in mons) / len(mons)
        ay = sum(e[1] for e in mons) / len(mons)
        n = math.hypot(ax, ay) or 1.0
        best = None
        for d in (14, 10, 18):
            for ang in (0, 0.6, -0.6, 1.2, -1.2):
                ca, sa_ = math.cos(ang), math.sin(ang)
                ux, uy = -(ax * ca - ay * sa_) / n, -(ax * sa_ + ay * ca) / n
                g = (int(x + ux * d), int(y + uy * d))
                if mp.ok(*g) and not mp.near_tele(g[0], g[1], 3):
                    best = g
                    break
            if best:
                break
        if not best:
            return False
        self._walk_to(mp, best, "lejos de los bichos")
        self.stats["state"] = "escapando"
        return True

    def _level_mem(self):
        try:
            if self.mem.learned and self.mem.addr[0] == "i16":
                d = self.mem.proc.read(self.mem.addr[1] + 72, 4)
                v = int.from_bytes(d, "little", signed=True) if d else 0
                if 1 <= v <= 180:
                    return v
        except Exception:
            pass
        return None

    def _mon_ok(self, e):
        """¿Le puedo pegar a este bicho? (no a los que ya me hicieron huir con este nivel)"""
        hard = self.world.learn.get("hard_types", {})
        lv = hard.get(str(e[5]))
        return lv is None or int(self.cfg.get("travel", {}).get("my_level", 1)) >= lv + 5

    def _mem_near(self):
        ents = self.ents.read(self.mem.proc)
        if not ents:
            return False
        now = time.time()
        px, py = self.pos.get("x"), self.pos.get("y")
        for e in ents:
            if e[2].startswith("_") and e[3] == 0 and abs(e[0]) <= 9 and abs(e[1]) <= 6 and self._ent_bl.get(e[2], 0) < now and self._mon_ok(e):
                if px is None or not self._near_door(2, (px + e[0], py + e[1])):
                    return True
        return False

    def _auto_mem(self):
        """Caza usando la lista de bichos de la memoria. None = no pude leer (usar el método de pantalla)."""
        ents = self.ents.read(self.mem.proc)
        if ents is None:
            return None
        now = time.time()
        tw, th = self._tile()
        cx, cy = self._char()
        self._ent_bl = {k: v for k, v in self._ent_bl.items() if v > now}
        lv = self._level_mem()
        if lv and lv != int(self.cfg.get("travel", {}).get("my_level", 0)):
            self.cfg.setdefault("travel", {})["my_level"] = lv
            cfgmod.save(self.cfg)
        mons = [e for e in ents if e[2].startswith("_") and e[3] == 0 and self._mon_ok(e)]
        seen = self.world.learn.setdefault("types_seen", {})
        for e in ents:
            if e[2].startswith("_") and str(e[5]) not in seen:
                seen[str(e[5])] = {"kind": e[3]}
                self.log(f"Bicho nuevo: tipo {e[5]} (clase {e[3]}).")
        self._last_mons = [{"name": e[2], "x": int(cx + e[0] * tw), "y": int(cy + e[1] * th), "w": tw, "h": th, "score": 1.0}
                           for e in mons if abs(e[0]) <= 10 and abs(e[1]) <= 7]
        self.stats["seen"] = len(mons)
        t = self.target
        if self.state == "attack" and t and t.get("ename"):
            e = next((e for e in mons if e[2] == t["ename"]), None)
            if e is None:
                self._release()
                self.stats["kills"] += 1
                self.log(f"✝ Monstruo muerto (kill #{self.stats['kills']}).")
                self._last_kill_t = time.time()
                if self.pos.get("map") and self.pos.get("x") is not None and not self._near_door(6) \
                        and not self._is_danger(self.pos["x"], self.pos["y"]):
                    self.world.learn["anchors"][self.pos["map"].strip().lower()] = [self.pos["x"], self.pos["y"]]
                    if self.stats["kills"] % 5 == 0:
                        self.world.save()
                self._last_enemy = now
                self._spot_kill(1 if t.get("etype") in set(self.cfg.get("easy_types", [240])) else 3)
                self.loot_pos, self.loot_at = (t["x"], t["y"]), now + self.cfg.get("loot_delay", 2.0)
                self.state = "loot"
                self.stats["state"], self.stats["target"] = "levantando", ""
                return True
            if now - self.target_since > self.cfg.get("mem_target_timeout", 25) or max(abs(e[0]), abs(e[1])) > 12:
                self._ent_bl[t["ename"]] = now + 20
                self._release()
                self.state, self.target = "seek", None
                self.log("No muere o se fue lejos: cambio de objetivo.")
                self._nokill = [t for t in getattr(self, "_nokill", []) if time.time() - t < 240] + [time.time()]
                return True
            t["x"], t["y"] = int(cx + e[0] * tw), int(cy + e[1] * th - th * 0.3)
            self._last_enemy = now
            self._hold_on(t["x"], t["y"])
            self.last_attack_click = now
            return True
        if self.state == "attack":
            self._release()
            self.state, self.target = "seek", None
        px, py = self.pos.get("x"), self.pos.get("y")
        cands = []
        for e in mons:
            if e[2] in self._ent_bl:
                continue
            if px is not None and self._near_door(2, (px + e[0], py + e[1])):
                continue
            cands.append((max(abs(e[0]), abs(e[1])), e))
        easy = set(self.cfg.get("easy_types", [240]))  # slimes: solo si no hay otra cosa cerca
        cands.sort(key=lambda c: c[0] + (5 if c[1][5] in easy and int(self.cfg.get("travel", {}).get("my_level", 1)) >= 15 else 0))
        near = [c for c in cands if abs(c[1][0]) <= 9 and abs(c[1][1]) <= 6]
        if near:
            e = near[0][1]
            m = {"name": "monstruo", "ename": e[2], "etype": e[5], "x": int(cx + e[0] * tw), "y": int(cy + e[1] * th - th * 0.3),
                 "w": tw, "h": th, "score": 1.0}
            self.target, self.state, self._track = m, "attack", None
            self.target_since = self.last_seen_target = self.last_target_found = now
            self._last_enemy = now
            self.stats["state"], self.stats["target"] = "atacando", e[2]
            self.nav["walking"] = False
            self._hold_on(m["x"], m["y"])
            self.last_attack_click = now
            return True
        if cands and px is not None:
            # hay bichos fuera de la pantalla: voy hacia el más cercano
            e = cands[0][1]
            mp = self.world.map_for(self.pos["map"]) if self.pos.get("map") else None
            if mp:
                g = mp.nearest_walkable(px + e[0], py + e[1]) or (px + e[0], py + e[1])
                if not self._is_danger(g[0], g[1]):
                    self._walk_to(mp, g, f"bichos a {cands[0][0]} baldosas")
                    return True
        # nada cerca: que explore ya, sin esperar
        self._last_enemy = min(getattr(self, "_last_enemy", 0), now - 120)
        self.stats["state"], self.stats["target"] = "buscando", ""
        return True

    def _auto(self):
        now = time.time()
        tw, th = self._tile()
        if self.state == "loot":
            if now >= self.loot_at:
                if self.cfg.get("loot_enabled") and self.templates.items["items"]:
                    self._loot_step()
                elif self.cfg.get("loot_enabled"):
                    # sin fotos de ítems: click en el lugar donde murió (si cayó algo, lo levanta)
                    lx, ly = self.loot_pos
                    t = self._tile_of(lx, ly)
                    if t and self._near_door(1, t):
                        self.state, self.target, self._track = "seek", None, None
                        return
                    winapi.click(*self.abs_pt(lx, ly))
                    self.stats["loot_clicks"] += 1
                    self._last_move = now
                    time.sleep(0.5)
                self.state, self.target, self._track = "seek", None, None
            return

        if self.cfg.get("memory", {}).get("hunt", True) and self.ents.center and self.mem.learned:
            r = self._auto_mem()
            if r is not None:
                return

        if self.state == "attack" and self._track is not None:
            if now - self.last_attack_click < self.cfg["attack_check_every"]:
                return
            t = self.target
            named = vision.enemy_pixels(self.img, t["x"], t["y"]) >= self.cfg.get("enemy_min_pixels", 18)
            if named or not self.cfg.get("hover_confirm", True):
                self.last_seen_target = now
                self._seen_alive = True
            else:
                # el nombre no está: ¿se movió? paso el mouse alrededor buscando el nombre
                found = self._reacquire(t)
                if found:
                    self.target = {**t, "x": found[0], "y": found[1]}
                    self.last_seen_target = now
                    self._seen_alive = True
                    self._hold_on(found[0], found[1])
                    self.last_attack_click = time.time()
                    return
            if now - self.target_since > self.cfg["target_timeout"]:
                self.log("No muere: cambio de objetivo.")
                self.blacklist.append((t["x"], t["y"], now + 20))
                self._release()
                self.state, self.target, self._track = "seek", None, None
                return
            if now - self.last_seen_target > 0.9:
                self._release()
                if self._seen_alive and now - self.target_since > 0.3:
                    self.stats["kills"] += 1
                    self.log(f"✝ Monstruo muerto (kill #{self.stats['kills']}).")
                    self._last_kill_t = time.time()
                    if self.pos.get("map") and self.pos.get("x") is not None and not self._near_door(6) and not self._is_danger(self.pos["x"], self.pos["y"]):
                        self.world.learn["anchors"][self.pos["map"].strip().lower()] = [self.pos["x"], self.pos["y"]]
                        if self.stats["kills"] % 5 == 0:
                            self.world.save()
                    self.state, self.loot_at, self.loot_pos = "loot", now + self.cfg["loot_delay"], (t["x"], t["y"])
                    self.stats["state"] = "esperando loot"
                    self._last_move = now
                    return
                self.state, self.target, self._track = "seek", None, None
                return
            self._hold_on(self.target["x"], self.target["y"])
            self.last_attack_click = now
            self._last_move = now
            return

        # buscar
        self.stats["state"], self.stats["target"] = "buscando", ""
        if self._under_attack():
            self._defend()
            return
        cands = self._auto_candidates()
        if cands is None:
            if time.time() - self._last_move < self.cfg.get("settle_time", 0.4):
                return
            cands = []
        self._last_mons = cands[:6]
        if self.cfg.get("debug") and time.time() - getattr(self, "_dbg_t", 0) > 5:
            self._dbg_t = time.time()
            self.log(f"debug: {len(cands)} cosas moviéndose cerca")
        melee = self.cfg.get("melee_range_tiles", 1.7)
        near = [m for m in cands if self._dist_tiles(m["x"], m["y"]) <= melee]
        for m in near[:3]:
            snap = self.img
            box0 = (m["x"] - 2.5 * tw, m["y"] - 2.5 * th, m["x"] + 2.5 * tw, m["y"] + 1.0 * th)
            self._name_base = vision.bright_text_pixels(snap, box0, cursor=(m["x"], m["y"]))
            self._seen_alive = False
            if self._hover_ok(m):
                self._hover_fail = 0
                self._seen_alive = True  # el nombre apareció al pasar el mouse
                size = int(max(tw, th) * 1.1)
                self._track = vision.crop_around(snap, m["x"], m["y"], size)
                if self._track is None:
                    continue
                self.target, self.state = m, "attack"
                self._last_enemy = time.time()
                self.target_since = self.last_seen_target = self.last_target_found = time.time()
                self.stats["state"], self.stats["target"] = "atacando", "monstruo"
                self._hold_on(m["x"], m["y"])
                self.last_attack_click = time.time()
                return
            self._hover_fail += 1
            self.blacklist.append((m["x"], m["y"], time.time() + 8))
        if time.time() - getattr(self, "_last_scan", 0) > self.cfg.get("scan_every", 1.2):
            self._last_scan = time.time()
            hit = self._scan_ring()
            if hit:
                self._engage(hit)
                return
        far = [m for m in cands if self._dist_tiles(m["x"], m["y"]) > melee]
        if far:
            # me acerco un poco al más cercano y vuelvo a mirar (sin perseguir con la pantalla moviéndose)
            m = far[0]
            if self._hover_ok(m):
                cx, cy = self._char()
                d = self._dist_tiles(m["x"], m["y"])
                k = max(0.0, (d - 1.0) / d)
                tx, ty = cx + (m["x"] - cx) * k, cy + (m["y"] - cy) * k
                self._release()
                winapi.move(*self.abs_pt(tx, ty))
                winapi.left_down()
                time.sleep(min(1.5, (d - 1.0) / self.cfg.get("walk_speed_tiles", 3.0)))
                winapi.left_up()
                self._last_move = time.time()
                self.last_target_found = time.time()
                self.stats["state"] = "acercándome"
                return
            self._hover_fail += 1
            self.blacklist.append((m["x"], m["y"], time.time() + 8))
            if self._hover_fail == 15:
                self.log("No veo nombres al pasar el mouse. Si los monstruos no muestran nombre, apagá 'Confirmar con el mouse' en Configuración.")
        if self.cfg.get("roam_enabled") and time.time() - self.last_target_found > self.cfg["roam_after"]:
            self._roam()
            self.last_target_found = time.time()

    # ------------------------------------------------------------------ pit
    def _pit(self):
        dirs = [d for d in self.cfg.get("pit_directions", []) if d in DIRS] or list(DIRS)
        d = dirs[self.pit_i % len(dirs)]
        self.pit_i += 1
        cx, cy = self._char()
        tw, th = self._tile()
        dx, dy = DIRS[d]
        if self.pos.get("x") is not None and self._near_door(1, (self.pos["x"] + dx, self.pos["y"] + dy)):
            return  # para ese lado hay una puerta: si pego al aire camino y entro
        mod = (self.cfg.get("attack_modifier") or "").upper()
        if mod:
            winapi.key_down(mod)
        winapi.click(*self.abs_pt(cx + dx * tw, cy + dy * th))
        if mod:
            winapi.key_up(mod)
        self.stats["state"], self.stats["target"] = "pit", d
        time.sleep(self.cfg["pit_cycle_delay"])
        if self.cfg.get("loot_enabled") and self.templates.items["items"] and self.pit_i % (len(dirs) * 3) == 0:
            self.loot_pos, self.loot_at = (cx, cy), 0
            self._loot_step()

    # -------------------------------------------------------------- preview
    def _preview_loop(self):
        """Hilo aparte para la vista en vivo: saca sus propias fotos de la ventana
        (no espera al bot, que se queda quieto mientras apunta o camina)."""
        scr = None
        fps = 20
        while self.alive:
            t0 = time.time()
            try:
                if self.preview_on and self.rect and time.time() - getattr(self, "_frame_wanted", 0) < 2.0:
                    if scr is None or (not isinstance(self.screen, vision.Screen) and scr is not self.screen):
                        scr = vision.Screen() if isinstance(self.screen, vision.Screen) else self.screen
                    scr.hwnd = self.hwnd
                    scr.use_printwindow = bool(self.cfg.get("background_mode"))
                    img = scr.grab(self.rect)
                    if img is not None:
                        mons = getattr(self, "_last_mons", []) if self.running else []
                        if self.running and not mons and self.target:
                            mons = [self.target]
                        ov = vision.draw_overlay(img, self.cfg, mons, (), self.target)
                        url = vision.to_data_url(ov, int(self.cfg.get("preview_quality", 75)),
                                                 max_w=int(self.cfg.get("preview_width", 1280)))
                        self.preview = url
                        self.frame_id += 1
                else:
                    time.sleep(0.1)
            except Exception:
                time.sleep(0.3)
            fps = max(5, min(30, int(self.cfg.get("preview_fps", 15))))
            time.sleep(max(0.005, 1.0 / fps - (time.time() - t0)))

    def frame(self, last_id=0):
        """Lo pide la ventana del bot. Devuelve el cuadro nuevo o nada si no cambió."""
        self._frame_wanted = time.time()
        if self.frame_id == last_id or not self.preview:
            return {"id": self.frame_id, "img": ""}
        return {"id": self.frame_id, "img": self.preview}

    def _update_preview(self):
        if self.img is None:
            self.preview = ""
            return
        mons = []
        if self.templates.items["monsters"] and not self.running and time.time() - getattr(self, "_last_prev_find", 0) > 3:
            self._last_prev_find = time.time()
            mons = self._monsters()
        elif self.running:
            mons = getattr(self, "_last_mons", []) or ([self.target] if self.target else [])
        ov = vision.draw_overlay(self.img, self.cfg, mons, (), self.target)
        self.preview = vision.to_data_url(ov, 55, max_w=640)

    # ---------------------------------------------------------------- state
    def snapshot(self):
        s = dict(self.stats)
        rt = max(1, s["runtime"])
        s["kph"] = round(s["kills"] * 3600 / rt) if s["runtime"] > 60 else 0
        return {
            "running": self.running,
            "stats": s,
            "bars": self.bars,
            "window": self.win_title,
            "rect": self.rect,
            "calib_msg": self.calib.get("msg", ""),
            "calib_slot": self.calib.get("slot"),
            "log": list(self.log_lines)[-60:],
            "preview": "1" if self.preview else "",
            "admin": winapi.is_admin(),
            "pos": {k: self.pos[k] for k in ("map", "x", "y", "text")},
            "mem": self.mem.status if self._mem_on() else "apagada",
            "progress": {"level": self.cfg.get("travel", {}).get("my_level"), "exp": self.progress.get("exp"),
                         "rate": self.progress.get("rate"), "eta_min": self.progress.get("eta_min"),
                         "levelups": self.progress.get("levelups", 0)},
            "nav": {"status": self.nav["status"], "dest": self.nav["dest"]},
        }
