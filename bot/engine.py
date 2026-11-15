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
from . import learn as learnmod
import difflib
import numpy as np

VK = {f"F{i}": 0x6F + i for i in range(1, 13)}
VK.update({"INSERT": 0x2D, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22, "PAUSE": 0x13, "SCROLLLOCK": 0x91})
DIRS = {"N": (0, -1), "NE": (1, -1), "E": (1, 0), "SE": (1, 1), "S": (0, 1), "SW": (-1, 1), "W": (-1, 0), "NW": (-1, -1)}
POINT_SLOTS = {"char_center", "hp_pot_slot", "mp_pot_slot", "sp_pot_slot", "food_slot", "recall_slot",
               "char_button", "plus_str", "plus_dex", "plus_vit", "plus_int", "plus_mag", "plus_chr", "stat_confirm"}
TWO_POINT_SLOTS = {"hp_bar", "mp_bar", "sp_bar", "game_area", "pos_box", "char_box"}


def _load_any(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


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
        srv = (self.cfg.get("server") or {}).get("name") or "Abaddon"
        self.pdir = cfgmod.profile_dir(self.cfg)
        gd = worldmod.find_game_dir(self.cfg.get("game_dir"), srv)
        if self.pdir != cfgmod.DATA and not os.path.exists(os.path.join(self.pdir, "mundo_base.json")):
            try:  # el mundo base (portales clásicos) es igual en todos los Helbreath
                import shutil
                shutil.copy(os.path.join(cfgmod.DATA, "mundo_base.json"), self.pdir)
            except Exception:
                pass
        self.world = worldmod.World(gd, os.path.join(self.pdir, "mundo.json"))
        self.pos = {"map": None, "x": None, "y": None, "t": 0, "text": ""}
        mp = (self.cfg.get("memory") or {}).get("pos")
        self.mem = memory.PosFinder(self.log, saved=mp)
        self._mem_hold = False
        self._last_ocr_xy = None
        self.ents = memory.Entities(self.log)
        self.oly = memory.OlyEntities()
        self.learner = learnmod.Learner(self)
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
        """Datos del juego. Primero busco en la carpeta del server (data/servers/<nombre>/): ahí va lo propio de cada
        uno (misiones, precios, guía); si no está, uso el archivo general de data/."""
        for d in (cfgmod.profile_dir(self.cfg), cfgmod.DATA):
            try:
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                continue
        return {}

    def _server_file(self, name):
        return os.path.join(cfgmod.profile_dir(self.cfg), name)

    # ------------------------------------------------------------ estado del server (pestaña 'Server')
    def server_status(self):
        """Qué necesita el bot para jugar en este server, qué ya tiene, qué va aprendiendo solo y qué hay que darle."""
        slug = cfgmod.server_slug(self.cfg)
        pdir = cfgmod.profile_dir(self.cfg)
        has = lambda n: os.path.exists(os.path.join(pdir, n))
        items = []

        def add(key, label, state, detail, how=""):
            items.append({"key": key, "label": label, "state": state, "detail": detail, "how": how})

        sv = self.cfg.get("server") or {}
        add("permiso", "Permiso para usar bots", "ok" if sv.get("bots_permitidos") else "ask",
            "Confirmado por vos." if sv.get("bots_permitidos") else "Sin confirmar: el bot no arranca solo.",
            "" if sv.get("bots_permitidos") else "Marcá en Configuración que los admins de este server permiten bots.")
        add("ventana", "Ventana del juego", "ok" if self.hwnd else "ask",
            "Encontrada." if self.hwnd else "No veo el juego abierto.", "" if self.hwnd else "Abrí el juego (y el bot como administrador si el juego lo es).")
        add("posicion", "Posición del personaje", "ok" if (self.mem.learned or self.pos.get("x") is not None) else "auto",
            "La leo de la memoria." if self.mem.learned else ("La leo de la pantalla." if self.pos.get("x") is not None else "La busco sola al entrar al juego (moverse unos pasos ayuda)."))
        ents_ok = bool(getattr(self, "_oly_last", None)) or bool(self.ents.center)
        add("bichos_mem", "Bichos en tiempo real", "ok" if ents_ok else "auto",
            "Veo la lista de bichos en la memoria." if ents_ok else "Todavía no la encontré: la descubro sola parado cerca de bichos (un par de minutos).")
        sh = self._sheet(24 * 3600)
        try:
            known = self._ficha_glyphs().known()
        except Exception:
            known = []
        miss = [d for d in "0123456789" if d not in known]
        add("ficha", "Ficha (nivel, exp, stats)", "ok" if sh and not miss else "auto" if sh or known else "ask",
            (f"Leída: nivel {sh['level']}." if sh else "Todavía no la pude leer.") + (f" Cifras que me falta aprender: {', '.join(miss)} (las aprendo cuando aparecen)." if miss and known else ""),
            "" if sh or known else "Si en este cliente la ficha no se abre con F5 o tiene otro formato, pasame una captura de la ventana del personaje.")
        nmaps = len(getattr(self.world, "maps", {}) or {}) if hasattr(self.world, "maps") else None
        gd = getattr(self.world, "game_dir", None)
        add("mapas", "Mapas del juego", "ok" if gd else "ask", f"Carpeta del juego: {gd}." if gd else "No encontré la carpeta del juego (de ahí saco los mapas para caminar).",
            "" if gd else "Decime dónde está instalado el juego (carpeta con 'mapdata').")
        places = {k: self._place(k) for k in ("shop", "smith")}
        okp = [k for k, v in places.items() if v]
        add("lugares", "Tienda y herrero", "ok" if len(okp) == 2 else "ask",
            ("Sé dónde están: " + ", ".join({"shop": "tienda", "smith": "herrero"}[k] for k in okp) + ".") if okp else "No sé dónde están la tienda ni el herrero.",
            "" if len(okp) == 2 else "Llevá al personaje hasta la puerta de cada uno y apretá 'Anotar tienda' / 'Anotar herrero', o decime mapa y baldosa.")
        cat = _load_any(os.path.join(pdir, "tienda.json"))
        n_items = sum(len(v) for v in (cat.get("tabs") or {}).values()) if cat else 0
        add("precios", "Precios de las tiendas", "ok" if n_items else "auto",
            f"{n_items} objetos con precio leídos de las tiendas." if n_items else "Los leo solo la primera vez que entro a cada tienda.")
        seen = self.world.learn.get("types_seen", {}) or {}
        crs = self._creatures_db()
        unk = [t for t in seen if str(t).isdigit() and int(t) not in crs]
        add("criaturas", "Datos de los bichos", "ok" if crs and not unk else "auto",
            f"{len(crs)} bichos en la base." + (f" {len(unk)} tipos vistos sin identificar: los mido peleando." if unk else ""),
            "Opcional: si el server tiene wiki de monstruos, pasame el link o un archivo y arranco sabiendo vida y daño de cada uno.")
        q = has(f"misiones.json") or bool(self._load_json(f"misiones_{slug}.json"))
        add("misiones", "Misiones", "ok" if q else "auto", "Tengo la lista de misiones del server." if q else "Sin lista: igual acepto las que ofrecen los NPC que veo (cartel '!').",
            "" if q else "Opcional: pasame la página de misiones de la wiki y las priorizo por nivel.")
        tp = has("training_path.json")
        add("guia", "Guía de niveles", "ok" if tp else "auto", "Guía propia de este server." if tp else "Uso la guía general de guerrero y la corrijo con lo que mido (exp por hora por mapa).",
            "" if tp else "Opcional: si este server tiene otros mapas o niveles, pasame la guía.")
        t = self.cfg.get("travel", {}) or {}
        add("ciudad", "Ciudadanía", "ok" if not t.get("join_city") else "auto", "Sin pendiente." if not t.get("join_city") else f"Me uno a {str(t['join_city']).title()} al llegar a nivel {t.get('join_level', 10)}.",
            "Si el server pide elegir ciudad y todavía no elegiste, decime cuál.")
        cal = self.cfg.get("calib", {}) or {}
        okc = bool(cal.get("tile")) and bool(cal.get("char_center")) and bool(cal.get("hp_bar"))
        add("calibracion", "Calibración de pantalla", "ok" if okc else "ask", "Hecha." if okc else "Falta calibrar (baldosa, centro del personaje, barras).",
            "" if okc else "Apretá HOME con el juego abierto: se calibra solo.")
        n_ok = sum(1 for i in items if i["state"] == "ok")
        return {"server": sv.get("name") or "Abaddon", "slug": slug, "folder": pdir, "items": items, "ok": n_ok, "total": len(items),
                "ready": all(i["state"] != "ask" for i in items)}

    def note_place(self, kind):
        """Anota la puerta de la tienda/herrero donde está parado el pj ahora (para servers nuevos)."""
        if kind not in ("shop", "smith") or self.pos.get("x") is None:
            return {"ok": False, "msg": "No sé dónde está el personaje."}
        pl = self.cfg.setdefault("places", {}).setdefault(kind, {})
        pl.update({"map": self.pos["map"], "door": [int(self.pos["x"]), int(self.pos["y"]) - 1],
                   "inside": ["shop", "tienda"] if kind == "shop" else ["smith", "herrer"]})
        pl.setdefault("npc_type", -1)
        pl.setdefault("npc", [0, 0])
        cfgmod.save(self.cfg)
        self.log(f"📌 Anoté la puerta de {'la tienda' if kind == 'shop' else 'el herrero'}: {pl['map']} {pl['door']}. Al vendedor lo busco por su menú.")
        return {"ok": True, "msg": f"Anotado en {pl['map']} {pl['door']}."}

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
            if getattr(self, "_setup_busy", False):
                self.log("Estoy haciendo la puesta a punto: arranco cuando termine.")
                return
            sv = self.cfg.get("server") or {}
            if not sv.get("bots_permitidos"):
                self.log(f"⛔ No arranco: en Config → Servidor no está marcado que {sv.get('name') or 'este servidor'} permite bots. "
                         "Marcalo solo si los admins lo confirmaron por escrito.")
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
                        if self.running or not (self.cfg.get("server") or {}).get("bots_permitidos"):
                            want_start = False  # sin permiso del servidor no insisto cada segundo
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
            # análisis a medida (lo manda Claude desde la conversación)
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
        if last is not None and pct + 40 < last and not (self._sheet(3600) and not self._mem_struct()):
            # la barra se vació: subió de nivel (donde leo la ficha, el nivel sale de ahí y no de la barra)
            t = self.cfg.setdefault("travel", {})
            lm = self._level_mem()
            t["my_level"] = max(lm or 0, int(t.get("my_level", 1)) + 1) if not self._mem_struct() else (lm or int(t.get("my_level", 1)) + 1)
            self._census_t = 0  # releer la ficha ya (nivel exacto y puntos)
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
    def _town_account(self):
        """Lleva la cuenta de cuánto tiempo se va en mandados (tiendas, ir y volver del pueblo) por minuto."""
        now = time.time()
        dt = min(2.0, max(0.0, now - getattr(self, "_bud_last", now)))
        self._bud_last = now
        m = (self.pos.get("map") or "").lower()
        if (getattr(self, "_busy_ui", False) or getattr(self, "mission", None) or getattr(self, "_err_v", None)
                or any(k in m for k in ("shop", "blacksmith"))):
            b = getattr(self, "_bud", None)
            if b is None:
                b = self._bud = {}
            k = int(now // 60)
            b[k] = b.get(k, 0.0) + dt
            for old in [x for x in b if x < k - 60]:
                del b[old]

    def _town_budget_left(self):
        """Segundos de mandados que me quedan en la última hora (regla: 90% cazando, 10% en el pueblo)."""
        k = int(time.time() // 60)
        used = sum(v for x, v in (getattr(self, "_bud", None) or {}).items() if x >= k - 60)
        return 3600.0 * float(self.cfg.get("town_share", 0.10)) - used

    # ---------- rendimiento por mapa: experiencia por hora, pociones por minuto ----------
    def _yield_entry(self, mk):
        """Datos de rendimiento del mapa; los viejos pesan menos cuanto más niveles subí desde que los medí."""
        lvl = self._level_mem()
        y = self.world.learn.setdefault("yield", {}).setdefault(mk, {"sec": 0.0, "exp": 0.0, "pots": 0.0, "lvl": lvl or 1})
        # sin nivel leído (desconectado, cargando) no toco nada: antes eso borraba todo lo medido
        if lvl and lvl >= y.get("lvl", lvl) - 1 and y.get("lvl") != lvl:
            f = 0.8 ** max(1, abs(lvl - y.get("lvl", lvl)))
            for k in ("sec", "exp", "pots"):
                y[k] = y.get(k, 0.0) * f
            y["lvl"] = lvl
        return y

    def _hunting_now(self):
        cur = (self.pos.get("map") or "").lower()
        if not cur or any(k in cur for k in ("shop", "blacksmith", "warehouse", "guild", "hall", "church", "tower")):
            return False
        if getattr(self, "mission", None) or getattr(self, "_busy_ui", False) or getattr(self, "_err_v", None):
            return False
        return not self._is_travelling()

    def _yield_add(self, exp=0.0):
        mk = (self.pos.get("map") or "").strip().lower()
        if mk and exp > 0:
            self._yield_entry(mk)["exp"] += exp

    def _yield_tick(self):
        now = time.time()
        dt = min(2.0, max(0.0, now - getattr(self, "_yt", now)))
        self._yt = now
        if not self.running or not self._hunting_now() or not self._level_mem():
            self._yt_pots = None
            return
        mk = (self.pos.get("map") or "").strip().lower()
        y = self._yield_entry(mk)
        y["sec"] += dt
        hp_p = self.stats.get("hp_pots", 0)
        last = getattr(self, "_yt_pots", None)
        if last is not None and 0 < hp_p - last <= 2:  # saltos raros (reconexión, teclas sin efecto) no cuentan
            y["pots"] += hp_p - last
        self._yt_pots = hp_p
        x, yy = self.pos.get("x"), self.pos.get("y")
        if x is not None:
            z = self.world.learn.setdefault("xpz", {}).setdefault(mk, {})
            e = z.setdefault(f"{x // self.ZC},{yy // self.ZC}", {"n": 0, "xp": 0.0, "x": x, "y": yy})
            e["sec"] = e.get("sec", 0.0) + dt
        if now - getattr(self, "_yt_save", 0) > 120:
            self._yt_save = now
            self.world.save()

    def _yield_score(self, mk):
        """Experiencia por hora en ese mapa (None si todavía no estuve 10 minutos). Si gasto muchas pociones
        por minuto, cuenta menos (es caro y peligroso)."""
        y = self.world.learn.get("yield", {}).get(mk.strip().lower())
        if not y or y.get("sec", 0) < 60:
            return None
        rate = y["exp"] / y["sec"] * 3600
        # en el mapa cazo en la mejor zona: si una zona ya está bien medida, el mapa vale lo que rinde esa zona
        zb = self._best_zone_rate(mk)
        if zb:
            rate = max(rate, 0.85 * zb)
        ppm = y.get("pots", 0) / (y["sec"] / 60.0)
        if y["sec"] < 120:
            ppm = min(ppm, 1.5 * float(self.cfg.get("pots_per_min_max", 3.0)))  # muy poco tiempo para juzgar
        lim = float(self.cfg.get("pots_per_min_max", 3.0))
        if y["sec"] < 600 and ppm <= 1.5 * lim:
            return None  # todavía poco tiempo y nada alarmante: sigo midiendo
        if ppm > lim:
            # se me van las pociones: con lo que puedo cargar no aguanto ni 5 minutos -> todavía no es para mí
            carry = max(10, int(self.cfg.get("pots_target", 20)))
            rate *= 0.1 if carry / ppm < 5 else 0.4
        return rate

    def _yield_burning(self, mk):
        y = self.world.learn.get("yield", {}).get(mk.strip().lower())
        if not y or y.get("sec", 0) < 120:
            return False
        return y.get("pots", 0) / (y["sec"] / 60.0) > float(self.cfg.get("pots_per_min_max", 3.0))

    def _best_zone_rate(self, mk):
        """Exp/hora de la mejor zona bien medida de ese mapa (al menos 4 minutos y 8 muertes), o None."""
        z = self.world.learn.get("xpz", {}).get(mk.strip().lower()) or {}
        best = None
        for c in z.values():
            if c.get("sec", 0) >= 240 and c.get("n", 0) >= 8:
                r = c.get("tot", 0) / c["sec"] * 3600
                best = r if best is None or r > best else best
        return best

    def _tab_check(self):
        """Si apreté TAB y en 75 s no maté nada, me equivoqué (estaba en modo ataque): lo vuelvo como estaba."""
        p = getattr(self, "_tab_pending", 0)
        if not p:
            return
        now = time.time()
        if getattr(self, "_last_kill_t", 0) > p:
            self._tab_pending = 0
            self.log("⚔ Modo ataque confirmado: volví a matar.")
        elif now - p > 75 and self.running and not getattr(self, "_busy_ui", False):
            self._tab_pending = 0
            winapi.press("TAB")
            self.log("⚔ Con TAB no cambió nada: lo vuelvo a como estaba.")

    def _watchdog(self):
        """Que no se quede nunca quieto: si hace rato que no se mueve ni gana experiencia (o un hilo de compras quedó
        colgado), lo destraba solo. Devuelve True si en este paso está escapando."""
        now = time.time()
        if not self.running:
            self._wd_pos_t = self._wd_exp_t = now
            return False
        # hilo de compras/ventanas colgado
        if getattr(self, "_busy_ui", False):
            self._busy_t0 = getattr(self, "_busy_t0", None) or now
            if now - self._busy_t0 > 300:
                self._busy_ui = False
                self.mission = None
                self._busy_t0 = None
                self.log("⚠ Alerta: una compra/venta quedó colgada más de 5 minutos. La corto y sigo cazando.")
            return False
        self._busy_t0 = None
        p = (self.pos.get("map"), self.pos.get("x"), self.pos.get("y"))
        if p != getattr(self, "_wd_pos", None):
            self._wd_pos, self._wd_pos_t = p, now
        # sin memoria del pj la exp se lee cada varios minutos (ficha): ahí la señal de vida son las muertes de bichos
        e = self._exp_mem() if self._mem_struct() else self.stats.get("kills")
        if e is not None and e != getattr(self, "_wd_exp", None):
            self._wd_exp, self._wd_exp_t = e, now
        if getattr(self, "_rest_since", 0) or getattr(self, "_kite", None):
            self._wd_pos_t = now  # descansando o retirándome a propósito: estar quieto no es estar trabado
        pos_t, exp_t = getattr(self, "_wd_pos_t", now), getattr(self, "_wd_exp_t", now)
        # escape en curso
        esc = getattr(self, "_escape", None)
        if esc and now < esc[1]:
            mp = self.world.map_for(self.pos.get("map") or "")
            if mp and self.pos.get("x") is not None:
                if max(abs(esc[0][0] - self.pos["x"]), abs(esc[0][1] - self.pos["y"])) <= 1:
                    self._escape = None
                    return False
                self._walk_to(mp, esc[0], "destrabándome")
                self.stats["state"] = "destrabándome"
                return True
        self._escape = None
        cur = (self.pos.get("map") or "").lower()
        if not cur:
            # sin mapa: puede ser que el juego esté en un menú (desconectado)
            if now - getattr(self, "_nomap_t", now) > 30 and now - getattr(self, "_nomap_chk", 0) > 30:
                self._nomap_chk = now
                try:
                    _w, txt = self._screen_words()
                    nt = "".join(c for c in txt if c.isalnum())
                    if any(k in nt.lower() for k in ("account", "server", "characterlist", "connectionlost", "createnew", "password", "logout")):
                        self._force_relog = now + 180
                        self.log("⚠ Alerta: el juego está en el menú (desconectado). Reconecto.")
                except Exception:
                    pass
            if not hasattr(self, "_nomap_t") or self._nomap_t is None:
                self._nomap_t = now
            return False
        self._nomap_t = None
        if getattr(self, "mission", None) or any(k in cur for k in ("shop", "blacksmith")):
            return False
        stuck_pos = now - pos_t > float(self.cfg.get("stuck_secs", 40)) and now - exp_t > float(self.cfg.get("stuck_secs", 40))
        no_exp = now - exp_t > float(self.cfg.get("no_exp_secs", 180)) and not self._is_travelling()
        if not (stuck_pos or no_exp):
            return False
        self._wd_pos_t = self._wd_exp_t = now
        try:
            _w, txt = self._screen_words()
            nt = "".join(c for c in txt if c.isalnum())
            if any(k in nt.lower() for k in ("newaccount", "server", "characterlist", "connectionlost", "createnew", "logout")):
                self._force_relog = now + 180
                self.pos["t"] = 0
                self.log("⚠ Alerta: el juego se desconectó (estoy en el menú). Reconecto.")
                return False
        except Exception:
            pass
        n = [t for t in getattr(self, "_wd_hits", []) if now - t < 600] + [now]
        self._wd_hits = n
        t = self.target or {}
        if t.get("ename"):
            self._ent_bl[t["ename"]] = now + 90
        self._release()
        self.state, self.target, self._track = "seek", None, None
        why = "no me muevo hace 40 s" if stuck_pos else "hace 3 minutos que no gano experiencia"
        if no_exp or len(n) >= 3:
            # la zona no rinde o me trabo seguido acá: la dejo descansar y busco otra
            zg = getattr(self, "_zone_goal", None)
            if zg:
                self.__dict__.setdefault("_zone_empty", {})[zg] = now + 900
            self._zone_goal = None
        mp = self.world.map_for(self.pos.get("map") or "")
        x, y = self.pos.get("x"), self.pos.get("y")
        if mp and x is not None:
            import random as _r
            for _ in range(40):
                gx, gy = x + _r.randint(-14, 14), y + _r.randint(-10, 10)
                if max(abs(gx - x), abs(gy - y)) < 6 or not mp.ok(gx, gy) or self._is_danger(gx, gy) or mp.near_tele(gx, gy, 3):
                    continue
                path = mp.astar((x, y), (gx, gy), max_nodes=8000)
                if path and len(path) <= 40:
                    self._escape = ((gx, gy), now + 15)
                    break
        self.log(f"⚠ Alerta: {why}. Suelto el objetivo y me muevo{' a otra zona' if (no_exp or len(n) >= 3) else ''}.")
        return bool(getattr(self, "_escape", None))

    def _tick(self):
        # segundo plano: los clicks SIEMPRE van directo a la ventana del juego (nunca al mouse de tu PC)
        if self.cfg.get("background_mode") and self.hwnd and winapi.BG_HWND != self.hwnd:
            winapi.set_background(self.hwnd)
        if self._watchdog():
            return
        self._town_account()
        self._tab_check()
        self._yield_tick()
        try:
            self.learner.tick()
        except Exception as e:
            if time.time() - getattr(self, "_learn_err_t", 0) > 600:
                self._learn_err_t = time.time()
                self.log(f"Aprendizaje: error {e!r}")
        self._zone_flush()
        self._fight_sample()
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
            # descansando: buen momento para revisar los stats (sin bichos cerca, quieto)
            if getattr(self, "_rest_since", 0):
                self._census()
                if (cfg.get("stats_auto", {}).get("enabled") and getattr(self, "_last_stats", 1) == 0
                        and cfgmod.server_slug(cfg) != "abaddon"):
                    self._stats_done = self._last_stats = time.time()
                    self._do_stats()
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
            # servers sin memoria del pj: la ficha (F5) dice el nivel, la exp y si hay puntos por repartir
            if not indoors and not getattr(self, "mission", None) and self._census():
                return
        if sa.get("enabled") and self.state != "attack" and getattr(self, "_last_stats", 1) == 0:
            self._last_stats = time.time()
            self._stats_done = time.time()
            self._do_stats()
            return
        if self._fleeing() and self.ents.center and self.mem.learned and self._flee_mem():
            return
        if self._fleeing() and cfgmod.server_slug(cfg) != "abaddon" and self.pos.get("x") is not None:
            ents_ = self._oly_ents()
            if ents_ and self._oly_kite(ents_, force=True):
                return
            if not getattr(self, "_kite", None):
                self._flee_until = 0  # ya estoy a salvo y con vida: sigo cazando
        if self._fleeing():
            home = cfg.get("travel", {}).get("home", "aresden")
            cur_ = self.pos.get("map") or ""
            if self._same(cur_, "Beginner Zone") or "principiantes" in cur_.lower():
                home = cur_  # en la Beginner Zone no salgo por el portal: me alejo y descanso ahí mismo
            if not self._travel(force_dest=home):
                self._release()
                self.stats["state"] = "descansando"
            return
        self._join_step()
        if self._goto_step():
            return
        if self._quest_step():
            return
        self._selfcheck()
        self._strategy()
        self._maybe_shop()
        self._maybe_repair()
        self._brain_step()
        self._close_junk_windows()
        self._maybe_gear()
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
        if mem_ok and not in_building and not focused and self._zone_step():
            return
        if mem_ok and not in_building and not focused:
            if self.state in ("attack", "loot", "lootq") or self._mem_near():
                self._auto()
                return
            if not self._fleeing() and self._goto_spot():
                return
        # Olympia: mapa sin relevar → lo recorro entero (solo peleo si me atacan)
        if (not mem_ok and cfg["mode"] == "auto" and self.state == "seek" and not self._fleeing() and not in_building
                and not focused and self._explore_step()):
            return
        # Olympia: lista de bichos leída de la memoria (objetos del heap)
        if (not mem_ok and cfg["mode"] == "auto" and self.state == "seek" and not self._fleeing() and not in_building
                and (not focused or self._under_attack()) and self._oly_step()):
            return
        # me están pegando y no tengo la lista de bichos de la memoria: freno, busco quién es y le pego
        if (not mem_ok and cfg["mode"] == "auto" and self.state == "seek" and not self._fleeing()
                and (self.nav.get("walking") or self._is_travelling()) and self._under_attack()
                and time.time() - getattr(self, "_hit_scan_t", 0) > 1.0):
            self._hit_scan_t = time.time()
            self._release()
            self.nav["walking"] = False
            time.sleep(0.15)
            self.img = self.screen.grab(self.rect)
            hit = self._scan_ring()
            if hit:
                self._engage(hit)
                return
            self._pit()  # no lo veo: pego alrededor una vez y sigo
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

    def _close_shop_window(self, words=None):
        """Ventana 'Tienda' abierta (el título gótico no se lee: la reconozco por las pestañas Pociones/Vender).
        La cierro con click derecho en la lista vacía. True si estaba abierta."""
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        if words is None:
            words = ocr.read_words(self.screen.grab(self.rect), {"x1": 0, "y1": 0, "x2": self.rect[2], "y2": int(self.rect[3] * 0.6)}, scale=1.0)
        tab = next((w for w in words if norm(w[0]).startswith(("pocion", "pocl", "pocio"))), None)
        if not tab or not any(norm(v[0]).startswith(("vender", "varios", "undcr", "arios")) and abs(v[2] - tab[2]) < 15 for v in words):
            return False
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
        winapi.click(*self.abs_pt(tab[1] + 166 * kx, tab[2] + 317 * ky), button="right")
        time.sleep(0.4)
        return True

    def _close_junk_windows(self):
        """Ventanas que el bot no usa (ej. 'Resumen de Muerte' de Olympia): las cierro con click derecho
        sobre la ventana. Miro cada ~8 s, solo si no estoy peleando."""
        titles = self.cfg.get("close_windows") or []
        if not titles or not self.rect or self.state == "attack" or getattr(self, "_busy_ui", False):
            return False
        now = time.time()
        if now - getattr(self, "_junk_t", 0) < 8:
            return False
        self._junk_t = now
        img = self.img if self.img is not None else self.screen.grab(self.rect)
        if img is None:
            return False
        H, W = img.shape[:2]
        try:
            words = ocr.read_words(img, {"x1": 0, "y1": 0, "x2": W, "y2": int(H * 0.85)}, scale=1.0)
        except Exception:
            return False
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        txt = [(norm(w[0]), w) for w in words]
        # carteles peligrosos: '¿tirar este objeto?' / '¿cuánto oro tirar?' y elegir talento → siempre No/Cancelar
        for key in ("tirar", "talento", "unirte"):
            hit = next((w for t, w in txt if key in t), None)
            jc = norm((self.cfg.get("travel", {}) or {}).get("join_city") or "")
            if hit and key == "unirte" and jc and self._join_active() and any(jc in t for t, _ in txt):
                yes = next((w for t, w in txt if t in ("si", "sí", "aceptar", "yes", "ok", "unirme", "unirse")
                            and 0 < w[2] - hit[2] < 200 and abs(w[1] - hit[1]) < 400), None)
                try:
                    import cv2
                    cv2.imwrite(os.path.join(cfgmod.DATA, "join.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
                except Exception:
                    pass
                if yes:
                    winapi.left_up()
                    winapi.click(*self.abs_pt(yes[1], yes[2]))
                    t_ = self.cfg["travel"]
                    self.log(f"🏛 Me uní a {t_['join_city'].title()} (lo pediste vos). Sigo con la guía fuera de la zona de principiantes.")
                    t_["join_city"] = None
                    t_.pop("stay_map", None)
                    cfgmod.save(self.cfg)
                    self.goto = None
                    time.sleep(1.5)
                    return True
                if now - getattr(self, "_join_warn", 0) > 60:
                    self._join_warn = now
                    self.log("🏛 Veo el cartel para unirme pero no encuentro el botón de aceptar: " + " | ".join(w[0] for _, w in txt if 0 < w[2] - hit[2] < 200)[:120])
                return False
            if hit:
                btn = next((w for t, w in txt if t in ("no", "cancelar") and 0 < w[2] - hit[2] < 200 and abs(w[1] - hit[1]) < 400), None)
                if btn:
                    winapi.left_up()
                    winapi.click(*self.abs_pt(btn[1], btn[2]))
                    self.log(f"Cartel '{key}': respondo '{btn[0]}' (nunca tiro cosas ni elijo talentos solo).")
                    if key == "unirte" and self.pos.get("x") is not None and any(c in t for t, _ in txt for c in ("aresden", "elvine")):
                        try:
                            mk = (self.pos.get("map") or "").strip().lower()
                            bl = self.world.learn.setdefault("no_pisar", {}).setdefault(mk, [])
                            pt_ = [int(self.pos["x"]), int(self.pos["y"])]
                            near_ok = not bl or any(max(abs(b[0] - pt_[0]), abs(b[1] - pt_[1])) <= 6 for b in bl)
                            if pt_ not in bl and near_ok:
                                bl.append(pt_)
                                self.world.save()
                                self.log(f"🚫 Anoto ({pt_[0]},{pt_[1]}) como portal de ciudad: no lo vuelvo a pisar.")
                            self._apply_no_pisar()
                        except Exception:
                            pass
                    time.sleep(0.4)
                    return True
        if not getattr(self, "_shop_th", None) or not self._shop_th.is_alive():
            if self._close_shop_window(words):
                self.log("Cierro la ventana de la tienda (click derecho).")
                return True
        for title in titles:
            first = norm(title.split()[0])
            for t, w in txt:
                if t.startswith(first):
                    rest = [norm(x) for x in title.split()[1:]]
                    near = " ".join(t2 for t2, w2 in txt if abs(w2[2] - w[2]) < 15 and 0 <= w2[1] - w[1] < 400)
                    if all(r in near for r in rest):
                        if first == "peso" and not w[0].strip().startswith("Peso:"):
                            continue  # 'peso' suelto (ej. 'c/u, peso 1' en la tienda): no es el bolso
                        if first == "peso":
                            # el bolso: con click derecho podría caer sobre un objeto (y el juego pregunta si
                            # tirarlo). Lo cierro con su tecla.
                            winapi.press("F6")
                            self.log("Cierro el bolso (F6).")
                            time.sleep(0.3)
                            return True
                        dy = 190 if first == "tienda" else 25
                        winapi.click(*self.abs_pt(w[1], w[2] + dy * self.rect[3] / 1080.0), button="right")
                        self.log(f"Cierro la ventana '{title}' (click derecho).")
                        time.sleep(0.3)
                        return True
        return False

    def _adjacent_mobs(self):
        """Bichos a una baldosa del pj (lista de la memoria). 0 si no se sabe."""
        try:
            if not (self.ents.center and self._mem_struct()):
                oly = self._oly_ents() if self.pos.get("x") is not None else []
                if not oly:
                    return 0
                px, py = self.pos["x"], self.pos["y"]
                return sum(1 for e in oly if max(abs(e[0] - px), abs(e[1] - py)) <= 1)
            c = getattr(self, "_adj_cache", None)
            if c and time.time() - c[0] < 0.5:
                return c[1]
            lst = self.ents.read(self.mem.proc, cols=2) or []
            me = (self.ents.name or "").lower()
            n = sum(1 for e in lst if abs(e[0]) <= 1 and abs(e[1]) <= 1 and e[2].lower() != me
                    and not self._is_player_name(e[2]))
            self._adj_cache = (time.time(), n)
            return n
        except Exception:
            return 0

    def _is_player_name(self, name):
        """Nombre de otro jugador (no figura en la base de criaturas)."""
        names = getattr(self, "_cr_names", None)
        if names is None:
            names = self._cr_names = {str(m.get("name", "")).lower() for m in self._creatures_db().values()}
        return bool(name) and name.lower() not in names

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

    def _mem_struct(self):
        """¿La estructura del pj alrededor de la posición es la que conozco (Abaddon)? Si el cliente es otro
        (Olympia) los desplazamientos no valen: no leo vida/nivel/stats de ahí para no tomar basura."""
        if not (self.mem.learned and self.mem.addr and self.mem.addr[0] == "i16"):
            return False
        now = time.time()
        c = getattr(self, "_mstruct", None)
        if c and c[0] == self.mem.addr[1] and now - c[1] < 30:
            return c[2]
        ok = False
        try:
            d = self.mem.proc.read(self.mem.addr[1], 84)
            if d and len(d) >= 84:
                i32 = lambda o: int.from_bytes(d[o:o + 4], "little", signed=True)
                hp, lvl = i32(12), i32(72)
                stats = [i32(32 + 4 * k) for k in range(6)]
                ok = 1 <= lvl <= 180 and 1 <= hp <= 20000 and all(1 <= v <= 400 for v in stats)
        except Exception:
            ok = False
        self._mstruct = (self.mem.addr[1], now, ok)
        return ok

    def _hp_mem(self):
        """Vida exacta desde la memoria (al lado de la posición). None si no se sabe."""
        try:
            if self._mem_struct():
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
        scr_dead = b.get("hp") == 0 and ((b.get("mp") or 0) > 0 or (b.get("sp") or 0) > 0)
        # la memoria a veces vuelve a la vida llena mientras sigo tirado (Olympia): la barra en 0 manda
        dead = (hm == 0) or scr_dead
        if not dead and cfgmod.server_slug(self.cfg) != "abaddon" and self.rect and getattr(self, "img", None) is not None \
                and now - getattr(self, "_rq_t", 0) > 5:
            # Olympia: la barra y la memoria a veces no muestran la muerte; el botón 'Reiniciar' sí
            self._rq_t = now
            try:
                H, W = self.img.shape[:2]
                ws_ = ocr.read_words(self.img, {"x1": int(W * 0.75), "y1": int(H * 0.82), "x2": W, "y2": int(H * 0.92)}, scale=1.0)
                if any("iciar" in "".join(c for c in x[0].lower() if c.isalnum()) for x in ws_):
                    dead = True
                    self._dead_since = getattr(self, "_dead_since", 0) or now - 3.5
            except Exception:
                pass
        if not dead:
            self._dead_since = 0
            self._death_logged = 0
            self._restart_tries = 0
            return False
        if not getattr(self, "_dead_since", 0):
            self._dead_since = now
            return True
        if now - self._dead_since < 3 or now - getattr(self, "_restart_click", 0) < 6:
            return True
        self._release()
        if not getattr(self, "_death_logged", 0):  # una sola vez por muerte (se rearma al revivir)
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
            try:
                mk = (self.pos.get("map") or "").strip().lower()
                dm = self.world.learn.setdefault("deaths", {}).setdefault(mk, {"n": 0, "lvl": 0, "t": 0})
                dm["n"] = dm.get("n", 0) + 1 if time.time() - dm.get("t", 0) < 7200 else 1
                dm["lvl"] = self._level_mem() or dm.get("lvl", 0)
                dm["t"] = int(time.time())
                self.world.save()
                self._avoid_t = 0
                if cfgmod.server_slug(self.cfg) != "abaddon":
                    self._oly_died()
                    pl = getattr(self, "_exp_plan", None)
                    if pl and self._same(pl.get("map", ""), self.pos.get("map") or ""):
                        pl["deaths"] = pl.get("deaths", 0) + 1
                try:
                    self.learner.on_death()
                except Exception:
                    pass
                if dm["n"] >= 2:
                    if dm["n"] in (2, 5, 10) or dm["n"] % 25 == 0:
                        self.log(f"☠ {dm['n']} muertes seguidas en {self.pos.get('map')}: si tengo otro mapa para mi nivel lo dejo descansar un rato (máximo 3 horas).")
            except Exception:
                pass
        # botón "Restart" (abajo a la derecha, donde sale Level Up)
        img = self.img
        pt = None
        try:
            H, W = img.shape[:2]
            words = ocr.read_words(img, {"x1": int(W * 0.6), "y1": int(H * 0.7), "x2": W, "y2": int(H * 0.92)}, scale=1.0)
            w = self._find_word(words, "restart") or self._find_word(words, "reiniciar")  # Olympia en castellano
            if not w:  # la letra gótica sale mal ('(IViniciAr'): alcanza con que tenga 'iciar'
                w = next((x for x in words if "iciar" in "".join(c for c in x[0].lower() if c.isalnum())), None)
            if w:
                pt = (w[1], w[2])
                rb_ = self.cfg["calib"].get("restart_btn") or {}
                if abs(rb_.get("x", 0) - pt[0]) > 15 or abs(rb_.get("y", 0) - pt[1]) > 15:
                    self.cfg["calib"]["restart_btn"] = {"x": int(pt[0]), "y": int(pt[1])}
                    cfgmod.save(self.cfg)
        except Exception:
            pass
        self._restart_tries = getattr(self, "_restart_tries", 0) + 1
        if not pt and self.cfg["calib"].get("restart_btn"):
            rb = self.cfg["calib"]["restart_btn"]
            pt = (int(rb["x"]), int(rb["y"]))
        if not pt and self.rect:
            pt = (int(self.rect[2] * 0.943), int(self.rect[3] * 0.85))
            if self._restart_tries >= 3 and self._restart_tries % 3 == 0:
                # no revivo: algo tapa el botón Restart (pasó con el minimapa corrido ahí). Lo arrastro arriba.
                sc_ = self.rect[2] / 1280.0
                self._drag_bg(1160 * sc_, 770 * sc_, 1160 * sc_, 110 * sc_)
                time.sleep(0.6)
                self.log("☠ No lograba revivir: corrí lo que tapaba el botón Restart.")
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
        # con pociones encima aguanto peleando y tomando; escapar a pie casi nunca salva. Sin pociones, me voy antes.
        _sm = self._supplies_mem()
        panic_base = self.learner.knob("panic_hp_pct", cfg["panic_hp_pct"]) if cfgmod.server_slug(cfg) != "abaddon" else cfg["panic_hp_pct"]
        panic = panic_base if not _sm or _sm["pot"] < 2 else min(panic_base, int(cfg.get("panic_with_pots_pct", 18)))
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
            if hm < getattr(self, "_hm_prev", hm):
                self._last_dmg_t = now  # me pegaron
            self._hm_prev = hm
            # fuera de pelea la vida vuelve sola: no gasto pociones (idea del bot de prueba de Helbreath/server)
            fighting = now - getattr(self, "_last_dmg_t", 0) < 5 or self._adjacent_mobs() > 0
            if not fighting and self.pos.get("x") is not None and cfgmod.server_slug(cfg) != "abaddon":
                # un slime a 3 baldosas me ve y viene: no me siento a descansar al lado
                px_, py_ = self.pos["x"], self.pos["y"]
                fighting = any(max(abs(e[0] - px_), abs(e[1] - py_)) <= 3 for e in (getattr(self, "_oly_last", None) or []))
            if (hp <= max(60, cfg.get("hp_pot_pct", 55)) and (fighting or hp <= 35)
                    and now - getattr(self, "_last_pot_t", 0) > min(0.8, cfg.get("pot_cooldown", 1.5))):
                self._last_pot_t = now
                if self._consume("pot"):
                    hm2 = self._hp_mem() or hm
                    hp = min(100.0, 100.0 * hm2 / self._hp_max)
                    b = dict(b, hp=hp)
        if hp == 0 and not b.get("mp") and not b.get("sp"):
            hp = None  # todo en 0: la imagen todavía no cargó, no es que me morí
        if hp is not None:
            if hp < getattr(self, "_hp_prev_pct", hp) - 0.5:
                self._last_dmg_t = now
            self._hp_prev_pct = hp
        # vida baja tiene que durar un ratito (al entrar/salir de edificios la pantalla parpadea)
        if hp is not None and hp <= panic:
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
            elif hp is not None:
                # tope: si en 45 s la vida no sube (sin comida, algo raro), dejo de huir en vez de dar vueltas para siempre
                best = getattr(self, "_flee_best", None)
                if best is None or hp > best[0] + 2:
                    self._flee_best = (hp, now)
                elif now - best[1] > 45:
                    self._flee_until, self._flee_best = 0, None
                    self._flee_block = now + 20
                    self.log(f"Vida {hp:.0f}%: no sube huyendo; vuelvo a actuar.")
            return False
        self._flee_best = None
        # descanso fuera de pelea: con menos de 50% y nadie pegándome, me quedo quieto hasta 80% (máx. 60 s)
        if hp is not None and cfg.get("rest_enabled", True):
            fighting = now - getattr(self, "_last_dmg_t", 0) < 5 or self._adjacent_mobs() > 0
            rs = getattr(self, "_rest_since", 0)
            if rs:
                if hp >= self.learner.knob("rest_until_pct", 80) or fighting or now - rs > 60:
                    self._rest_since = 0
                    if now - rs > 60 and hp < 80:
                        self._rest_block = now + 30
                    if fighting and now - rs < 8:
                        self._rest_block = now + 10  # me interrumpieron enseguida: no insisto ya mismo
                    elif now - rs >= 8:
                        self.log(f"Descanso terminado (vida {hp:.0f}%).")
                else:
                    self._release()
                    self.stats["state"] = f"descansando ({hp:.0f}%)"
                    return True
            elif hp < self.learner.knob("rest_below_pct", 50) and not fighting and now > getattr(self, "_rest_block", 0) and not self._is_travelling():
                self._rest_since = now
                self._release()
                self.state, self.target = "seek", None
                if now - getattr(self, "_rest_said", 0) > 60:
                    self._rest_said = now
                    self.log(f"Vida {hp:.0f}% y nadie me pega: descanso un poco en vez de gastar pociones.")
                return True
        # enjambre: con 3+ bichos pegados y menos de 60% de vida, me retiro antes de quedar acorralado
        if (hp is not None and hp < 60 and cfg.get("panic_action") == "flee" and now > getattr(self, "_flee_block", 0)
                and not (cfgmod.server_slug(cfg) != "abaddon" and "beginner" in (self.pos.get("map") or "").lower())
                and self._adjacent_mobs() >= 3):
            self._flee_until = now + cfg.get("flee_max_sec", 600)
            self._release()
            self.state, self.target = "seek", None
            self.log(f"¡{self._adjacent_mobs()} bichos encima y vida {hp:.0f}%! Me retiro antes de que me rodeen.")
            return False
        if hp is not None and hp <= panic and cfg.get("panic_action") == "flee" and now > getattr(self, "_flee_block", 0) \
                and not (cfgmod.server_slug(cfg) != "abaddon" and "beginner" in (self.pos.get("map") or "").lower()):
            self._do_action(cfg["hp_pot_action"], "hp_pot_slot")
            self._flee_until = now + cfg.get("flee_max_sec", 600)
            self._release()
            tt = (self.target or {}).get("etype")
            if False and tt is not None:
                # una sola huida no alcanza (capaz me pegaba otro): lo esquivo recién si me pasa dos veces seguidas
                hard = self.world.learn.setdefault("hard_types", {})
                h = hard.get(str(tt))
                h = h if isinstance(h, dict) and now - h.get("t", 0) < 1800 else {"n": 0}
                h = {"lv": int(cfg.get("travel", {}).get("my_level", 1)), "t": now, "n": h.get("n", 0) + 1}
                hard[str(tt)] = h
                self.world.save()
                if h["n"] >= 2:
                    self.log(f"Ese bicho (tipo {tt}) me hizo huir dos veces: lo esquivo por 45 minutos.")
            self.state, self.target = "seek", None
            self.log(f"¡Vida {hp:.0f}%! Me alejo a descansar.")
            return False
        if hp is not None and hp <= panic:
            self.panic_count += 1
            self._do_action(cfg["hp_pot_action"], "hp_pot_slot")
            self.stats["hp_pots"] += 1
            if cfg["panic_action"] in ("stop", "recall_and_stop") and self.panic_count >= 3:
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

    def _drop_cursor_item(self):
        """Si no camino puede ser que haya ventanas abiertas tapando: las cierro con ESC y con sus botones."""
        try:
            sc = self.rect[2] / 1280.0
            winapi.move(*self.abs_pt(640 * sc, 760 * sc))
            for _ in range(2):
                winapi.press("ESC")
                time.sleep(0.25)
            if vision.find_bag_origin(self.screen.grab(self.rect)):
                winapi.click(*self.abs_pt(875 * sc, 918 * sc))
                time.sleep(0.4)
            self.log("No avanzaba: solté el mouse y cerré ventanas.")
        except Exception:
            pass

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
        if cfgmod.server_slug(self.cfg) != "abaddon":
            # clientes nuevos (Olympia) muestran más mapa: la baldosa sigue midiendo 64 px a 1080 de alto
            tile = max(16, int(round(64 * (gy2 - gy1) / 1080.0)))
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
        srv = (self.cfg.get("server") or {}).get("name") or "Abaddon"
        if srv.lower() == "abaddon":
            base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Helbreath Abaddon")
            for p in (os.path.join(base, "HelbreathAbaddon.exe"), os.path.join(base, "game", "Helbreath.exe")):
                if os.path.exists(p):
                    return p
        gd = self.world.game_dir
        if gd:
            # el lanzador suele estar una carpeta arriba del juego; si no, el exe del juego
            for d in (os.path.dirname(gd), gd):
                try:
                    exes = [f for f in os.listdir(d) if f.lower().endswith(".exe")
                            and not any(k in f.lower() for k in ("unins", "setup", "update", "crash"))]
                except OSError:
                    continue
                exes.sort(key=lambda f: (0 if srv.lower() in f.lower() else 1 if "helbreath" in f.lower() else 2))
                if exes:
                    return os.path.join(d, exes[0])
        return None

    def _screen_words(self):
        if self.img is None:
            return [], ""
        h, w = self.img.shape[:2]
        words = ocr.read_words(self.img, {"x1": 0, "y1": 0, "x2": w, "y2": h}, scale=1.0)
        return words, " ".join(x[0].lower() for x in words)

    def _game_on_top(self):
        """True si en el centro y en las cuatro esquinas del área del juego lo que hay en pantalla es la ventana del juego."""
        if not winapi.IS_WIN or not self.hwnd or not getattr(self, "rect", None):
            return True
        if getattr(self.screen, "_wgc", None) and self.cfg.get("background_mode"):
            return True  # la imagen viene de la ventana del juego (WGC) y los clicks van directo a ella
        try:
            import ctypes
            import ctypes.wintypes as wt
            u = ctypes.windll.user32
            u.WindowFromPoint.argtypes = [wt.POINT]
            u.WindowFromPoint.restype = wt.HWND
            u.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
            u.GetAncestor.restype = wt.HWND
            x, y, w, h = self.rect
            for fx, fy in ((0.5, 0.45), (0.1, 0.1), (0.9, 0.1), (0.1, 0.85), (0.9, 0.85)):
                hw = u.WindowFromPoint(wt.POINT(int(x + w * fx), int(y + h * fy)))
                root = u.GetAncestor(hw, 2) if hw else None
                if not root or int(root) != int(self.hwnd):
                    return False
            return True
        except Exception:
            return False

    def _autologin_step(self):
        """True si está ocupado reconectando (no cazar). Sale solo cuando vuelve a leer la posición en el mapa."""
        al = self.cfg.get("autologin", {})
        if not al.get("enabled"):
            return False
        now = time.time()
        in_game = (not self.cfg["calib"].get("pos_box")) or (self.pos.get("t") and now - self.pos["t"] < 20 and self.pos.get("map"))
        # la memoria puede seguir dando la última posición aunque el juego se haya desconectado: si el vigilante
        # vio una pantalla de menú, mando a reconectar igual
        if now < getattr(self, "_force_relog", 0):
            in_game = False
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
        # SEGURIDAD: si lo que veo no es el juego (otra ventana encima, el propio bot, la barra de tareas),
        # no hago clicks ni escribo nada: el texto leído podría ser de otra ventana.
        if not self._game_on_top():
            self._release()
            self.stats["state"] = "reconectando: no veo el juego"
            if now - getattr(self, "_al_hidden_said", 0) > 300:
                self._al_hidden_said = now
                self.log("⛔ Auto-login: hay otra ventana encima del juego. No toco nada hasta ver el juego adelante.")
            return True
        words, txt = self._screen_words()
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        # 1b) lista de personajes (acá vuelve el juego cuando se corta la conexión). OJO: tiene un botón
        #     "Change Password": en esta pantalla NUNCA se escribe la contraseña, solo se aprieta Start.
        sc_ = self.rect[2] / 1280.0
        ntxt = norm(txt)
        # 0) menú principal (Log In / New Account / Exit): aprieto "Log In"
        if "exit" in ntxt and ("account" in ntxt or "log" in ntxt) and "password" not in ntxt and "server" not in ntxt:
            self._release()
            self.stats["state"] = "reconectando: menú"
            if now - getattr(self, "_al_menu_try", 0) < 12:
                return True
            self._al_menu_try = now
            w_ = next((w for w in words if norm(w[0]) in ("login", "log")), None)
            winapi.click(*(self.abs_pt(w_[1] + (w_[3] if norm(w_[0]) == "log" else 0), w_[2]) if w_ else self.abs_pt(872 * sc_, 397 * sc_)))
            self.log("Auto-login: estoy en el menú principal, aprieto Log In.")
            return True
        # 1a) elegir servidor (primera pantalla del juego)
        # (el OCR lee mal esa letra gótica: alcanza con ver "server" sin nada de la lista de personajes)
        if ("server" in ntxt and "password" not in ntxt
                and not any(k in ntxt for k in ("character", "createnew", "logout", "delete"))):
            self._release()
            self.stats["state"] = "reconectando: servidor"
            if now - getattr(self, "_al_srv_try", 0) < 15:
                return True
            self._al_srv_try = now
            # el renglón con el nombre del servidor configurado; si no lo leo, el primero que dice SERVER
            sname = norm((self.cfg.get("server") or {}).get("name") or "abaddon")
            own = [w for w in words if len(sname) >= 4 and (sname[:5] in norm(w[0]) or norm(w[0])[:5] in sname and len(norm(w[0])) >= 4)
                   and w[2] > self.rect[3] * 0.25]
            sv = sorted([w for w in words if "server" in norm(w[0])], key=lambda w: w[2])
            if own:
                pt = self.abs_pt(own[0][1] + own[0][3] / 2 if len(own[0]) > 3 else own[0][1], own[0][2])
            elif sv:
                pt = self.abs_pt(sv[0][1] - 60 * sc_, sv[0][2])
            else:
                pt = self.abs_pt(430 * sc_, 392 * sc_)
            winapi.click(*pt)
            self.log(f"Auto-login: elijo el servidor {(self.cfg.get('server') or {}).get('name') or 'Abaddon'}.")
            return True
        on_list = any(k in ntxt for k in ("characterlist", "character", "createnew", "logout", "newpass", "passconfirm", "change", "testserver"))
        if on_list:
            self._release()
            self.stats["state"] = "reconectando: personaje"
            fails = getattr(self, "_al_fails", 0)
            if now - getattr(self, "_al_char_try", 0) < (20 if fails < 5 else 90):
                return True
            self._al_char_try = now
            self._al_fails = fails + 1
            if any(k in ntxt for k in ("newpass", "passconfirm")):
                c = next((w for w in words if norm(w[0]) == "cancel"), None)
                winapi.click(*(self.abs_pt(c[1], c[2]) if c else self.abs_pt(778 * sc_, 622 * sc_)))
                self.log("Auto-login: había quedado abierto el cartel de cambiar contraseña: lo cancelé (no escribí nada).")
                return True
            if any(k in ntxt for k in ("connectionlost", "cannot", "cannotchange")):
                ok = next((w for w in words if norm(w[0]) == "ok"), None)
                winapi.click(*(self.abs_pt(ok[1], ok[2]) if ok else self.abs_pt(778 * sc_, 545 * sc_)))
                time.sleep(1.0)
                if fails == 4:
                    self.log("Auto-login: el server no responde (Connection Lost). Sigo probando cada minuto y medio.")
                return True
            # el botón Start está a la derecha; a la izquierda hay un texto de ayuda que también dice "START"
            st = next((w for w in words if norm(w[0]) == "start" and w[1] > self.rect[2] * 0.55), None)
            winapi.click(*(self.abs_pt(st[1], st[2]) if st else self.abs_pt(852 * sc_, 575 * sc_)))
            if fails < 3:
                self.log("Auto-login: estoy en la lista de personajes, aprieto Start.")
            return True
        self._al_fails = 0
        # 2) pantalla de login
        if any(k in txt for k in ("password", "contraseña", "account", "login", "log in")):
            self._release()
            self.stats["state"] = "reconectando: login"
            if now - getattr(self, "_al_login_try", 0) < 25:
                return True
            self._al_login_try = now
            # la contraseña solo se escribe con el juego en primer plano, sin nada encima, y en una pantalla de login
            # leída del juego (nunca en segundo plano: las teclas podrían terminar en el chat)
            if winapi.IS_WIN and (not winapi.is_foreground(self.hwnd) or not self._game_on_top()):
                self.log("⛔ Auto-login: el juego no está adelante; no escribo la contraseña.")
                return True
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
        self._force_relog = 0  # no es ninguna pantalla de menú: estoy en el juego
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
            winapi.set_background(self.hwnd if self.cfg.get("background_mode") else None)
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
        if self._mem_struct() and not self.ents.center and not self.ents.finding \
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

    # ---------- puesta a punto: abre todas las ventanas, las fotografía, lee sus textos y las cierra ----------
    def setup_scan(self):
        th = getattr(self, "_setup_th", None)
        if th is not None and th.is_alive():
            return {"ok": False, "msg": "Ya estoy haciendo la puesta a punto."}
        if not self._ensure_window():
            return {"ok": False, "msg": "Abrí el juego y entrá con el pj."}
        was = self.running
        if was:
            self.stop("puesta a punto")

        def go():
            from . import setup_scan as ss
            self._setup_busy = True
            if self.cfg.get("background_mode"):
                winapi.set_background(self.hwnd)
            try:
                self.log("🛠 Puesta a punto: abro y cierro todas las ventanas del juego (unos 3 minutos). No toques el juego.")
                d = os.path.join(getattr(self, "pdir", cfgmod.DATA), "puesta_a_punto")
                zp = ss.SetupScan(self, d).run(self.log)
                self.log(f"🛠 Puesta a punto lista: {zp}" if zp else "🛠 Puesta a punto: no pude ver el juego.")
            except Exception as e:
                self.log(f"🛠 Puesta a punto: error {e!r}")
            finally:
                self._setup_busy = False
                winapi.set_background(self.hwnd if self.cfg.get("background_mode") else None)
                if was:
                    self.start()

        self._setup_th = threading.Thread(target=go, daemon=True)
        self._setup_th.start()
        return {"ok": True, "msg": "Puesta a punto en marcha: unos 3 minutos. No toques el juego."}

    # ---------- investigación: el bot recorre y saca fotos para poder programar lo que falta ----------
    def investigate(self):
        if cfgmod.server_slug(self.cfg) != "abaddon":
            # en Olympia la investigación de Abaddon (herrero/tienda con clicks de Abaddon) es peligrosa:
            # acá "investigar" = relevar el mapa (dónde sale cada bicho)
            return self.explore_map()
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

    def _loc_map_name(self, name):
        """Clientes traducidos (Olympia en castellano: "Granja Aresden"): paso el nombre del mapa al inglés
        con la tabla locale.csv del juego, para que valga todo lo que el bot sabe de cada mapa."""
        tb = getattr(self, "_locmaps", None)
        if tb is None:
            tb = self._locmaps = {}
            gd = self.world.game_dir
            try:
                import csv
                with open(os.path.join(gd, "locale.csv"), encoding="utf-8-sig", newline="") as f:
                    for row in csv.reader(f):
                        if len(row) > 2 and row[0].startswith("map.") and row[1] and row[2]:
                            for tr in row[2:]:
                                k = "".join(c for c in tr.lower() if c.isalnum())
                                if k:
                                    tb.setdefault(k, row[1])
            except Exception:
                pass
        if not tb:
            return name
        k = "".join(c for c in str(name).lower() if c.isalnum())
        if k in tb:
            return tb[k]
        import difflib
        m = difflib.get_close_matches(k, list(tb), n=1, cutoff=0.85)
        return tb[m[0]] if m else name

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
        name = self._loc_map_name(name)
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
        # plan propio del server (armado con los requisitos reales de sus tiendas) si existe; si no, la guía general
        plan = (self._load_json("stat_plan.json") or {}).get("stat_plan") or self._load_json("training_path.json").get("stat_plan", [])
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
            if self._mem_struct():
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
        """Poción de vida con la tecla del juego (1), sin abrir el bolso. Verifico en la memoria que haya una
        poción menos (mirar si sube la vida no sirve: en plena pelea la vida baja igual y parecía que fallaba)."""
        key = self.cfg.get("potion_key", "1")
        if not key or time.time() < getattr(self, "_potkey_off", 0):
            return False
        self._inv_cache = None
        sm0 = self._supplies_mem()
        if not sm0 or sm0["pot"] <= 0:
            return False
        winapi.press(str(key))
        t0 = time.time()
        while time.time() - t0 < 0.7:
            time.sleep(0.12)
            self._inv_cache = None
            sm = self._supplies_mem()
            if sm and sm["pot"] < sm0["pot"]:
                if not getattr(self, "_potkey_ok", False):
                    self._potkey_ok = True
                    self.log(f"🧪 Las pociones andan con la tecla {key}: ya no abro el bolso para tomarlas.")
                self._potkey_fail = 0
                self.stats["hp_pots"] = self.stats.get("hp_pots", 0) + 1
                self._left = dict(getattr(self, "_left", {}), pot=sm["pot"])
                self._last_move = time.time()
                return True
        self._potkey_fail = getattr(self, "_potkey_fail", 0) + 1
        if self._potkey_fail >= 4:
            self._potkey_fail = 0
            self._potkey_off = time.time() + (15 if getattr(self, "_potkey_ok", False) else 600)
        return getattr(self, "_potkey_ok", False)  # si la tecla ya anduvo antes, no abro el bolso en plena pelea

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
            if self._mem_struct():
                d = self.mem.proc.read(self.mem.addr[1] + 80, 4)
                v = int.from_bytes(d, "little", signed=True) if d else -1
                if 0 <= v <= 600:
                    return v
        except Exception:
            pass
        sh = self._sheet(1800)
        return sh["pending"] if sh else None

    def _stats_mem(self):
        """STR/VIT/DEX/INT/MAG/CHR desde la memoria (al lado de la posición)."""
        try:
            if self._mem_struct():
                d = self.mem.proc.read(self.mem.addr[1] + 32, 24)
                v = [int.from_bytes(d[i:i + 4], "little", signed=True) for i in range(0, 24, 4)]
                if all(1 <= x <= 400 for x in v):
                    return dict(zip(("STR", "VIT", "DEX", "INT", "MAG", "CHR"), v))
        except Exception:
            pass
        sh = self._sheet(6 * 3600)
        if sh:
            st = dict(sh["stats"])
            st["CHR"] = st.get("LUK")
            return st
        return {}

    # ---------- ficha del pj leída de la pantalla (servers sin estructura de memoria conocida) ----------
    def _sheet(self, max_age=1800):
        """Última ficha leída (nivel, exp, stats) si no es muy vieja."""
        sh = getattr(self, "sheet", None)
        if sh is None and not getattr(self, "_sheet_loaded", False):
            self._sheet_loaded = True
            try:
                with open(os.path.join(cfgmod.profile_dir(self.cfg), "ficha.json"), encoding="utf-8") as f:
                    sh = self.sheet = json.load(f)
                self._exp_off = sh.get("exp_off", 0)
            except Exception:
                sh = None
        return sh if sh and time.time() - sh.get("t", 0) < max_age else None

    def _ficha_glyphs(self):
        """Cifras de la letra de la ficha del personaje, aprendidas de números que el OCR leyó enteros."""
        from . import glyphs as glmod
        path = os.path.join(cfgmod.profile_dir(self.cfg), "glifos_ficha.json")
        g = getattr(self, "_fglyphs", None)
        if g is None or g.path != path:
            g = self._fglyphs = glmod.GlyphReader(path)
        return g

    def _ficha_mask(self, im, box):
        x1, y1, x2, y2 = [int(v) for v in box]
        roi = im[max(0, y1):y2, max(0, x1):x2]
        if roi.size == 0:
            return None
        m = self._text_mask(roi)
        ys, xs = np.nonzero(m)
        if len(xs) < 4:
            return None
        return m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]

    def _ficha_num(self, im, box, max_segs=3):
        m = self._ficha_mask(im, box)
        return self._ficha_glyphs().read(m, max_segs=max_segs) if m is not None else None

    def _sheet_read(self):
        """Abre la ventana del personaje (F5), lee nivel, experiencia y stats, y la cierra. Sirve en cualquier
        cliente que muestre 'Nivel/Exp/STR/VIT/DEX/INT/MAG/LUK' con el número a la derecha. None si no pude."""
        if not self.rect:
            return None
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        fix = str.maketrans({"o": "0", "O": "0", "D": "0", "g": "9", "q": "9", "l": "1", "I": "1", "|": "1", "i": "1",
                             "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2", "A": "4", "b": "6", "G": "6", "T": "7"})

        def num(t, big=False):
            d = "".join(c for c in (t or "").strip().translate(fix) if c.isdigit())
            return int(d) if d and (big or len(d) <= 3) else None

        kx = self.rect[2] / 1920.0
        labels = {"STR": ("str", "slr", "sir"), "VIT": ("vit", "v1t"), "DEX": ("dex", "oex", "0ex"), "INT": ("int", "lnt", "1nt"),
                  "MAG": ("mag",), "LUK": ("luk", "lux", "lik", "lix", "luc"), "LEVEL": ("nivel", "level"), "EXP": ("exp",),
                  "NEXT": ("sig", "next")}
        reach = {"LEVEL": 300, "EXP": 300}
        self._release()
        best = {}
        opened = False
        try:
            for attempt in range(3):
                im = self.screen.grab(self.rect)
                ws = ocr.read_words(im, {"x1": 0, "y1": 0, "x2": im.shape[1], "y2": int(im.shape[0] * 0.92)},
                                    scale=(1.0, 1.5, 2.0)[attempt])
                is_open = any(norm(w[0]).startswith("estad") for w in ws) and any(norm(w[0]).startswith("misiones") for w in ws) \
                    or sum(1 for w in ws if norm(w[0])[:3] in ("str", "vit", "dex", "mag")) >= 3
                if not is_open:
                    if opened:
                        continue
                    winapi.press("F5")
                    opened = True
                    time.sleep(1.1)
                    im = self.screen.grab(self.rect)
                    ws = ocr.read_words(im, {"x1": 0, "y1": 0, "x2": im.shape[1], "y2": int(im.shape[0] * 0.92)}, scale=1.0)
                else:
                    opened = True
                ky = self.rect[3] / 1080.0
                pure = lambda t: t.strip().isdigit() and len(t.strip()) <= 3
                # columna de los valores de arriba (Nivel/Exp/Peso): la saco de la fila 'Peso: 23/115'
                col = next((v[1] for v in ws if "/" in v[0] and any(norm(w[0]) == "peso" and abs(w[2] - v[2]) < 10
                                                                    and 0 < v[1] - w[1] < 300 * kx for w in ws)), None)
                for key, keys in labels.items():
                    if best.get(key) is not None:
                        continue
                    lab = None
                    for w in sorted(ws, key=lambda w: w[2]):
                        t = norm(w[0])
                        if not any(t.startswith(k) for k in keys):
                            continue
                        if key == "LEVEL" and w[0].strip()[:1] not in ("N", "L"):
                            continue  # 'A sig. nivel', 'Subir nivel'
                        if key == "EXP" and (t != "exp" or any(norm(v[0]).startswith("desc") and abs(v[2] - w[2]) < 10 for v in ws)):
                            continue  # 'Exp Desc.'
                        lab = w
                        break
                    if not lab:
                        continue
                    if key in ("LEVEL", "EXP", "NEXT"):
                        cxv = col if col else lab[1] + 195 * kx
                        box = (int(cxv - 75 * kx), int(lab[2] - 13 * ky), int(cxv + 75 * kx), int(lab[2] + 13 * ky))
                    else:
                        x0 = int(lab[1] + lab[3] / 2 + 5 * kx)
                        box = (x0, int(lab[2] - 13 * ky), int(x0 + 58 * kx), int(lab[2] + 13 * ky))
                    inbox = [v for v in ws if box[0] <= v[1] <= box[2] and abs(v[2] - lab[2]) < 10]
                    if key == "NEXT":
                        v = self._ficha_num(im, box, max_segs=10)
                        if v is not None:
                            best[key] = v
                        continue
                    if key == "EXP":
                        cands = ["".join(v[0] for v in sorted(inbox, key=lambda v: v[1]))]
                        try:
                            crop = im[max(0, box[1] - 4):box[3] + 4, max(0, box[0]):box[2]]
                            for sc in (3.0, 4.0):
                                cands.append("".join(v[0] for v in sorted(
                                    ocr.read_words(crop, {"x1": 0, "y1": 0, "x2": crop.shape[1], "y2": crop.shape[0]}, scale=sc),
                                    key=lambda v: v[1])))
                        except Exception:
                            pass
                        v = self._ficha_num(im, box, max_segs=10)
                        if v is not None:
                            best[key] = v
                            continue
                        good = [int(re.sub(r"[.,\s]", "", c)) for c in cands if re.fullmatch(r"\d{1,3}([.,]\d{3})+", c.strip())]
                        if good:
                            best[key] = max(set(good), key=good.count)
                        continue
                    v = self._ficha_num(im, box)
                    if len(inbox) == 1 and pure(inbox[0][0]) and len(inbox[0][0].strip()) >= 2:
                        o = int(inbox[0][0])
                        if v is None or v == o:
                            m_ = self._ficha_mask(im, box)
                            if m_ is not None:
                                self._ficha_glyphs().learn(m_, str(o))  # el OCR leyó el número entero: aprendo sus cifras
                            v = o
                    if v is not None:
                        best[key] = v
                dbg = getattr(self, "_sheet_dbg", None)
                if dbg is not None:
                    dbg.append(dict(best))
                if all(best.get(k) is not None for k in labels if k != "NEXT"):
                    break
        except Exception:
            pass
        finally:
            if opened:
                try:
                    winapi.press("F5")
                    time.sleep(0.5)
                    self._junk_t = 0
                except Exception:
                    pass
        names = ("STR", "VIT", "DEX", "INT", "MAG", "LUK")
        if not best.get("LEVEL") or any(best.get(n) is None for n in names) or not all(5 <= best[n] <= 400 for n in names):
            self._sheet_fail = getattr(self, "_sheet_fail", 0) + 1
            return None
        lvl = best["LEVEL"]
        start = int(self.cfg.get("stats_start_total", 70))
        per = int(self.cfg.get("stats_per_level", 3))
        pend = per * (lvl - 1) - (sum(best[n] for n in names) - start)
        prev = self._sheet(10 ** 9)
        if not (1 <= lvl <= 250) or not (0 <= pend <= per * 60) or (prev and not (0 <= lvl - prev["level"] <= 5)):
            self._sheet_fail = getattr(self, "_sheet_fail", 0) + 1
            return None  # lectura que no cierra (un dígito mal leído): no la uso
        sh = {"t": time.time(), "level": lvl, "exp": best.get("EXP"), "next": best.get("NEXT"), "pending": max(0, pend),
              "stats": {n: best[n] for n in names}}
        # 'Exp' es la del nivel actual (vuelve a cero al subir): armo un total que no retrocede para medir exp por hora
        off = getattr(self, "_exp_off", 0)
        if prev and lvl > prev["level"] and prev.get("exp") is not None:
            off += prev["exp"] + (prev.get("next") or 0)
            self._exp_off = off
        sh["exp_total"] = off + sh["exp"] if sh["exp"] is not None else None
        if sh["exp"] is not None and sh["next"]:
            sh["pct"] = round(100.0 * sh["exp"] / (sh["exp"] + sh["next"]), 1)
        self.sheet = sh
        self._sheet_loaded = True
        try:
            with open(os.path.join(cfgmod.profile_dir(self.cfg), "ficha.json"), "w", encoding="utf-8") as f:
                json.dump(dict(sh, exp_off=getattr(self, "_exp_off", 0)), f)
        except Exception:
            pass
        self._sheet_fail = 0
        self._stats_vals = dict(sh["stats"])
        t = self.cfg.setdefault("travel", {})
        if int(t.get("my_level", 0) or 0) != lvl:
            if int(t.get("my_level", 0) or 0) < lvl:
                self.log(f"⬆ Nivel {lvl} (leído en la ficha).")
            t["my_level"] = lvl
            cfgmod.save(self.cfg)
        return sh

    def _census(self):
        """Cada tanto, con el pj tranquilo, releo la ficha: es de donde salen nivel, exp por hora y puntos por repartir
        en los servers donde no leo eso de la memoria. True = usé el turno."""
        if cfgmod.server_slug(self.cfg) == "abaddon" or self._mem_struct() or not self.rect:
            return False
        now = time.time()
        every = 240 if getattr(self, "_sheet_fail", 0) < 3 else 900
        if now - getattr(self, "_census_t", 0) < every or self.state == "attack" or getattr(self, "_busy_ui", False):
            return False
        try:
            ents = self._oly_ents() if self.pos.get("x") is not None else []
            px, py = self.pos.get("x"), self.pos.get("y")
            if any(max(abs(e[0] - px), abs(e[1] - py)) <= 4 for e in ents or []):
                return False  # con bichos cerca no abro ventanas
        except Exception:
            pass
        self._census_t = now
        sh = self._sheet_read()
        if sh and sh["pending"] > 0 and self.cfg.get("stats_auto", {}).get("enabled"):
            self._last_stats = 0
        return True

    def _drag_real(self, x1, y1, x2, y2):
        """Arrastra con el mouse de verdad (las ventanas del juego no se mueven con clicks en segundo plano)."""
        if self.cfg.get("background_mode"):
            return self._drag_bg(x1, y1, x2, y2)  # en segundo plano nunca toco tu mouse
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

    def _do_stats_ui(self):
        """Reparto de stats en clientes nuevos (Olympia): ventana 'Subir nivel' con '-' y '+' por stat y 'Aplicar'.
        Sigue la guía de guerrero 2H (training_path.json). Verifica cada número leído antes de aplicar."""
        if not self.rect:
            return
        self._release()
        grab = lambda: self.screen.grab(self.rect)
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        click = lambda x, y, b="left": winapi.click(*self.abs_pt(x, y), button=b)

        def find_win(img):
            ws = ocr.read_words(img, {"x1": 0, "y1": 0, "x2": img.shape[1], "y2": int(img.shape[0] * 0.92)}, scale=1.0)
            ap = next((w for w in ws if norm(w[0]).startswith("aplicar")), None)
            asg = next((w for w in ws if norm(w[0]).startswith("asignar")), None)
            sts = [w for w in ws if norm(w[0]) == "str"]
            if asg:
                # solo la fila STR de la ventana (debajo de 'Puntos por asignar'), no el 'STR' de un cartelito de ayuda
                ky = self.rect[3] / 1080.0
                sts = [w for w in sts if 0 < w[2] - asg[2] < 120 * ky and w[1] < asg[1]]
            st = min(sts, key=lambda w: w[2]) if sts else None
            if not st and asg:
                # el chat del juego a veces pasa por encima de la fila STR: la ubico desde 'Puntos por asignar'
                kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
                st = ("STR", asg[1] - 130 * kx, asg[2] + 60 * ky)
            return (st, ap, asg) if st and ap else None

        img = grab()
        win = find_win(img)
        if not win:
            # el texto 'Subir de Nivel' de abajo a la derecha abre la ventana
            ws = ocr.read_words(img, {"x1": int(img.shape[1] * 0.6), "y1": int(img.shape[0] * 0.78),
                                      "x2": img.shape[1], "y2": int(img.shape[0] * 0.93)}, scale=1.0)
            sb = next((w for w in ws if norm(w[0]).startswith("subir") or norm(w[0]) == "nivel"), None)
            ob = self.cfg["calib"].get("levelup_btn") or {"x": int(self.rect[2] * 0.816), "y": int(self.rect[3] * 0.883)}
            pt = (sb[1] + 40, sb[2]) if sb else (ob["x"], ob["y"])  # el texto gótico casi nunca se lee: uso el lugar conocido
            for _try in range(3):
                if _try < 2:
                    # camino seguro: F5 (personaje) -> botón 'Subir nivel'
                    winapi.press("F5")
                    time.sleep(1.0)
                    im5 = grab()
                    w5 = ocr.read_words(im5, {"x1": 0, "y1": 0, "x2": im5.shape[1], "y2": int(im5.shape[0] * 0.92)}, scale=1.0)
                    sbt = next((w for w in w5 if norm(w[0]) == "subir" and any(norm(v[0]).startswith("misiones") for v in w5)), None)
                    if sbt:
                        click(sbt[1] + 25, sbt[2])
                    else:
                        winapi.press("F5")
                        time.sleep(0.4)
                        winapi.move(*self.abs_pt(*pt))
                        time.sleep(0.4)
                        click(*pt)
                else:
                    winapi.move(*self.abs_pt(*pt))
                    time.sleep(0.4)
                    click(*pt)
                time.sleep(1.2)
                img = grab()
                win = find_win(img)
                if win:
                    break
            if not win:
                self.log("Stats: no encuentro la ventana 'Subir nivel'.")
                self._stats_fail = getattr(self, "_stats_fail", 0) + 1
                return
            if self.cfg["calib"].get("levelup_btn") != {"x": int(pt[0]), "y": int(pt[1])}:
                self.cfg["calib"]["levelup_btn"] = {"x": int(pt[0]), "y": int(pt[1])}
                cfgmod.save(self.cfg)
        st, ap, asg = win
        rowh = 54 * self.rect[3] / 1080.0
        names = ["STR", "VIT", "DEX", "INT", "MAG", "LUK"]
        plus_x = st[1] + 225 * self.rect[2] / 1920.0

        fix = str.maketrans({"o": "0", "O": "0", "D": "0", "g": "9", "q": "9", "l": "1", "I": "1", "|": "1", "i": "1",
                             "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2", "A": "4", "b": "6", "G": "6", "T": "7"})

        def num(t):
            d = "".join(c for c in (t or "").strip().translate(fix) if c.isdigit())
            return int(d) if d and len(d) <= 3 else None

        def points(img):
            if not asg:
                return None
            # leo la ventana entera (en recortes chicos el OCR no ve un número solo) y tomo lo que sigue a 'asignar'
            ws = ocr.read_words(img, {"x1": 0, "y1": 0, "x2": img.shape[1], "y2": int(img.shape[0] * 0.92)}, scale=1.0)
            ws = [w for w in ws if abs(w[2] - asg[2]) < 14 and 0 < w[1] - asg[1] < 170]
            for w in sorted(ws, key=lambda w: w[1]):
                if w[1] > asg[1] + 20:
                    if w[0].strip() in ("O", "o"):
                        return None  # el OCR confunde 9 y 0 con 'O': no lo tomo como cero
                    v = num(w[0])
                    if v is not None:
                        return v
            return None

        def stats_f5():
            """Los números de la ventana de personaje (F5) se leen bien; los de 'Subir nivel' no."""
            winapi.press("F5")
            time.sleep(1.0)
            im = grab()
            ws = ocr.read_words(im, {"x1": 0, "y1": 0, "x2": im.shape[1], "y2": int(im.shape[0] * 0.92)}, scale=1.0)
            out = {}
            alias = {"LUK": ("luk", "lux", "lik", "lix", "luc"), "STR": ("str", "slr", "sir"), "INT": ("int", "lnt", "iNt")}
            lab = None
            for n in names:
                keys = alias.get(n, (n.lower(),))
                for w in ws:
                    t = norm(w[0])
                    k = next((k for k in keys if t.startswith(k.lower())), None)
                    if not k:
                        continue
                    rest = t[len(k):]
                    if rest and num(rest) is not None:  # 'STR:14' leído pegado
                        out[n], lab = num(rest), w
                        break
                    if t == k.lower():
                        right = sorted([v for v in ws if abs(v[2] - w[2]) < 10 and 0 < v[1] - w[1] < 90], key=lambda v: v[1])
                        if right and num(right[0][0]) is not None:
                            out[n], lab = num(right[0][0]), w
                            break
                out.setdefault(n, None)
            # nivel: la fila 'Nivel:' de arriba (no 'A sig. nivel' ni el botón 'Subir nivel')
            nvs = sorted([w for w in ws if norm(w[0]) == "nivel" and w[0].strip()[:1] == "N"], key=lambda w: w[2])
            for nv in nvs:
                right = sorted([v for v in ws if abs(v[2] - nv[2]) < 10 and 0 < v[1] - nv[1] < 260], key=lambda v: v[1])
                lv = next((num(v[0]) for v in right if num(v[0])), None)
                if lv:
                    out["LEVEL"] = lv
                    break
            winapi.press("F5")  # la cierro (misma tecla)
            time.sleep(0.6)
            if find_win_char(grab()):
                winapi.click(*self.abs_pt(lab[1], lab[2] - 150), button="right") if lab else None
            return out

        def find_win_char(im):
            ws = ocr.read_words(im, {"x1": 0, "y1": 0, "x2": im.shape[1], "y2": int(im.shape[0] * 0.92)}, scale=1.0)
            return any(norm(w[0]).startswith("estad") for w in ws) and any(norm(w[0]).startswith("misiones") for w in ws)

        # el nivel está arriba a la derecha de esta ventana ('Nivel 5'): lo sincronizo siempre, haya puntos o no
        try:
            ws_l = ocr.read_words(img, {"x1": 0, "y1": 0, "x2": img.shape[1], "y2": int(img.shape[0] * 0.92)}, scale=1.0)
            nv = next((w for w in ws_l if norm(w[0]).startswith("nivel") and asg and abs(w[2] - asg[2]) < 25 and w[1] > asg[1]), None)
            lvl_ = None
            if nv:
                if num(nv[0][5:]):
                    lvl_ = num(nv[0][5:])
                else:  # el OCR a veces lee el 5 como 'S': uso la misma tabla de arreglos que para los números
                    lw = next((w for w in sorted(ws_l, key=lambda w: w[1]) if abs(w[2] - nv[2]) < 12 and 0 < w[1] - nv[1] < 90 and num(w[0])), None)
                    lvl_ = num(lw[0]) if lw else None
            if lvl_ and 1 <= lvl_ <= 200:
                t_ = self.cfg.setdefault("travel", {})
                if int(t_.get("my_level", 0) or 0) != lvl_:
                    self.log(f"Nivel real: {lvl_} (leído en 'Subir nivel').")
                    t_["my_level"] = lvl_
                    cfgmod.save(self.cfg)
        except Exception:
            pass
        # si quedaron sumas sin aplicar de un intento anterior, las deshago para empezar de cero
        try:
            ws0 = ocr.read_words(img, {"x1": 0, "y1": 0, "x2": img.shape[1], "y2": img.shape[0]}, scale=1.0)
            rb0 = next((w for w in ws0 if norm(w[0]).startswith("restablecer")), None)
            if rb0:
                click(rb0[1], rb0[2])
                time.sleep(0.6)
                img = grab()
        except Exception:
            pass
        pts = points(img)
        for _ in range(3):
            if pts is not None:
                break
            time.sleep(0.6)
            img = grab()
            pts = points(img)
        if pts == 0 and not getattr(self, "_stats_force", None):
            self._stats_fail = 0
            click((st[1] + ap[1]) / 2, st[2] - 60, "right")  # nada para repartir: cierro
            return
        def boxes(im):
            """Los 6 números de las cajitas (STR..LUK) de 'Subir nivel': los pego en una sola fila y la leo de una
            (el OCR de Windows no lee números sueltos tan chicos)."""
            kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
            cx = int(st[1] + 66 * kx)
            parts = []
            for i in range(6):
                cy = int(st[2] + i * rowh)
                c = im[max(0, cy - int(11 * ky)):cy + int(11 * ky), max(0, cx - int(24 * kx)):cx + int(26 * kx)]
                if c.size == 0:
                    return None
                parts += [c, np.zeros((c.shape[0], int(60 * kx), 3), np.uint8)]
            h = min(p_.shape[0] for p_ in parts)
            row = np.hstack([p_[:h] for p_ in parts])
            pitch = parts[0].shape[1] + parts[1].shape[1]
            out = {}
            for sc in (3.0, 4.0, 2.0):
                try:
                    ws_ = ocr.read_words(row, {"x1": 0, "y1": 0, "x2": row.shape[1], "y2": row.shape[0]}, scale=sc)
                except Exception:
                    ws_ = []
                for w in ws_:
                    t = w[0].strip()
                    if t.isdigit() and 1 <= int(t) <= 250:
                        k = int(w[1] // pitch)
                        if 0 <= k < 6 and names[k] not in out:
                            out[names[k]] = int(t)
                if all(n in out for n in names):
                    break
            # para el plan alcanzan STR, VIT y DEX
            return out if all(n in out for n in ("STR", "VIT", "DEX")) else None

        bx = boxes(img)
        if bx:
            vals = dict(bx)
            try:
                wsl = ocr.read_words(img, {"x1": 0, "y1": 0, "x2": img.shape[1], "y2": int(img.shape[0] * 0.92)}, scale=1.0)
                nv = next((w for w in wsl if norm(w[0]) == "nivel" and abs(w[2] - asg[2]) < 25), None) if asg else None
                lvw = next((w for w in wsl if nv and abs(w[2] - nv[2]) < 12 and 0 < w[1] - nv[1] < 90 and norm(w[0]).isdigit()), None)
                if lvw:
                    vals["LEVEL"] = int(norm(lvw[0]))
                elif nv and norm(nv[0]) != "nivel" and norm(nv[0])[5:].isdigit():
                    vals["LEVEL"] = int(norm(nv[0])[5:])
            except Exception:
                pass
        else:
            # leo los stats actuales en la ventana de personaje (cierro un momento 'Subir nivel')
            click((st[1] + ap[1]) / 2, st[2] - 60, "right")
            time.sleep(0.6)
            vals = stats_f5()
        self._stats_vals = {k: v for k, v in vals.items() if v is not None}
        if vals.get("LEVEL"):
            t = self.cfg.setdefault("travel", {})
            if int(t.get("my_level", 0) or 0) != vals["LEVEL"]:
                t["my_level"] = vals["LEVEL"]  # el nivel real, leído de la ventana de personaje
                cfgmod.save(self.cfg)
        if getattr(self, "_stats_force", None) and self._sheet():
            vals = dict(self._sheet()["stats"], LEVEL=self._sheet()["level"])
        if any(vals.get(n) is None for n in ("STR", "VIT", "DEX")):
            self.log(f"Stats: no leo bien mis stats en la ventana de personaje ({vals}); no toco nada.")
            self._stats_fail = getattr(self, "_stats_fail", 0) + 1
            return
        ob = self.cfg["calib"].get("levelup_btn") or {"x": int(self.rect[2] * 0.816), "y": int(self.rect[3] * 0.883)}
        img = grab()
        win = find_win(img)
        if not win:
            winapi.press("F5")
            time.sleep(1.0)
            im5 = grab()
            w5 = ocr.read_words(im5, {"x1": 0, "y1": 0, "x2": im5.shape[1], "y2": int(im5.shape[0] * 0.92)}, scale=1.0)
            sbt = next((w for w in w5 if norm(w[0]) == "subir"), None)
            click(*((sbt[1] + 25, sbt[2]) if sbt else (ob["x"], ob["y"])))
            time.sleep(1.2)
            img = grab()
            win = find_win(img)
        if not win:
            self.log("Stats: no pude volver a abrir 'Subir nivel'.")
            return
        st, ap, asg = win
        plus_x = st[1] + 225 * self.rect[2] / 1920.0
        # puntos esperados: 3 por nivel desde el 1, menos lo ya repartido (arranque: 70 entre los 6 stats)
        pts_ocr = pts
        force = getattr(self, "_stats_force", None)
        if force:
            pts = force  # la ficha (F5) manda: es la cuenta exacta
            sh_ = self._sheet()
            if sh_:
                vals = dict(sh_["stats"], LEVEL=sh_["level"])
        elif vals.get("LEVEL") and all(vals.get(n) is not None for n in names):
            exp_pts = 3 * (vals["LEVEL"] - 1) - (sum(vals[n] for n in names) - int(self.cfg.get("stats_start_total", 70)))
            if pts is None or pts != exp_pts:
                self.log(f"Stats: leí {pts} puntos, la cuenta da {exp_pts}; uso el menor.")
                pts = min(p_ for p_ in (pts, exp_pts) if p_ is not None)
        if not pts or pts <= 0:
            return
        plan_vals = dict(vals, CHR=vals.get("LUK"))
        added = {}
        for _ in range(int(pts)):
            n = self._next_stat(plan_vals)
            if n not in names:
                break
            i = names.index(n)
            click(plus_x, st[2] + i * rowh)
            time.sleep(0.25)
            plan_vals[n] = (plan_vals.get(n) or 0) + 1
            added[n] = added.get(n, 0) + 1
        img2 = grab()
        pts2 = points(img2)
        ok = pts2 == int(pts) - sum(added.values()) or (pts2 is None and sum(added.values()) == int(pts)) \
            or (pts_ocr is not None and pts2 is not None and pts2 == int(pts_ocr) - sum(added.values())) \
            or (force and sum(added.values()) == int(force))
        if not ok:
            self.log(f"Stats: después de sumar {added} quedan {pts2} puntos (esperaba {int(pts) - sum(added.values())}); no aplico.")
            ws = ocr.read_words(img2, {"x1": 0, "y1": 0, "x2": img2.shape[1], "y2": img2.shape[0]}, scale=1.0)
            rb = next((w for w in ws if norm(w[0]).startswith("restablecer")), None)
            if rb:
                click(rb[1], rb[2])  # deshago lo pendiente (no se aplicó nada)
                time.sleep(0.5)
            click((st[1] + ap[1]) / 2, st[2] - 60, "right")
            self._stats_fail = getattr(self, "_stats_fail", 0) + 1
            return
        click(ap[1], ap[2])
        time.sleep(1.0)
        self.log("📈 Stats (guía 2H): " + ", ".join(f"+{v} {k}" for k, v in added.items()) + ".")
        self._stats_fail = 0
        img3 = grab()
        if find_win(img3):
            click((st[1] + ap[1]) / 2, st[2] - 60, "right")  # cierro con click derecho
            time.sleep(0.5)

    def _do_stats(self):
        if cfgmod.server_slug(self.cfg) != "abaddon":
            sh0 = self._sheet_read()
            if not sh0:
                self.log("Stats: no pude leer la ficha del personaje; pruebo más tarde.")
                return
            if sh0["pending"] <= 0:
                self._stats_fail = 0
                return  # no hay nada para repartir: ni abro la ventana
            self._stats_force = sh0["pending"]
            try:
                self._do_stats_ui()
                sh1 = self._sheet_read()
                if sh1:
                    dif = {n: sh1["stats"][n] - sh0["stats"][n] for n in sh0["stats"] if sh1["stats"][n] != sh0["stats"][n]}
                    if dif:
                        self.log("📈 Stats confirmados en la ficha: " + ", ".join(f"+{v} {k}" for k, v in dif.items())
                                 + (f" (quedan {sh1['pending']})" if sh1["pending"] else "") + ".")
                    elif sh1["pending"]:
                        self.log(f"Stats: siguen {sh1['pending']} puntos sin repartir; pruebo más tarde.")
                return
            finally:
                self._stats_force = None
                # nunca dejar 'Subir nivel' abierta (tapa el juego): si sigue ahí, click derecho adentro
                try:
                    im_ = self.screen.grab(self.rect)
                    ws_ = ocr.read_words(im_, {"x1": 0, "y1": 0, "x2": im_.shape[1], "y2": int(im_.shape[0] * 0.6)}, scale=1.0)
                    asg_ = next((w for w in ws_ if "".join(c for c in w[0].lower() if c.isalnum()).startswith("asignar")), None)
                    if asg_:
                        winapi.click(*self.abs_pt(asg_[1] + 300 * self.rect[2] / 1920.0, asg_[2]), button="right")
                        time.sleep(0.4)
                except Exception:
                    pass
                self._junk_t = 0  # revisar ya que no haya quedado ninguna ventana abierta
                self.img = self.screen.grab(self.rect) if self.rect else self.img
                self._close_junk_windows()
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
                if not self._lu_mem() or time.time() - getattr(self, "_lu_rec_t", 0) < 90:
                    return
                self._lu_rec_t = time.time()
                # la ventana puede haber quedado corrida fuera de la pantalla (a la izquierda): la traigo
                self._drag_bg(25 * sc, 388 * sc, 470 * sc, 300 * sc)
                img = grab()
                btn = vision.find_stat_buttons(img)
                if not btn:
                    # o no está abierta: F5 (ventana del personaje) -> botón "Level Set." (lo busco por el texto;
                    # la ventana puede estar en cualquier lado)
                    self._release()
                    winapi.press("F5")
                    time.sleep(1.2)
                    img = grab()
                    ws = ocr.read_words(img, {"x1": 0, "y1": 0, "x2": img.shape[1], "y2": img.shape[0]}, scale=1.0)
                    ls = next((w for w in ws if "".join(c for c in w[0].lower() if c.isalpha()) in ("levelset", "set")
                               and any("level" in v[0].lower() and abs(v[2] - w[2]) < 12 * sc and 0 < w[1] - v[1] < 90 * sc for v in ws + [w])), None)
                    lv = ls or next((w for w in ws if w[0].lower().startswith("level") and "set" in w[0].lower()), None)
                    if lv:
                        click(lv[1], lv[2])
                    else:
                        click(395 * sc, 607 * sc)
                    time.sleep(1.2)
                    img = grab()
                    btn = vision.find_stat_buttons(img)
                    if not btn:
                        winapi.press("ESC")
                        time.sleep(0.3)
                        winapi.press("ESC")
                        self.log("Stats: hay puntos para repartir pero no encuentro la ventana. Pruebo más tarde.")
                        return
                self.log("Stats: recuperé la ventana de puntos.")
            else:
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
        p = os.path.join(getattr(self, "pdir", cfgmod.DATA), "progreso.json")
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
        sm = t.get("stay_map")
        if sm and self._same(self.pos.get("map") or "", sm):
            return sm  # me pidieron quedarme en este mapa (ej. Beginner Zone, sin hacerme ciudadano)
        try:
            qd = self._quest_dest()
        except Exception:
            qd = None
        if qd:
            return qd
        lvl = int(t.get("my_level", 1))
        path = self._load_json("training_path.json")
        from . import brain as _brain
        st_ = self._stats_mem()
        phases = [ph for ph in path.get("phases", []) if _brain.phase_ok(ph, lvl, st_)]
        if not phases:
            return None
        cur = self.pos.get("map") or ""
        if self._errand_due():
            return "Aresden Farm"  # mandados (comprar, vender, reparar, provisiones): se hacen desde la Farm
        avoid = self._avoid()
        try:
            pick = self._yield_pick(phases, cur, avoid)
        except Exception:
            pick = None
        if pick:
            return pick
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

    def _yield_pick(self, phases, cur, avoid):
        """Elige el mapa de caza por rendimiento medido (experiencia por hora): me quedo al menos 10 minutos donde
        llego; después comparo con lo que ya medí y con los mapas que mis stats habilitan y todavía no probé
        (esos los pruebo primero, del más alto al más bajo). Se recalcula cada 30 s."""
        now = time.time()
        if now - getattr(self, "_yp_t", 0) < 30 and getattr(self, "_yp_v", None):
            return self._yp_v
        self._yp_t = now
        cands = []
        for ph in reversed(phases):
            for name in ph["maps"]:
                n = name.split(" (")[0]
                if n not in cands and (self._same(cur, n) or self._known_route(cur, n, avoid)):
                    cands.append(n)
        if not cands:
            return None
        here = next((n for n in cands if self._same(cur, n)), None)
        vis = getattr(self, "_visit", None)
        if here and (not vis or vis[0] != here):
            self._visit = vis = (here, now)
        scores = {n: self._yield_score(n) for n in cands}
        untried = [n for n in cands if scores[n] is None]
        # recién llegué a un mapa sin medir y no hay otro sin medir más alto en la escalera: me quedo a medirlo
        if here and scores[here] is None and untried and untried[0] == here:
            self._yp_v = here
            return here
        base = here or getattr(self, "_yp_hunt", None)  # en el pueblo comparo con el último mapa de caza
        if here:
            self._yp_hunt = here
        if untried:
            self._yp_v = pick = untried[0]  # el más alto de la escalera que todavía no medí
        else:
            pick = max(cands, key=lambda n: scores[n])
            if base in scores and pick != base and scores[pick] < 1.25 * scores[base]:
                pick = base  # no vale la pena el viaje por poca diferencia
        # recién llegado: me quedo al menos 20 minutos, salvo que se me vayan las pociones (puntaje castigado)
        if (here and pick != here and vis and now - vis[1] < 20 * 60 and scores.get(here) is not None
                and not self._yield_burning(here)):
            pick = here
        if pick != getattr(self, "_yp_said", None):
            self._yp_said = pick
            txt = ", ".join(f"{n} ~{int(v):,} exp/h".replace(",", ".") for n, v in scores.items() if v is not None)
            best_n = max((n for n in cands if scores[n] is not None), key=lambda n: scores[n], default=None)
            why = ("todavía no lo probé" if scores.get(pick) is None else
                   "es el que más rinde" if pick == best_n else "el mejor rinde poco más: no vale el viaje")
            self.log(f"📈 Rendimiento: {txt or 'sin medir todavía'} → voy a {pick} ({why}).")
        self._yp_v = pick
        return pick

    def _errand_due(self):
        """Motivo para volver al pueblo (o None). Se recalcula cada 15 s. Solo vuelvo por algo que valga el viaje."""
        now = time.time()
        if now - getattr(self, "_err_t", 0) < 15:
            return getattr(self, "_err_v", None)
        self._err_t = now
        v = None
        try:
            cur = self.pos.get("map") or ""
            in_town = any(self._same(cur, n) for n in ("Aresden Farm", "Aresden City"))
            if cur and now > getattr(self, "_err_block", 0):
                sm = self._supplies_mem()
                if sm and in_town and sm["pot"] < int(self.cfg.get("pots_to_leave_town", 10)):
                    v = "tengo pocas pociones para salir"
                elif sm and sm["pot"] <= int(self.cfg.get("buy_pots_below", 3)):
                    v = "me quedé sin pociones"
                elif sm and sm["meat"] <= int(self.cfg.get("buy_meat_below", 1)) and now - getattr(self, "_shop_last", 0) > 900:
                    v = "me quedé sin comida"
                elif self._worn_out() and now - getattr(self, "_rep_last", 0) > 600:
                    v = "el equipo está por romperse"
                elif now - getattr(self, "_gear_last", 0) > 45 * 60:
                    d = self._gear_state()
                    if d and d["free"] < 3 and d["sell"]:
                        v = "tengo el bolso lleno"
                    elif d and d["buy"] and (in_town or self._town_budget_left() > 240):
                        v = "ya puedo comprar " + ", ".join(b["name"] for b in d["buy"][:2])
            if v and not in_town:
                self._err_hold = 0
                if v != getattr(self, "_err_said", None):
                    self._err_said = v
                    self.log(f"🏠 Vuelvo al pueblo: {v}.")
            elif v and in_town:
                if not getattr(self, "_err_hold", 0):
                    self._err_hold = now + 600  # ya en el pueblo: 10 minutos para hacer el mandado
                elif now > self._err_hold and "pociones" not in v:
                    # no se pudo resolver: no me quedo dando vueltas, vuelvo a cazar y pruebo más tarde
                    # (sin pociones no salgo: me quedo cazando cerca del pueblo hasta poder comprar)
                    self._err_hold, self._err_block, v = 0, now + 45 * 60, None
            else:
                self._err_hold, self._err_said = 0, None
        except Exception:
            v = None
        self._err_v = v
        return v

    def _avoid(self):
        """Mapas que todavía no me conviene pisar según nivel y equipo (se recalcula cada minuto)."""
        now = time.time()
        if now - getattr(self, "_avoid_t", 0) > 60:
            try:
                from . import brain
                pj = self._load_json("pj.json") or self._load_json("pj.default.json")
                lvl = self._level_mem() or int(self.cfg.get("travel", {}).get("my_level", 1))
                # los mapas de la escalera de caza que ya me tocan por nivel no se frenan por "margen" ni "peligro"
                exempt = brain.ladder_maps(self._load_json("training_path.json"), lvl, self._stats_mem())
                self._avoid_v = brain.avoid_maps(self.db, lvl, getattr(self, "_owned", None) or pj.get("owned", []), self._load_json("tiendas.json"), self.cfg.get("comfort", {}), exempt)
                # mapas donde me mataron 2 veces seguidas: los dejo descansar (1 hora por muerte) y vuelvo a probar;
                # también vuelvo si subí 3 niveles
                ex_l = [str(e).split(" (")[0].strip().lower() for e in (exempt or [])]
                cand = []
                for mk, dm in self.world.learn.get("deaths", {}).items():
                    n = dm.get("n", 0)
                    # descanso de 1 a 3 horas como mucho (antes crecía sin tope y dejaba al pj sin dónde cazar)
                    if n >= 2 and lvl < dm.get("lvl", 0) + 3 and now - dm.get("t", 0) < 3600 * min(3, n - 1):
                        cand.append(mk)
                for mk in cand:
                    alts = [m for m in ex_l if m != mk and m not in cand and m not in self._avoid_v]
                    if mk in ex_l and not alts:
                        continue  # es el único mapa de caza que me toca: no me lo prohíbo, cazo con más cuidado
                    if mk not in self._avoid_v:
                        self._avoid_v.append(mk)
            except Exception:
                self._avoid_v = [a.lower() for a in self.cfg.get("travel", {}).get("avoid", [])]
            self._avoid_t = now
        return self._avoid_v

    def _map_cost(self, name):
        """Cuánto me cuesta pisar un mapa al armar un camino: ciudad y farm casi nada; mapas con bichos bravos
        para mi nivel, dungeons o lugares donde ya me mataron, mucho. Así el viaje va por lo seguro."""
        try:
            from . import brain
            n = (name or "").lower()
            lvl = self._level_mem() or int(self.cfg.get("travel", {}).get("my_level", 1))
            c = 1.0
            dg = brain.map_danger(self.db, n)
            c += 2.0 * max(0, dg - 1) + 6.0 * max(0, dg - brain.allowed_danger(lvl, self.cfg.get("comfort", {})))
            if any(k in n for k in ("dungeon", "tower", "mine")):
                c += 4.0
            dm = self.world.learn.get("deaths", {}).get(n)
            if dm:
                c += 8.0 * dm.get("n", 0)
            return c
        except Exception:
            return 1.0

    def _known_route(self, cur, dest, avoid=()):
        if not cur:
            return False
        known = {v["to"] for v in self.world.learn["links"].values()}
        dk = next((k for k in known if self._same(k, dest)), None)
        return bool(dk and self.world.route(cur, dk, forbidden=avoid, cost=self._map_cost))

    def _oly_ents(self):
        """Bichos alrededor según la memoria de Olympia: [(x, y, criatura, id)]. [] si no hay."""
        if cfgmod.server_slug(self.cfg) == "abaddon" or not getattr(self.mem, "proc", None):
            return []
        now = time.time()
        if now - getattr(self, "_oly_t", 0) < 0.15:
            return self._oly_last
        self._oly_t = now
        pdir = cfgmod.profile_dir(self.cfg)
        if getattr(self, "_oly_sig_dir", None) != pdir:
            self._oly_sig_dir = pdir
            sig = None
            try:
                with open(os.path.join(pdir, "memoria.json"), encoding="utf-8") as f:
                    sig = json.load(f).get("entidades")
            except Exception:
                pass
            self.oly.set_sig(sig or memory.DEFAULT_SIG)
        # lectura en tiempo real (hilo propio): tomo la última lista; si el hilo no anda, leo acá
        self.oly.start(lambda: getattr(self.mem, "proc", None))
        if now - getattr(self.oly, "latest_t", 0) < 1.0:
            raw = self.oly.latest
        else:
            try:
                raw = self.oly.read(self.mem.proc, full_every=3.0)
            except Exception:
                raw = []
        # server nuevo / cliente actualizado: si en 3 minutos cazando no veo ningún bicho en memoria, busco
        # solo dónde los guarda (los bichos que se mueven cerca del pj) y guardo la firma para la próxima
        if raw:
            self._oly_empty_t = 0
        elif self.running and self.pos.get("x") is not None:
            t0 = getattr(self, "_oly_empty_t", 0) or now
            self._oly_empty_t = t0
            th = getattr(self, "_oly_disc_th", None)
            if now - t0 > 180 and now - getattr(self, "_oly_disc_last", 0) > 900 and not (th and th.is_alive()):
                self._oly_disc_last = now
                self._oly_disc_th = threading.Thread(target=self._oly_discover, daemon=True)
                self._oly_disc_th.start()
        crs = self._creatures_db()
        seen, out = set(), []
        generic = {"name": "bicho", "type": -1, "dmg_avg": 0, "hit": 0, "hp": 0} if self.oly.sig.get("type_off") is None else None
        for x, y, typ, st, oid in raw:
            cr = crs.get(typ) or generic
            # el campo "id" de Olympia cambia solo (es un contador): identifico al bicho por tipo y baldosa
            key = (typ, x, y)
            if not cr or key in seen:
                continue
            seen.add(key)
            out.append((x, y, cr, key))
        if out and not getattr(self, "_oly_said", False):
            self._oly_said = True
            self.log(f"🧠 Memoria (Olympia): ya veo la lista de bichos ({len(out)} cerca). Cazo con ella.")
        self._oly_last = out
        try:
            self._spawn_record(out)
        except Exception:
            pass
        return out

    # ---- relevamiento de spawns del server (lo que el bot ve en la memoria)
    def _survey_path(self):
        return os.path.join(cfgmod.profile_dir(self.cfg), "spawns_vistos.json")

    def _survey(self):
        sv = getattr(self, "_svy", None)
        if sv is None or getattr(self, "_svy_dir", None) != self._survey_path():
            self._svy_dir = self._survey_path()
            try:
                with open(self._svy_dir, encoding="utf-8") as f:
                    sv = json.load(f)
            except Exception:
                sv = {"maps": {}}
            self._svy = sv
        return sv

    def _spawn_record(self, ents):
        """Cada 5 s anoto qué bicho veo y en qué baldosa (de a celdas de 4x4), por mapa."""
        now = time.time()
        if now - getattr(self, "_svy_t", 0) < 5 or not ents or not self.pos.get("map"):
            return
        self._svy_t = now
        sv = self._survey()
        mk = self.pos["map"]
        m = sv["maps"].setdefault(mk, {})
        for x, y, cr, _k in ents:
            if cr.get("type", -1) == -1 or not cr.get("name"):
                continue
            cell = f"{x // 4},{y // 4}"
            d = m.setdefault(cr["name"], {"type": cr.get("type"), "cells": {}})
            c = d["cells"].setdefault(cell, [0, int(now)])
            c[0] += 1
            c[1] = int(now)
        if now - getattr(self, "_svy_save", 0) > 120:
            self._svy_save = now
            self._spawn_zones_rebuild()

    def _spawn_zones_rebuild(self):
        """Junto las celdas vistas de cada bicho en rectángulos (zonas) y lo guardo."""
        sv = self._survey()
        zones = {}
        for mk, mobs in sv["maps"].items():
            zl = []
            for name, d in mobs.items():
                cells = {tuple(map(int, k.split(","))): v[0] for k, v in d["cells"].items() if v[0] >= 2}
                left = set(cells)
                while left:
                    st = [left.pop()]
                    grp = set(st)
                    while st:
                        cx_, cy_ = st.pop()
                        for nx in (cx_ - 1, cx_, cx_ + 1):
                            for ny in (cy_ - 1, cy_, cy_ + 1):
                                if (nx, ny) in left:
                                    left.discard((nx, ny))
                                    grp.add((nx, ny))
                                    st.append((nx, ny))
                    n = sum(cells[c] for c in grp)
                    if n < 4:
                        continue
                    xs = [c[0] for c in grp]
                    ys = [c[1] for c in grp]
                    zl.append({"mob": name, "type": d.get("type"), "rect": [min(xs) * 4, min(ys) * 4, max(xs) * 4 + 3, max(ys) * 4 + 3],
                               "seen": n, "survey": True})
            if zl:
                zones[mk] = {"zones": zl}
        sv["zones"] = zones
        try:
            with open(self._survey_path(), "w", encoding="utf-8") as f:
                json.dump(sv, f, ensure_ascii=False)
        except Exception:
            pass
        self._spawns = None  # que se recalcule con lo nuevo

    TOWNS = ("shop", "blacksmith", "cityhall", "city hall", "warehouse", "guild", "church", "magic tower", "barrack",
             "jail", "command hall", "tienda", "herrer")

    def _explore_step(self):
        """Mapa nuevo (o pedido desde el botón): lo recorro entero en zigzag anotando qué bicho sale dónde.
        Solo peleo si me atacan. Si me matan 2 veces mientras exploro, corto y cazo en lo que ya vi."""
        if cfgmod.server_slug(self.cfg) == "abaddon" or not self.cfg.get("explore_maps", True):
            return False
        if self.cfg.get("need_weapon"):
            return False  # sin arma no salgo a recorrer el mapa: primero consigo una
        mk = self.pos.get("map") or ""
        if not mk or any(k in mk.lower() for k in self.TOWNS) or self.pos.get("x") is None:
            return False
        sv = self._survey()
        ex = sv.setdefault("explored", {})
        st_ = ex.get(mk)
        if st_ and st_.get("done"):
            self._exploring = False
            return False
        m = self.world.map_for(mk)
        if not m:
            return False
        now = time.time()
        plan = getattr(self, "_exp_plan", None)
        if not plan or plan.get("map") != mk:
            # puntos caminables cada 10 baldosas, recorridos de cerca en cerca
            pts = []
            step = max(16, int(max(m.w, m.h) / 10))  # ~100 puntos por mapa (se ve unas 15 baldosas a cada lado)
            for yy in range(step // 2, m.h - 3, step):
                for xx in range(step // 2, m.w - 3, step):
                    c = m.nearest_walkable(xx, yy, r=6)
                    if c and m.ok(c[0], c[1]):
                        pts.append(tuple(c))
            px, py = self.pos["x"], self.pos["y"]
            order = []
            while pts:
                nxt = min(pts, key=lambda t: abs(t[0] - px) + abs(t[1] - py))
                pts.remove(nxt)
                order.append(nxt)
                px, py = nxt
            plan = self._exp_plan = {"map": mk, "pts": order, "t0": now, "n0": len(order), "deaths": (st_ or {}).get("deaths", 0),
                                     "tp": now, "last": None}
            ex[mk] = {"done": False, "deaths": plan["deaths"], "t": int(now)}
            self.log(f"🗺 Mapa sin relevar: {mk}. Lo recorro ({len(order)} puntos) para ver dónde sale cada bicho.")
        if plan["deaths"] >= 2 or now - plan["t0"] > 40 * 60 or not plan["pts"]:
            why = "me mataron 2 veces" if plan["deaths"] >= 2 else ("terminé" if not plan["pts"] else "pasaron 40 minutos")
            ex[mk] = {"done": True, "deaths": plan["deaths"], "t": int(now), "why": why}
            self._exp_plan = None
            self._exploring = False
            self._spawn_zones_rebuild()
            zs = self._survey().get("zones", {}).get(mk, {}).get("zones", [])
            self.log(f"🗺 Relevamiento de {mk} listo ({why}): {len(zs)} zona/s de bichos. "
                     + ", ".join(sorted({z['mob'] for z in zs})[:8]))
            return False
        if self._under_attack() or self._adjacent_mobs() > 0:
            return False  # primero me defiendo (lo hace la caza normal)
        self._exploring = True
        px, py = self.pos["x"], self.pos["y"]
        g = plan["pts"][0]
        if max(abs(g[0] - px), abs(g[1] - py)) <= 4 or (plan["last"] == g and now - plan["tp"] > 30):
            plan["pts"].pop(0)  # llegué (o no hay forma de llegar en 45 s): al siguiente
            plan["tp"], plan["last"] = now, None
            if len(plan["pts"]) % 10 == 0 and plan["pts"]:
                self.log(f"🗺 Relevando {mk}: faltan {len(plan['pts'])} de {plan['n0']} puntos.")
            return True
        if plan["last"] != g:
            plan["last"], plan["tp"] = g, now
        self.stats["state"] = f"relevando {mk} ({plan['n0'] - len(plan['pts'])}/{plan['n0']})"
        self._walk_to(m, g, "relevamiento")
        return True

    def explore_map(self):
        """Botón: vuelvo a relevar el mapa donde estoy."""
        mk = self.pos.get("map") or ""
        sv = self._survey()
        sv.setdefault("explored", {}).pop(mk, None)
        self._exp_plan = None
        return {"ok": True, "msg": f"Voy a relevar {mk or 'este mapa'}: recorro todo y anoto dónde sale cada bicho."}

    def _spawns_all(self):
        """Spawns de la base (Abaddon); en los mapas que ya relevé en este server, uso lo relevado."""
        base = dict(self._load_json("spawns.json").get("maps") or {})
        if cfgmod.server_slug(self.cfg) == "abaddon":
            return base
        # server nuevo: los spawns de Abaddon no sirven (Olympia los movió); solo lo que relevé acá
        out = {}
        for mk, v in self._survey().get("zones", {}).items():
            key = next((k for k in base if self._same(k, mk)), mk)
            out[key] = dict(base.get(key, {}), zones=v["zones"], surveyed=True)
        return out

    def _mobs_near_view(self):
        """Para la pantalla: bichos que veo ahora (memoria en tiempo real) alrededor del pj."""
        try:
            if cfgmod.server_slug(self.cfg) == "abaddon" or self.pos.get("x") is None:
                return None
            px, py = self.pos["x"], self.pos["y"]
            out = [{"x": e[0], "y": e[1], "n": e[2]["name"]} for e in (getattr(self, "_oly_last", None) or [])
                   if max(abs(e[0] - px), abs(e[1] - py)) <= 20]
            return {"map": self.pos.get("map"), "list": out[:80], "near": sum(1 for o in out if max(abs(o["x"] - px), abs(o["y"] - py)) <= 9)}
        except Exception:
            return None

    def _oly_kite(self, ents, force=False):
        """Pelear como un jugador: si me rodean más de los que aguanto o me queda poca vida, me alejo del grupo
        (para el lado contrario a los bichos) hasta que nadie me siga, descanso hasta 85% y vuelvo.
        Vale para cualquier bicho (uso la lista de la memoria). True = ocupé esta vuelta."""
        if self.pos.get("x") is None:
            return False
        px, py = self.pos["x"], self.pos["y"]
        now = time.time()
        hp = self.bars.get("hp")
        dist = lambda e, x=px, y=py: max(abs(e[0] - x), abs(e[1] - y))
        adj = [e for e in ents if dist(e) <= 1]
        near4 = [e for e in ents if dist(e) <= 4]
        # cuántos pegados aguanto: lo aprendido + 1, y uno más si tengo buena vida
        cap = min(self._oly_allow(), 1) + (1 if (hp or 0) >= 70 else 0)
        k = getattr(self, "_kite", None)
        if k is not None and adj:
            # rodeado y sin poder salir (el camino está tapado): huir es comer golpes de espalda. Peleo.
            lp = k.get("lp")
            if lp != (px, py):
                k["lp"], k["lpt"] = (px, py), now
            elif now - k.get("lpt", now) > 2.5:
                self._kite = None
                self._kite_block = now + 20
                self.log(f"🗡 No puedo salir ({len(adj)} encima): peleo en vez de huir.")
                return False
        if k is None and now < getattr(self, "_kite_block", 0) and not force:
            return False
        if k is None:
            if not (force or (hp is not None and hp < 50 and adj) or len(adj) > max(1, cap)):
                return False
            why = f"vida {hp:.0f}%" if hp is not None and hp < 45 else f"{len(adj)} bichos encima"
            k = self._kite = {"t": now, "pt": None, "why": why}
            self.log(f"🏃 Me alejo del grupo ({why}): descanso y vuelvo.")
        m = self.world.map_for(self.pos.get("map") or "")
        if not m:
            self._kite = None
            return False
        if not near4:
            # nadie cerca: descanso quieto hasta 85% (o un máximo de 90 s)
            if hp is None or hp >= 85 or now - k["t"] > 150:
                self._kite = None
                self.log(f"🏃 Vida {hp if hp is None else round(hp)}%: vuelvo a cazar.")
                return False
            self._release()
            self.nav["walking"] = False
            self.stats["state"] = f"descansando lejos ({hp:.0f}%)" if hp is not None else "descansando lejos"
            return True
        pt = k.get("pt")
        if not pt or any(max(abs(e[0] - pt[0]), abs(e[1] - pt[1])) <= 5 for e in ents) or dist((pt[0], pt[1])) <= 1:
            # punto nuevo: en un anillo de 8-12 baldosas, el más lejos de todos los bichos
            best, bs = None, -1
            for r in (9, 12, 7):
                for i in range(16):
                    a = 2 * math.pi * i / 16
                    c = (int(round(px + math.cos(a) * r)), int(round(py + math.sin(a) * r)))
                    c = m.nearest_walkable(c[0], c[1], r=2)
                    if not c or not m.ok(c[0], c[1]) or m.tele[c[1], c[0]]:
                        continue
                    sc = min((max(abs(e[0] - c[0]), abs(e[1] - c[1])) for e in ents), default=99)
                    if sc > bs:
                        best, bs = c, sc
                if bs >= 7:
                    break
            k["pt"] = best
            pt = best
        if not pt:
            self._kite = None
            return False
        self.stats["state"] = "alejándome del grupo"
        self._walk_to(m, (int(pt[0]), int(pt[1])), "alejarme del grupo")
        return True

    def _oly_discover(self):
        try:
            self.log("🔎 No encuentro a los bichos en la memoria: los busco solo (los que se mueven cerca).")
            sig = memory.discover_entities(self.mem.proc, self.pos["x"], self.pos["y"], log=self.log)
            if not sig:
                self.log("🔎 No los encontré esta vez (necesito bichos moviéndose cerca). Pruebo en 15 minutos.")
                return
            path = os.path.join(cfgmod.profile_dir(self.cfg), "memoria.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"entidades": sig, "t": int(time.time())}, f, indent=1)
            self.oly.set_sig(sig)
            self.log(f"🔎 ¡Encontré cómo guarda los bichos este server! ({sig['found']} moviéndose). Firma guardada.")
            try:
                self.learner.note("Encontré la firma de bichos en memoria: " + json.dumps(sig))
            except Exception:
                pass
        except Exception as e:
            self.log(f"🔎 Error buscando bichos en memoria: {e!r}")

    def _oly_step(self):
        """Elijo un bicho de la lista de memoria: si está en pantalla le pego; si no, camino hasta él."""
        if self.pos.get("x") is None or time.time() - self.pos.get("t", 0) > 5:
            return False
        ents = self._oly_ents()
        if not ents:
            return False
        px, py = self.pos["x"], self.pos["y"]
        now = time.time()
        if self._oly_kite(ents):
            return True
        # recién maté uno: levanto lo que haya tirado (Shift + clic en la baldosa) si no tengo a nadie encima
        lt = getattr(self, "_oly_loot", None)
        if lt and now - lt[2] < 10 and not any(max(abs(e[0] - px), abs(e[1] - py)) <= 1 for e in ents):
            self._oly_loot = None
            if max(abs(lt[0] - px), abs(lt[1] - py)) <= 6:
                cx, cy = self._char()
                tw, th = self._tile()
                self._release()
                winapi.key_down("SHIFT")
                time.sleep(0.05)
                winapi.click(*self.abs_pt(int(cx + (lt[0] - px) * tw), int(cy + (lt[1] - py) * th)))
                time.sleep(0.05)
                winapi.key_up("SHIFT")
                time.sleep(1.2)
                return True
        bad = getattr(self, "_oly_bad", {})
        self._oly_bad = bad = {k: v for k, v in bad.items() if v > now}
        hpmax = self._hpmax_est() or 40
        cap = max(3.0, 0.10 * hpmax) if hpmax < 150 else 0.04 * hpmax
        want = getattr(self, "_hunt_mob", None)
        # un bicho "en mi misma baldosa" es un dato viejo de la memoria (dos no pueden ocupar el mismo lugar)
        bz_ = [z for z in getattr(self, "_oly_badzone", []) if z[2] > now]
        ok = [e for e in ents if e[3] not in bad and (e[0], e[1]) != (px, py)
              and not any(max(abs(e[0] - z[0]), abs(e[1] - z[1])) <= 5 and max(abs(e[0] - px), abs(e[1] - py)) > 1 for z in bz_)
              and (e[2].get("dmg_avg", 99) <= cap or e[2]["name"] == want)]
        if not ok:
            return False
        d = lambda e: max(abs(e[0] - px), abs(e[1] - py))
        # nidos: un slime ve a 3 baldosas, así que cuento los vecinos a 3 del que voy a pegar. Cuántos acepto lo
        # voy midiendo: arranco sueltos (0 vecinos) y subo de a uno cuando salgo bien parado varias peleas seguidas.
        crowd = lambda e: sum(1 for o in ents if o is not e and max(abs(o[0] - e[0]), abs(o[1] - e[1])) <= 3)
        self._oly_fight_review()
        allow = self._oly_allow()
        hp_now = self.bars.get("hp") or 100
        # nidos: no los descarto; voy sacando de a uno desde el borde (el de menos vecinos y más cerca).
        # Si el más suelto igual tiene más vecinos de los que aguanto, entro solo con la vida casi llena.
        ok0 = list(ok)
        if ok:
            low = min(crowd(e) for e in ok)
            if low > allow and hp_now < 85:
                ok = [e for e in ok if d(e) <= 1]  # solo me defiendo hasta recuperar vida
            else:
                ok = [e for e in ok if crowd(e) <= max(allow, low)]
        if getattr(self, "_exp_plan", None) and getattr(self, "_exploring", False):
            ok = [e for e in ok if d(e) <= 1]  # relevando: solo me defiendo
        qm = self._quest_mob_names()
        if qm:
            qok = [e for e in ok if e[2]["name"] in qm and d(e) <= 30]
            if qok:
                ok = qok  # misión activa: primero sus bichos
            else:
                # no hay bichos de la misión cerca: no pierdo tiempo con otros (salvo que me peguen encima);
                # camino hacia el bicho de la misión más suelto que vea en la memoria (aunque esté lejos)
                ok = [e for e in ok if d(e) <= 1]
                far = [e for e in ok0 if e[2]["name"] in qm and d(e) > 1]
                if not ok and far and hp_now < 85:
                    self._release()
                    self.stats["state"] = f"recupero vida antes de ir ({hp_now:.0f}%)"
                    return True
                if not ok and far:
                    tgt = min(far, key=lambda e: d(e) + 4 * crowd(e))
                    m = self.world.map_for(self.pos.get("map") or "")
                    if m:
                        # me paro a 4 baldosas del más suelto y de ahí lo saco de a uno
                        dx, dy = tgt[0] - px, tgt[1] - py
                        n_ = max(abs(dx), abs(dy)) or 1
                        st = max(0, n_ - 4) / n_
                        dest = m.nearest_walkable(int(round(px + dx * st)), int(round(py + dy * st)), r=3) or (tgt[0], tgt[1])
                        self.stats["state"] = f"voy hacia los {tgt[2]['name']} ({tgt[0]},{tgt[1]})"
                        self._walk_to(m, (int(dest[0]), int(dest[1])), tgt[2]["name"])
                        return True
        if not ok and ok0 and hp_now < 85 and not any(d(e) <= 3 for e in ents):
            # hay bichos pero todos en grupo y estoy bajo de vida: descanso acá en vez de irme a explorar
            self._release()
            self.stats["state"] = f"recupero vida ({hp_now:.0f}%)"
            return True
        if not ok:
            if now - getattr(self, "_oly_nest_said", 0) > 120:
                self._oly_nest_said = now
                self.log(f"🪺 Solo veo slimes en nido (más de {allow} juntos): todavía no me da, busco sueltos.")
            return False
        e = min(ok, key=lambda e: d(e) + 3 * crowd(e))
        # élite: el slime común muere en 1-3 golpes; si a los 7 s de pegarle sigue vivo, es élite y lo dejo
        fe = getattr(self, "_oly_first", {})
        if e[3] not in fe:
            if d(e) <= 1:  # cuento desde que lo tengo pegado (no mientras camino hasta él)
                fe = {k: v for k, v in fe.items() if now - v < 60}
                fe[e[3]] = now
                self._oly_first = fe
        elif now - fe[e[3]] > 7 and d(e) <= 1:
            bad[e[3]] = now + 600
            self.log(f"👑 Ese {e[2]['name']} no muere: debe ser élite (100 de vida). Lo dejo y me alejo.")
            self._release()
            return False
        self._oly_cur = {"id": e[3], "crowd": crowd(e), "allow": allow, "t": now, "x": e[0], "y": e[1]}
        vx, vy = self.cfg.get("view_tiles", [9, 6])
        if abs(e[0] - px) <= vx and abs(e[1] - py) <= vy:
            # mismo bicho muchas veces sin morir: lo salteo un rato (puede ser un dato viejo de la memoria)
            cnt = getattr(self, "_oly_cnt", {})
            c = cnt.get(e[3], [0, now])
            if now - c[1] > 90:
                c = [0, now]
            c[0] += 1
            cnt[e[3]] = c
            self._oly_cnt = cnt
            if c[0] > 25:
                bad[e[3]] = now + 120
                return False
            cx, cy = self._char()
            tw, th = self._tile()
            pt = (int(cx + (e[0] - px) * tw), int(cy + (e[1] - py) * th - th * 0.3))
            if d(e) <= 1:
                # pegado: el sprite del pj tapa al bicho y el clic izquierdo cae sobre mí. Con click derecho en
                # modo ataque el juego pega en esa dirección sin caminar (wiki: Gameplay).
                self._release()
                winapi.click(*self.abs_pt(pt[0], pt[1] + int(th * 0.3)), button="right")
                self.state = "seek"
                self._last_enemy = now
                self.stats["state"], self.stats["target"] = "atacando", e[2]["name"]
                time.sleep(0.35)
                return True
            self._engage(pt)
            self.stats["target"] = e[2]["name"]
            return True
        if d(e) <= 120:
            m = self.world.map_for(self.pos.get("map") or "")
            # no llego (agua, pared, mapa mal): si en 25 s no me acerqué, lo salteo un rato
            ow = getattr(self, "_oly_walk", None)
            if not ow or max(abs(ow["x"] - e[0]), abs(ow["y"] - e[1])) > 4:
                ow = self._oly_walk = {"x": e[0], "y": e[1], "t": now, "best": d(e)}
            elif d(e) < ow["best"]:
                ow["best"], ow["t"] = d(e), now
            elif now - ow["t"] > 25:
                self._oly_badzone = [z for z in getattr(self, "_oly_badzone", []) if z[2] > now] + [(e[0], e[1], now + 300)]
                self._oly_walk = None
                self.log(f"🚧 No llego al {e[2]['name']} de ({e[0]},{e[1]}): lo salteo 5 min.")
                return False
            if m:
                self.stats["state"] = f"voy a un {e[2]['name']} ({e[0]},{e[1]})"
                self._walk_to(m, (int(e[0]), int(e[1])), e[2]["name"])
                return True
        return False

    def _oly_allow(self):
        mob = getattr(self, "_hunt_mob", None) or "Slime"
        if "oly_nest" not in self.world.learn:
            # primera vez en este server: arranco con lo que aguanté en otros servers con números parecidos
            prior = self.learner.nest_prior(mob)
            self.world.learn["oly_nest"] = {"allow": prior, "ok": 0}
            if prior:
                self.log(f"🧠 Por lo aprendido en otros servers, arranco aceptando {prior} vecino/s por bicho.")
        lr = self.world.learn["oly_nest"]
        allow = int(lr.get("allow", 0))
        lvl = int(self.cfg.get("travel", {}).get("my_level", 1) or 1)
        cap = self.learner.crowd_cap(mob, lvl)
        allow = min(allow, 1 + lvl // 6)  # nivel bajo: pocos vecinos aunque venga bien (los nidos matan de golpe)
        if time.time() < getattr(self, "_careful_until", 0) or self._chill():
            return 0  # vengo muriendo seguido: solo bichos sueltos por un rato
        return allow if cap is None else min(allow, cap)

    def _oly_fight_review(self):
        """Terminó la pelea anterior (elijo otro bicho): si salí con buena vida sumo una buena; si quedé muy
        bajo, bajo el máximo de vecinos aceptado. Con 6 buenas seguidas me animo a un vecino más."""
        cur = getattr(self, "_oly_cur", None)
        if not cur or any(e[3] == cur["id"] for e in (getattr(self, "_oly_last", None) or [])):
            return
        self._oly_cur = None
        if self.cfg.get("loot_olympia", True) and cur.get("x") is not None:
            self._oly_loot = (cur["x"], cur["y"], time.time())
        hp = self.bars.get("hp")
        if hp is None:
            return
        lr = self.world.learn.setdefault("oly_nest", {"allow": 0, "ok": 0})
        if hp < 35:
            if lr["allow"] > 0:
                lr["allow"] -= 1
                self.log(f"🪺 Quedé con {hp:.0f}% de vida: bajo a {lr['allow']} vecinos máximo por bicho.")
            lr["ok"] = 0
        elif hp >= 60 and cur["crowd"] >= lr["allow"]:
            lr["ok"] = lr.get("ok", 0) + 1
            lvl = int(self.cfg.get("travel", {}).get("my_level", 1) or 1)
            if lr["ok"] >= 6 and lr["allow"] < min(8, 1 + lvl // 6):
                lr["allow"] += 1
                lr["ok"] = 0
                self.log(f"🪺 Vengo aguantando bien: me animo a bichos con hasta {lr['allow']} vecinos (nidos más cargados).")
                try:
                    self.learner.note_nest(lr["allow"], getattr(self, "_hunt_mob", None) or "Slime")
                except Exception:
                    pass
        self.world.save()

    def _oly_died(self):
        lr = self.world.learn.setdefault("oly_nest", {"allow": 0, "ok": 0})
        if lr.get("allow", 0) > 0:
            lr["allow"] -= 1
        lr["ok"] = 0
        self._oly_cur = None

    def _goto_step(self):
        """Clic en el mapa del bot: el pj va solo a ese mapa y punto. Si le pegan, pelea primero."""
        g = getattr(self, "goto", None)
        if not g or not self.pos.get("map") or time.time() - self.pos.get("t", 0) > 5:
            return False
        if time.time() - g["t"] > 1800:
            self.log("🗺 Cancelé el viaje del mapa: tardaba más de 30 min.")
            self.goto = None
            return False
        if self._attacked_recently():
            return False
        cur = self.pos["map"]
        label = f"{g['map']} ({g['x']},{g['y']})"
        if not self._same(cur, g["map"]):
            self.stats["state"] = f"yendo a {label}"
            if self._travel(force_dest=g["map"]):
                return True
            if not self._same(self.pos.get("map") or "", g["map"]):
                self.log(f"🗺 No encuentro camino a {g['map']}. Cancelo.")
                self.goto = None
                return False
        m = self.world.map_for(cur)
        if not m:
            return False
        px, py = self.pos["x"], self.pos["y"]
        if max(abs(g["x"] - px), abs(g["y"] - py)) <= 1:
            self._release()
            self.nav["walking"] = False
            self.log(f"🗺 Llegué a {label}.")
            self.goto = None
            return False
        self.stats["state"] = f"yendo a {label}"
        self.nav["status"] = f"voy al punto del mapa {label}"
        self._walk_to(m, (int(g["x"]), int(g["y"])), label)
        return True

    def _join_active(self):
        """¿Me pidieron hacerme ciudadano y ya tengo el nivel? (decisión del dueño, guardada en la config)."""
        t = self.cfg.get("travel", {}) or {}
        jp = t.get("join_portal") or {}
        return bool(t.get("join_city") and jp.get("map") and int(t.get("my_level", 1) or 1) >= int(t.get("join_level", 10))
                    and self._same(self.pos.get("map") or "", jp["map"]))

    def _join_step(self):
        """Camino al portal de la ciudad elegida; el cartel '¿unirte?' se responde en _close_junk_windows."""
        if not self._join_active() or getattr(self, "goto", None):
            return
        now = time.time()
        if now - getattr(self, "_join_t", 0) < 15:
            return
        self._join_t = now
        m = self.world.map_for(self.pos.get("map") or "")
        if m is not None and getattr(m, "_no_pisar", None):
            for (x, y), v in (getattr(m, "_np_orig", None) or {}).items():
                m.walk[y, x] = v
            m._np_orig = {}
            m._no_pisar = "off"
        jp = self.cfg["travel"]["join_portal"]
        if not getattr(self, "_join_said", False):
            self._join_said = True
            self.log(f"🏛 Ya soy nivel {self.cfg['travel'].get('my_level')}: voy al portal para unirme a {self.cfg['travel']['join_city'].title()}.")
        self.goto = {"map": jp["map"], "x": int(jp["x"]), "y": int(jp["y"]), "t": now}

    def _apply_no_pisar(self, m=None):
        """Baldosas que no hay que pisar (portales que preguntan si me uno a una ciudad): las bloqueo con margen."""
        mk = (self.pos.get("map") or "").strip().lower()
        m = m or self.world.map_for(self.pos.get("map") or "")
        if not m:
            return
        if self._join_active():
            m._no_pisar = "off"
            return
        orig = m.__dict__.setdefault("_np_orig", {})
        for x, y in self.world.learn.get("no_pisar", {}).get(mk, []):
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if 0 <= x + dx < m.w and 0 <= y + dy < m.h:
                        orig.setdefault((x + dx, y + dy), bool(m.walk[y + dy, x + dx]))
                        m.walk[y + dy, x + dx] = False
        m._no_pisar = mk

    def _mob_costs(self, m, goal):
        """Costo extra de las baldosas cerca de bichos (lista en vivo de la memoria), para que el camino rodee los
        nidos en vez de cruzarlos. Cerca del destino y de donde estoy parado no cobro (si voy a pegarle a uno, llego)."""
        now = time.time()
        c = getattr(self, "_mc", None)
        if c and c[0] == id(m) and c[1] == tuple(goal) and now - c[2] < 2.0:
            return c[3]
        ex = {}
        try:
            px, py = self.pos["x"], self.pos["y"]
            for e in (self._oly_ents() or []):
                x, y = e[0], e[1]
                if max(abs(x - goal[0]), abs(y - goal[1])) <= 3 or max(abs(x - px), abs(y - py)) <= 1:
                    continue
                for dy in range(-3, 4):
                    for dx in range(-3, 4):
                        d_ = max(abs(dx), abs(dy))
                        k = (x + dx, y + dy)
                        ex[k] = ex.get(k, 0.0) + (5.0, 4.0, 2.5, 1.2)[d_]
        except Exception:
            ex = {}
        self._mc = (id(m), tuple(goal), now, ex)
        return ex

    def _walk_to(self, m, goal, label):
        if getattr(m, "_no_pisar", None) is None and cfgmod.server_slug(self.cfg) != "abaddon":
            self._apply_no_pisar(m)
        if cfgmod.server_slug(self.cfg) != "abaddon":
            m.extra = self._mob_costs(m, goal)
        now = time.time()
        px, py = self.pos["x"], self.pos["y"]
        if (px, py) != self.nav.get("last_pos"):
            self.nav["last_pos"], self.nav["still_since"] = (px, py), now
            self.nav["stuck_n"] = 0
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
            # el juego camina derecho hacia el mouse: apunto solo hasta donde hay línea libre (pozos, paredes, pasillos)
            if not m.los((px, py), p):
                break
            ex_ = getattr(m, "extra", None)
            if ex_ and wp != path[min(1, len(path) - 1)] or ex_ and p != path[1]:
                # el camino rodea un nido: no lo corto en línea recta por el medio
                n_s = max(abs(p[0] - px), abs(p[1] - py))
                if any(ex_.get((int(round(px + (p[0] - px) * t / n_s)), int(round(py + (p[1] - py) * t / n_s))), 0.0) >= 4.0
                       for t in range(1, n_s + 1)):
                    break
            wp = p
        stuck = now - self.nav["still_since"] > 4
        if stuck:
            n_st = self.nav.get("stuck_n", 0) + 1
            self.nav["stuck_n"] = n_st
            self.nav["still_since"] = now
            # suelto el click: si quedó apretado sobre una ventana (bolso, personaje) el juego no camina
            self._release()
            if n_st == 2:
                self._drop_cursor_item()
            if n_st % 3:
                # trabado: doy el paso siguiente del camino, de a una baldosa, en vez de apuntar lejos
                wp = path[min(1, len(path) - 1)]
            else:
                for _ in range(10):
                    wp = (px + random.randint(-3, 3), py + random.randint(-3, 3))
                    if m.ok(*wp) and m.los((px, py), wp) and not m.near_tele(wp[0], wp[1], 1):
                        break
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
        elif (self.state in ("attack", "loot", "lootq") or time.time() - getattr(self, "_last_enemy", 0) < 6) and not self._is_travelling():
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
            # tengo la tabla de zonas de bichos de este mapa: voy a la zona que corresponde y busco ahí
            if not (self.ents.center and self._mem_struct()) and self.pos.get("x") is not None:
                mk_ = cur.strip().lower()
                z_ = self.world.learn.setdefault("xpz", {}).setdefault(mk_, {})
                r_ = self._spawn_step(m, mk_, z_, self.pos["x"], self.pos["y"])
                if r_ is not None:
                    return r_
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
        route = self.world.route(cur, dest_key, forbidden=avoid, cost=self._map_cost)
        home = t.get("home", "aresden")
        if not route and not self._same(cur, home):
            # no sé llegar al destino: primero voy a mi ciudad y exploro desde ahí
            route = self.world.route(cur, home, forbidden=avoid, cost=self._map_cost)
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
        rings = self.cfg.get("scan_rings", [1.0, 1.6, 2.3, 3.0])
        if time.time() - getattr(self, "_zone_mob_t", 0) < 5:
            # estoy en la zona de un bicho conocido: miro más lejos (los slimes casi no se mueven)
            rings = self.cfg.get("scan_rings_zone", [1.0, 1.6, 2.3, 3.0, 3.8, 4.6, 5.4, 6.2])
        for r in rings:
            n = max(8, int(r * 8))
            for i in range(n):
                a = 2 * math.pi * i / n + (r * 0.37)
                pts.append((int(cx + math.cos(a) * r * tw), int(cy - th * 0.3 + math.sin(a) * r * th)))
        roi = self._game_roi()
        # muchos puntos (zona amplia): reviso de a tandas de 40 para no frenar el resto del bot
        if len(pts) > 48:
            off = getattr(self, "_ring_off", 0) % len(pts)
            near = pts[:24]  # lo pegado al pj siempre
            pts = near + (pts[24:] * 2)[off:off + 40]
            self._ring_off = off + 40
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

    # ---------- zonas por experiencia: cuánto rinde cada parte del mapa (los bichos fuertes dan más) ----------
    ZC = 14  # tamaño de una zona, en baldosas

    def _exp_mem(self):
        try:
            if self._mem_struct():
                d = self.mem.proc.read(self.mem.addr[1] + 76, 4)
                if d:
                    return int.from_bytes(d, "little", signed=True)
                return None
        except Exception:
            pass
        sh = self._sheet(3600)
        return sh.get("exp_total") if sh else None

    def _zone_kill(self):
        """Al matar anoto dónde fue; la experiencia llega un instante después, así que la cuento en _zone_flush."""
        x, y = self.pos.get("x"), self.pos.get("y")
        if x is not None and self.pos.get("map"):
            self.__dict__.setdefault("_zk", []).append((time.time(), x, y, self.pos["map"].strip().lower()))

    def _zone_flush(self):
        """Reparte la experiencia ganada entre las muertes recientes y la anota por zona: así sé qué partes del
        mapa tienen bichos que rinden más que los slimes."""
        try:
            now = time.time()
            if now - getattr(self, "_zk_t", 0) < 0.5:
                return
            self._zk_t = now
            zk = self.__dict__.setdefault("_zk", [])
            e1 = self._exp_mem()
            if e1 is None:
                return
            e0 = getattr(self, "_zk_exp", None)
            if e0 is None or not zk:
                if e0 is None or e1 < e0:
                    self._zk_exp = e1
                return
            if now - zk[-1][0] < 1.2:
                return
            ready, self._zk = list(zk), []
            self._zk_exp = e1
            d = (e1 - e0) / float(len(ready))
            if d <= 0 or d > 500000:
                return
            self._yield_add(exp=e1 - e0)
            for _, x, y, mk in ready:
                z = self.world.learn.setdefault("xpz", {}).setdefault(mk, {})
                e = z.setdefault(f"{x // self.ZC},{y // self.ZC}", {"n": 0, "xp": 0.0, "x": x, "y": y})
                e["n"] = min(e["n"] + 1, 40)
                a = 1.0 / e["n"] if e["n"] < 8 else 0.15
                e["xp"] += (d - e["xp"]) * a
                e["tot"] = e.get("tot", 0) + d
                e["x"] += (x - e["x"]) * 0.3
                e["y"] += (y - e["y"]) * 0.3
                e["t"] = int(now)
        except Exception:
            pass

    def _zone_step(self):
        """Elige dónde cazar dentro del mapa por experiencia por bicho: deja las zonas pobres (slimes) por las que
        rinden más, y cada tanto explora partes nuevas del mapa para encontrarlas. True = estoy caminando por esto."""
        if not self.cfg.get("zone_hunt", True) or self.state != "seek":
            return False
        if getattr(self, "mission", None) or self._fleeing() or self._attacked_recently():
            return False
        cur, x, y = self.pos.get("map") or "", self.pos.get("x"), self.pos.get("y")
        if not cur or x is None or time.time() - self.pos.get("t", 0) > 5:
            return False
        dest = self.travel_dest()
        if not dest or not self._same(cur, dest):
            return False
        mp = self.world.map_for(cur)
        if not mp:
            return False
        now, mk, ZC = time.time(), cur.strip().lower(), self.ZC
        z = self.world.learn.get("xpz", {}).get(mk, {})
        seen = self.world.learn.setdefault("xpz_seen", {}).setdefault(mk, [])
        ck = f"{x // ZC},{y // ZC}"
        if ck not in seen:
            seen.append(ck)
        # lo que alcanzo a ver desde acá también cuenta: zonas vacías = vistas; zonas con bichos = para ir a probar
        if now - getattr(self, "_zone_look", 0) > 1.0:
            self._zone_look = now
            try:
                mobs = self.__dict__.setdefault("_zone_mobs", {}).setdefault(mk, set())
                ents = self.ents.read(self.mem.proc) or []
                with_mob = {f"{(x + e[0]) // ZC},{(y + e[1]) // ZC}" for e in ents
                            if e[2].startswith("_") and e[3] == 0 and abs(e[0]) <= 10 and abs(e[1]) <= 7}
                for cx in range((x - 10) // ZC, (x + 10) // ZC + 1):
                    for cy in range((y - 7) // ZC, (y + 7) // ZC + 1):
                        k = f"{cx},{cy}"
                        ctr = (cx * ZC + ZC // 2, cy * ZC + ZC // 2)
                        if k in with_mob:
                            if z.get(k, {}).get("n", 0) < 3:
                                mobs.add(k)
                        elif abs(ctr[0] - x) <= 5 and abs(ctr[1] - y) <= 3 and k not in seen:
                            seen.append(k)
                mobs.discard(ck)
            except Exception:
                pass
        # si conozco dónde sale cada bicho en este mapa (tabla de zonas), voy a la zona del mejor bicho para mi nivel
        r_ = self._spawn_step(mp, mk, z, x, y)
        if r_ is not None:
            return r_
        known = {k: v for k, v in z.items() if v.get("n", 0) >= 3 and not self._is_danger(int(v["x"]), int(v["y"]))}
        if not known:
            return False
        xs_ = sorted(v["xp"] for v in known.values())
        base = xs_[len(xs_) // 2]  # lo "normal" del mapa (la mediana: un par de muestras flojas no la mueven)
        empty = getattr(self, "_zone_empty", None)
        if empty is None:
            empty = self._zone_empty = {}
        good = [(k, v) for k, v in known.items() if empty.get(mk + k, 0) < now]
        best_k, best = max(good, key=lambda kv: kv[1]["xp"]) if good else (None, None)
        curz = known.get(ck)
        scouting = getattr(self, "_scout_until", 0) > now
        # ¿toca explorar? Solo mientras no conozca ninguna zona claramente mejor que la más floja
        top = max(v["xp"] for v in known.values())
        if (not scouting and top < 1.8 * base and self._town_budget_left() > 0
                and now - getattr(self, "_scout_last", 0) > self.cfg.get("scout_every_min", 20) * 60):
            self._scout_last = now
            self._scout_until = now + self.cfg.get("scout_minutes", 4) * 60
            self._scout_goal = None
            scouting = True
            self.log(f"🔭 Acá los bichos dan poca experiencia (~{int(base)} por bicho): exploro {self.cfg.get('scout_minutes', 4)} minutos el mapa buscando algo que rinda más.")
        if curz is None:
            # zona sin medir: si hay bichos los mato para saber cuánto dan; si no, sigo explorando
            if scouting and not self._mem_near():
                return self._scout_walk(mp, seen, x, y)
            return False
        if scouting and curz["xp"] >= 1.5 * base:
            self._scout_until = 0
            scouting = False
            self.log(f"🔭 Encontré una zona mejor: ~{int(curz['xp'])} de experiencia por bicho (los de antes daban ~{int(base)}). Me quedo cazando acá.")
        if scouting:
            return self._scout_walk(mp, seen, x, y)
        if best is None or curz["xp"] >= 0.7 * best["xp"]:
            # estoy en una zona que rinde: si se quedó sin bichos, la dejo descansar un rato
            if self._mem_near():
                self._zone_idle = now
            elif now - getattr(self, "_zone_idle", now) > 45:
                empty[mk + ck] = now + 180
                self._zone_idle = now
            return False
        if best["xp"] >= 1.4 * curz["xp"]:
            g = (int(best["x"]), int(best["y"]))
            if max(abs(g[0] - x), abs(g[1] - y)) <= 4:
                empty[mk + best_k] = now + 180  # llegué y no hay nada
                return False
            if now - getattr(self, "_zone_said", 0) > 180:
                self._zone_said = now
                self.log(f"🎯 Voy a la zona que más rinde: ~{int(best['xp'])} de exp por bicho (acá ~{int(curz['xp'])}).")
            self._walk_to(mp, g, f"zona que rinde más (~{int(best['xp'])} exp por bicho)")
            self.stats["state"] = "yendo a mejor zona"
            return True
        return False

    def _hpmax_est(self):
        """Vida máxima: la mayor vista o la fórmula (VIT x3 + nivel x2 + STR/2), lo que sea más alto."""
        lvl = self._level_mem() or int(self.cfg.get("travel", {}).get("my_level", 1))
        hpmax = max(getattr(self, "_hp_max", 0) or 0, self._hp_mem() or 0)
        st_ = self._stats_mem() or {}
        if st_.get("VIT"):
            hpmax = max(hpmax, st_["VIT"] * 3 + lvl * 2 + st_.get("STR", 0) // 2)
        if not hpmax:
            hpmax = 12 * 3 + lvl * 2 + 7  # sin memoria del pj: estimo con los stats iniciales
        return hpmax

    def _creatures_db(self):
        crs = getattr(self, "_creatures", None)
        if crs is None:
            crs = self._creatures = {m["type"]: m for m in self._load_json("criaturas.json").get("creatures", [])}
        return crs

    def _mob_est(self, cr):
        """Cuánto me pega este bicho por golpe: lo medido en pelea si ya lo enfrenté bastante; si no, el dato de la
        base corregido por lo que vengo midiendo con otros bichos (mi armadura y el server cambian los números)."""
        fight = self.world.learn.get("fight", {})
        f = fight.get(cr["name"])
        if f and f.get("n", 0) >= 15:
            return f["avg"], True
        ratios = sorted(fight[m["name"]]["avg"] / m["dmg_avg"] for m in self._creatures_db().values()
                        if fight.get(m["name"], {}).get("n", 0) >= 15 and m["dmg_avg"] > 0)
        factor = ratios[len(ratios) // 2] if ratios else 1.0
        return cr["dmg_avg"] * max(0.5, factor), False

    def _fight_sample(self):
        """Mide en pelea cuánto me sacan por golpe (bajadas de vida) contra el bicho de la zona donde estoy."""
        hm, now = self._hp_mem(), time.time()
        last, self._fs_hp = getattr(self, "_fs_hp", None), hm
        mob = getattr(self, "_zone_mob", None)
        if hm is None or last is None or not mob or now - getattr(self, "_zone_mob_t", 0) > 5:
            return
        d = last - hm
        if 1 <= d <= 0.6 * max(last, 1) and (self.state == "attack" or now - getattr(self, "_last_enemy", 0) < 3):
            # solo cuenta si hay UN bicho pegado a mí (si hay varios, o uno grande al lado, el golpe puede ser de otro)
            try:
                ents = self.ents.read(self.mem.proc) or []
                near = [e for e in ents if e[2].startswith("_") and e[3] == 0 and max(abs(e[0]), abs(e[1])) <= 2]
            except Exception:
                near = []
            if len(near) != 1:
                return
            f = self.world.learn.setdefault("fight", {}).setdefault(mob, {"n": 0, "avg": 0.0, "max": 0, "kn": 0, "ttk": 0.0})
            f["n"] = min(f["n"] + 1, 400)
            f["avg"] += (d - f["avg"]) * (1.0 / f["n"] if f["n"] < 30 else 0.05)
            f["max"] = max(f["max"], d)

    def _fight_kill(self, secs):
        mob = getattr(self, "_zone_mob", None)
        if mob and time.time() - getattr(self, "_zone_mob_t", 0) < 5 and 0.5 < secs < 90:
            f = self.world.learn.setdefault("fight", {}).setdefault(mob, {"n": 0, "avg": 0.0, "max": 0, "kn": 0, "ttk": 0.0})
            f["kn"] = min(f.get("kn", 0) + 1, 400)
            f["ttk"] = f.get("ttk", 0.0) + (secs - f.get("ttk", 0.0)) * (1.0 / f["kn"] if f["kn"] < 20 else 0.08)

    def _fight_report(self):
        """Cada 10 minutos: cuánto me pegan, cuánto tardo en matar, mi equipo, y qué me falta para el bicho que sigue."""
        now = time.time()
        if now - getattr(self, "_fr_t", 0) < 600:
            return
        mob = getattr(self, "_zone_mob", None)
        crs = self._creatures_db()
        cur = next((m for m in crs.values() if m["name"] == mob), None)
        f = self.world.learn.get("fight", {}).get(mob or "", {})
        if not cur or f.get("n", 0) < 15:
            return
        self._fr_t = now
        lvl = self._level_mem() or 1
        hpmax, share = self._hpmax_est(), float(self.cfg.get("mob_dmg_share", 0.04))
        idb = self._items_db()
        eq = [idb[i] for i in (self._equipped_ids() or []) if i in idb]
        dfn = sum(int(i.get("def") or 0) for i in eq)
        wp = next((i for i in eq if i.get("avg")), None)
        line = (f"📊 {mob}: me saca ~{f['avg']:.0f} por golpe (máximo {f['max']}; la base decía ~{cur['dmg_avg']:.0f})"
                + (f", lo mato en ~{f['ttk']:.0f} s" if f.get("kn", 0) >= 5 else "")
                + f". Tengo vida {hpmax}, defensa de equipo {dfn}" + (f", arma {wp['name']} (~{wp['avg']:.0f} por golpe sin contar fuerza)" if wp else "") + ".")
        nxt = None
        # solo bichos de mapas de la escalera (o del pueblo): nada de mapas PvP ni de la otra ciudad
        okmaps = {"aresden farm", "aresden city"}
        for ph in self._load_json("training_path.json").get("phases", []):
            okmaps.update(x.split(" (")[0].lower() for x in ph.get("maps", []))
        for m0 in crs.values():
            zs = [z for z in m0["zones"] if z["map"].lower() in okmaps]
            if not zs or m0["exp_dice"] <= cur["exp_dice"]:
                continue
            m = dict(m0, zones=zs)
            est, _ = self._mob_est(m)
            need_hp, need_lv = int(est / share) + 1, int(max(1, (m["defense"] - 30) / 1.5)) + 1
            if need_hp <= hpmax and need_lv <= lvl:
                continue
            cost = max(0, need_hp - hpmax) / 11.0 + max(0, need_lv - lvl)
            if nxt is None or cost < nxt[0]:
                nxt = (cost, m, est, need_hp, need_lv)
        if nxt:
            _, m, est, need_hp, need_lv = nxt
            parts = []
            if need_hp > hpmax:
                parts.append(f"{need_hp - hpmax} de vida (unos {int((need_hp - hpmax) / 11.0) + 1} niveles poniendo todo en VIT)")
            if need_lv > lvl:
                parts.append(f"{need_lv - lvl} niveles para acertarle (defensa {m['defense']})")
            line += f" Lo que sigue: {m['name']} (vida ~{m['hp']}, pegaría ~{est:.0f}) en {m['zones'][0]['map']} {tuple(m['zones'][0]['center'])}: me faltan " + " y ".join(parts) + "."
        self.stats["fight_line"] = line
        self.log(line)

    def _spawn_step(self, mp, mk, z, x, y):
        """Con la tabla de zonas de bichos del mapa: elijo la zona que más rinde entre las de bichos que ya puedo
        enfrentar (las que todavía no medí valen como "prometedoras": voy y las pruebo). None = no hay tabla."""
        sp = getattr(self, "_spawns", None)
        if sp is None:
            sp = self._spawns = self._spawns_all()
        ent = next((v for k, v in sp.items() if self._same(k, self.pos.get("map") or "")), None)
        if not ent:
            return None
        now = time.time()
        lvl = self._level_mem() or int(self.cfg.get("travel", {}).get("my_level", 1))
        empty = self.__dict__.setdefault("_zone_empty", {})
        crs = self._creatures_db()
        hpmax = self._hpmax_est()
        cand, meas = [], []
        # tope de daño por golpe: 4% de la vida; con poca vida (pj recién hecho) hasta 10%, si no no podría cazar nada
        dmg_cap = float(self.cfg.get("mob_dmg_share", 0.04)) * max(hpmax, 1)
        if hpmax < 150:
            dmg_cap = max(dmg_cap, 0.10 * max(hpmax, 1), 3.0)
        # zonas de bichos que todavía no aguanto: no cazo en zonas pegadas a ellas (se mezclan: los slimes de
        # la Farm comparten lugar con los Stone Golem) y si estoy adentro me voy
        bad = []
        for zn in ent["zones"]:
            cr = crs.get(zn.get("type"))
            if cr and (self._mob_est(cr)[0] > dmg_cap or cr["defense"] > 30 + 1.5 * lvl):
                bad.append(zn["rect"])
        near_bad = lambda px, py, m=4: any(r[0] - m <= px <= r[2] + m and r[1] - m <= py <= r[3] + m for r in bad)
        for zn in ent["zones"]:
            x1, y1, x2, y2 = zn["rect"]
            cells = [v for v in z.values() if x1 - 3 <= v["x"] <= x2 + 3 and y1 - 3 <= v["y"] <= y2 + 3]
            # si ya cacé ahí un buen rato sin problemas, alcanza con que no se pise de verdad con la peligrosa
            mg = 1 if sum(v.get("sec", 0) for v in cells) >= 240 else 7
            if any(not (x2 < r[0] - mg or x1 > r[2] + mg or y2 < r[1] - mg or y1 > r[3] + mg) for r in bad):
                if zn["rect"] not in bad:
                    continue  # se superpone con una zona peligrosa
            cr = crs.get(zn.get("type"))
            if cr:
                # ¿lo puedo enfrentar? que no me saque más del 4% de la vida por golpe y que le pueda acertar
                est_, _m = self._mob_est(cr)
                if est_ > dmg_cap or cr["defense"] > 30 + 1.5 * lvl:
                    continue
                zn = dict(zn, min_level=cr["exp_dice"], hp=cr["hp"], dmg=round(est_, 1))
            elif zn.get("min_level", 80) > lvl - int(self.cfg.get("mob_level_margin", 4)):
                continue
            c = mp.nearest_walkable((x1 + x2) // 2, (y1 + y2) // 2, r=6)
            if not c or not mp.ok(c[0], c[1]) or self._is_danger(c[0], c[1]):
                continue
            n = sum(v["n"] for v in cells)
            sec = sum(v.get("sec", 0) for v in cells)
            tot = sum(v.get("tot", 0) for v in cells)
            # lo que manda es la experiencia por HORA estando ahí (bichos que dan mucho pero salen pocos no sirven);
            # con poco tiempo medido estimo por la experiencia por bicho (unos 2 bichos por minuto)
            if sec >= 300:
                xp = tot / sec * 3600
            elif n >= 5:
                xp = sum(v["xp"] * v["n"] for v in cells) / n * 120
            else:
                xp = None
            if xp is not None:
                meas.append(xp)
            cand.append([xp, zn, c, f"{mk}|{x1},{y1}", xp is not None])
        if not cand:
            return None
        if self._chill():
            ds = [c_[1]["dmg"] for c_ in cand if c_[1].get("dmg") is not None]
            if ds:
                easy = [c_ for c_ in cand if c_[1].get("dmg") is not None and c_[1]["dmg"] <= min(ds) * 1.5]
                cand, meas = easy, [c_[0] for c_ in easy if c_[0] is not None]
        qm = self._quest_mob_names()
        if qm:
            qc = [c_ for c_ in cand if c_[1].get("mob") in qm]
            if qc:
                cand = qc  # misión activa: solo zonas de su bicho
                meas = [c_[0] for c_ in qc if c_[0] is not None] or meas
        top = max(meas) if meas else 1000.0
        for c_ in cand:
            if c_[0] is None:
                c_[0] = 2.0 * top + c_[1].get("min_level", 0)  # sin medir: prometedora, más cuanto más fuerte el bicho
        live = [c_ for c_ in cand if empty.get(c_[3], 0) < now] or cand
        best_c = max(live, key=lambda c_: c_[0])
        # no salto de zona por diferencias chicas: me quedo en la actual si rinde al menos 80% de la mejor
        cur_c = next((c_ for c_ in live if c_[3] == getattr(self, "_zone_goal", None)), None)
        if cur_c and cur_c is not best_c and cur_c[0] >= 0.8 * best_c[0] and now - getattr(self, "_zone_since", 0) < 1200:
            best_c = cur_c
        if best_c[3] != getattr(self, "_zone_goal", None):
            self._zone_since = now
        score, zn, ctr, key, measured = best_c
        x1, y1, x2, y2 = zn["rect"]
        if near_bad(x, y, 3) and not (x1 - 4 <= x <= x2 + 4 and y1 - 4 <= y <= y2 + 4):
            if now - getattr(self, "_bad_said", 0) > 120:
                self._bad_said = now
                self.log(f"⚠ Estoy en zona de bichos que todavía no aguanto: me voy a la de {zn['mob']} ({ctr[0]},{ctr[1]}).")
            self._release()
            self.state, self.target = "seek", None
            self._walk_to(mp, ctr, f"salgo de zona peligrosa hacia {zn['mob']}")
            self.stats["state"] = "saliendo de zona peligrosa"
            return True
        self.stats["hunt_zone"] = f"{zn['mob']} ({ctr[0]},{ctr[1]})"
        self._hunt_mob = zn["mob"]
        if x1 - 4 <= x <= x2 + 4 and y1 - 4 <= y <= y2 + 4:
            self._zone_mob, self._zone_mob_t = zn["mob"], now
            vis = not (self.ents.center and self._mem_struct())  # sin lista de bichos en memoria: busco con el mouse
            if self._mem_near() or (vis and now - getattr(self, "_last_enemy", 0) < 40):
                self._zone_idle = now
            elif now - getattr(self, "_zone_idle", now) > (120 if vis else 40):
                empty[key] = now + 120  # no queda nada: mientras reaparecen voy a la siguiente
                self._zone_idle = now
                if vis:
                    self.log(f"Revisé la zona de {zn['mob']} y no encontré nada: pruebo otra mientras reaparecen.")
            elif vis:
                # adentro de la zona: me muevo por puntos de la zona y dejo que el buscador revise alrededor
                g = getattr(self, "_zone_pt", None)
                if not g or g[2] != key or max(abs(g[0] - x), abs(g[1] - y)) <= 1 or now - g[3] > 25:
                    for _ in range(30):
                        gx, gy = random.randint(x1, x2), random.randint(y1, y2)
                        if mp.ok(gx, gy) and not self._is_danger(gx, gy):
                            g = self._zone_pt = (gx, gy, key, now)
                            break
                if g and now - getattr(self, "_last_enemy", 0) > 6 and self.state == "seek":
                    self._walk_to(mp, (g[0], g[1]), f"buscando {zn['mob']} en su zona")
                    self.stats["state"] = f"buscando {zn['mob']} en su zona"
                return False
            elif max(abs(ctr[0] - x), abs(ctr[1] - y)) > 3:
                self._walk_to(mp, ctr, f"centro de la zona de {zn['mob']}")
                self.stats["state"] = "buscando en la zona"
                return True
            else:
                self._release()
                self.stats["state"] = "esperando que salgan bichos"
                return True
            return False
        self._zone_idle = now
        if getattr(self, "_zone_goal", None) != key:
            self._zone_goal = key
            how = f"~{int(score)} de exp por hora" if measured else "todavía no la probé"
            extra = f", vida ~{zn['hp']}, pega ~{zn['dmg']} por golpe" if "hp" in zn else ""
            self.log(f"🎯 Nivel {lvl} → cazo {zn['mob']}{extra} → zona ({ctr[0]},{ctr[1]}) [{how}].")
        self._walk_to(mp, ctr, f"zona de {zn['mob']}")
        self.stats["state"] = "yendo a zona de caza"
        return True

    def _scout_walk(self, mp, seen, x, y):
        """Camina hacia la zona sin visitar más cercana. False si no queda nada por explorar."""
        now, ZC = time.time(), self.ZC
        g = getattr(self, "_scout_goal", None)
        mk = (self.pos.get("map") or "").strip().lower()
        mobs = self.__dict__.setdefault("_zone_mobs", {}).setdefault(mk, set())
        here = f"{x // ZC},{y // ZC}"
        if g and (g[0] == here or (g[0] in seen and g[0] not in mobs) or now - g[2] > 50):
            if g[0] not in seen:
                seen.append(g[0])  # no pude llegar: no insisto
            mobs.discard(g[0])
            g = None
        if not g:
            best = None
            for cx in range(0, mp.w // ZC + 1):
                for cy in range(0, mp.h // ZC + 1):
                    k = f"{cx},{cy}"
                    if k in seen and k not in mobs:
                        continue
                    p = mp.nearest_walkable(cx * ZC + ZC // 2, cy * ZC + ZC // 2, r=5)
                    if not p or not mp.ok(p[0], p[1]) or self._is_danger(p[0], p[1]) or mp.near_tele(p[0], p[1], 3):
                        continue
                    d = max(abs(p[0] - x), abs(p[1] - y)) - (60 if k in mobs else 0)  # primero donde vi bichos sin probar
                    if best is None or d < best[0]:
                        best = (d, k, p)
            if not best:
                self._scout_until = 0
                return False
            g = self._scout_goal = (best[1], best[2], now)
        self._walk_to(mp, g[1], "explorando zonas nuevas")
        self.stats["state"] = "explorando"
        return True

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

    def _read_small_num(self, img, x1, y1, x2, y2):
        """Lee un número chico (1-3 cifras) de un recuadro: aíslo el texto claro, lo recorto justo y lo pongo dos
        veces seguido ('4' -> '44'), porque el OCR de Windows no lee una cifra sola."""
        import cv2 as _cv
        roi = img[max(0, y1):y2, max(0, x1):x2]
        if roi.size == 0:
            return None
        m = self._text_mask(roi)
        ys, xs = np.nonzero(m)
        if len(xs) < 4:
            return None
        a, b, c, d = max(0, ys.min() - 3), ys.max() + 4, max(0, xs.min() - 3), xs.max() + 4
        gr = self._glyph_reader()
        t = np.where(m[a:b, c:d], 255, 0).astype(np.uint8)
        t = _cv.cvtColor(t, _cv.COLOR_GRAY2BGR)
        pad = np.zeros((t.shape[0], 24, 3), np.uint8)
        gap = np.zeros((t.shape[0], 3, 3), np.uint8)
        for k, sc in ((2, 3.0), (2, 2.0), (3, 3.0), (2, 4.0)):
            row = np.hstack([pad] + sum(([t, gap] for _ in range(k)), [])[:-1] + [pad])
            row = _cv.copyMakeBorder(row, 10, 10, 0, 0, _cv.BORDER_CONSTANT, value=0)
            try:
                ws = ocr.read_words(row, {"x1": 0, "y1": 0, "x2": row.shape[1], "y2": row.shape[0]}, scale=sc)
            except Exception:
                ws = []
            txt = "".join(w[0] for w in ws)
            txt = "".join(ch for ch in txt if ch.isdigit())
            if txt and len(txt) % k == 0 and txt == txt[:len(txt) // k] * k:
                val = int(txt[:len(txt) // k])
                if gr:
                    gr.learn(m[a:b, c:d], str(val))
                return val
        # el OCR no pudo: pruebo con las cifras que fui aprendiendo en este server
        return gr.read(m[a:b, c:d]) if gr else None

    @staticmethod
    def _text_mask(roi):
        """Píxeles de texto del juego: verde, blanco o amarillo sobre fondo oscuro."""
        r_, g_, b_ = roi[:, :, 2].astype(int), roi[:, :, 1].astype(int), roi[:, :, 0].astype(int)
        mx = np.maximum(np.maximum(r_, g_), b_)
        mn = np.minimum(np.minimum(r_, g_), b_)
        return (((g_ > 100) & (g_ - r_ > 30) & (g_ - b_ > 30)) | ((mn > 120) & (mx - mn < 50))
                | ((r_ > 150) & (g_ > 120) & (b_ < 110) & (r_ - b_ > 70)))

    def _glyph_reader(self):
        """Plantillas de cifras aprendidas en este server (glifos.json)."""
        try:
            from . import glyphs as glmod
            path = os.path.join(cfgmod.profile_dir(self.cfg), "glifos.json")
            g = getattr(self, "_glyphs", None)
            if g is None or g.path != path:
                g = self._glyphs = glmod.GlyphReader(path)
            return g
        except Exception:
            return None

    def _glyph_learn(self, img, box, text):
        """El OCR leyó bien un número de 2+ cifras en 'box': guardo sus cifras como ejemplos."""
        t = "".join(ch for ch in str(text) if ch.isdigit())
        if len(t) < 2:
            return
        x1, y1, x2, y2 = [int(v) for v in box]
        roi = img[max(0, y1):y2, max(0, x1):x2]
        if roi.size == 0:
            return
        m = self._text_mask(roi)
        ys, xs = np.nonzero(m)
        if len(xs) < 4:
            return
        gr = self._glyph_reader()
        if gr:
            gr.learn(m[ys.min():ys.max() + 1, xs.min():xs.max() + 1], t)

    # ---------------------------------------------------------------- misiones (Olympia)
    QUEST_TURNIN = ("reclamar", "completar", "entregar", "finalizar", "terminar")

    def _q_words(self, img, box=None):
        box = box or {"x1": 0, "y1": 0, "x2": img.shape[1], "y2": int(img.shape[0] * 0.92)}
        return ocr.read_words(img, box, scale=1.0)

    def _quest_hud(self, img):
        """Cartel de la misión activa arriba a la derecha: ('Humble Beginning', hechos, total) o None."""
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
        ws = self._q_words(img, {"x1": int(1670 * kx), "y1": int(225 * ky), "x2": img.shape[1], "y2": int(420 * ky)})
        prog = next((w for w in ws if re.fullmatch(r"\d+/\d+", w[0].strip())), None)
        if not prog:
            done = next((w for w in ws if "mplet" in w[0].lower() or "mpiet" in w[0].lower()), None)
            if done:  # '¡Completado!': lista para cobrar
                name = " ".join(w[0] for w in ws if w[2] < done[2] - 12 * ky)
                return name.strip(), 1, 1
            return None
        a_, b_ = map(int, prog[0].strip().split("/"))
        name = " ".join(w[0] for w in ws if w[2] < prog[2] - 12 * ky)
        return name.strip(), a_, b_

    def _reward_pick(self, opts):
        """Premio a elegir: lo más cómodo para un guerrero de espada a dos manos. opts = [(texto, x, y)]."""
        two_h = ("claymore", "great sword", "greatsword", "flameberge", "giant sword", "battle axe", "two-hand",
                 "two hand", "2h", "templar", "barbarian", "dark executor", "black shadow", "lightning blade")
        armor = ("plate", "chain", "scale", "hauberk", "helm", "leggings", "hose", "boots", "cape", "shield", "mail")
        bad = ("wand", "staff", "bow", "dagger", "rapier", "esterk", "robe", "magic")

        need_w = bool(self.cfg.get("need_weapon"))
        melee = ("dagger", "sword", "gladius", "falchion", "gauche", "axe", "rapier", "esterk", "claymore", "saber", "blade")

        def score(t):
            t = t.lower()
            if any(k in t for k in two_h):
                return 100
            if need_w and any(k in t for k in melee):
                return 95  # el guerrero sin arma: cualquier arma cuerpo a cuerpo antes que el oro
            if any(k in t for k in armor):
                return 70
            if "gold" in t or " oro" in t:
                return 60
            if any(k in t for k in bad):
                return 5
            return 20
        return max(opts, key=lambda o: score(o[0])) if opts else None

    # ---- base de misiones (wiki de Olympia) para ir subiendo con misiones
    def _quest_db(self):
        db = getattr(self, "_qdb", None)
        if db is None:
            db = self._qdb = self._load_json("misiones_olympia.json") or {"quests": [], "npcs": {}}
        return db

    def _quest_active(self):
        """Misión del cartel (Humble Beginning 2/50) cruzada con la base: (entrada, hechos, total) o None."""
        hud = getattr(self, "quest", None)
        if not hud or not hud[0]:
            return None
        nm = "".join(c for c in hud[0].lower() if c.isalnum())
        best, br = None, 0.0
        for q in self._quest_db().get("quests", []):
            r = difflib.SequenceMatcher(None, nm, "".join(c for c in q["name"].lower() if c.isalnum())).ratio()
            if r > br:
                best, br = q, r
        return (best, hud[1], hud[2]) if best and br >= 0.75 else None

    def _quest_mob_names(self):
        qa = self._quest_active()
        if not qa or qa[1] >= qa[2]:
            return set()
        return {o["mob"] for o in qa[0]["objectives"] if o["mob"] != "*" and not o.get("elite")}

    def _quest_map_for(self, q):
        """Mapa de la misión que me corresponde (lado Aresden) y que conozco."""
        side = [m for m in q["maps"] if not any(k in m.lower() for k in ("elvine", "eldiniel"))] or q["maps"]
        for m in side:
            if self.world.map_for(m) or m.lower() in ("beginner zone",):
                return m
        return None

    def _quest_dest(self):
        """Con una misión activa: el mapa donde se cumple (si es de mapa) o donde salen sus bichos."""
        qa = self._quest_active()
        if not qa or qa[1] >= qa[2]:
            return None
        q = qa[0]
        cur = self.pos.get("map") or ""
        if q.get("map_only"):
            m = self._quest_map_for(q)
            return m
        mobs = self._quest_mob_names()
        sp = getattr(self, "_spawns", None)
        if sp is None:
            sp = self._spawns = self._spawns_all()
        here = next((v for k, v in sp.items() if self._same(k, cur)), None)
        if here and any(z.get("mob") in mobs for z in here.get("zones", [])):
            return cur
        for k, v in sp.items():
            if any(z.get("mob") in mobs for z in v.get("zones", [])) and not any(w in k.lower() for w in ("elvine", "eldiniel")) \
                    and self.world.map_for(k):
                return k
        return None

    def _quest_pick(self):
        """Sin misión: la próxima que conviene (kill, nivel, sin enfriamiento, bichos que aguanto), por exp por bicho."""
        lvl = int(self.cfg.get("travel", {}).get("my_level", 1) or 1)
        done = (self.learner.st or {}).get("quests_done", {}) if self.learner.st else {}
        crs = {c["name"]: c for c in self._creatures_db().values()}
        hpmax = self._hpmax_est() or 40
        cap = max(3.0, 0.10 * hpmax) if hpmax < 150 else 0.04 * hpmax
        now = time.time()
        best = None
        last = getattr(self, "_q_last_seen", None)
        for q in self._quest_db().get("quests", []):
            if q.get("kind") != "kill" or lvl < int(q.get("level") or 1):
                continue
            if last and last[0] == q["name"] and now - last[1] < 1800:
                continue  # la vi activa hace poco (sin terminar): el cartel no se leyó, no la voy a pedir de nuevo
            t_done = done.get(q["name"])
            if t_done:
                rep = q.get("repeat_h")
                if rep is None or now - t_done < max(0.05, rep or 0) * 3600:
                    continue
            if not self._quest_map_for(q) and q.get("map_only"):
                continue
            ok = True
            for o in q["objectives"]:
                cr = crs.get(o["mob"])
                if o["mob"] == "*" or o.get("elite") or not cr or cr.get("dmg_avg", 99) > cap:
                    ok = False
            if not ok:
                continue
            kills = sum(o["count"] for o in q["objectives"]) or 1
            score = q["exp"] / kills
            if best is None or score > best[0]:
                best = (score, q)
        return best[1] if best else None

    def _quest_go_accept(self):
        """Voy al NPC de la próxima misión (si sé dónde está) y la acepto."""
        q = self._quest_pick()
        if not q:
            return False
        now = time.time()
        if now - getattr(self, "_qacc_t", 0) < 600 and getattr(self, "_qacc_name", None) == q["name"] and not getattr(self, "goto", None):
            return False
        loc = None
        stay = (self.cfg.get("travel", {}) or {}).get("stay_map") or ""
        cur0 = self.pos.get("map") or ""
        cands = [(mp_, xy) for mp_, xy in (self._quest_db().get("npcs", {}).get(q["npc"], {}) or {}).items()
                 if not any(k in mp_.lower() for k in ("elvine", "eldiniel"))]
        cands.sort(key=lambda c: (not self._same(c[0], stay or cur0), not self._same(c[0], cur0)))
        if stay:
            cands = [c for c in cands if self._same(c[0], stay)]
        for mp_, xy in cands:
            loc = (mp_, xy[0], xy[1])
            break
        if not loc:
            return False
        cur = self.pos.get("map") or ""
        if not self._same(cur, loc[0]) or max(abs(self.pos["x"] - loc[1]), abs(self.pos["y"] - loc[2])) > 2:
            if not getattr(self, "goto", None) and now - getattr(self, "_qacc_go_t", 0) > 120:
                self._qacc_go_t = now
                self.log(f"📜 Próxima misión: '{q['name']}' ({q['exp']:,} de exp). Voy a buscarla con {q['npc']} ({loc[0]} {loc[1]},{loc[2]}).")
                self.goto = {"map": loc[0], "x": loc[1], "y": loc[2] + 1, "t": now}
            return bool(getattr(self, "goto", None))
        self._qacc_t, self._qacc_name = now, q["name"]
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        grab = lambda: self.screen.grab(self.rect)
        click = lambda x, y, b="left": winapi.click(*self.abs_pt(x, y), button=b)
        tw, th = self._tile()
        cx, cy = self._char()
        self._release()
        click(cx + (loc[1] - self.pos["x"]) * tw, cy + (loc[2] - self.pos["y"]) * th - th * 0.3)
        time.sleep(2.2)
        ws = self._q_words(grab())
        key = norm(q["name"].split()[0])
        rows = [w for w in ws if norm(w[0]).startswith("misi")]
        pick = None
        for r_ in rows:
            line = " ".join(v[0] for v in ws if abs(v[2] - r_[2]) < 8)
            if key in norm(line):
                pick = r_
                break
        pick = pick or (rows[0] if rows else None)
        if not pick:
            self.log(f"📜 {q['npc']} no me ofrece '{q['name']}' ahora.")
            return False
        click(pick[1] + 60 * tw / 72, pick[2])
        time.sleep(1.5)
        ac = next((w for w in self._q_words(grab()) if norm(w[0]) == "aceptar"), None)
        if ac:
            click(ac[1] + 30 * tw / 72, ac[2])
            time.sleep(1.2)
            self.log(f"📜 Acepté '{q['name']}'.")
            self._q_npc = None
        if any(norm(w[0]) == "activas" for w in self._q_words(grab())):
            winapi.press("CTRL+Q")
        return True

    def _quest_step(self):
        """Misiones: acepta las que ofrezca un NPC con '!' amarillo y, cuando el cartel marca la misión completa,
        vuelve al NPC (Ctrl+Q dice dónde está) y cobra eligiendo el premio más útil para el guerrero 2H."""
        if cfgmod.server_slug(self.cfg) == "abaddon" or not self.cfg.get("quests_auto", True):
            return False
        now = time.time()
        if self.state != "seek" or self._under_attack() or self._fleeing() or not self.rect:
            return False
        if now - getattr(self, "_q_t", 0) < 20:
            return False
        self._q_t = now
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        grab = lambda: self.screen.grab(self.rect)
        click = lambda x, y, b="left": winapi.click(*self.abs_pt(x, y), button=b)
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
        img = grab()
        hud = self._quest_hud(img)
        if hud:
            self._q_miss = 0
        else:
            self._q_miss = getattr(self, "_q_miss", 0) + 1
            if self._q_miss < 4 and getattr(self, "quest", None):
                hud = None  # una lectura fallida no es "sin misión": espero 4 seguidas antes de ir a buscar otra
                self._q_scan_t = now
                return False
        self.quest = hud
        if hud and hud[1] < hud[2]:
            qa = self._quest_active()
            if qa:
                self._q_last_seen = (qa[0]["name"], now)
        # 1) misión completa: voy a cobrar
        if hud and hud[2] > 0 and hud[1] >= hud[2]:
            qn = getattr(self, "_q_npc", None)
            if not qn or now - qn.get("t", 0) > 600:
                qn = self._quest_where()
                if qn:
                    qn["t"] = now
                    self._q_npc = qn
            if not qn:
                return False
            if not self._same(self.pos.get("map") or "", qn["map"]) or max(abs(self.pos["x"] - qn["x"]), abs(self.pos["y"] - qn["y"])) > 2:
                if not getattr(self, "goto", None):
                    self.log(f"📜 Misión '{hud[0]}' lista ({hud[1]}/{hud[2]}): voy a cobrarla con {qn['npc']} ({qn['map']} {qn['x']},{qn['y']}).")
                    self.goto = {"map": qn["map"], "x": qn["x"], "y": qn["y"] + 1, "t": now}
                return True
            self._quest_turnin(qn, hud)
            return True
        # 2) sin misión activa: voy a buscar la que más conviene (base de la wiki)
        if not hud:
            try:
                if self._quest_go_accept():
                    return True
            except Exception as e:
                self.log(f"📜 Error buscando misión: {e!r}")
        # 3) NPC con '!' amarillo en pantalla: le hablo y acepto
        if now - getattr(self, "_q_scan_t", 0) < 45:
            return False
        self._q_scan_t = now
        g = self._game_roi()
        roi = img[g["y1"]:g["y2"], g["x1"]:min(g["x2"], int(1660 * kx))]
        b_, g_, r_ = roi[:, :, 0].astype(int), roi[:, :, 1].astype(int), roi[:, :, 2].astype(int)
        mask = ((r_ > 200) & (g_ > 170) & (b_ < 90)).astype(np.uint8)
        import cv2
        n, lab, st_, cen = cv2.connectedComponentsWithStats(mask)
        marks = []
        for i in range(1, n):
            w_, h_, ar = st_[i, cv2.CC_STAT_WIDTH], st_[i, cv2.CC_STAT_HEIGHT], st_[i, cv2.CC_STAT_AREA]
            if 8 <= ar <= 260 and h_ >= 1.6 * w_ and 8 * ky <= h_ <= 40 * ky:
                marks.append((int(cen[i][0]) + g["x1"], int(cen[i][1]) + g["y1"]))
        tried = getattr(self, "_q_tried", {})
        self._q_tried = tried = {k: v for k, v in tried.items() if v > now}
        for mx, my in marks:
            key = (self.pos.get("map"), round((mx - self._char()[0]) / (72 * kx)) + (self.pos.get("x") or 0),
                   round((my - self._char()[1]) / (72 * ky)) + (self.pos.get("y") or 0))
            if key in tried:
                continue
            tried[key] = now + 900
            self._release()
            click(mx, my + 60 * ky)
            time.sleep(2.2)
            ws = self._q_words(grab())
            ms = next((w for w in ws if norm(w[0]).startswith("misi")), None)
            if not ms:
                cancel = next((w for w in ws if norm(w[0]) == "cancelar"), None)
                if cancel:
                    click(cancel[1], cancel[2])
                continue
            click(ms[1] + 60 * kx, ms[2])
            time.sleep(1.5)
            ws = self._q_words(grab())
            ac = next((w for w in ws if norm(w[0]) == "aceptar"), None)
            if ac:
                click(ac[1] + 30 * kx, ac[2])
                time.sleep(1.2)
                self.log(f"📜 Acepté una misión nueva (NPC en {key[0]} {key[1]},{key[2]}).")
                self._q_npc = None
            if any(norm(w[0]) == "activas" for w in self._q_words(grab())):
                winapi.press("CTRL+Q")
            return True
        return False

    def _quest_where(self):
        """Abre la lista de misiones (Ctrl+Q) y lee dónde está el NPC: 'Enzu (Granja Aresden en 49,97)'."""
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        self._release()
        img = self.screen.grab(self.rect)
        ws = self._q_words(img)
        if not any(norm(w[0]) == "activas" for w in ws):  # si ya está abierta, Ctrl+Q la cerraría
            winapi.press("CTRL+Q")
            time.sleep(1.2)
            img = self.screen.grab(self.rect)
            ws = self._q_words(img)
        ver = next((w for w in ws if norm(w[0]) == "ver"), None)
        if ver and not any(norm(w[0]) == "en" for w in ws):
            winapi.click(*self.abs_pt(ver[1], ver[2]))
            time.sleep(0.8)
            img = self.screen.grab(self.rect)
            ws = self._q_words(img)
        out = None
        try:
            line = None
            for w in ws:
                if norm(w[0]) == "en":
                    row = sorted([v for v in ws if abs(v[2] - w[2]) < 9], key=lambda v: v[1])
                    txt = " ".join(v[0] for v in row)
                    if "(" in txt and re.search(r"\d+\s*,\s*\d+", txt):
                        line = txt
                        break
            if line:
                m = re.match(r"\s*(.+?)\s*\((.+?)\s+en\s+(\d+)\s*,\s*(\d+)", line)
                if m:
                    mp = self._loc_map_name(m.group(2)) or m.group(2)
                    out = {"npc": m.group(1), "map": mp, "x": int(m.group(3)), "y": int(m.group(4))}
        finally:
            if any(norm(w[0]) == "activas" for w in self._q_words(self.screen.grab(self.rect))):
                winapi.press("CTRL+Q")  # la cierro con la misma tecla
                time.sleep(0.5)
            for t in ("misiones", "menú", "menu"):
                hd = next((w for w in self._q_words(self.screen.grab(self.rect)) if norm(w[0]).startswith(norm(t))), None)
                if hd:
                    winapi.click(*self.abs_pt(hd[1], hd[2]), button="right")
                    time.sleep(0.4)
        return out

    def _quest_turnin(self, qn, hud):
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        grab = lambda: self.screen.grab(self.rect)
        click = lambda x, y, b="left": winapi.click(*self.abs_pt(x, y), button=b)
        tw, th = self._tile()
        cx, cy = self._char()
        self._release()
        click(cx + (qn["x"] - self.pos["x"]) * tw, cy + (qn["y"] - self.pos["y"]) * th - th * 0.3)
        time.sleep(2.2)
        ws = self._q_words(grab())
        ms = next((w for w in ws if norm(w[0]).startswith("misi")), None)
        if not ms:
            self.log(f"📜 No me abre la charla con {qn['npc']}. Pruebo de nuevo en un rato.")
            self._q_npc = None
            return
        click(ms[1] + 60 * tw / 72, ms[2])
        time.sleep(1.5)
        ws = self._q_words(grab())
        el = next((w for w in ws if norm(w[0]).startswith("elige")), None)
        pick = None
        if el:
            rows = {}
            for w in ws:
                if el[2] + 8 < w[2] < el[2] + 140 * th / 72 and w[1] > el[1] - 20:
                    rows.setdefault(round(w[2] / 9), []).append(w)
            opts = [(" ".join(v[0] for v in sorted(r_, key=lambda v: v[1])), min(v[1] for v in r_), r_[0][2]) for r_ in rows.values()]
            opts = [o for o in opts if not any(k in o[0].lower() for k in ("aceptar", "abandonar", "ocultar", "complet", "entregar"))]
            pick = self._reward_pick(opts)
            if pick:
                click(pick[1] + 20, pick[2])
                time.sleep(0.8)
                self.log(f"📜 Premio elegido: {pick[0]}.")
        ws = self._q_words(grab())
        bt = next((w for k in self.QUEST_TURNIN for w in ws if norm(w[0]) == k or norm(w[0]).startswith(k) and len(norm(w[0])) <= len(k) + 2), None)
        if bt:
            click(bt[1], bt[2])
            time.sleep(1.5)
            h2 = self._quest_hud(grab())
            if h2 and h2[1] >= h2[2] and h2[2] > 0:
                self.log("📜 Apreté para cobrar pero la misión sigue sin cobrar; pruebo de nuevo en un rato.")
                self._q_npc = None
                return
            self.log(f"📜 Cobré la misión '{hud[0]}'.")
            if self.cfg.get("need_weapon") and el and pick and any(k in pick[0].lower() for k in ("dagger", "sword", "gladius", "falchion", "gauche", "axe")):
                try:
                    self._equip_from_bag(pick[0])
                except Exception as e:
                    self.log(f"🛡 No pude ponerme el arma: {e!r}")
            try:
                qa = self._quest_active()
                self.learner._profile()
                self.learner.st.setdefault("quests_done", {})[qa[0]["name"] if qa else hud[0]] = time.time()
                self.learner.save()
            except Exception:
                pass
        else:
            self.log("📜 No encuentro el botón para cobrar la misión (dejo la ventana para que la veas).")
            self._q_t = time.time() + 600
            return
        self._q_npc = None
        if any(norm(w[0]) == "activas" for w in self._q_words(grab())):
            winapi.press("CTRL+Q")

    KEEP_WORDS = ("potion", "poción", "pocion", "gold", "oro", "ring", "anillo", "necklace", "collar", "ticket",
                  "scroll", "pergamino", "key", "llave", "baguette", "meat", "carne")

    def _oly_bag(self, names=True):
        """Bolso abierto (F6): [{'name','x','y','kind'}] con lo que hay; kind = pot/gold/item. También deja
        self._gold con el oro leído. Pasa el mouse por cada cosa para leer el nombre en el cartelito."""
        import cv2
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
        img = self.screen.grab(self.rect)
        ws = ocr.read_words(img, {"x1": 0, "y1": 0, "x2": img.shape[1], "y2": int(img.shape[0] * 0.92)}, scale=1.0)
        pw = next((w for w in ws if w[0].strip().startswith("Peso:")), None)
        if not pw:
            return None
        x1, y1 = int(pw[1] - 40 * kx), int(pw[2] + 28 * ky)
        x2, y2 = int(pw[1] + 440 * kx), int(pw[2] + 330 * ky)
        roi = img[y1:y2, x1:x2]
        self._release()
        winapi.left_up()  # nunca pasar por el bolso con el botón apretado: arrastraría (y tiraría) cosas
        mask = (roi.max(axis=2) > 90).astype(np.uint8)
        mask = cv2.dilate(mask, np.ones((5, 5), np.uint8))
        n, lab, st_, cen = cv2.connectedComponentsWithStats(mask)
        comps = sorted(range(1, n), key=lambda i: -st_[i, cv2.CC_STAT_AREA])
        out = []
        for i in comps:
            if st_[i, cv2.CC_STAT_AREA] < 60 * kx * ky or st_[i, cv2.CC_STAT_HEIGHT] < 10 * ky:
                continue
            bx, by = int(cen[i][0]) + x1, int(cen[i][1]) + y1
            # salto los números (cantidades): bajitos y justo debajo de otro objeto
            if st_[i, cv2.CC_STAT_HEIGHT] < 20 * ky and any(abs(bx - o["x"]) < 45 * kx and 0 < by - o["y"] < 45 * ky for o in out):
                continue
            px_ = roi[max(0, by - y1 - 10):by - y1 + 10, max(0, bx - x1 - 12):bx - x1 + 12].reshape(-1, 3).astype(int)
            yel = int(((px_[:, 2] > 150) & (px_[:, 1] > 140) & (px_[:, 0] < 110)).sum())
            kind = "gold" if yel >= 12 else "item"
            out.append({"x": bx, "y": by, "kind": kind, "name": ""})
        for o in out:
            if o["kind"] == "gold":
                o["name"] = "Gold"
                gbox = (o["x"] + int(2 * kx), o["y"] + int(10 * ky), o["x"] + int(80 * kx), o["y"] + int(38 * ky))
                d, approx = None, False
                try:
                    # el juego abrevia: '774', '2.6k', '1.2m'. Lo busco entre las palabras de la ventana (se lee mejor entera)
                    for w in ws:
                        if abs(w[1] - (o["x"] + 22 * kx)) < 40 * kx and 0 <= w[2] - o["y"] < 34 * ky:
                            t = w[0].strip().lower().replace(",", ".")
                            mm = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", t)
                            if mm:
                                d = int(round(float(mm.group(1)) * {"": 1, "k": 1000, "m": 1000000}[mm.group(2)]))
                                approx = bool(mm.group(2))
                                break
                    if d is None:
                        d = next((int(norm(w[0])) for w in ocr.read_words(img, {"x1": gbox[0], "y1": gbox[1], "x2": gbox[2], "y2": gbox[3]}, scale=2.0) if norm(w[0]).isdigit()), None)
                except Exception:
                    d = None
                if d is not None and d >= 10 and not approx:
                    self._glyph_learn(img, gbox, d)
                if d is None:
                    d = self._read_small_num(img, *gbox)
                self._gold = d
                self._eco_note(d, approx)
                continue
            if not names:
                continue
            winapi.move(*self.abs_pt(int(pw[1] + 300 * kx), int(pw[2] - 60 * ky)))
            time.sleep(0.3)
            im1 = self.screen.grab(self.rect)
            winapi.move(*self.abs_pt(o["x"], o["y"]))
            time.sleep(0.8)
            im2 = self.screen.grab(self.rect)
            # el cartelito con el nombre es lo que cambió al pasar el mouse
            dif = (np.abs(im2.astype(int) - im1.astype(int)).max(axis=2) > 40).astype(np.uint8)
            dif[max(0, o["y"] - int(30 * ky)):o["y"] + int(30 * ky), max(0, o["x"] - int(30 * kx)):o["x"] + int(30 * kx)] = 0
            ys_, xs_ = np.nonzero(dif)
            tw_ = []
            if len(xs_) > 200:
                tw_ = ocr.read_words(im2, {"x1": int(xs_.min()), "y1": int(ys_.min()), "x2": int(xs_.max()) + 1, "y2": int(ys_.max()) + 1}, scale=1.0)
            tw_ = [w for w in tw_ if not w[0].strip().isdigit()]
            if tw_:
                top = min(w[2] for w in tw_)
                o["name"] = " ".join(w[0] for w in sorted([w for w in tw_ if w[2] - top < 10 * ky], key=lambda w: w[1]))
            if any(k in o["name"].lower() for k in ("potion", "poción", "pocion")):
                o["kind"] = "pot"
        winapi.move(*self.abs_pt(int(pw[1] + 300 * kx), int(pw[2] - 60 * ky)))
        return out

    # ---------------------------------------------------------------- economía
    def _eco_note(self, gold, approx=False):
        """Anoto cada lectura del oro: de ahí sale cuánto junto por hora (lo que gasto no cuenta como pérdida)."""
        if gold is None:
            return
        now = time.time()
        e = self.__dict__.setdefault("_eco", {"t0": now, "g0": gold, "earned": 0, "spent": 0, "last": gold, "hist": []})
        d = gold - e["last"]
        if d > 0:
            e["earned"] += d
        elif d < 0:
            e["spent"] += -d
        e["last"], e["t"], e["approx"] = gold, now, bool(approx)
        e["hist"].append((int(now), gold))
        del e["hist"][:-200]
        try:
            mk = (self.pos.get("map") or "").strip().lower()
            if d > 0 and mk:
                y = self._yield_entry(mk)
                y["gold"] = y.get("gold", 0.0) + d
        except Exception:
            pass

    def economy(self):
        """Resumen para la pestaña Economía: oro, ritmo, bolso, equipo y plan de compras."""
        now = time.time()
        e = getattr(self, "_eco", None) or {}
        hrs = (now - e["t0"]) / 3600.0 if e else 0
        per_h = int(e["earned"] / hrs) if e and hrs > 0.15 else None
        p = getattr(self, "plan", None) or {}
        sv = p.get("saving")
        gold = e.get("last") if e else getattr(self, "_gold", None)
        if sv and gold is not None and sv.get("price"):
            miss = max(0, int(sv["price"]) - max(0, gold - int(self.cfg.get("gold_reserve", 150))))
            sv = dict(sv, missing=miss, eta_h=(round(miss / per_h, 1) if per_h and miss else None))
        bag = []
        for o in getattr(self, "_bag_list", None) or []:
            bag.append({"name": o.get("name") or "", "kind": o.get("kind"), "n": o.get("n")})
        zones = []
        for mk, y in (self.world.learn.get("yield", {}) or {}).items():
            if y.get("sec", 0) >= 300:
                zones.append({"map": mk.title(), "exp_h": int(y.get("exp", 0) / y["sec"] * 3600),
                              "gold_h": int(y.get("gold", 0) / y["sec"] * 3600), "min": int(y["sec"] / 60)})
        zones.sort(key=lambda z: -z["exp_h"])
        sh = self._sheet(6 * 3600) or {}
        return {"gold": gold, "approx": bool(e.get("approx")), "gold_age": int(now - e["t"]) if e.get("t") else None,
                "earned": e.get("earned"), "spent": e.get("spent"), "per_hour": per_h, "hours": round(hrs, 1),
                "pots": dict(getattr(self, "_pot_counts", None) or {}), "bag": bag, "bag_age": int(now - getattr(self, "_bag_t", now)) if getattr(self, "_bag_list", None) else None,
                "equipped": list(getattr(self, "_owned", None) or []),
                "gear": [{"slot": g["slot"], "have": g.get("have"),
                          "next": ({"name": g["next"]["name"], "price": g["next"].get("price")} if g.get("next") else None),
                          "blocked": g.get("blocked")} for g in (p.get("gear") or [])],
                "saving": sv, "buy_now": p.get("buy_now") or [],
                "strategy": dict(getattr(self, "_strat", None) or {}),
                "lines": ([(getattr(self, "_strat", None) or {}).get("line")] if (getattr(self, "_strat", None) or {}).get("line") else []) + [l for l in (p.get("lines") or [])], "zones": zones[:6], "level": sh.get("level") or self.cfg.get("travel", {}).get("my_level"),
                "pct": sh.get("pct"), "server": cfgmod.server_slug(self.cfg),
                "prices": dict((self.learner.st or {}).get("prices", {})) if getattr(self.learner, "st", None) else {},
                "check": getattr(self, "health", None)}

    def _equip_from_bag(self, name):
        """Me pongo un objeto del bolso con Ctrl + click (wiki: equipa lo que se pueda equipar)."""
        img = self.screen.grab(self.rect)
        if not any(w[0].strip().startswith("Peso:") for w in self._q_words(img)):
            winapi.press("F6")
            time.sleep(0.9)
        bag = self._oly_bag() or []
        key = name.lower().split("(")[0].strip().split()[-1] if name else "dagger"
        it = next((o for o in bag if key in o.get("name", "").lower()), None)
        if not it:
            it = next((o for o in bag if o["kind"] == "item" and not any(k in o.get("name", "").lower() for k in self.KEEP_WORDS)), None)
        if not it:
            self.log("🛡 No encuentro el arma nueva en el bolso.")
            return False
        winapi.left_up()
        winapi.key_down("CTRL")
        time.sleep(0.05)
        winapi.click(*self.abs_pt(it["x"], it["y"]))
        time.sleep(0.05)
        winapi.key_up("CTRL")
        time.sleep(0.8)
        winapi.press("F6")
        self.cfg["need_weapon"] = False
        cfgmod.save(self.cfg)
        self.log(f"🛡 Me puse {it.get('name') or name}.")
        return True

    def _oly_pots(self):
        """Pociones de vida en el bolso (Olympia): abro F6, busco los frascos rojos y leo el número de abajo."""
        import cv2
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0

        def peso(img):
            ws = ocr.read_words(img, {"x1": 0, "y1": 0, "x2": img.shape[1], "y2": int(img.shape[0] * 0.92)}, scale=1.0)
            return next((w for w in ws if w[0].strip().startswith("Peso:")), None)

        img = self.screen.grab(self.rect)
        pw = peso(img)
        opened = False
        if not pw:
            self._release()
            winapi.press("F6")
            time.sleep(0.9)
            img = self.screen.grab(self.rect)
            pw = peso(img)
            opened = True
        try:
            if not pw:
                return None
            x1, y1 = int(pw[1] - 40 * kx), int(pw[2] + 15 * ky)
            x2, y2 = int(pw[1] + 440 * kx), int(pw[2] + 330 * ky)
            roi = img[y1:y2, x1:x2]
            b_, g_, r_ = roi[:, :, 0].astype(int), roi[:, :, 1].astype(int), roi[:, :, 2].astype(int)
            mask = ((r_ > 140) & (g_ < 70) & (b_ < 80)).astype(np.uint8)
            n, lab, st_, cen = cv2.connectedComponentsWithStats(mask)
            total, seen, unknown = 0, [], False
            for i in range(1, n):
                if st_[i, cv2.CC_STAT_AREA] < 12:
                    continue
                cx, cy = int(cen[i][0]) + x1, int(cen[i][1]) + y1
                if any(abs(cx - q[0]) < 20 * kx and abs(cy - q[1]) < 20 * ky for q in seen):
                    continue  # el mismo frasco partido en dos manchas (tapa y cuerpo)
                seen.append((cx, cy))
                d = self._read_small_num(img, cx + int(2 * kx), cy + int(13 * ky), cx + int(42 * kx), cy + int(36 * ky))
                if d is None:
                    unknown = True
                total += d if d else 1
            self._pots_unknown = unknown
            try:
                bag = self._oly_bag(names=False) or []
                # clasifico cada cosa del bolso por el color del ícono: frascos azules (maná) y verdes (energía)
                pc = {"vida": total, "maná": 0, "energía": 0}
                others = 0
                for o in bag:
                    if o["kind"] != "item":
                        continue
                    p_ = img[max(0, o["y"] - 12):o["y"] + 12, max(0, o["x"] - 10):o["x"] + 10].reshape(-1, 3).astype(int)
                    blue = int(((p_[:, 0] > 140) & (p_[:, 2] < 110) & (p_[:, 1] < 110)).sum())
                    green = int(((p_[:, 1] > 140) & (p_[:, 2] < 110) & (p_[:, 0] < 110)).sum())
                    red = int(((p_[:, 2] > 140) & (p_[:, 1] < 70) & (p_[:, 0] < 80)).sum())
                    if red >= 12:
                        o["kind"] = "pot"
                        continue
                    if blue >= 12 or green >= 12:
                        o["kind"] = "pot"
                        n_ = self._read_small_num(img, o["x"] - int(9 * kx), o["y"] + int(13 * ky), o["x"] + int(13 * kx), o["y"] + int(36 * ky)) or 1
                        pc["maná" if blue >= green else "energía"] += n_
                    else:
                        others += 1
                self._pot_counts = pc
                self._bag_items = others
                self._bag_list = [{"name": "", "kind": "item"} for _ in range(others)]
                self._bag_t = time.time()
            except Exception:
                pass
            return total
        finally:
            if opened:
                winapi.press("F6")
                time.sleep(0.3)

    def _oly_supplies(self):
        """Olympia: cuento las pociones abriendo el bolso cada tanto y, si quedan pocas, voy a la tienda."""
        now = time.time()
        th = getattr(self, "_shop_th", None)
        if th is not None and th.is_alive():
            return
        if self.state != "seek" or self._under_attack() or self._fleeing():
            return
        if now - getattr(self, "_pots_t", 0) > 240:
            self._pots_t = now
            try:
                n = self._oly_pots()
            except Exception:
                n = None
            if n is not None:
                if n != getattr(self, "_left", {}).get("pot"):
                    self.log(f"🧪 Me quedan {n}{'+' if getattr(self, '_pots_unknown', False) else ''} pociones de vida.")
                self._left = {"pot": n, "meat": 99}
        n = getattr(self, "_left", {}).get("pot")
        lim = 0 if getattr(self, "_pots_unknown", False) else int(self.cfg.get("buy_pots_below", 3))
        gold = getattr(self, "_gold", None)
        afford = gold is not None and self._pot_budget(gold, n or 0) >= (1 if not n else 3)
        many = (getattr(self, "_bag_items", 0) or 0) >= int(self.cfg.get("sell_when_items", 12))
        if now - getattr(self, "_shop_last", 0) < 900:
            return
        if not many and (n is None or n > lim or not afford):
            return
        self._shop_last = now
        self._shop_th = threading.Thread(target=self._oly_shop_trip, daemon=True)
        self._shop_th.start()

    def _oly_shop_trip(self):
        """Tienda de Aresden (granja) en Olympia: Kiora <Tendera> → Comerciar → Health Potion → Máx. → Comprar."""
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        inshop = lambda: any(k in (self.pos.get("map") or "").lower() for k in ("shop", "tienda"))
        try:
            self.log(f"🛒 Quedan {getattr(self, '_left', {}).get('pot')} pociones: voy a la tienda de la granja de Aresden.")
            t0 = time.time()
            if any(k in (self.pos.get("map") or "").lower() for k in ("smith", "herrer")):
                time.sleep(1.0)  # estoy en el herrero: el encargo me saca solo
            far = self.pos.get("x") is None or max(abs(self.pos["x"] - 63), abs(self.pos["y"] - 72)) > 3
            if (far or not self._same(self.pos.get("map") or "", "Aresden Farm")) and not inshop():
                # siempre me paro primero frente a la puerta (del lado de afuera): de costado hay pared
                self.goto = {"map": "Aresden Farm", "x": 63, "y": 72, "t": time.time()}
                while self.goto and time.time() - t0 < 600 and self.alive and self.running:
                    time.sleep(0.5)
            if not inshop():
                self.mission = {"map": "Aresden Farm", "goal": [63, 69], "label": "Tienda (pociones)", "until": time.time() + 300, "enter": True, "hold": True}
                while not inshop() and time.time() - t0 < 900 and self.alive and self.running:
                    time.sleep(0.3)
            if not inshop():
                self.log("🛒 No llegué a la tienda.")
                return
            self._busy_ui = True
            self._release()
            time.sleep(1.5)
            grab = lambda: self.screen.grab(self.rect)
            words = lambda im: ocr.read_words(im, {"x1": 0, "y1": 0, "x2": im.shape[1], "y2": int(im.shape[0] * 0.92)}, scale=1.0)
            click = lambda x, y, b="left": winapi.click(*self.abs_pt(x, y), button=b)
            tw, th = self._tile()
            cx, cy = self._char()
            pl = self._place("shop") or {"npc_type": 15, "npc": [59, 42]}
            menu = self._npc_menu(pl["npc_type"], pl["npc"], "comerciar") if pl.get("npc_type", -1) >= 0 else None
            menu = menu or self._npc_find("shop", "comerciar")
            com = next((w for w in (menu or []) if norm(w[0]).startswith("comerciar")), None)
            if not com:
                self.log("🛒 No encontré a la tendera.")
                return
            click(com[1], com[2])
            time.sleep(1.8)
            ws = words(grab())
            tabp = next((w for w in ws if norm(w[0]).startswith("poci") or norm(w[0]).startswith("pocl")), None)
            kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
            # 1) vender lo que no sirve (arrastrar del bolso a la lista de venta y 'Vender ítems')
            bag = self._oly_bag() or []
            sell = [o for o in bag if o["kind"] == "item" and o["name"] and not any(k in o["name"].lower() for k in self.KEEP_WORDS)]
            if not self.cfg.get("sell_olympia", False):
                sell = []  # apagado hasta probarlo bien: leía mal los nombres y podía vender lo que no debía
            if sell and tabp:
                click(tabp[1] + 346 * kx, tabp[2])
                time.sleep(1.0)
                for o in sell:
                    self._drag_bg(o["x"], o["y"], tabp[1] + 166 * kx, tabp[2] + 317 * ky)
                vb = next((w for w in words(grab()) if norm(w[0]) == "vender" and w[2] > tabp[2] + 500 * ky), None)
                click(*(vb[1:3] if vb else (tabp[1] + 170 * kx, tabp[2] + 647 * ky)))
                time.sleep(1.5)
                self.log("🛒 Vendí: " + ", ".join(o["name"] for o in sell) + ".")
                click(tabp[1], tabp[2])
                time.sleep(1.0)
            self._oly_bag(names=False)  # leo el oro
            # 2) pociones: solo con el oro que sobra después de la reserva para equipo, y hasta 4
            gold = getattr(self, "_gold", None)
            have = getattr(self, "_left", {}).get("pot") or 0
            reserve = int(self.cfg.get("gold_reserve", 150))
            # cuántas: hasta el objetivo de pociones, gastando como mucho un tercio del oro que sobra de la reserva
            want_n = int(self.cfg.get("pots_target", 15))
            n = self._pot_budget(gold, have)
            if n <= 0:
                self.log(f"🛒 No compro pociones: tengo {gold} de oro y guardo {reserve} para equipo.")
                return
            ws = words(grab())
            try:
                rows = {}
                for w in ws:
                    rows.setdefault(round(w[2] / 10), []).append(w)
                pr = {}
                for r_ in rows.values():
                    r_ = sorted(r_, key=lambda v: v[1])
                    if len(r_) >= 2 and r_[-1][0].strip().isdigit() and any(k in " ".join(v[0] for v in r_).lower() for k in ("potion", "meat", "baguette")):
                        pr[" ".join(v[0] for v in r_[:-1])] = int(r_[-1][0].strip())
                if pr:
                    self.learner.note_prices(pr)
            except Exception:
                pass
            hp = None
            for w in ws:
                if norm(w[0]) == "health" and not any(norm(v[0]) == "big" and abs(v[2] - w[2]) < 8 and v[1] < w[1] for v in ws):
                    hp = w
                    break
            if not hp:
                self.log("🛒 No veo las Health Potion en la lista.")
                return
            click(hp[1], hp[2])
            time.sleep(1.0)
            ws = words(grab())
            plus = next((w for w in ws if w[0].strip() == "+" and w[2] > hp[2] + 400 * ky), None)
            if not plus and tabp:  # el '+' casi nunca se lee: está siempre en el mismo lugar respecto de la pestaña
                plus = ("+", tabp[1] + 81 * kx, tabp[2] + 640 * ky)
            for _ in range(n - 1):
                if plus:
                    click(plus[1], plus[2])
                    time.sleep(0.3)
            cp = next((w for w in words(grab()) if norm(w[0]).startswith("comprar")), None)
            if not cp and tabp:
                cp = ("Comprar", tabp[1] + 355 * kx, tabp[2] + 640 * ky)
            if not cp:
                self.log("🛒 No veo el botón Comprar.")
                return
            click(cp[1], cp[2])
            time.sleep(1.5)
            self.log(f"🛒 Compré {n} pociones de vida (tenía {gold} de oro).")
            self._pots_t = 0
            tienda = next((w for w in words(grab()) if norm(w[0]) == "tienda"), None)
            if tienda:
                click(tienda[1], tienda[2], "right")
                time.sleep(0.4)
        except Exception as e:
            self.log(f"🛒 Error en la tienda: {e!r}")
        finally:
            # cierro la ventana de la tienda (click derecho adentro); abierta no deja caminar
            try:
                self._close_shop_window()
            except Exception:
                pass
            self.mission = None
            self._busy_ui = False
            self._shop_last = time.time()

    def _maybe_shop(self):
        if cfgmod.server_slug(self.cfg) != "abaddon":
            return self._oly_supplies()
        sm = self._supplies_mem()
        if sm:
            self._left = {"pot": sm["pot"], "meat": sm["meat"]}
        lf = getattr(self, "_left", {})
        need = lf.get("meat", 99) <= self.cfg.get("buy_meat_below", 1) or lf.get("pot", 99) <= self.cfg.get("buy_pots_below", 3)
        few = lf.get("pot", 99) < int(self.cfg.get("pots_to_leave_town", 10))
        if few and str(getattr(self, "_err_v", None) or "").startswith(("tengo pocas", "me quedé sin")):
            need = True
        full = bool(sm and self.INV_SLOTS - sm["items"] < 4)
        if getattr(self, "_restock_now", False):
            need, self._restock_now = True, False  # recién vendí: completo pociones en la misma vuelta
        if not (need or full) or getattr(self, "mission", None):
            return
        for a_ in ("_shop_th", "_rep_th", "_inv_th", "_gear_th"):
            th = getattr(self, a_, None)
            if th is not None and th.is_alive():
                return
        # vender va SIEMPRE junto con la recarga: primero vendo todo lo que no sirve, después compro pociones
        if time.time() - getattr(self, "_sell_last", 0) > 120 and time.time() > getattr(self, "_sell_fail_until", 0):
            try:
                d = self._gear_state()
            except Exception:
                d = None
            cur_ = self.pos.get("map") or ""
            town_ = next((n for n in ("Aresden Farm", "Aresden City") if self._same(cur_, n)), None)
            hp_ok = (self._hp_mem() or 0) >= 0.5 * max(1, self._hpmax_est())
            if d and d["sell"] and town_ and hp_ok:
                self._sell_last = time.time()
                self._restock_now = True
                self._sold_trip = 0
                self.log(f"🛒 Vuelta de recarga: primero vendo {len(d['sell'])} cosa(s) que no sirven, después compro pociones.")
                self._gear_th = threading.Thread(target=self._gear_trip, args=(dict(d, buy=[], equip=[]), town_, True), daemon=True)
                self._gear_th.start()
                return
        if not need:
            return
        if time.time() - getattr(self, "_shop_last", 0) < (300 if few else 900):
            return
        if not self._same(self.pos.get("map") or "", "Aresden Farm"):
            return
        self._shop_last = time.time()
        self._shop_th = threading.Thread(target=self._shop_trip, daemon=True)
        self._shop_th.start()

    INV_OFF, INV_SLOTS = 508, 50   # punteros a los items del bolso, contados desde la posición del pj
    ID_GOLD, ID_POT, ID_MEAT = 90, 91, 99

    def _inv_mem(self):
        """Inventario leído de la memoria del juego (sin abrir el bolso): lista de items con
        id, cantidad, durabilidad actual/máxima y lugar en el bolso. None si no se pudo leer."""
        now = time.time()
        c = getattr(self, "_inv_cache", None)
        if c and now - c[0] < 1.0:
            return c[1]
        out = None
        try:
            if self._mem_struct():
                pr = self.mem.proc
                blk = pr.read(self.mem.addr[1] + self.INV_OFF, self.INV_SLOTS * 8)
                if blk and len(blk) == self.INV_SLOTS * 8:
                    out = []
                    for i in range(self.INV_SLOTS):
                        p = int.from_bytes(blk[i * 8:i * 8 + 8], "little")
                        if not (0x10000 < p < 0x7FFFFFFFFFFF):
                            continue
                        ob = pr.read(p, 160)
                        if not ob or len(ob) < 160:
                            continue
                        iid = int.from_bytes(ob[50:52], "little", signed=True)
                        cnt = int.from_bytes(ob[112:116], "little", signed=True)
                        if not (0 < iid < 5000) or not (0 <= cnt < 2_000_000_000):
                            continue
                        out.append({"slot": i, "id": iid, "count": cnt,
                                    "dur": int.from_bytes(ob[134:136], "little", signed=True),
                                    "max": int.from_bytes(ob[98:100], "little", signed=True),
                                    "attr": [ob[138], ob[139], ob[140], ob[141]],
                                    "x": int.from_bytes(ob[78:80], "little", signed=True),
                                    "y": int.from_bytes(ob[80:82], "little", signed=True)})
                    if not out:
                        out = None
        except Exception:
            out = None
        self._inv_cache = (now, out)
        return out

    def _gold_mem(self):
        """Oro del pj: el item 90 del inventario. None si no se sabe."""
        inv = self._inv_mem()
        if inv is None:
            e = getattr(self, "_eco", None)  # sin inventario en memoria: el oro leído del bolso (si es reciente)
            return e["last"] if e and time.time() - e.get("t", 0) < 1800 else None
        return sum(i["count"] for i in inv if i["id"] == self.ID_GOLD)

    def _supplies_mem(self):
        """Pociones de vida y carnes que quedan, contadas en la memoria. None si no se sabe."""
        inv = self._inv_mem()
        if inv is None:
            return None
        return {"pot": sum(max(1, i["count"]) for i in inv if i["id"] == self.ID_POT),
                "meat": sum(max(1, i["count"]) for i in inv if i["id"] == self.ID_MEAT),
                "items": len(inv)}

    # ---------- manejar el bolso sabiendo dónde está cada cosa (memoria) ----------
    BAG_OX, BAG_OY, BAG_S = 847, 289, 1.6   # bolso estacionado a la derecha: pantalla(1280) = origen + 1.6 * (x, y) del item

    def _bag_pt(self, x, y):
        sc = self.rect[2] / 1280.0
        ox, oy = getattr(self, "_bag_oxy", None) or (self.BAG_OX, self.BAG_OY)
        return (ox + self.BAG_S * x) * sc, (oy + self.BAG_S * y) * sc

    def _bag_keep(self, o0):
        """Si un arrastre agarró el bolso en vez de un item (se movió la ventana), lo vuelvo a donde estaba."""
        sc = self.rect[2] / 1280.0
        winapi.move(*self.abs_pt(640 * sc, 790 * sc))
        time.sleep(0.2)
        o = self._bag_sync()
        if o and o0 and (abs(o[0] - o0[0]) > 8 or abs(o[1] - o0[1]) > 8):
            gx, gy = o[0] + 150, o[1] - 262  # la tapa del bolso
            self._drag_bg(gx * sc, gy * sc, (gx + o0[0] - o[0]) * sc, (gy + o0[1] - o[1]) * sc)
            time.sleep(0.4)
            self._bag_sync()

    def _bag_sync(self):
        """El bolso se puede mover: lo ubico por el texto "UPGRADE". Devuelve dónde está o None si está cerrado."""
        o = vision.find_bag_origin(self.screen.grab(self.rect))
        if o:
            self._bag_oxy = (o[0] + 5, o[1] - 212)
        return o

    def _bag_ready(self, open_=True):
        """Abre (o cierra) el bolso y lo deja en su lugar de siempre, a la derecha."""
        sc = self.rect[2] / 1280.0
        winapi.move(*self.abs_pt(640 * sc, 790 * sc))
        time.sleep(0.3)
        o = self._bag_sync()
        if bool(o) != bool(open_):
            winapi.click(*self.abs_pt(875 * sc, 918 * sc))
            time.sleep(1.2)
            winapi.move(*self.abs_pt(640 * sc, 790 * sc))
            time.sleep(0.3)
            o = self._bag_sync()
        if not open_:
            return not o
        for _ in range(3):
            if not o or (abs(o[0] - 866) <= 6 and abs(o[1] - 545) <= 6):
                break
            gx, gy = o[0] + 150, o[1] - 262  # la tapa del bolso
            self._drag_bg(gx * sc, gy * sc, (gx + 866 - o[0]) * sc, (gy + 545 - o[1]) * sc)
            time.sleep(0.5)
            o = self._bag_sync()
        return bool(o)

    EQ_OFFS = (192, 196, 200, 204, 208, 212, 216, 220, 224)   # ids de lo puesto (cabeza, pecho, brazos, piernas, ..., arma, escudo, ...)

    def _equipped_ids(self):
        """Ids de los items que tiene puestos (los guarda el juego al lado de la posición del pj). None si no se sabe."""
        try:
            if self._mem_struct():
                b = self.mem.proc.read(self.mem.addr[1] + 192, 36)
                if b and len(b) == 36:
                    ids = [int.from_bytes(b[o - 192:o - 190], "little", signed=True) for o in self.EQ_OFFS]
                    return [i for i in ids if 0 < i < 5000]
        except Exception:
            pass
        return None

    def _inv_item(self, item_id=None, slot=None):
        self._inv_cache = None
        for i in self._inv_mem() or []:
            if (slot is not None and i["slot"] == slot) or (slot is None and i["id"] == item_id):
                return i
        return None

    def _windows(self, bag=True, char=False):
        """Deja las ventanas en un estado conocido: cierra todo con Esc y abre lo pedido."""
        sc = self.rect[2] / 1280.0
        P = lambda x, y: self.abs_pt(x * sc, y * sc)
        self._release()
        for _ in range(3):
            winapi.press("ESC")
            time.sleep(0.25)
        isopen = lambda: sum(len(v) for v in vision.find_bag_items(self.screen.grab(self.rect), self._bag_box())) > 0
        if isopen() != bool(bag):
            winapi.click(*P(875, 918))
            time.sleep(1.0)
        if char:
            winapi.click(*P(816, 918))
            time.sleep(1.0)
        return isopen() == bool(bag)

    def _free_spot(self, taken=()):
        """Un lugar del bolso sin nada cerca (en coordenadas del bolso)."""
        inv = self._inv_mem() or []
        pts = [(i["x"], i["y"]) for i in inv] + list(taken)
        best = None
        for y in (118, 108, 128):
            for x in range(8, 176, 14):
                d = min((abs(x - px) + abs(y - py) for px, py in pts), default=99)
                if best is None or d > best[0]:
                    best = (d, (x, y))
        return best[1] if best else (90, 118)

    def _isolate(self, slot, tries=5):
        """Lleva el item de ese casillero a un lugar libre del bolso. Como las cosas se tapan, puede que agarre
        otra: lo sé porque en la memoria se mueve la que agarré. Insisto hasta mover la que quiero."""
        for _ in range(tries):
            o0 = self._bag_sync()
            if not o0:
                return None  # bolso cerrado
            it = self._inv_item(slot=slot)
            if not it:
                return None
            before = {i["slot"]: (i["x"], i["y"]) for i in self._inv_mem() or []}
            ex, ey = self._free_spot()
            self._drag_bg(*self._bag_pt(it["x"], it["y"]), *self._bag_pt(ex, ey))
            time.sleep(0.5)
            self._cancel_drop()
            self._bag_keep(o0)
            self._inv_cache = None
            moved = [i for i in self._inv_mem() or [] if before.get(i["slot"]) != (i["x"], i["y"])]
            if any(i["slot"] == slot for i in moved):
                return self._inv_item(slot=slot)
            if not moved:
                # no agarré nada: pruebo un poco corrido
                self._drag_bg(*self._bag_pt(it["x"] + 6, it["y"] + 10), *self._bag_pt(ex, ey))
                time.sleep(0.5)
                self._cancel_drop()
                self._bag_keep(o0)
                self._inv_cache = None
                if (self._inv_item(slot=slot) or {}).get("x") != it["x"]:
                    return self._inv_item(slot=slot)
        return None

    def _equip_slot(self, slot):
        """Se pone el item de ese casillero: lo aparta en el bolso y lo arrastra al muñeco de Character."""
        sc = self.rect[2] / 1280.0
        it = self._isolate(slot)
        if not it:
            return False
        self._drag_bg(*self._bag_pt(it["x"], it["y"]), 155 * sc, 345 * sc)
        time.sleep(0.8)
        return True

    def _worn_out(self):
        """True si algo del bolso con durabilidad está por romperse (queda 12% o menos)."""
        inv = self._inv_mem() or []
        return any(i["max"] >= 100 and 0 <= i["dur"] <= i["max"] * 0.12 for i in inv)

    def _items_db(self):
        """Tabla de items del juego: id -> datos (nombre, precio, requisitos, daño/defensa)."""
        d = getattr(self, "_idb", None)
        if d is None:
            d = {i["id"]: i for i in self._load_json("items_abaddon.json").get("items", [])}
            self._idb = d
        return d

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
            idb = self._items_db()
            inv = self._inv_mem()
            if inv and idb:
                owned = [idb[i["id"]]["name"] for i in inv if i["id"] in idb]
            elif cfgmod.server_slug(self.cfg) != "abaddon":
                owned = list(self.cfg.get("owned_items") or [])  # pj de otro server: no heredo el equipo del de Abaddon
            else:
                owned = pj.get("owned", [])
            self._owned = owned
            from . import gear
            # la tabla de objetos es la de Abaddon: en otro server manda el catálogo leído en sus tiendas
            idb_l = list(idb.values()) if cfgmod.server_slug(self.cfg) == "abaddon" else []
            p = brain.plan(self.db, self._load_json("training_path.json"), cat, lvl, self._stats_mem(),
                           self._gold_mem(), owned, reach, cur, idb_l, gear.owned_bonus(inv, idb))
            self.plan = p
            try:
                self._fight_report()
            except Exception:
                pass
            key = "|".join(p["lines"][:-1])
            if key != getattr(self, "_brain_key", None):
                self._brain_key = key
                for ln in p["lines"]:
                    self.log("🧭 " + ln)
        except Exception as ex:
            if not getattr(self, "_brain_err", False):
                self._brain_err = True
                self.log(f"🧭 Error armando el plan: {ex!r}")

    # lugares del server (puertas y vendedores). Se pueden pisar desde la config ("places") para otro server.
    PLACES = {"olympia": {"shop": {"map": "Aresden Farm", "door": [63, 69], "inside": ("shop", "tienda"), "npc_type": 15, "npc": [59, 42]},
                          "smith": {"map": "Aresden Farm", "door": [75, 86], "inside": ("smith", "herrer"), "npc_type": 24, "npc": [49, 33]}}}

    def _place(self, kind):
        p = dict((self.PLACES.get(cfgmod.server_slug(self.cfg)) or {}).get(kind) or {})
        p.update((self.cfg.get("places") or {}).get(kind) or {})
        return p or None

    def _npc_find(self, kind, want):
        """Server nuevo: no sé cuál de los personajes del local es el vendedor. Pruebo con cada uno que esté cerca
        hasta que alguno abra un menú con la palabra buscada, y lo dejo anotado para la próxima."""
        try:
            px, py = self.pos["x"], self.pos["y"]
            cands = [(r[0], r[1], r[2]) for r in self.oly.read(self.mem.proc)
                     if (r[0], r[1]) != (px, py) and max(abs(r[0] - px), abs(r[1] - py)) <= 18 and r[0] > 0]
        except Exception:
            cands = []
        cands.sort(key=lambda c: abs(c[0] - px) + abs(c[1] - py))
        for x, y, typ in cands[:10]:
            menu = self._npc_menu(typ, (x, y), want, tries=2)
            if menu:
                pl = self.cfg.setdefault("places", {}).setdefault(kind, {})
                pl.update({"npc_type": int(typ), "npc": [int(x), int(y)]})
                cfgmod.save(self.cfg)
                self.log(f"📌 Encontré al vendedor ({kind}) en ({x},{y}): lo anoto.")
                return menu
        return None

    def _npc_menu(self, npc_type, fallback, want, tries=10):
        """Camino hasta un vendedor (lo ubico por su tipo en la memoria, o por la baldosa conocida) y le hago click
        hasta que salga su menú con la palabra 'want'. Si sale el menú de otro (ej. el oficial de clanes), lo cancelo.
        Devuelve las palabras del menú o None."""
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        tw, th = self._tile()
        cx, cy = self._char()
        try:  # una ventana abierta (bolso, personaje) se come los clicks: las cierro antes
            ws0 = self._q_words(self.screen.grab(self.rect))
            if any(w[0].strip().startswith("Peso:") for w in ws0):
                winapi.press("F6")
                time.sleep(0.5)
            if any(norm(w[0]).startswith("estad") for w in ws0) and any(norm(w[0]).startswith("misiones") for w in ws0):
                winapi.press("F5")
                time.sleep(0.5)
        except Exception:
            pass
        for _i in range(tries):
            if not (self.alive and self.running):
                return None
            tx, ty = fallback
            try:
                # el 'tipo' que leo de la memoria puede cambiar (es la animación): lo uso solo si está cerca de donde lo espero
                cand = [(r[0], r[1]) for r in self.oly.read(self.mem.proc)
                        if r[2] == npc_type and max(abs(r[0] - fallback[0]), abs(r[1] - fallback[1])) <= 4]
                if cand:
                    tx, ty = min(cand, key=lambda t_: abs(t_[0] - fallback[0]) + abs(t_[1] - fallback[1]))
            except Exception:
                pass
            px, py = self.pos.get("x"), self.pos.get("y")
            if px is None:
                time.sleep(1.0)
                continue
            if max(abs(tx - px), abs(ty - py)) > 3:
                # de lejos el click no abre el menú: primero me acerco a 2 baldosas
                # voy de a tramos de hasta 6 baldosas (un click muy lejos a veces cae fuera de la pantalla útil)
                p0 = (px, py)
                step = max(abs(tx - px), abs(ty - py))
                f = min(1.0, 6.0 / step) if step > 8 else 1.0
                off = [(-2, 1), (2, 1), (0, 2), (-2, -1), (2, -1)][_i % 5] if f == 1.0 else (0, 0)
                ax = px + (tx - px) * f + (off[0] if px <= tx else -off[0])
                ay = py + (ty - py) * f + off[1]
                winapi.left_up()
                winapi.click(*self.abs_pt(cx + (ax - px) * tw, cy + (ay - py) * th))
                t1 = time.time()
                while time.time() - t1 < 6:
                    time.sleep(0.5)
                    px, py = self.pos.get("x"), self.pos.get("y")
                    if max(abs(tx - px), abs(ty - py)) <= 3:
                        break
                time.sleep(0.4)
                px, py = self.pos.get("x"), self.pos.get("y")
                if (px, py) == p0:
                    self.log(f"🔎 El personaje no se movió hacia el vendedor (intento {_i + 1}): pruebo por otro lado.")
                    continue
                if max(abs(tx - px), abs(ty - py)) > 3:
                    continue  # sigo acercándome en la próxima vuelta
            winapi.click(*self.abs_pt(cx + (tx - px) * tw, cy + (ty - py) * th - th * 0.3))
            t0 = time.time()
            while time.time() - t0 < 4.5:
                time.sleep(0.8)
                ws = self._q_words(self.screen.grab(self.rect))
                can = next((w for w in ws if norm(w[0]) == "cancelar"), None)
                if not can:
                    continue
                menu = [w for w in ws if abs(w[1] - can[1]) < 260 and 0 <= can[2] - w[2] < 240]
                if any(norm(w[0]).startswith(want) for w in menu):
                    return menu
                winapi.click(*self.abs_pt(can[1], can[2]))  # menú de otro: lo cierro sin tocar nada
                time.sleep(0.8)
                break
        return None

    def _oly_repair_trip(self, why=""):
        """Herrero (cualquier server con este cliente): puerta → 'Reparar Todo' → 'Sí' → me vuelvo a poner lo que
        haya quedado suelto en el bolso (un arma rota se desequipa sola)."""
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        pl = self._place("smith")
        if not pl:
            return
        inside = lambda: any(k in (self.pos.get("map") or "").lower() for k in pl["inside"])
        try:
            self.log("🔧 Voy al herrero a reparar el equipo" + (f" ({why})" if why else "") + ".")
            t0 = time.time()
            d = pl["door"]
            far = self.pos.get("x") is None or max(abs(self.pos["x"] - d[0]), abs(self.pos["y"] - d[1] - 2)) > 4
            if not inside() and (far or not self._same(self.pos.get("map") or "", pl["map"])):
                # primero hasta el frente de la puerta por el camino normal (el encargo va en línea más directa)
                self.goto = {"map": pl["map"], "x": d[0], "y": d[1] + 2, "t": time.time()}
                while self.goto and time.time() - t0 < 600 and self.alive and self.running:
                    time.sleep(0.5)
            if not inside():
                self.mission = {"map": pl["map"], "goal": list(d), "label": "Herrero (reparar)", "until": time.time() + 300, "enter": True, "hold": True}
                while not inside() and time.time() - t0 < 900 and self.alive and self.running:
                    time.sleep(0.3)
            if not inside():
                self.log("🔧 No llegué al herrero.")
                return
            self._busy_ui = True
            self._release()
            time.sleep(1.5)
            menu = self._npc_menu(pl["npc_type"], pl["npc"], "reparar") if pl.get("npc_type", -1) >= 0 else None
            menu = menu or self._npc_find("smith", "reparar")
            if not menu:
                self.log("🔧 No encontré al herrero.")
                return
            todo = next((w for w in menu if norm(w[0]) == "todo"), None) or next(w for w in menu if norm(w[0]).startswith("reparar"))
            winapi.click(*self.abs_pt(todo[1], todo[2]))
            time.sleep(1.8)
            ws = self._q_words(self.screen.grab(self.rect))
            q = next((w for w in ws if "reparar" in norm(w[0]) and w[2] < self.rect[3] * 0.3), None)
            if not q:  # a veces tarda en aparecer el cartel
                time.sleep(1.5)
                ws = self._q_words(self.screen.grab(self.rect))
                q = next((w for w in ws if "reparar" in norm(w[0]) and w[2] < self.rect[3] * 0.3), None)
            yes = next((w for w in ws if norm(w[0]) in ("sí", "si", "sl", "s1") and q and 0 < w[2] - q[2] < 200 and w[1] < self.rect[2] * 0.36), None)
            if q and not yes:
                # el cartel está pero no leo el botón: 'Sí' queda siempre abajo a la izquierda del texto
                yes = ("Sí", q[1] + 60 * self.rect[2] / 1920.0, q[2] + 130 * self.rect[3] / 1080.0)
            cost = next((int(norm(w[0])) for w in ws if q and norm(w[0]).isdigit() and abs(w[2] - q[2]) < 30), None)
            gold = getattr(self, "_gold", None)
            if yes and (cost is None or gold is None or cost <= gold):
                winapi.click(*self.abs_pt(yes[1], yes[2]))
                time.sleep(1.5)
                self.log(f"🔧 Reparé todo el equipo" + (f" por {cost} de oro." if cost else "."))
            elif yes:
                no = next((w for w in ws if norm(w[0]) == "no" and abs(w[2] - yes[2]) < 15), None)
                if no:
                    winapi.click(*self.abs_pt(no[1], no[2]))
                self.log(f"🔧 Reparar cuesta {cost} y tengo {gold}: no me alcanza.")
                return
            else:
                self.log("🔧 No hay nada para reparar.")
            # solo me vuelvo a poner cosas si algo se había roto (lo roto se desequipa solo); un repuesto guardado no se toca
            lo = int(self.cfg.get("loose_ok", 0))
            if time.time() - getattr(self, "_worn_seen", 0) < 3600 or (getattr(self, "_bag_items", 0) or 0) > lo:
                self._reequip_loose(max_n=max(1, (getattr(self, "_bag_items", 0) or 0) - lo))
            self._worn_seen = 0
        except Exception as e:
            self.log(f"🔧 Error en el herrero: {e!r}")
        finally:
            self.mission = None
            self._busy_ui = False
            self._rep_last = time.time()

    def _reequip_loose(self, max_n=3):
        """Lo que quedó suelto en el bolso y no es poción ni oro (un arma que se desequipó al romperse, o lo recién
        comprado): doble click para ponérmelo. En este cliente, equipar desde el bolso manda al bolso lo que estaba
        puesto, así que hago como mucho 'max_n' cambios y no vuelvo a tocar el mismo lugar."""
        try:
            if not any(w[0].strip().startswith("Peso:") for w in self._q_words(self.screen.grab(self.rect))):
                winapi.press("F6")
                time.sleep(0.9)
            done, n = [], 0
            for _ in range(max_n):
                img = self.screen.grab(self.rect)
                bag = self._oly_bag(names=False) or []
                cand = []
                for o in bag:
                    if o["kind"] != "item" or any(abs(o["x"] - p[0]) < 18 and abs(o["y"] - p[1]) < 18 for p in done):
                        continue
                    p_ = img[max(0, o["y"] - 12):o["y"] + 12, max(0, o["x"] - 10):o["x"] + 10].reshape(-1, 3).astype(int)
                    if ((p_.max(axis=1) - p_.min(axis=1)) > 90).sum() >= 12:
                        continue  # ícono de color fuerte: poción u otro consumible; no lo toco
                    cand.append(o)
                if not cand:
                    break
                o = cand[0]
                done.append((o["x"], o["y"]))
                x, y = self.abs_pt(o["x"], o["y"])
                winapi.click(x, y)
                time.sleep(0.09)
                winapi.click(x, y)
                time.sleep(1.1)
                n += 1
            if n:
                self.log(f"🛡 Me puse {n} objeto/s que estaban sueltos en el bolso.")
            try:
                self._oly_pots()
                self._loose_ok = getattr(self, "_bag_items", 0) or 0  # lo que queda suelto es repuesto: no es señal de rotura
                if int(self.cfg.get("loose_ok", -1)) != self._loose_ok:
                    self.cfg["loose_ok"] = self._loose_ok
                    cfgmod.save(self.cfg)
            except Exception:
                pass
            if any(w[0].strip().startswith("Peso:") for w in self._q_words(self.screen.grab(self.rect))):
                winapi.press("F6")
                time.sleep(0.4)
        except Exception as e:
            self.log(f"🛡 No pude revisar el bolso: {e!r}")

    def _worn_check(self):
        """El juego avisa abajo a la izquierda cuando algo se rompió ('El objeto está desgastado'). Lo miro cada 20 s."""
        now = time.time()
        if now - getattr(self, "_worn_chk", 0) < 20 or self.img is None:
            return False
        self._worn_chk = now
        try:
            H, W = self.img.shape[:2]
            ws = ocr.read_words(self.img, {"x1": 0, "y1": int(H * 0.80), "x2": int(W * 0.55), "y2": int(H * 0.915)}, scale=1.0)
            t = " ".join(w[0].lower() for w in ws)
            if "desgastad" in t or "repáralo" in t or "reparalo" in t:
                self._worn_seen = now
                return True
        except Exception:
            pass
        return False

    def _pot_price(self):
        try:
            return int((self._load_json("tiendas.json").get("shop_prices") or {}).get("Health Potion") or 45)
        except Exception:
            return 45

    def _chill(self):
        return (getattr(self, "_strat", None) or {}).get("mode") == "chill"

    def _strategy(self):
        """Decido como un jugador qué me conviene AHORA: seguir en la zona que más experiencia da (gasta pociones
        y arreglos) o cazar tranquilo bichos fáciles, sin pociones, hasta juntar para volver bien armado.
        Miro lo medido: oro por hora, pociones por hora de la zona, muertes y lo que tengo encima."""
        if cfgmod.server_slug(self.cfg) == "abaddon":
            return
        now = time.time()
        if now - getattr(self, "_strat_t", 0) < 120:
            return
        self._strat_t = now
        st = self.__dict__.setdefault("_strat", {"mode": "exp", "since": now, "why": ""})
        price = self._pot_price()
        kit = int(self.cfg.get("pots_kit", 10)) * price
        gold = getattr(self, "_gold", None)
        pots = (getattr(self, "_left", {}) or {}).get("pot")
        if getattr(self, "_pots_unknown", False):
            pots = None
        e = getattr(self, "_eco", None) or {}
        hrs = (now - e["t0"]) / 3600.0 if e else 0
        gold_h = e["earned"] / hrs if e and hrs > 0.25 else None
        mk = (self.pos.get("map") or "").strip().lower()
        y = (self.world.learn.get("yield", {}) or {}).get(mk) or {}
        pots_h = y.get("pots", 0) / y["sec"] * 3600 if y.get("sec", 0) >= 600 else None
        rep_h = float(self.cfg.get("repair_cost_h", 100))  # arreglos: ~250 cada dos horas y media
        cost_h = (pots_h * price + rep_h) if pots_h is not None else None
        net_h = (gold_h - cost_h) if gold_h is not None and cost_h is not None else None
        d30 = sum(1 for t in (getattr(self, "_sc_d", None) or []) if now - t < 1800)
        st.update(gold_h=None if gold_h is None else int(gold_h), cost_h=None if cost_h is None else int(cost_h),
                  net_h=None if net_h is None else int(net_h), deaths_30m=d30, kit=kit)
        mode, why = st["mode"], None
        if mode == "exp":
            if pots == 0 and gold is not None and gold < 5 * price:
                why = f"sin pociones y con {gold} de oro no llego ni a 5"
            elif d30 >= 3:
                why = f"me mataron {d30} veces en media hora"
            elif net_h is not None and net_h < 0 and gold is not None and gold < kit and pots is not None and pots < 3:
                why = f"esta zona me cuesta ~{int(cost_h)} de oro por hora y junto ~{int(gold_h)}"
            if why:
                st.update(mode="chill", since=now, why=why, g0=gold)
                self._zone_goal = None
                self.log(f"🧠 Plan: {why}. Me conviene cazar tranquilo bichos fáciles (sin gastar pociones ni "
                         f"volver al pueblo a cada rato) hasta juntar {kit} de oro para {kit // price} pociones.")
        else:
            mins = (now - st["since"]) / 60.0
            back = None
            if gold is not None and gold >= kit and mins >= 10:
                back = f"ya junté {gold} de oro: compro pociones y vuelvo a la zona que más experiencia da"
                self._shop_last = 0
            elif mins >= 45 and gold is not None and gold - (st.get("g0") or 0) < price and d30 == 0:
                back = "acá casi no cae oro, así que no gano nada esperando: vuelvo a la mejor zona cuidándome (bichos sueltos y descanso)"
                self._careful_until = now + 1800
            elif mins >= 120:
                back = "ya pasé dos horas tranquilo: pruebo de nuevo la mejor zona"
            if back:
                st.update(mode="exp", since=now, why=back)
                self._zone_goal = None
                self.log("🧠 Plan: " + back[0].upper() + back[1:] + ".")
        st["line"] = self._strat_line(st, gold, pots, price)

    def _strat_line(self, st, gold, pots, price):
        if st["mode"] == "chill":
            miss = None if gold is None else max(0, st["kit"] - gold)
            eta = f" (~{round(miss / st['gold_h'], 1)} h)" if miss and st.get("gold_h") else ""
            return (f"Modo tranquilo: bichos fáciles sin pociones porque {st.get('why')}. "
                    + (f"Me faltan {miss} de oro{eta} para {st['kit'] // price} pociones." if miss is not None else ""))
        if st.get("net_h") is not None:
            sg = "me sobran" if st["net_h"] >= 0 else "pierdo"
            return (f"Modo experiencia: junto ~{st['gold_h']} de oro por hora y gasto ~{st['cost_h']} en pociones y "
                    f"arreglos ({sg} ~{abs(st['net_h'])} por hora).")
        return "Modo experiencia: cazo donde más experiencia da; todavía estoy midiendo cuánto oro deja y cuánto gasta."

    def _pot_budget(self, gold, have):
        """Cuántas pociones compro. Sin pociones no se puede cazar: lo mínimo va primero, aunque toque la reserva.
        El resto depende de si la próxima mejora de equipo está cerca (guardo) o lejos (las pociones rinden ya)."""
        if gold is None:
            return 0
        price = self._pot_price()
        want = max(0, int(self.cfg.get("pots_target", 15)) - have)
        reserve = int(self.cfg.get("gold_reserve", 150))
        sv = (getattr(self, "plan", None) or {}).get("saving") or {}
        gh = (getattr(self, "_strat", None) or {}).get("gold_h")
        miss = max(0, int(sv.get("price") or 0) - gold) if sv.get("price") else None
        near = miss is not None and (miss == 0 or (gh and miss / max(gh, 1) <= 1.0))
        if near:
            n = int((gold - reserve) * float(self.cfg.get("pots_gold_share", 0.34))) // price
        else:
            n = int(gold * 0.7) // price  # la mejora está lejos: el oro rinde más en pociones para cazar mejor
        if have < 3:
            n = max(n, min(5 - have, gold // price))
        return max(0, min(want, n))

    def _selfcheck(self):
        """'¿Estoy bien?' — lo que un jugador mira cada tanto: ¿mato al ritmo normal?, ¿muero seguido?, ¿tengo
        pociones?, ¿se rompió algo? Si algo está mal, eso pasa a ser lo urgente. Vale para cualquier server."""
        if cfgmod.server_slug(self.cfg) == "abaddon":
            return
        now = time.time()
        k = self.stats.get("kills", 0)
        d = self.stats.get("deaths", 0) or 0
        kh = self.__dict__.setdefault("_sc_k", [])
        dh = self.__dict__.setdefault("_sc_d", [])
        if k != getattr(self, "_sc_lk", k):
            kh.append(now)
        if d != getattr(self, "_sc_ld", d):
            dh.append(now)
        self._sc_lk, self._sc_ld = k, d
        del kh[:-400]
        del dh[:-100]
        hunting = self._hunting_now() and not getattr(self, "_busy_ui", False)
        if hunting:
            self._sc_hunt = getattr(self, "_sc_hunt", 0.0) + min(2.0, now - getattr(self, "_sc_t", now))
        self._sc_t = now
        if now - getattr(self, "_sc_last", 0) < 120:
            return
        self._sc_last = now
        k10 = sum(1 for t in kh if now - t < 600)
        d15 = sum(1 for t in dh if now - t < 900)
        pots = (getattr(self, "_left", {}) or {}).get("pot")
        hunted = getattr(self, "_sc_hunt", 0.0)
        issues = []
        # 1) muero seguido: un rato solo bichos sueltos y descansando más
        if d15 >= 2 and now > getattr(self, "_careful_until", 0):
            self._careful_until = now + 1200
            issues.append(f"me mataron {d15} veces en 15 min: por 20 min solo bichos sueltos")
        # 2) no mato nada hace rato cazando: lo primero que sospecho es el arma
        if hunted > 480 and k10 == 0:
            self._sc_hunt = 0.0
            if now - getattr(self, "_rep_last", 0) > 1200 and self._place("smith"):
                self._worn_seen = now
                self._rep_last = now - 400
                issues.append("hace 8 min que cazo y no mato nada: voy al herrero a revisar el arma")
            else:
                self._wd_exp_t = 0
                issues.append("hace 8 min que no mato nada: cambio de zona")
        elif k10 > 0:
            self._sc_hunt = min(hunted, 240.0)
        # 3) sin pociones y con oro: que el viaje a la tienda no espere
        if pots == 0 and (getattr(self, "_gold", None) or 0) >= 200 and now - getattr(self, "_shop_last", 0) > 600:
            self._shop_last = 0
            issues.append("me quedé sin pociones: paso por la tienda")
        st = {"t": int(now), "kills_10m": k10, "deaths_15m": d15, "pots": pots, "issues": issues,
              "careful": now < getattr(self, "_careful_until", 0)}
        self.health = st
        for i in issues:
            self.log("🩺 " + i[0].upper() + i[1:] + ".")
        if now - getattr(self, "_sc_said", 0) > 1800:
            self._sc_said = now
            self.log(f"🩺 Chequeo: {k10} bichos en 10 min, {d15} muerte/s en 15 min, pociones {pots if pots is not None else 'sin contar'}"
                     + (", modo cuidadoso" if st["careful"] else "") + ".")

    def _maybe_repair(self):
        """El equipo se gasta y cuando se rompe se desequipa solo (queda pegando a mano): voy al herrero cada tanto,
        o enseguida si de golpe dejo de matar."""
        if getattr(self, "mission", None) or getattr(self, "_busy_ui", False):
            return
        if cfgmod.server_slug(self.cfg) != "abaddon":
            if not self._place("smith"):
                return
            for a in ("_shop_th", "_rep_th"):
                th = getattr(self, a, None)
                if th is not None and th.is_alive():
                    return
            now = time.time()
            if self.state == "attack" or self._under_attack() or self._fleeing():
                return
            if not hasattr(self, "_rep_last"):
                self._rep_last = now
            worn = self._worn_check() or now - getattr(self, "_worn_seen", 0) < 600 and getattr(self, "_worn_seen", 0) > self._rep_last
            if not hasattr(self, "_loose_ok"):
                self._loose_ok = int(self.cfg.get("loose_ok", 0))
            loose = (getattr(self, "_bag_items", 0) or 0) > self._loose_ok and now - self._rep_last > 1800
            due = now - self._rep_last > float(self.cfg.get("repair_every_min", 150)) * 60
            if not (worn or loose or due) or now - self._rep_last < 300:
                return
            self._rep_last = now
            why = "se rompió algo" if worn else "hay equipo suelto en el bolso" if loose else "mantenimiento"
            self._rep_th = threading.Thread(target=self._oly_repair_trip, args=(why,), daemon=True)
            self._rep_th.start()
            return
        for a in ("_shop_th", "_rep_th", "_inv_th", "_gear_th"):
            th = getattr(self, a, None)
            if th is not None and th.is_alive():
                return
        if not self._same(self.pos.get("map") or "", "Aresden Farm"):
            return
        now = time.time()
        if not hasattr(self, "_rep_last"):
            self._rep_last = now - self.cfg.get("repair_every_min", 180) * 60 + 600  # primera pasada a los 10 min
        stalled = len([t for t in getattr(self, "_nokill", []) if now - t < 240]) >= 5 and now - getattr(self, "_last_kill_t", 0) > 150
        # reparar "por las dudas" cada tanto solo si sobra tiempo de pueblo; lo urgente es el desgaste real (abajo)
        due = now - self._rep_last > self.cfg.get("repair_every_min", 180) * 60 and self._town_budget_left() > 240
        if not due and now - self._rep_last > 300 and now - getattr(self, "_worn_t", 0) > 30:
            self._worn_t = now
            due = self._worn_out()
        if not (due or (stalled and now - self._rep_last > 600)):
            return
        self._rep_last = now
        self._nokill = []
        self._rep_th = threading.Thread(target=self._repair_trip, args=(stalled,), daemon=True)
        self._rep_th.start()

    def _find_keeper(self, cx=745, cy=345):
        """El vendedor no siempre queda en el mismo píxel: lo busco pasando el mouse cerca de donde suele estar
        hasta que aparezca su cartel azul. Devuelve (x, y) en coordenadas de 1280 o None."""
        sc = self.rect[2] / 1280.0
        pts = sorted([(x, y) for y in range(cy - 110, cy + 111, 22) for x in range(cx - 150, cx + 151, 25)],
                     key=lambda p: abs(p[0] - cx) + abs(p[1] - cy))
        for x, y in pts:
            if not (self.alive and self.running):
                return None
            if 590 <= x <= 700 and 370 <= y <= 520:
                continue  # ahí estoy parado yo
            winapi.move(*self.abs_pt(x * sc, y * sc))
            time.sleep(0.10)
            if vision.neutral_pixels(self.screen.grab(self.rect), x * sc, y * sc) >= 25:
                return (x, y)
        return None

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
                self._rep_last = time.time() - self.cfg.get("repair_every_min", 180) * 60 + 600
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
                self._rep_last = time.time() - self.cfg.get("repair_every_min", 180) * 60 + 600
                return
            dx, dy = (self.pos.get("x") - 47) * 64, (self.pos.get("y") - 35) * 64
            kp = self._find_keeper(745 - dx, 345 - dy)
            if not kp:
                self.log("🔧 No encontré al herrero. Pruebo más tarde.")
                self._rep_last = time.time() - self.cfg.get("repair_every_min", 180) * 60 + 600
                return
            before = (self.pos.get("x"), self.pos.get("y"))
            worn0 = sum(i["max"] - i["dur"] for i in (self._inv_mem() or []) if i["max"] >= 100)
            winapi.click(*P(*kp))
            time.sleep(2.5)
            if (self.pos.get("x"), self.pos.get("y")) != before:  # caminé en vez de hablarle: lo busco de nuevo
                kp = self._find_keeper(kp[0], kp[1])
                if not kp:
                    self.log("🔧 No pude hablarle al herrero. Pruebo más tarde.")
                    self._rep_last = time.time() - self.cfg.get("repair_every_min", 180) * 60 + 600
                    return
                winapi.click(*P(*kp))
                time.sleep(2.5)
            winapi.click(*P(kp[0] + 100, kp[1] - 33))  # Repair All
            time.sleep(2.0)
            winapi.click(*P(902, 574))  # Repair
            time.sleep(1.5)
            winapi.press("ESC")
            time.sleep(0.5)
            self._inv_cache = None
            worn1 = sum(i["max"] - i["dur"] for i in (self._inv_mem() or []) if i["max"] >= 100)
            if worn0 and worn1 >= worn0:
                self.log("🔧 Fui al herrero pero el equipo sigue gastado: no salió la reparación. Pruebo más tarde.")
                self._rep_last = time.time() - self.cfg.get("repair_every_min", 180) * 60 + 600
            else:
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
            self._equip_block = 0
            self._gear_chk = 0  # la revisión de equipo se pone lo que se haya salido

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

    # ------------------------------------------------------------ equipo
    SHOP_DOORS = {"shop": {"Aresden Farm": [63, 70], "Aresden City": [127, 167]},
                  "smith": {"Aresden Farm": [75, 86], "Aresden City": [169, 194]}}

    def _wheel(self, x, y, n):
        """Rueda del mouse sobre el juego (n > 0 sube). Ojo: el juego a veces pierde muescas, no sirve para contar."""
        import ctypes
        sc = self.rect[2] / 1280.0
        ax, ay = self.abs_pt(x * sc, y * sc)
        winapi.move(ax, ay)
        time.sleep(0.15)
        for _ in range(abs(n)):
            dl = -120 if n < 0 else 120
            ctypes.windll.user32.PostMessageW(self.hwnd, 0x020A, (dl & 0xFFFF) << 16, ((int(ay) & 0xFFFF) << 16) | (int(ax) & 0xFFFF))
            time.sleep(0.12)

    def _gear_state(self):
        """Qué ponerme / vender / guardar según lo que hay en el bolso, y qué comprar según el plan."""
        from . import gear
        self._inv_cache = None
        inv, eq, idb = self._inv_mem(), self._equipped_ids(), self._items_db()
        if not inv or eq is None or not idb:
            return None
        cat = self._load_json("tiendas.json")
        lvl = self._level_mem() or 1
        d = gear.decide(inv, eq, idb, cat, lvl, self._stats_mem(),
                        self.cfg.get("keep_items") or [], self.cfg.get("junk_items") or [], int(self.cfg.get("max_rare") or 0))
        buys = []
        for it in (getattr(self, "plan", None) or {}).get("buy_now", []):
            kind = {"Blacksmith": "smith", "Shop": "shop"}.get(it.get("shop"))
            if it.get("name") in ("Health Potion", "Meat") or not kind:
                continue
            if vision.row_template(it["name"]) is None:
                if it["name"] not in getattr(self, "_no_tpl", set()):
                    self._no_tpl = getattr(self, "_no_tpl", set()) | {it["name"]}
                    self.log(f"🛡 Quiero comprar {it['name']} pero no conozco su fila en la lista de la tienda.")
                continue
            buys.append({"name": it["name"], "price": it.get("price", 0), "kind": kind, "id": it.get("id")})
        d["buy"] = buys
        d["free"] = self.INV_SLOTS - len(inv)
        # la comida de más ocupa el lugar de las pociones: me quedo con unas pocas y vendo el resto
        meats = [i for i in inv if i["id"] == self.ID_MEAT]
        keep_m = int(self.cfg.get("meat_keep", 6))
        if len(meats) > keep_m + 3:
            d["sell"] += [{"slot": i["slot"], "id": i["id"], "name": "Meat", "why": "comida de más", "kind": "shop"} for i in meats[keep_m:]]
        return d

    # ------------------------------------------------------------ comprar equipo (clientes tipo Olympia)
    def _shop_rows(self, img, tab):
        """Filas visibles de la lista de una tienda: [(nombre, precio, x, y)]."""
        ws = ocr.read_words(img, {"x1": int(tab[1] - 75), "y1": int(tab[2] + 22), "x2": int(tab[1] + 470), "y2": int(tab[2] + 445)}, scale=1.0)
        by = {}
        for w in ws:
            by.setdefault(round(w[2] / 9), []).append(w)
        out = []
        for k in sorted(by):
            r = sorted(by[k], key=lambda v: v[1])
            pr = [v for v in r if re.fullmatch(r"\d{1,3}(\.\d{3})*", v[0].strip()) and v[1] > tab[1] + 330]
            nm = [v for v in r if v[1] < tab[1] + 330]
            if pr and nm:
                out.append((" ".join(v[0] for v in nm), int(pr[-1][0].replace(".", "")), nm[0][1], r[0][2]))
        return out

    def _wheel(self, x, y, delta):
        sx, sy = self.abs_pt(x, y)
        winapi.move(sx, sy)
        time.sleep(0.05)
        try:
            winapi.user32.PostMessageW(winapi.BG_HWND, 0x020A, (delta & 0xFFFF) << 16, ((int(sy) & 0xFFFF) << 16) | (int(sx) & 0xFFFF))
        except Exception:
            pass

    def _oly_buy_trip(self, items):
        """Compra equipo en el herrero: Comerciar → pestaña → busco la fila por nombre y precio → la selecciono,
        confirmo en el recuadro de abajo que es ese objeto → Comprar → me lo pongo. items: entradas del catálogo."""
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        pl = self._place("smith")
        if not pl or not items:
            return
        inside = lambda: any(k in (self.pos.get("map") or "").lower() for k in pl["inside"])
        bought = []
        try:
            self.log("🛍 Voy al herrero a comprar: " + ", ".join(f"{i['name']} ({i['price']})" for i in items) + ".")
            t0 = time.time()
            d = pl["door"]
            far = self.pos.get("x") is None or max(abs(self.pos["x"] - d[0]), abs(self.pos["y"] - d[1] - 2)) > 4
            if not inside() and (far or not self._same(self.pos.get("map") or "", pl["map"])):
                # primero hasta el frente de la puerta por el camino normal (el encargo va en línea más directa)
                self.goto = {"map": pl["map"], "x": d[0], "y": d[1] + 2, "t": time.time()}
                while self.goto and time.time() - t0 < 600 and self.alive and self.running:
                    time.sleep(0.5)
            if not inside():
                self.mission = {"map": pl["map"], "goal": list(d), "label": "Herrero (comprar)", "until": time.time() + 300, "enter": True, "hold": True}
                while not inside() and time.time() - t0 < 900 and self.alive and self.running:
                    time.sleep(0.3)
            if not inside():
                self.log("🛍 No llegué al herrero.")
                return
            self._busy_ui = True
            self._busy_t0 = None
            self._release()
            time.sleep(1.5)
            menu = self._npc_menu(pl["npc_type"], pl["npc"], "comerciar") if pl.get("npc_type", -1) >= 0 else None
            menu = menu or self._npc_find("smith", "comerciar")
            com = next((w for w in (menu or []) if norm(w[0]).startswith("comerciar")), None)
            if not com:
                self.log("🛍 No pude abrir la tienda del herrero.")
                return
            winapi.click(*self.abs_pt(com[1], com[2]))
            time.sleep(2.0)
            ws = self._q_words(self.screen.grab(self.rect))
            tabs = {norm(w[0]): w for w in ws if norm(w[0]) in ("armas", "armadura", "varios") and w[2] < self.rect[3] * 0.25}
            t_ref = tabs.get("armas")
            if not t_ref:
                self.log("🛍 No veo las pestañas de la tienda.")
                return
            kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
            for it in items:
                self._busy_t0 = None
                tname = it.get("tab") or ("armas" if it.get("slot") in ("arma", "arma2m") else "armadura")
                tb = tabs.get(tname)
                if not tb:  # las pestañas en letra gótica no siempre se leen: están a distancia fija de la primera
                    off = {"armas": 0, "armadura": 148, "varios": 298}.get(tname)
                    if off is None:
                        continue
                    tb = (tname, t_ref[1] + off * kx, t_ref[2])
                winapi.click(*self.abs_pt(tb[1], tb[2]))
                time.sleep(0.9)
                for _ in range(25):
                    self._wheel(t_ref[1] + 150, t_ref[2] + 250, 120)
                    time.sleep(0.05)
                time.sleep(0.4)
                want = norm(it["name"])
                row = None
                for _pg in range(40):
                    winapi.move(*self.abs_pt(t_ref[1] + 700, t_ref[2] + 500))
                    time.sleep(0.3)
                    rows = self._shop_rows(self.screen.grab(self.rect), t_ref)
                    # el OCR a veces se come una palabra ('Superior'): exijo nombre igual y precio igual
                    sup = "superior" in want

                    def _same_item(nm):
                        n_ = norm(nm)
                        if ("superior" in n_) != sup:
                            return False
                        return n_ == want or difflib.SequenceMatcher(None, n_, want).ratio() >= 0.86

                    row = next((r for r in rows if r[1] == int(it["price"]) and _same_item(r[0])), None)
                    if row:
                        break
                    self._wheel(t_ref[1] + 150, t_ref[2] + 250, -120)
                    time.sleep(0.35)
                if not row:
                    self.log(f"🛍 No encontré '{it['name']}' a {it['price']} en la lista.")
                    continue
                winapi.click(*self.abs_pt(row[2] + 20, row[3]))
                time.sleep(0.9)
                winapi.move(*self.abs_pt(t_ref[1] + 700, t_ref[2] + 500))
                time.sleep(0.4)
                img = self.screen.grab(self.rect)
                # recuadro de abajo: nombre del objeto elegido y su precio 'N c/u'
                box = ocr.read_words(img, {"x1": int(t_ref[1] - 75), "y1": int(t_ref[2] + 440), "x2": int(t_ref[1] + 470), "y2": int(t_ref[2] + 590)}, scale=1.0)
                txt = norm(" ".join(w[0] for w in sorted(box, key=lambda w: (round(w[2] / 9), w[1]))))
                toks = [norm(t) for t in re.split(r"[\s()]+", it["name"]) if len(norm(t)) >= 2]
                price_ok = str(int(it["price"])) in txt or str(int(it["price"])) in txt.replace("o", "0")
                if not (all(t in txt for t in toks) and price_ok):
                    self.log(f"🛍 Seleccioné una fila pero el recuadro no dice '{it['name']}' a {it['price']}: no compro (leí: {txt[:50]}).")
                    continue
                cp = next((w for w in self._q_words(img) if norm(w[0]).startswith("comprar") and w[2] > t_ref[2] + 560 * ky), None)
                cpt = (cp[1], cp[2]) if cp else (t_ref[1] + 389 * kx, t_ref[2] + 640 * ky)
                winapi.click(*self.abs_pt(*cpt))
                time.sleep(1.6)
                msg = norm(" ".join(w[0] for w in ocr.read_words(self.screen.grab(self.rect), {"x1": 0, "y1": int(self.rect[3] * 0.80), "x2": int(self.rect[2] * 0.6), "y2": int(self.rect[3] * 0.915)}, scale=1.0)))
                if "compraste" in msg and (want[:8] in msg):
                    bought.append(it)
                    self.log(f"🛍 Compré {it['name']} por {it['price']} de oro.")
                else:
                    self.log(f"🛍 Apreté Comprar para {it['name']} pero no vi la confirmación (leí: {msg[-60:]}).")
            # cierro la tienda y me pongo lo nuevo
            winapi.click(*self.abs_pt(t_ref[1] + 200 * kx, t_ref[2] + 317 * ky), button="right")
            time.sleep(0.7)
            if bought:
                self._reequip_loose(max_n=len(bought))
                own = [o for o in (self.cfg.get("owned_items") or [])]
                cat = {i["name"]: i for i in (self._load_json("tiendas.json").get("items") or [])}
                for it in bought:
                    own = [o for o in own if cat.get(o, {}).get("slot") != it.get("slot")] + [it["name"]]
                self.cfg["owned_items"] = own
                cfgmod.save(self.cfg)
                self._brain_t = 0
                self._pots_t = 0
        except Exception as e:
            self.log(f"🛍 Error comprando equipo: {e!r}")
        finally:
            self.mission = None
            self._busy_ui = False
            self._gear_last = time.time()

    def _maybe_gear(self):
        """Cada tanto reviso el bolso: me pongo lo mejor, vendo lo que no sirve y compro lo que dice el plan."""
        if getattr(self, "mission", None) or getattr(self, "_busy_ui", False):
            return
        if cfgmod.server_slug(self.cfg) != "abaddon":
            # el plan (brain.plan) ya dice qué mejora puedo usar y pagar con el oro que sobra de la reserva
            now = time.time()
            for a in ("_shop_th", "_rep_th", "_gear_th"):
                th = getattr(self, a, None)
                if th is not None and th.is_alive():
                    return
            if (now - getattr(self, "_gear_chk", 0) < 30 or self.state == "attack" or self._under_attack()
                    or now - getattr(self, "_gear_last", 0) < float(self.cfg.get("gear_trip_every_min", 30)) * 60):
                return
            self._gear_chk = now
            if not self.cfg.get("buy_gear_auto", True) or not self._place("smith"):
                return
            buy = [i for i in ((getattr(self, "plan", None) or {}).get("buy_now") or []) if i.get("shop") == "Blacksmith" and i.get("confirmed")]
            g_, p_ = getattr(self, "_gold", None), (getattr(self, "_left", {}) or {}).get("pot") or 0
            if g_ is not None and p_ < 5:  # un jugador no se compra el arma y se queda sin plata para pociones
                keep, ok_ = (5 - p_) * self._pot_price(), []
                for i in buy:
                    if g_ - int(i.get("price") or 0) >= keep:
                        ok_.append(i)
                        g_ -= int(i.get("price") or 0)
                buy = ok_
            if not buy:
                return
            self._gear_last = now
            self._gear_th = threading.Thread(target=self._oly_buy_trip, args=(buy[:3],), daemon=True)
            self._gear_th.start()
            return
        for a in ("_shop_th", "_rep_th", "_inv_th", "_gear_th"):
            th = getattr(self, a, None)
            if th is not None and th.is_alive():
                return
        now = time.time()
        if now - getattr(self, "_gear_chk", 0) < 20:
            return
        self._gear_chk = now
        cur = self.pos.get("map") or ""
        town = next((n for n in ("Aresden Farm", "Aresden City") if self._same(cur, n)), None)
        if not town or self._attacked_recently():
            return
        d = self._gear_state()
        if not d:
            return
        if now < getattr(self, "_equip_block", 0):
            d["equip"] = []
        trip_ok = now - getattr(self, "_gear_last", 0) > self.cfg.get("gear_trip_every_min", 20) * 60
        sell_now = False  # vender va siempre junto con la recarga de pociones (ver _maybe_shop)
        # vender y comprar mejoras no es urgente: solo si no me pasé del 10% de tiempo en el pueblo
        # (urgente = bolso casi lleno, o poco lugar y pocas pociones: hay que vender para poder comprarlas)
        sm_ = self._supplies_mem() or {}
        urgent = d["sell"] and (d["free"] < 3 or (d["free"] < 12 and sm_.get("pot", 99) < int(self.cfg.get("pots_to_leave_town", 10))))
        if trip_ok and not urgent and self._town_budget_left() < 150:
            trip_ok = False
        if not d["equip"] and not (trip_ok and d["buy"]):
            return
        if not (trip_ok and d["buy"]):
            d = dict(d, sell=[])
        if not d["equip"]:
            self._gear_last = now
        self._gear_th = threading.Thread(target=self._gear_trip, args=(d, town, bool(trip_ok and (sell_now or d["buy"]))), daemon=True)
        self._gear_th.start()

    def _equip_items(self, entries):
        """Se pone items del bolso con doble click (afuera de las tiendas) y verifica en la memoria que quedaron puestos."""
        done = []
        if not entries:
            return done
        cur = self.pos.get("map") or ""
        if not any(self._same(cur, n) for n in ("Aresden Farm", "Aresden City")):
            return done  # adentro de las tiendas el doble click abre "reparar"
        self._busy_ui = True
        try:
            self._release()
            time.sleep(1.0)
            if not self._bag_ready(True):
                self.log("🛡 No pude abrir el bolso para equiparme.")
                return done
            for e in entries:
                for _ in range(2):
                    if not (self.alive and self.running):
                        return done
                    iso = self._isolate(e["slot"])
                    if not iso:
                        break
                    x, y = self._bag_pt(iso["x"] + 4, iso["y"] + 8)
                    winapi.click(*self.abs_pt(x, y), double=True)
                    time.sleep(1.8)
                    if e["id"] in (self._equipped_ids() or []):
                        done.append(e)
                        self.log(f"🛡 Me puse {e['name']} ({e.get('why', 'nuevo')}).")
                        break
                else:
                    self.log(f"🛡 No pude ponerme {e['name']}.")
            self._bag_ready(False)
        except Exception as ex:
            self.log(f"🛡 Error equipando: {ex!r}")
        finally:
            self._busy_ui = False
        return done

    def _enter_store(self, kind, town):
        """Va hasta la tienda y entra (en el herrero se para frente al mostrador). True si entró; deja _busy_ui prendido."""
        sc = self.rect[2] / 1280.0
        P = lambda x, y: self.abs_pt(x * sc, y * sc)
        self.mission_result = None
        self.mission = {"map": town, "goal": self.SHOP_DOORS[kind][town], "label": ("Blacksmith" if kind == "smith" else "Shop") + " (equipo)",
                        "until": time.time() + 480}
        t0 = time.time()
        while time.time() - t0 < 480 and self.alive and self.running:
            if kind in (self.pos.get("map") or "").lower():
                break
            time.sleep(0.1)
        else:
            return False
        self._busy_ui = True
        self._release()
        time.sleep(1.5)
        if kind == "smith":
            # el herrero tiene dos puertas: camino hasta el mostrador (47,35) con el mapa de adentro
            t1 = time.time()
            while time.time() - t1 < 20 and self.alive and self.running:
                x_, y_ = self.pos.get("x") or 0, self.pos.get("y") or 0
                if abs(x_ - 47) <= 1 and abs(y_ - 35) <= 1:
                    break
                mp = self.world.map_for(self.pos.get("map") or "")
                if mp:
                    self._walk_to(mp, (47, 35), "mostrador del herrero")
                else:
                    winapi.click(*P(1000, 470))
                time.sleep(0.7)
            self._release()
            time.sleep(1.0)
        return True

    def _talk_keeper(self, kind):
        sc = self.rect[2] / 1280.0
        P = lambda x, y: self.abs_pt(x * sc, y * sc)

        def roam():
            pts = [(x, y) for y in range(420, 720, 48) for x in range(700, 1250, 40)]
            pts += [(x, y) for y in range(120, 800, 56) for x in range(60, 1240, 44) if not (x >= 700 and 420 <= y < 720)]
            for x, y in pts:
                if not (self.alive and self.running):
                    return None
                winapi.move(*P(x, y))
                time.sleep(0.11)
                if vision.neutral_pixels(self.screen.grab(self.rect), x * sc, y * sc) >= 25:
                    return (x, y)
            return None

        for _ in range(3):
            if not (self.alive and self.running):
                return None
            if kind == "smith":
                kp = self._find_keeper(745 - ((self.pos.get("x") or 47) - 47) * 64, 345 - ((self.pos.get("y") or 35) - 35) * 64)
            else:
                kp = roam()
            if not kp:
                continue
            winapi.click(*P(*kp))
            time.sleep(2.5)
            m = vision.find_npc_menu(self.screen.grab(self.rect))
            if m:
                return {k: (v[0] / sc, v[1] / sc) for k, v in m.items() if k != "score"}
        return None

    def _cancel_drop(self):
        """Si quedó abierto el cartel "¿tirar este item al piso?" (un arrastre que cayó afuera), aprieto No."""
        try:
            img = self.screen.grab(self.rect)
            ws = ocr.read_words(img, {"x1": 0, "y1": 0, "x2": img.shape[1], "y2": img.shape[0]}, scale=1.0)
            if not any("drop" in w[0].lower() for w in ws):
                return False
            no = next((w for w in ws if w[0].strip().lower() == "no"), None)
            if no:
                winapi.click(*self.abs_pt(no[1], no[2]))
            else:
                winapi.press("ESC")
            time.sleep(0.6)
            self.log("💰 Se había abierto el cartel de tirar un item: apreté No (no tiré nada).")
            return True
        except Exception:
            return False

    def _sell_items(self, entries, kind):
        """Con el vendedor adelante: abre Sell, arrastra cada cosa al cartel y confirma. Devuelve cuántas vendió."""
        sc = self.rect[2] / 1280.0
        P = lambda x, y: self.abs_pt(x * sc, y * sc)
        entries = entries[:12]
        self._cancel_drop()
        if not entries:
            return 0
        # primero hablo con el vendedor con el bolso CERRADO (el bolso tapa la zona donde lo busco), abro "Sell"
        # y recién ahí abro el bolso
        self._bag_ready(False)
        m = self._talk_keeper(kind)
        if not m:
            self.log("💰 No pude hablarle al vendedor para vender.")
            return 0
        winapi.click(*P(*m["sell"]))
        time.sleep(2.0)
        if not self._bag_ready(True):
            self.log("💰 No pude abrir el bolso para vender.")
            winapi.press("ESC")
            return 0

        def sell_btn():
            winapi.move(*P(640, 800))
            time.sleep(0.3)
            b = vision.find_sell_button(self.screen.grab(self.rect))
            return (b[0] / sc, b[1] / sc) if b else None

        btn = sell_btn()
        if not btn:
            self.log("💰 No se abrió el cartel de venta. Pruebo en otra vuelta.")
            return 0
        self._inv_cache = None
        n0, g0 = len(self._inv_mem() or []), self._gold_mem() or 0
        put = 0
        for e in entries:
            if not (self.alive and self.running):
                break
            iso = self._isolate(e["slot"])
            if self._cancel_drop():
                iso = None
            if not iso:
                continue
            btn = sell_btn()
            if not btn:  # sin el cartel abierto no arrastro nada: caería al piso
                break
            self._drag_bg(*self._bag_pt(iso["x"], iso["y"]), (btn[0] + 102) * sc, (btn[1] - 114) * sc)
            time.sleep(0.8)
            put += 1
        self._cancel_drop()
        btn = sell_btn()
        if not btn or not put:
            winapi.press("ESC")
            return 0
        winapi.click(*P(*btn))
        time.sleep(2.0)
        self._inv_cache = None
        n1, g1 = len(self._inv_mem() or []), self._gold_mem() or 0
        sold = max(0, n0 - n1)
        self._sold_trip = getattr(self, "_sold_trip", 0) + sold
        if sold:
            self.log(f"💰 Vendí {sold} cosa(s) que no uso por {g1 - g0} de oro: " + ", ".join(e["name"] for e in entries[:put]) + ".")
        else:
            self.log("💰 Puse cosas a la venta pero no se vendieron.")
            winapi.press("ESC")
        return sold

    def _open_trade(self, kind, menu=None):
        """Abre la lista de venta y la acomoda en su lugar (PRICE en 435,159). True si quedó lista."""
        sc = self.rect[2] / 1280.0
        P = lambda x, y: self.abs_pt(x * sc, y * sc)
        m = menu or self._talk_keeper(kind)
        if not m:
            return False
        winapi.click(*P(*m["trade"]))
        time.sleep(2.0)
        pw = None
        for _ in range(3):
            winapi.move(*P(700, 800))
            time.sleep(0.3)
            h = vision.find_shop_header(self.screen.grab(self.rect))
            if not h:
                time.sleep(0.5)
                continue
            pw = (h[0] / sc, h[1] / sc)
            if abs(pw[0] - 435) <= 8 and abs(pw[1] - 159) <= 8:
                return True
            gx, gy = pw[0] - 92, pw[1]
            self._drag_bg(gx * sc, gy * sc, (gx + 435 - pw[0]) * sc, (gy + 159 - pw[1]) * sc)
        return bool(pw) and abs(pw[0] - 435) <= 12 and abs(pw[1] - 159) <= 12

    def _buy_item(self, b):
        """Con la lista de venta abierta: busca la fila por su foto, compra y verifica en la memoria qué entró."""
        sc = self.rect[2] / 1280.0
        P = lambda x, y: self.abs_pt(x * sc, y * sc)
        idb = self._items_db()
        self._inv_cache = None
        inv0 = self._inv_mem() or []
        if len(inv0) >= self.INV_SLOTS:
            self.log(f"🛡 No compro {b['name']}: el bolso está lleno.")
            return False
        g0 = self._gold_mem() or 0
        if g0 < b.get("price", 0):
            return False
        self._wheel(300, 300, 90)
        time.sleep(0.6)
        y = None
        for _ in range(45):
            if not (self.alive and self.running):
                return False
            winapi.move(*P(700, 800))
            time.sleep(0.35)
            y = vision.find_row(self.screen.grab(self.rect), b["name"])
            if y is not None:
                break
            self._wheel(300, 300, -2)
            time.sleep(0.45)
        if y is None:
            self.log(f"🛡 No encontré {b['name']} en la lista de la tienda.")
            return False
        y /= sc
        winapi.move(*P(272, y))
        time.sleep(0.2)
        winapi.click(*P(272, y))
        time.sleep(1.3)
        winapi.move(*P(220, 561))
        time.sleep(0.2)
        winapi.click(*P(220, 561))
        time.sleep(1.8)
        self._inv_cache = None
        ids0 = sorted(i["id"] for i in inv0)
        new = sorted(i["id"] for i in self._inv_mem() or [])
        for i in ids0:
            if i in new:
                new.remove(i)
        g1 = self._gold_mem() or 0
        names = [idb.get(i, {}).get("name", str(i)) for i in new]
        if b["name"] in names:
            self.log(f"🛡 Compré {b['name']} por {g0 - g1} de oro (me quedan {g1}).")
            return True
        if names:
            self.log(f"🛡 Quise comprar {b['name']} y entró {', '.join(names)} ({g0 - g1} de oro). Lo vendo en la próxima vuelta.")
        else:
            winapi.click(*P(417, 561))  # Cancel, por si quedó el cartel abierto
            time.sleep(0.6)
            self.log(f"🛡 No pude comprar {b['name']}.")
        return False

    def _leave_store(self, kind):
        try:
            if self._busy_ui:
                self._bag_ready(False)
            self._park_shop_dialog()
        except Exception:
            pass
        self._busy_ui = False
        t0 = time.time()
        while time.time() - t0 < 90 and self.alive and self.running and kind in (self.pos.get("map") or "").lower():
            time.sleep(0.3)
        time.sleep(2.5)

    def _gear_trip(self, d, town, trip):
        """Me pongo lo mejor que tengo; si hace falta voy a las tiendas a vender lo que sobra y comprar lo del plan."""
        try:
            if d["equip"]:
                if len(self._equip_items(d["equip"])) < len(d["equip"]):
                    self._equip_block = time.time() + 600  # no salió: no insisto enseguida
            if not trip:
                return
            d = self._gear_state() or d
            if not d["buy"] and not d["equip"]:
                d = dict(d, buy=[], equip=[])  # vuelta de venta: no compro mejoras acá
            smith_buys = [b for b in d["buy"] if b["kind"] == "smith"]
            shop_buys = [b for b in d["buy"] if b["kind"] == "shop"]
            smith_sell = [e for e in d["sell"] if e.get("kind", "smith") == "smith"]
            shop_sell = [e for e in d["sell"] if e.get("kind") == "shop"]
            d = dict(d, sell=smith_sell)
            bought = 0
            if d["sell"] or smith_buys:
                self.log("🛡 Voy al herrero: " + ", ".join(([f"vendo {len(d['sell'])} cosa(s)"] if d["sell"] else []) +
                                                          [f"compro {b['name']}" for b in smith_buys]) + ".")
                inside = self._enter_store("smith", town)
                try:
                    if not inside:
                        self.log("🛡 No llegué al herrero.")
                    else:
                        if d["sell"]:
                            if self._sell_items(d["sell"], "smith"):
                                self._shop_last = 0  # hay lugar nuevo: que compre pociones enseguida si faltan
                        if smith_buys and self._open_trade("smith"):
                            for b in smith_buys:
                                bought += bool(self._buy_item(b))
                finally:
                    self._leave_store("smith")
            if (shop_buys or shop_sell) and self.alive and self.running:
                cur = self.pos.get("map") or ""
                town = next((n for n in ("Aresden Farm", "Aresden City") if self._same(cur, n)), None)
                if town:
                    self.log("🛡 Voy a la Shop: " + ", ".join(([f"vendo {len(shop_sell)} cosa(s)"] if shop_sell else []) +
                                                            [f"compro {b['name']}" for b in shop_buys]) + ".")
                    inside = self._enter_store("shop", town)
                    try:
                        if not inside:
                            self.log("🛡 No llegué a la Shop.")
                        else:
                            if shop_sell:
                                # el bolso cambió al vender en el herrero: vuelvo a leer dónde está cada cosa
                                d3 = self._gear_state() or {}
                                shop_sell = [e for e in d3.get("sell", []) if e.get("kind") == "shop"] or shop_sell
                                self._sell_items(shop_sell, "shop")
                            if shop_buys and self._open_trade("shop"):
                                for b in shop_buys:
                                    bought += bool(self._buy_item(b))
                    finally:
                        self._leave_store("shop")
            if bought and self.alive and self.running:
                d2 = self._gear_state()
                if d2 and d2["equip"]:
                    self._equip_items(d2["equip"])
            self._brain_t = 0
        except Exception as ex:
            self.log(f"🛡 Error en la vuelta de equipo: {ex!r}")
        finally:
            self._busy_ui = False
            if getattr(self, "_restock_now", False):
                self._shop_last = 0  # vendí para la recarga: ahora compro pociones
                if not getattr(self, "_sold_trip", 0):
                    # no pude vender nada: no insisto en cada vuelta (así no hago 200 viajes); pruebo en 20 min
                    self._sell_fail_until = time.time() + 1200
                    self.log("💰 Esta vez no pude vender: compro las pociones igual y vuelvo a probar en 20 minutos.")

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
            sm = self._supplies_mem()
            if sm:
                free = self.INV_SLOTS - sm["items"] - int(self.cfg.get("bag_free_slots", 2))
                meat_n = 1 if sm["meat"] <= int(self.cfg.get("buy_meat_below", 1)) + 2 else 0
                pot_n = max(0, min(2, (int(self.cfg.get("pots_target", 20)) - sm["pot"] + 5) // 10))
                while (meat_n + pot_n) * 10 > max(0, free) and (meat_n + pot_n) > 0:
                    if pot_n > 1 or meat_n == 0:
                        pot_n -= 1
                    else:
                        meat_n -= 1
                # si no entra una decena, compro pociones de a una hasta llenar el lugar que haya
                pot_1 = 0
                if pot_n == 0 and sm["pot"] < int(self.cfg.get("pots_target", 20)):
                    pot_1 = max(0, min(9, free - meat_n * 10, int(self.cfg.get("pots_target", 20)) - sm["pot"]))
                if meat_n + pot_n + pot_1 == 0:
                    self.log("🛒 No compro provisiones: no hay lugar en el bolso.")
                    self._shop_last = time.time()
                    return
            else:
                pot_1 = 0
            if meat_n:
                steps += [(c.get("meat_row", [272, 421]), 1.2)] + [([361, 431], 0.35)] * meat_n + [([220, 561], 1.4)]
            if pot_n:
                steps += [(c.get("pot_row", [272, 191]), 1.2)] + [([361, 431], 0.35)] * pot_n + [([220, 561], 1.4)]
            for _ in range(pot_1):
                steps += [(c.get("pot_row", [272, 191]), 1.2), ([220, 561], 1.4)]
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
            if ms.get("hold"):
                # entré a un local para hacer algo con ventanas: me quedo quieto ya (si no, el paso siguiente me saca)
                self._busy_ui = True
                self._busy_t0 = None
            if ms.get("enter"):
                # el pj sigue caminando hacia el último click después de cruzar la puerta (y vuelve a salir):
                # lo freno haciendo click donde está parado
                try:
                    cx_, cy_ = self._char()
                    tw_, th_ = self._tile()
                    winapi.click(*self.abs_pt(cx_ + tw_, cy_ + th_))
                except Exception:
                    pass
            self.mission_result = {"map": cur, "x": self.pos.get("x"), "y": self.pos.get("y"), "label": ms.get("label")}
            self.mission = None
            return bool(ms.get("hold"))  # con 'hold' corto la vuelta acá: si sigo, el viaje automático me saca del local
        if self.state != "seek":
            self._release()
            self.state, self.target = "seek", None
        mp = self.world.map_for(cur)
        if not mp or self.pos.get("x") is None or time.time() - self.pos.get("t", 0) > 5:
            return True
        g = tuple(ms["goal"])
        if ms.get("enter") and max(abs(g[0] - self.pos["x"]), abs(g[1] - self.pos["y"])) <= 2:
            # es una puerta: llegar al lado no alcanza, hay que pisarla. Hago click en la baldosa y, si no entro,
            # voy probando las de alrededor (la puerta exacta cambia un poco de server a server)
            now_ = time.time()
            if now_ - ms.get("_ct", 0) > 1.3:
                ms["_ct"] = now_
                k = ms["_k"] = ms.get("_k", -1) + 1
                ring = [(0, 0), (0, -1), (-1, 0), (1, 0), (-1, -1), (1, -1), (0, 1), (-1, 1), (1, 1), (0, -2)]
                dx, dy = ring[(k // 2) % len(ring)]
                tw, th = self._tile()
                cx, cy = self._char()
                self._release()
                winapi.click(*self.abs_pt(cx + (g[0] + dx - self.pos["x"]) * tw, cy + (g[1] + dy - self.pos["y"]) * th))
            self.stats["state"] = "encargo"
            return True
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
            if self._mem_struct():
                d = self.mem.proc.read(self.mem.addr[1] + 72, 4)
                v = int.from_bytes(d, "little", signed=True) if d else 0
                if 1 <= v <= 180:
                    return v
        except Exception:
            pass
        sh = self._sheet(6 * 3600)
        return sh["level"] if sh else None

    def _mon_ok(self, e):
        """¿Le puedo pegar a este bicho? (el dato que leo de la memoria es su animación, no su especie: no sirve para vetar)"""
        return True

    def _reach(self, e):
        """¿Cómo llego a este bicho? Devuelve (costo en pasos, hay que rodear) o None si no hay camino
        (está del otro lado de una montaña, un pozo o una pared). Se recuerda 2 s por bicho."""
        px, py = self.pos.get("x"), self.pos.get("y")
        d = max(abs(e[0]), abs(e[1]))
        mp = self.world.map_for(self.pos["map"]) if self.pos.get("map") else None
        if px is None or not mp or d < 1:
            return (d, False)
        if d == 1:
            # pegado pero con una cerca/pared en el medio (en diagonal se corta): hay que rodear
            tx, ty = px + e[0], py + e[1]
            if e[0] and e[1] and not (mp.ok(px + e[0], py) or mp.ok(px, py + e[1])):
                path = mp.astar((px, py), (tx, ty) if mp.ok(tx, ty) else (mp.nearest_walkable(tx, ty, r=2) or (tx, ty)), max_nodes=6000)
                return (len(path), True) if path and len(path) <= 45 else None
            return (d, False)
        now = time.time()
        rc = self.__dict__.setdefault("_reach_c", {})
        c = rc.get(e[2])
        if c and now - c[0] < 2.0 and c[1] == (px, py, e[0], e[1]):
            return c[2]
        t = (px + e[0], py + e[1])
        if mp.los((px, py), t):
            r = (d, False)
        else:
            g = t if mp.ok(*t) else mp.nearest_walkable(t[0], t[1], r=2)
            path = mp.astar((px, py), g, max_nodes=6000) if g else None
            r = (len(path), True) if path and len(path) <= 45 else None
        if len(rc) > 200:
            rc.clear()
        rc[e[2]] = (now, (px, py, e[0], e[1]), r)
        return r

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
                self._last_kill_t = now
                self._spot_kill(1)
                self._zone_kill()
                self._fight_kill(now - (self.target_since or now))
                # power hunting: si hay más bichos cerca sigo matando y levanto todo junto después.
                # Levanto igual si junté 8, si el primero cayó hace más de 60 s (el respawn no me encierra) o si
                # tengo que ir a recargar.
                q = self.__dict__.setdefault("_loot_q", [])
                if t.get("mx") is not None:
                    q.append((t["mx"], t["my"], now))
                others = [o for o in mons if o[2] != t["ename"] and o[2] not in self._ent_bl and max(abs(o[0]), abs(o[1])) <= 7]
                sm_ = self._supplies_mem() or {}
                urgent = (sm_.get("pot", 99) <= int(self.cfg.get("buy_pots_below", 3)) or getattr(self, "_err_v", None)
                          or (sm_ and self.INV_SLOTS - sm_.get("items", 0) < 2))
                if (self.cfg.get("power_hunt", True) and others and q and len(q) < 8 and now - q[0][2] < 60 and not urgent):
                    self.state, self.target = "seek", None
                    self.stats["state"], self.stats["target"] = f"power hunting ({len(q)} para levantar)", ""
                    return True
                if q:
                    self.state = "lootq"
                    self.stats["state"], self.stats["target"] = f"levantando todo ({len(q)})", ""
                    self._lootq_t = now
                    return True
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
            if self.pos.get("x") is not None:
                t["mx"], t["my"] = self.pos["x"] + e[0], self.pos["y"] + e[1]
            self._last_enemy = now
            rch = self._reach(e)
            if rch is None:
                # quedó del otro lado de algo que no se puede cruzar: lo dejo y busco otro
                self._ent_bl[t["ename"]] = now + 30
                self._release()
                self.state, self.target = "seek", None
                return True
            if rch[1]:
                # hay un obstáculo en el medio: voy caminando por el camino que lo rodea, sin apuntarle derecho
                mp_ = self.world.map_for(self.pos["map"])
                if now - t.setdefault("walk0", now) > 25:
                    self._ent_bl[t["ename"]] = now + 30  # no llego: lo dejo
                    self._release()
                    self.state, self.target = "seek", None
                    return True
                if self.mod_held:
                    self._release()
                self._walk_to(mp_, (self.pos["x"] + e[0], self.pos["y"] + e[1]), "rodeando un obstáculo hasta el bicho")
                self.stats["state"] = "atacando"
                self.target_since = max(self.target_since, now - 12)  # caminar no cuenta como "no muere"
                return True
            self.nav["walking"] = False
            # pegado al bicho y no muere ni él ni nadie hace rato: seguro quedé en "modo paz" (pasa al volver a
            # entrar al juego). TAB cambia a modo ataque. Si me equivoco, se vuelve a corregir solo al rato.
            if max(abs(e[0]), abs(e[1])) <= 1:
                if (now - t.setdefault("adj0", now) > 12 and now - getattr(self, "_tab_t", 0) > 600
                        and now - getattr(self, "_last_kill_t", 0) > 60 and not getattr(self, "_tab_pending", 0)):
                    self._tab_t = self._tab_pending = now
                    t["adj0"] = now
                    self._release()
                    winapi.press("TAB")
                    self.log("⚔ Hace un minuto que no mato nada pegado a los bichos: pruebo cambiar a modo ataque (TAB).")
                    self.target_since = now
                    return True
            else:
                t.pop("adj0", None)
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
        cands.sort(key=lambda c: c[0])
        # de los que están a la vista, me quedo con los que tienen camino y ordeno por pasos reales (no en línea recta)
        near = []
        for c in [c for c in cands if abs(c[1][0]) <= 9 and abs(c[1][1]) <= 6][:8]:
            rch = self._reach(c[1])
            if rch is None:
                self._ent_bl[c[1][2]] = now + 20
                continue
            near.append((rch[0], c[1]))
        near.sort(key=lambda c: c[0])
        cands = [c for c in cands if c[1][2] not in self._ent_bl]
        if near:
            e = near[0][1]
            m = {"name": "monstruo", "ename": e[2], "etype": e[5], "x": int(cx + e[0] * tw), "y": int(cy + e[1] * th - th * 0.3),
                 "w": tw, "h": th, "score": 1.0}
            self.target, self.state, self._track = m, "attack", None
            self.target_since = self.last_seen_target = self.last_target_found = now
            self._last_enemy = now
            self._exp_t0 = self._exp_mem()
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

    def _loot_queue_step(self):
        """Pasada de levantar: voy por cada lugar donde murió un bicho (el más cercano primero) y clickeo ahí.
        Si me atacan, primero me defiendo. Máximo 30 s por pasada."""
        now = time.time()
        q = self.__dict__.setdefault("_loot_q", [])
        px, py = self.pos.get("x"), self.pos.get("y")
        if not q or px is None or now - getattr(self, "_lootq_t", now) > 30 or not self.cfg.get("loot_enabled", True):
            self._loot_q = []
            self.state, self.target, self._track = "seek", None, None
            return
        if self._under_attack():
            # me pegan mientras levanto: vuelvo a pelear y sigo levantando después
            self.state, self.target = "seek", None
            return
        tw, th = self._tile()
        cx, cy = self._char()
        q.sort(key=lambda a: max(abs(a[0] - px), abs(a[1] - py)))
        mx, my, _ = q.pop(0)
        dx, dy = mx - px, my - py
        if max(abs(dx), abs(dy)) > 8:
            mp = self.world.map_for(self.pos["map"]) if self.pos.get("map") else None
            if mp:
                q.insert(0, (mx, my, _))
                self._walk_to(mp, (mx, my), "a levantar lo que cayó")
            return
        winapi.click(*self.abs_pt(cx + dx * tw, cy + dy * th))
        self.stats["loot_clicks"] += 1
        self._last_move = now
        time.sleep(0.35 + 0.12 * max(abs(dx), abs(dy)))
        if not q:
            self.state, self.target, self._track = "seek", None, None

    def _auto(self):
        now = time.time()
        tw, th = self._tile()
        if self.state == "lootq":
            self._loot_queue_step()
            return
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

        if self.cfg.get("memory", {}).get("hunt", True) and self.ents.center and self._mem_struct():
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
            "mobs": self._mobs_near_view(),
            "goto": getattr(self, "goto", None),
        }
