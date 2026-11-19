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
from . import grid as gridmod
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
        self._manual_off = False
        winapi.OFF = False
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
        self._walk_only = False
        try:
            winapi.key_up("ALT")
        except Exception:
            pass
        if reason in ("se perdió la ventana del juego",) and self.running:
            self._resume_when_game = True
        if reason == "manual":
            self._manual_off = True
        with self.lock:
            was = self.running
            self.running = False
            self._release()
            # apagado = apagado: corto toda entrada de mouse/teclado, también la de encargos que sigan en otro hilo
            winapi.OFF = True
            self._busy_ui = False
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
            if r.get("manual_off") and not r.get("running"):
                want_start = False  # lo apagaste vos: no me prendo solo por actualizarme
                self._manual_off = True
                self.log("Me actualicé, pero me habías apagado a mano: sigo apagado hasta que me prendas (INSERT).")
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
                    elif time.time() - changed_at > 8 and not getattr(self, "_busy_ui", False) and (
                            # pasados 7 min me reinicio igual, pero nunca con poca vida o mientras me pegan
                            (time.time() - changed_at > 420 and not self._under_attack() and (self.bars.get("hp") or 100) >= 50)
                            or not self.running or cfgmod.server_slug(self.cfg) == "abaddon"
                            or (self._mobs_within(8) == 0 and not self._under_attack() and (self.bars.get("hp") or 100) >= 80)):
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
                json.dump({"running": self.running, "manual_off": bool(getattr(self, "_manual_off", False))}, f)
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

    def abs_pt(self, x, y, bar=False):
        # Olympia: la barra de abajo tiene botones (el de la derecha abre el menú del juego: Discord, Cerrar sesión).
        # Ningún clic de caminar/pegar/levantar puede caer ahí: lo subo al borde del área de juego.
        try:
            if not bar and self.rect and cfgmod.server_slug(self.cfg) != "abaddon" and y > self.rect[3] * 0.905:
                # excepción: el bolso abierto puede quedar encima de la barra (la ventana se corre); un clic sobre un
                # objeto del bolso va al bolso, no a la barra. Sin esto los clics al bolso caían en otro objeto.
                bb = getattr(self, "_bag_box_now", None)
                inbag = bb and time.time() - bb[4] < 30 and bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3]
                if not inbag:
                    y = self.rect[3] * 0.905
        except Exception:
            pass
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
        if self.img is not None and cfgmod.server_slug(self.cfg) != "abaddon" and c.get("hp_bar"):
            try:
                self._expbar_thin(c)
            except Exception:
                pass
        elif eb and self.img is not None:
            import cv2
            y = int(eb["y1"])
            if 0 <= y < self.img.shape[0]:
                row = cv2.cvtColor(self.img[y:y + 1, int(eb["x1"]):int(eb["x2"])], cv2.COLOR_BGR2HSV)[0]
                g = (row[:, 0] > 40) & (row[:, 0] < 85) & (row[:, 1] > 120) & (row[:, 2] > 90)
                if g.any():
                    ix = np.where(g)[0]
                    self._expbar = (int(ix.max() - ix.min() + 1), time.time())  # largo en píxeles de lo verde
                    self._exp_update(round(100.0 * (np.where(g)[0].max() + 1) / len(g), 1))
        hp = self.bars.get("hp")
        if hp is not None:
            h = getattr(self, "_hp_hist", [])
            h.append((time.time(), hp))
            self._hp_hist = [x for x in h if time.time() - x[0] < 4]

    def _expbar_thin(self, c):
        """Barrita de experiencia de Olympia: una línea verde finita que cruza la pantalla justo arriba del tablero
        (arranca bien a la izquierda; la barra de energía también es verde pero arranca en el medio). Busco la fila
        cada minuto y mido el largo de lo verde en cada vuelta."""
        import cv2
        now = time.time()
        H, W = self.img.shape[:2]
        hy = int(c["hp_bar"]["y1"])
        row = getattr(self, "_xb_row", None)

        def run(y):
            r = cv2.cvtColor(self.img[y:y + 1, 0:W], cv2.COLOR_BGR2HSV)[0]
            g = (r[:, 0] > 28) & (r[:, 0] < 88) & (r[:, 1] > 90) & (r[:, 2] > 90)
            ix = np.where(g)[0]
            if len(ix) < 12 or ix[0] > 0.16 * W:
                return 0, 0
            # tramo verde continuo desde el arranque (tolero cortes de hasta 6 px: el texto del chat puede taparla)
            end = ix[0]
            for v in ix:
                if v - end > 6:
                    break
                end = v
            return int(end - ix[0] + 1), int(ix[0])
        if not row or now - row[1] > 60:
            best = (0, None)
            for y in range(max(0, hy - 70), max(0, hy - 14)):
                ln, _x0 = run(y)
                if ln > best[0]:
                    best = (ln, y)
            row = self._xb_row = (best[1], now)
        if row[0] is None:
            return
        ln, _x0 = run(row[0])
        if ln >= 12:
            self._expbar = (ln, now)

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
        if cfgmod.server_slug(self.cfg) != "abaddon":
            self._tab_pending = 0  # en este cliente el modo se ve en el ícono: no toco TAB a ciegas
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
            # mientras venda/compre algo (latido), no está colgada: la cuento desde la última venta
            if now - max(self._busy_t0, getattr(self, "_busy_beat", 0)) > 300:
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
        live = None if self._mem_struct() else self._exp_live()
        e = self._exp_mem() if self._mem_struct() else (live if live is not None else self.stats.get("kills"))
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
        if now < getattr(self, "_rabia_until", 0) or now - getattr(self, "_last_kill_t", 0) < 40:
            stuck_pos = False  # plantado peleando (rabia) o matando: no es estar trabado
        no_exp = now - exp_t > float(self.cfg.get("no_exp_secs", 180)) and not self._is_travelling()
        if now - getattr(self, "_last_kill_t", 0) < float(self.cfg.get("no_exp_secs", 180)):
            no_exp = False  # estoy matando: que no lea la barra de experiencia no quiere decir que no gane
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
        self.__dict__.setdefault("_sal_stuck", []).append(now)
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
        try:
            # cartel 'Subir de Nivel' (abajo a la derecha): zona prohibida para los clics de caminar/pegar. Un clic
            # ahí abre la ventana de stats y los siguientes apretaban sus '+' (VIT, DEX, INT, LUK al azar).
            if self.rect and cfgmod.server_slug(self.cfg) != "abaddon":
                ob = self.cfg.get("calib", {}).get("levelup_btn") or {"x": int(self.rect[2] * 0.816), "y": int(self.rect[3] * 0.883)}
                W_, H_ = self.rect[2], self.rect[3]
                x1, y1 = self.abs_pt(ob["x"] - 0.075 * W_, ob["y"] - 0.035 * H_)
                x2, y2 = self.abs_pt(ob["x"] + 0.075 * W_, ob["y"] + 0.035 * H_)
                winapi.NOCLICK = [(x1, y1, x2, y2)]
        except Exception:
            pass
        try:
            # el cartel de tirar un objeto traba todo (hasta revivir): se contesta 'No' antes que cualquier otra cosa
            if not getattr(self, "_busy_ui", False) and time.time() - getattr(self, "_dropno_t", 0) > 6:
                self._dropno_t = time.time()
                if self._drop_no():
                    return
        except Exception:
            pass
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
        try:
            self._tmeter()
        except Exception:
            pass
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
                        and cfgmod.server_slug(cfg) != "abaddon" and self._mobs_within(3) == 0):
                    self._stats_done = self._last_stats = time.time()
                    self._do_stats()
            return
        self._timers()
        try:
            self._poison_check()
            self._mode_check()
        except Exception:
            pass
        try:
            want_ = str(cfg.get("ocr_engine", "windows")).lower()
            if want_ != ocr.ENGINE:
                ocr.set_engine(want_)
                self.log(f"🔤 Motor de lectura de texto: {want_}.")
            # zona segura (adentro de tienda, herrería o almacén, sin bichos): ahí todo se lee con RapidOCR, que es
            # más lento pero más certero (comprar, vender, reparar, ordenar el bolso). Afuera, Windows (combate).
            mp_ = (self.pos.get("map") or "").lower()
            # solo mientras de verdad estoy usando una ventana del local (_busy_ui): parado o caminando adentro, RapidOCR
            # hacía cada vuelta de 3-4 s, la posición quedaba vieja y no encontraba la salida (clavado en la tienda)
            safe_ = any(k in mp_ for k in ("shop", "blacksmith", "warehouse", "smith")) and self._mobs_within(8) == 0 \
                and bool(getattr(self, "_busy_ui", False))
            if safe_ != ocr.SAFE:
                ocr.SAFE = safe_
                if want_ == "mixto":
                    self.log("🔤 Zona segura: leo con RapidOCR." if safe_ else "🔤 Afuera: leo con el OCR de Windows.")
        except Exception:
            pass
        # pantalla de ingreso (sesión cerrada): ahí no se hace NINGÚN clic (Crear cuenta / Discord / Salir) ni se
        # escribe nada. Me apago y aviso; la contraseña la pone Juan.
        try:
            if cfgmod.server_slug(cfg) != "abaddon" and time.time() - getattr(self, "_login_t", 0) > 6 and self.img is not None:
                self._login_t = time.time()
                H_, W_ = self.img.shape[:2]
                wl = ocr.read_words(self.img, {"x1": int(W_ * 0.14), "y1": int(H_ * 0.52), "x2": int(W_ * 0.38), "y2": int(H_ * 0.90)}, scale=1.0)
                tl = " ".join(w[0].lower() for w in wl)
                if sum(k in tl for k in ("entrar", "crear cuenta", "recordar", "salir", "discord")) >= 3:
                    self._release()
                    self.log("⛔ El juego está en la pantalla de ingreso (sesión cerrada): me apago. Entrá vos y prendeme con INSERT.")
                    self.stop("manual")
                    return
        except Exception:
            pass
        # menú del juego abierto (ESC): tapa todo y un clic ahí abre Discord o cierra la sesión. Lo miro cada 5 s,
        # leyendo solo el centro de la pantalla, antes de cualquier clic.
        try:
            if cfgmod.server_slug(cfg) != "abaddon" and time.time() - getattr(self, "_gm_t", 0) > 5 and self.img is not None \
                    and not getattr(self, "_busy_ui", False):
                self._gm_t = time.time()
                H_, W_ = self.img.shape[:2]
                wg = ocr.read_words(self.img, {"x1": int(W_ * 0.36), "y1": int(H_ * 0.14), "x2": int(W_ * 0.64), "y2": int(H_ * 0.62)}, scale=1.0)
                if self._game_menu_close(wg):
                    return
        except Exception:
            pass
        try:
            self._hunger_watch()
        except Exception:
            pass
        # REGLA DE ORO: antes que cualquier otra cosa (viaje, misión, compras, pensar), si me pegan me defiendo
        try:
            if cfg["mode"] == "auto" and not self._fleeing() and self._reflex():
                return
        except Exception as e_:
            if time.time() - getattr(self, "_rfx_err", 0) > 300:
                self._rfx_err = time.time()
                self.log(f"Regla de oro: error {e_!r}")
        if getattr(self, "_walk_only", False):
            # clic en el mapa con el bot apagado: solo camino hasta ahí (si me pegan, la regla de oro ya me defendió
            # arriba) y me vuelvo a apagar. No cazo, no compro, no reviso nada.
            if not getattr(self, "goto", None):
                self._walk_only = False
                self.log("🗺 Llegué (o se canceló el viaje): me vuelvo a apagar.")
                self.stop("manual")
                return
            self._goto_step()
            return
        sa = cfg.get("stats_auto", {})
        if sa.get("enabled") and time.time() - getattr(self, "_lvl_check", 0) > 8:
            self._lvl_check = time.time()
            lu_pts = self._lu_mem()
            mn_ = (self.pos.get("map") or "").lower()
            indoors = any(k in mn_ for k in ("shop", "blacksmith", "cityhall", "city hall", "warehouse", "guild", "church", "magic tower", "barrack", "jail", "command hall"))
            wait_ = 20 if getattr(self, "_stats_fail", 0) < 2 else 180
            # una misión de cazar (casi siempre hay una activa) NO frena los stats ni la ficha: solo una misión que
            # está usando ventanas (hold_ui) o un viaje en curso. Antes: con misiones siempre activas el nivel quedó
            # clavado en 56 (era 60) y 10 puntos sin repartir toda la noche.
            ms_ = getattr(self, "mission", None)
            busy_m = bool(getattr(self, "goto", None)) or bool(ms_ and ms_.get("hold_ui"))
            # adentro de tienda/herrería también sirve (sin bichos: el mejor momento), mientras no esté usando sus
            # ventanas. Antes solo afuera, y afuera en la Granja siempre hay bichos a 4 baldosas: nunca leía la ficha.
            ui_busy = getattr(self, "_busy_ui", False) or self._ui_th_alive()
            if (not ui_busy and not busy_m and (lu_pts or vision.find_level_up(self.img))
                    and time.time() - getattr(self, "_stats_done", 0) > wait_):
                self._last_stats = 0
            # sin memoria de los puntos y con la ficha fallando, el cartel 'Subir de Nivel' casi nunca se detecta:
            # cada 15 minutos miro la ventana de stats igual (si no hay puntos, la cierro y listo)
            if (cfgmod.server_slug(cfg) != "abaddon" and not ui_busy and not busy_m and lu_pts is None
                    and time.time() - getattr(self, "_stats_done", 0) > 900):
                self._last_stats = 0
            # servers sin memoria del pj: la ficha (F5) dice el nivel, la exp y si hay puntos por repartir
            if not ui_busy and not busy_m and (not indoors or time.time() - getattr(self, "_census_in_t", 0) > 300):
                if indoors:
                    self._census_in_t = time.time()
                if self._census():
                    return
        if sa.get("enabled") and self.state != "attack" and getattr(self, "_last_stats", 1) == 0 \
                and not getattr(self, "_busy_ui", False) and not self._ui_th_alive() \
                and not (cfgmod.server_slug(cfg) != "abaddon" and (self._adjacent_mobs() > 0 or self._mobs_within(3) > 0
                         or (self.bars.get("hp") or 100) < 50)):
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
        g_ = getattr(self, "goto", None)
        if g_ and not getattr(self, "_busy_ui", False) and not self._same(self.pos.get("map") or "", g_.get("map") or "") \
                and time.time() - getattr(self, "_exit_t", 0) > 2.5 and self._exit_building():
            # tengo que ir a otro mapa y estoy adentro de un local (ej. de la tienda al herrero): primero salgo
            self._exit_t = time.time()
            return
        if self._goto_step():
            return
        # PELEANDO (bicho pegado, me pegan o estoy en rabia): nada de tareas de fondo (misiones, plan, revisar equipo
        # u ordenar el bolso, escanear nidos). Cada una frenaba el golpe 1-30 s en pleno nido. Se hacen al terminar.
        try:
            fighting_ = cfgmod.server_slug(cfg) != "abaddon" and (
                self._adjacent_mobs() > 0 or self._under_attack() or time.time() < getattr(self, "_rabia_until", 0))
        except Exception:
            fighting_ = False
        now_f = time.time()
        if not fighting_:
            self._fight_since = None
        elif not getattr(self, "_fight_since", None):
            self._fight_since = now_f
        elif now_f - self._fight_since > 60 and now_f - getattr(self, "_hk_forced_t", 0) > 60:
            # pelea sin corte hace más de un minuto (nido que no termina): dejo pasar las tareas de fondo una vez por
            # minuto, para no quedar sin pociones o con el bolso lleno sin enterarme
            self._hk_forced_t = now_f
            fighting_ = False
        try:
            self._pot_pace()
        except Exception:
            pass
        try:
            hp_ = self.bars.get("hp")
            if hp_ is not None:
                hh = self.__dict__.setdefault("_hp_hist", [])
                if not hh or now_f - hh[-1][0] > 1.0:
                    hh.append((now_f, float(hp_)))
                    del hh[:-900]
        except Exception:
            pass
        try:
            self._nest_track()
        except Exception:
            pass
        try:
            self._mode_tick()
        except Exception:
            pass
        try:
            self._salud()
        except Exception:
            pass
        if fighting_:
            self._close_junk_windows()
        elif self._director():
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
        in_town = self._in_town()
        if in_town and not self._attacked_recently():
            focused = True  # en la ciudad no hay nada que cazar: lo que "veo" ahí son vecinos, gatos y guardias
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
        # algo tirado al alcance y ningún bicho pegado: lo levanto (de viaje, solo lo que queda de paso)
        if (not mem_ok and cfg["mode"] == "auto" and self.state == "seek" and not self._fleeing() and not in_building
                and cfgmod.server_slug(cfg) != "abaddon" and self.pos.get("x") is not None and not self._under_attack()):
            try:
                if self._loot_pass(self._oly_ents() or [], reach=(3 if focused else None)):
                    return
            except Exception:
                pass
        # venía ganando experiencia en otro lado y acá no pasa nada: vuelvo ahí antes que explorar
        if (not mem_ok and cfg["mode"] == "auto" and self.state == "seek" and not self._fleeing() and not in_building
                and not self._town_block() and not focused and not self._under_attack() and self._hot_return()):
            return
        # Olympia: mapa sin relevar → lo recorro entero (solo peleo si me atacan)
        if (not mem_ok and cfg["mode"] == "auto" and self.state == "seek" and not self._fleeing() and not in_building
                and not self._town_block() and not focused and self._explore_step()):
            return
        # Olympia: lista de bichos leída de la memoria (objetos del heap)
        if (not mem_ok and cfg["mode"] == "auto" and self.state == "seek" and not self._fleeing() and not in_building
                and not self._town_block() and (not focused or self._under_attack()) and self._oly_step()):
            return
        # me están pegando y no tengo la lista de bichos de la memoria: freno, busco quién es y le pego
        oly_live = cfgmod.server_slug(cfg) != "abaddon" and getattr(self, "_oly_said", False) and not cfg.get("vision_hunt", False)
        if (not mem_ok and not oly_live and cfg["mode"] == "auto" and self.state == "seek" and not self._fleeing()
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
        if (not mem_ok and not oly_live and not focused and self.state == "seek" and self.nav.get("walking") and self.cfg.get("hover_confirm", True)
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
        if in_town and not self._attacked_recently():
            self._release()
            self.state, self.target = "seek", None
            self.stats["state"] = "en la ciudad (busco cómo salir a cazar)"
            if time.time() - getattr(self, "_town_said", 0) > 300:
                self._town_said = time.time()
                self.log(f"🏙 Estoy en {self.pos.get('map')} y no encuentro camino a {self.travel_dest() or 'un mapa de caza'}: acá no cazo.")
            return
        mname = (self.pos.get("map") or "").lower()
        if any(k in mname for k in ("shop", "blacksmith", "cityhall", "city hall", "warehouse", "guild", "cathedral", "church", "wizard", "magic tower", "barrack", "jail", "command hall")):
            self.stats["state"] = "en un edificio"
            return
        if cfg["mode"] == "pit":
            self._pit()
        elif cfg["mode"] == "templates":
            self._hunt()
        elif cfgmod.server_slug(cfg) != "abaddon" and getattr(self, "_oly_said", False) and not cfg.get("vision_hunt", False):
            # tengo la lista de bichos vivos de la memoria: el buscador por imagen (marca como "monstruo" todo lo que
            # se mueve: mi pj, el pasto) acá solo mete ruido y muertes falsas. No lo uso.
            if self.state != "seek" or self.target:
                self._release()
                self.state, self.target, self._track = "seek", None, None
            self._last_mons = []
            if not self._hot_return():
                self.stats["state"] = "buscando"
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
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum() or c in "áé")
        tab = next((w for w in words if norm(w[0]).startswith(("pocion", "pocl", "pocio"))), None)
        smith = False
        if not tab:
            # herrero: pestañas Armas / Armadura / Varios / Vender
            tab = next((w for w in words if norm(w[0]) in ("armas", "arrnas") or norm(w[0]).startswith("armadur")), None)
            smith = bool(tab)
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
        if not tab or not any(norm(v[0]).startswith(("vender", "varios", "undcr", "arios")) and abs(v[2] - tab[2]) < 15 for v in words):
            # las pestañas (letra gótica) a veces no se leen: la ventana también se reconoce por el pie, que es
            # letra común: 'Comprar' con 'Máx.' en el mismo renglón
            full = ocr.read_words(self.screen.grab(self.rect), {"x1": 0, "y1": 0, "x2": self.rect[2], "y2": int(self.rect[3] * 0.9)}, scale=1.0)
            cp = next((w for w in full if norm(w[0]) == "comprar"), None)
            if cp and any(norm(v[0]).startswith(("máx", "max")) and abs(v[2] - cp[2]) < 15 * ky and v[1] < cp[1] for v in full):
                winapi.click(*self.abs_pt(cp[1] - 250 * kx, cp[2] - 120 * ky), button="right")
                time.sleep(0.4)
                return True
            # con el bolso lleno los botones quedan grises y no se leen: el renglón 'Total N (tienes M)' sí
            tt = next((w for w in full if norm(w[0]).startswith("total")), None)
            if tt and any("tienes" in norm(v[0]) and abs(v[2] - tt[2]) < 15 * ky for v in full):
                winapi.click(*self.abs_pt(tt[1] + 190 * kx, tt[2] - 80 * ky), button="right")  # probado: cierra la ventana
                time.sleep(0.4)
                return True
            # sin nada elegido no hay 'Total' y los botones quedan grises: la reconozco por la columna de precios
            # (4+ números uno abajo del otro, con el nombre a la izquierda) y cierro en el recuadro de abajo
            col = self._shop_price_col(full)
            if col:
                winapi.click(*self.abs_pt(col[0] - 190 * kx, col[1] + 470 * ky), button="right")
                time.sleep(0.6)
                again = ocr.read_words(self.screen.grab(self.rect), {"x1": 0, "y1": 0, "x2": self.rect[2], "y2": int(self.rect[3] * 0.9)}, scale=1.0)
                if self._shop_price_col(again):
                    winapi.click(*self.abs_pt(col[0] - 190 * kx, col[1] - 90 * ky), button="right")  # el título
                    time.sleep(0.5)
                return True
            return False
        if smith:
            # la lista del herrero está llena de filas y con algo elegido el recuadro de abajo tampoco cierra: el
            # click derecho va a la barra del título (probado: cierra siempre y no elige nada)
            winapi.left_up()
            winapi.move(*self.abs_pt(tab[1] + 20 * kx, tab[2] - 43 * ky))
            time.sleep(0.2)
            winapi.click(*self.abs_pt(tab[1] + 20 * kx, tab[2] - 43 * ky), button="right")
        else:
            winapi.click(*self.abs_pt(tab[1] + 166 * kx, tab[2] + 317 * ky), button="right")
            time.sleep(0.5)
            try:
                w2 = ocr.read_words(self.screen.grab(self.rect), {"x1": 0, "y1": 0, "x2": self.rect[2], "y2": int(self.rect[3] * 0.6)}, scale=1.0)
                if any(norm(w[0]).startswith(("pocion", "pocl", "pocio")) and abs(w[2] - tab[2]) < 12 for w in w2):
                    winapi.click(*self.abs_pt(tab[1] + 20 * kx, tab[2] - 43 * ky), button="right")  # sigue abierta: al título
            except Exception:
                pass
        time.sleep(0.4)
        return True

    def _shop_price_col(self, words):
        """Lista de una tienda: 4+ números alineados en columna, a un renglón de distancia, con texto a la
        izquierda. Devuelve (x de la columna, y del primer renglón) o None."""
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
        nums = [w for w in words if w[0].strip().isdigit() and w[2] < self.rect[3] * 0.62]
        for w in nums:
            col = sorted((v for v in nums if abs(v[1] - w[1]) < 22 * kx), key=lambda v: v[2])
            if len(col) < 4:
                continue
            gaps = [b[2] - a[2] for a, b in zip(col, col[1:])]
            if sum(1 for g_ in gaps if 18 * ky <= g_ <= 36 * ky) < 3:
                continue
            named = sum(1 for v in col if any(u[0].strip()[:1].isalpha() and abs(u[2] - v[2]) < 8 * ky
                                              and 150 * kx < v[1] - u[1] < 480 * kx for u in words))
            if named >= 3:
                return (col[0][1], col[0][2])
        return None

    def _game_menu_close(self, words=None):
        """'Menú del juego' abierto (Opciones, Tienda, Ticket, Discord, Cerrar sesión): un clic ahí adentro abre el
        navegador o cierra la sesión. Lo cierro con ESC (la misma tecla que lo abre). True si estaba abierto."""
        try:
            norm = lambda t: "".join(c for c in t.lower() if c.isalnum() or c in "áéíóú")
            if words is None:
                img = self.screen.grab(self.rect)
                words = ocr.read_words(img, {"x1": 0, "y1": 0, "x2": img.shape[1], "y2": int(img.shape[0] * 0.92)}, scale=1.0)
            t = " ".join(norm(w[0]) for w in words)
            hits = sum(k in t for k in ("opciones", "discord", "ticket", "capturas", "sesión", "sesion", "noticias", "eventos"))
            if hits >= 3:
                # se cierra con clic DERECHO en la ventana (Juan). Lo hago sobre el título 'Menú del juego', lejos de
                # los botones (un clic izquierdo en un botón abre Discord o cierra la sesión).
                self._release()
                tit = next((w for w in words if norm(w[0]) in ("menú", "menu")), None)
                if tit and time.time() - getattr(self, "_gm_rc", 0) > 4:
                    self._gm_rc = time.time()
                    winapi.left_up()
                    winapi.click(*self.abs_pt(tit[1], tit[2]), button="right")
                    time.sleep(0.6)
                    self.log("🗙 Cerré el menú del juego (clic derecho en el título).")
                    return True
                if time.time() - getattr(self, "_gm_said", 0) > 60:
                    self._gm_said = time.time()
                    self.log("⛔ El menú del juego está abierto: no toco nada hasta que se cierre (un clic ahí abre Discord o cierra la sesión).")
                return True
        except Exception:
            pass
        return False

    def _drop_no(self, img=None):
        """Cartel '¿Estás seguro de que quieres tirar este objeto?' (sale abajo a la derecha si un arrastre cae fuera
        del bolso): SIEMPRE 'No'. Lo leo recortado, porque la lectura de pantalla entera no llega ahí abajo."""
        if cfgmod.server_slug(self.cfg) == "abaddon" or not self.rect:
            return False
        img = img if img is not None else self.screen.grab(self.rect)
        H, W = img.shape[:2]
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        # el cartel sale donde quedó el mouse: abajo a la derecha, abajo a la izquierda o al medio
        for bx in ({"x1": int(W * 0.48), "y1": int(H * 0.55), "x2": W, "y2": int(H * 0.985)},
                   {"x1": 0, "y1": int(H * 0.55), "x2": int(W * 0.52), "y2": int(H * 0.985)},
                   {"x1": int(W * 0.15), "y1": int(H * 0.15), "x2": int(W * 0.85), "y2": int(H * 0.60)}):
            ws = ocr.read_words(img, bx, scale=1.0)
            hit = next((w for w in ws if norm(w[0]) == "tirar"), None)
            if not hit or not any(norm(w[0]) in ("seguro", "quieres", "objeto") for w in ws):
                continue
            no = next((w for w in ws if norm(w[0]) == "no" and 0 < w[2] - hit[2] < H * 0.09), None)
            if not no:
                si = next((w for w in ws if norm(w[0]) in ("si", "sí") and 0 < w[2] - hit[2] < H * 0.09), None)
                if not si:
                    continue
                no = ("No", si[1] + int(W * 0.102), si[2])  # el botón 'No' está a la derecha del 'Sí'
            winapi.left_up()
            winapi.click(*self.abs_pt(no[1], no[2]))
            time.sleep(0.5)
            self.log("🚫 Había quedado abierto el cartel de tirar un objeto: respondí 'No' (nunca tiro nada).")
            return True
        return False

    def _ui_clear(self):
        """Antes de caminar: nada abierto (tienda o bolso). Con una ventana abierta el personaje no camina."""
        try:
            self._drop_no()
        except Exception:
            pass
        try:
            if self._close_shop_window():
                self.log("Cierro la ventana de la tienda antes de salir.")
                time.sleep(0.4)
            if bool(self._bag_open(self.screen.grab(self.rect))):
                winapi.press("F6")
                time.sleep(0.4)
        except Exception:
            pass

    def _close_junk_windows(self):
        """Ventanas que el bot no usa (ej. 'Resumen de Muerte' de Olympia): las cierro con click derecho
        sobre la ventana. Miro cada ~8 s, solo si no estoy peleando."""
        titles = list(self.cfg.get("close_windows") or []) + ["Heldenian", "Registro de chat", "Habilidades", "Magia", "Mejora de objetos"]
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
        if cfgmod.server_slug(self.cfg) != "abaddon" and self._game_menu_close(words):
            return True
        # ventana de Tienda abierta sin que nadie esté comprando: la cierro (abierta no deja caminar, y un clic
        # perdido ahí adentro puede comprar cualquier cosa)
        try:
            alive = any(getattr(self, a, None) is not None and getattr(self, a).is_alive()
                        for a in ("_shop_th", "_rep_th", "_gear_th", "_audit_th", "_town_th"))
            if not alive or getattr(self, "goto", None) or (getattr(self, "mission", None) and not self.mission.get("hold_ui")):
                top = [w for w in words if w[2] < H * 0.6]
                if self._close_shop_window(top):
                    self.log("Cierro la ventana de Tienda que había quedado abierta.")
                    return True
        except Exception:
            pass
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        txt = [(norm(w[0]), w) for w in words]
        # ventana 'Subir nivel' olvidada abierta (tapa el juego: los clics para caminar caen adentro y el pj queda
        # parado horas). Si nadie está repartiendo stats: deshago lo pendiente ('Restablecer', nada se aplicó) y la
        # cierro con click derecho. El reparto de stats la vuelve a abrir bien cuando le toque.
        try:
            if not getattr(self, "_stats_force", None) and self._stats_win_close(words):
                self.log("Cierro la ventana 'Subir nivel' que había quedado abierta (sin aplicar nada).")
                # si estaba abierta es que hay puntos (el cartel 'Subir de Nivel' queda en pantalla y un clic perdido
                # la abre): reparto en el próximo momento tranquilo y releo la ficha (nivel real)
                self._last_stats = 0
                self._census_t = 0
                return True
        except Exception:
            pass
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
                        if first == "peso" and not w[0].strip().startswith("Peso"):
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

    def _ui_th_alive(self):
        """¿Hay otro encargo de pueblo/ventanas en marcha? Dos a la vez se pisan (tienda y herrero juntos: 11 minutos
        perdidos)."""
        for a in ("_shop_th", "_rep_th", "_inv_th", "_gear_th", "_audit_th", "_town_th"):
            th = getattr(self, a, None)
            if th is not None and th.is_alive():
                return True
        return False

    def _mobs_within(self, r=3):
        """Bichos a r baldosas o menos (servers sin estructura de memoria: lista de entidades). 0 si no se sabe."""
        try:
            if self.pos.get("x") is None:
                return 0
            px, py = self.pos["x"], self.pos["y"]
            return sum(1 for e in (self._oly_ents() or []) if max(abs(e[0] - px), abs(e[1] - py)) <= r)
        except Exception:
            return 0

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

    def _pit_deadly(self, p, hours=8):
        """¿Me mataron cerca de ese nido en las últimas horas? Entonces no voy (ni por misión ni por experiencia)."""
        now = time.time()
        return any(max(abs(p["x"] - z[1]), abs(p["y"] - z[2])) <= max(15, int(p.get("r", 15))) and now - z[3] < hours * 3600
                   for z in self._dangers() if len(z) >= 4)

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
        if not scr_dead:
            self._cover_since = 0
        elif hm is None or hm > 0:
            # la barra se lee en 0 pero la memoria dice que tengo vida (o todavía no la sé, recién arrancado): casi
            # siempre es una ventana (chat, habilidades, magia, bolso) tapando la barra. Cierro ventanas en vez de
            # 'revivir'. Si la memoria no sabe y la barra sigue en 0 después de 15 s, ahí sí es muerte (y el botón
            # Reiniciar sigue mandando siempre)
            if not getattr(self, "_cover_since", 0):
                self._cover_since = now
            # Olympia: la vida en memoria no siempre está (hm None). Ahí la barra sola NO alcanza para darme por muerto
            # (la tienda o el chat la tapan y me 'moría' adentro de la tienda): manda el botón 'Reiniciar'
            if hm is not None or cfgmod.server_slug(self.cfg) != "abaddon" or now - self._cover_since < 15:
                scr_dead = False
            if not scr_dead and now - getattr(self, "_cover_t", 0) > 6:
                self._cover_t = now
                self._junk_t = 0
                try:
                    if not self._close_junk_windows():
                        self._ui_clear()
                except Exception:
                    pass
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
                self._killer_note()
            except Exception:
                pass
            if cfgmod.server_slug(self.cfg) != "abaddon":
                self._town_need("me mataron")
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
                and now > getattr(self, "_kite_block", 0)
                and not ((getattr(self, "_left", {}) or {}).get("pot") or 0) >= 1
                and self._adjacent_mobs() >= 3):
            self._flee_until = now + cfg.get("flee_max_sec", 600)
            self._release()
            self.state, self.target = "seek", None
            self.log(f"¡{self._adjacent_mobs()} bichos encima y vida {hp:.0f}%! Me retiro antes de que me rodeen.")
            return False
        # con bichos pegados NO se huye ni se queda parado tomando: darles la espalda (o quedarse quieto) es morir.
        # Tomo la poción y sigo pegando. Solo me voy si no me queda ninguna.
        try:
            adj_n_ = self._adjacent_mobs()
        except Exception:
            adj_n_ = 0
        pots_n_ = (getattr(self, "_left", {}) or {}).get("pot")
        stand_ = cfgmod.server_slug(cfg) != "abaddon" and adj_n_ >= 1 and pots_n_ != 0
        if stand_:
            self._flee_until = 0
            fight_pct_ = int(cfg.get("hp_pot_fight_pct", 40))
            if now < getattr(self, "_austere_until", 0):
                fight_pct_ = min(fight_pct_, 25)  # ritmo austero: aguanto más antes de tomar
            if hp is not None and hp <= max(panic, fight_pct_) and now - getattr(self, "_stand_pot_t", 0) > 1.3:
                self._stand_pot_t = now
                self._do_action(cfg["hp_pot_action"], "hp_pot_slot")
                self.stats["hp_pots"] += 1
        # vida en 0 justo con maná/energía visibles = barra tapada por una ventana (vivo con 0 no se puede estar):
        # no huyo ni tomo pociones por eso
        covered_ = hp is not None and hp <= 0.5 and ((self.bars.get("mp") or 0) > 0 or (self.bars.get("sp") or 0) > 0)
        if (not stand_) and not covered_ and hp is not None and hp <= panic and cfg.get("panic_action") == "flee" and now > getattr(self, "_flee_block", 0) \
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
        if covered_:
            # barra tapada: cierro lo que la tape (tienda olvidada, chat, habilidades) si no hay un encargo usándola
            if now - getattr(self, "_cover_t", 0) > 6 and not getattr(self, "_busy_ui", False) and not self._ui_th_alive():
                self._cover_t = now
                self._junk_t = 0
                try:
                    if not self._close_junk_windows():
                        self._ui_clear()
                except Exception:
                    pass
        if (not stand_) and not covered_ and hp is not None and hp <= panic:
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
            thr = cfg[f"{k}_pot_pct"]
            if k == "hp" and cfgmod.server_slug(cfg) != "abaddon":
                # descansar es gratis y la poción no: la tomo con bichos encima y vida baja, o al borde
                thr = int(cfg.get("hp_pot_fight_pct", 40)) if self._adjacent_mobs() > 0 else int(cfg.get("hp_pot_alone_pct", 22))
            if v is None or v >= thr:
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
            if cfgmod.server_slug(self.cfg) != "abaddon":
                # en Olympia ESC ABRE el menú del juego (Opciones / Discord / Cerrar sesión): nunca lo aprieto a ciegas.
                # Cierro lo que haya con sus propias teclas y, si el menú quedó abierto, lo cierro.
                self._ui_clear()
                self._game_menu_close()
                return
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
        self._inv_cache = self._oinv_cache = None
        sm0 = self._supplies_mem()
        if not sm0 or sm0["pot"] <= 0:
            return False
        winapi.press(str(key))
        t0 = time.time()
        while time.time() - t0 < 0.7:
            time.sleep(0.12)
            self._inv_cache = self._oinv_cache = None
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
        if kind == "pot":
            try:
                la_ = getattr(self, "_last_adj", None)
                if la_ and time.time() - la_[1] < 5:
                    for nm_ in la_[0]:
                        r_ = self._risk_rec(nm_)
                        r_["pots"] += 1.0 / len(la_[0])
            except Exception:
                pass
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
        # sin ficha: los últimos stats confirmados en la ventana 'Subir nivel' (solo suben: sirven aunque sean viejos)
        sl = self.cfg.get("stats_last") or {}
        if sl.get("STR"):
            st = {k: v for k, v in sl.items() if k != "t"}
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

    PJ_KEYS = ("oly_ttk", "fight", "oly_hot", "loot_blind")

    def _pj_dir(self):
        d = os.path.join(cfgmod.profile_dir(self.cfg), "personajes")
        os.makedirs(d, exist_ok=True)
        return d

    def _pj_snapshot(self, lvl, stats):
        """Todo lo que es de UN personaje (no del servidor), para guardarlo y poder volver a él."""
        snap = {"t": time.time(), "level": int(lvl), "stats": dict(stats or {}),
                "cfg": {k: self.cfg.get(k) for k in ("worn", "owned_items", "need_weapon", "train_skill_until", "repair_every_min")},
                "learn": {k: self.world.learn.get(k) for k in self.PJ_KEYS if self.world.learn.get(k) is not None}}
        try:
            with open(os.path.join(cfgmod.profile_dir(self.cfg), "bolso.json"), encoding="utf-8") as f:
                snap["bolso"] = json.load(f)
        except Exception:
            pass
        try:
            st = self.learner.st or {}
            snap["quests_done"] = st.get("quests_done")
        except Exception:
            pass
        return snap

    def _new_character(self, lvl, old_lvl, stats=None, prev=None):
        """Cambió el personaje. NO borro nada: lo del personaje anterior lo guardo en data/servers/<srv>/personajes/
        (nivel + stats lo identifican). Si el que entró ya lo conozco (mismo nivel o más, stats iguales o mayores, el
        más parecido), le devuelvo lo suyo; si es nuevo, arranca de cero en lo del personaje y con todo lo del
        servidor (mapas, nidos, tiendas, cuánto pega cada bicho, dónde se muere)."""
        d = self._pj_dir()
        # 1) guardo el anterior
        try:
            ps = (prev or {}).get("stats") or {}
            snap = self._pj_snapshot(old_lvl, ps)
            tag = f"nivel{int(old_lvl)}_" + "_".join(f"{k}{v}" for k, v in sorted(ps.items()))
            with open(os.path.join(d, f"{tag}.json"), "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
            self.log(f"💾 Guardé lo del personaje anterior (nivel {old_lvl}) para poder volver a él.")
        except Exception as e:
            self.log(f"💾 No pude guardar lo del personaje anterior: {e!r}")
        # 2) ¿al que entró lo conozco?
        best_ = None
        try:
            for fn in os.listdir(d):
                if not fn.endswith(".json"):
                    continue
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    sn = json.load(f)
                st_ = sn.get("stats") or {}
                if int(sn.get("level", 0)) > int(lvl) or not stats:
                    continue
                if any(int(st_.get(k, 0)) > int(stats.get(k, 0)) for k in stats):
                    continue  # los stats nunca bajan: si el guardado tiene más en algo, no es este personaje
                dist = (int(lvl) - int(sn.get("level", 0))) * 10 + sum(int(stats.get(k, 0)) - int(st_.get(k, 0)) for k in stats)
                if dist <= 40 and (best_ is None or dist < best_[0]):
                    best_ = (dist, sn, fn)
        except Exception:
            best_ = None
        c = self.cfg
        L = self.world.learn
        if best_:
            sn = best_[1]
            for k, v in (sn.get("cfg") or {}).items():
                c[k] = v
            for k in self.PJ_KEYS:
                if (sn.get("learn") or {}).get(k) is not None:
                    L[k] = sn["learn"][k]
                else:
                    L.pop(k, None)
            try:
                if sn.get("bolso"):
                    with open(os.path.join(cfgmod.profile_dir(c), "bolso.json"), "w", encoding="utf-8") as f:
                        json.dump(sn["bolso"], f, ensure_ascii=False)
                if sn.get("quests_done") is not None:
                    self.learner.st["quests_done"] = sn["quests_done"]
                    self.learner.save()
            except Exception:
                pass
            self.log(f"🧑 Volvió un personaje que conozco (nivel {sn.get('level')} → {lvl}): le devuelvo su equipo, bolso, misiones y tiempos de pelea.")
        else:
            self.log(f"🧑 Es otro personaje (nivel {lvl}; el anterior era nivel {old_lvl}): lo que aprendí del servidor queda, "
                     f"y lo del personaje arranca de cero (lo del anterior quedó guardado).")
            c["worn"] = {}
            c["owned_items"] = []
            c["need_weapon"] = False
            c["train_skill_until"] = 0
            for k in self.PJ_KEYS:
                L.pop(k, None)
            try:
                os.remove(os.path.join(cfgmod.profile_dir(c), "bolso.json"))
            except Exception:
                pass
            try:
                if self.learner.st and self.learner.st.get("quests_done"):
                    self.learner.st["quests_done"] = {} if isinstance(self.learner.st["quests_done"], dict) else []
                    self.learner.save()
            except Exception:
                pass
        c.setdefault("travel", {})["my_level"] = int(lvl)
        try:
            cfgmod.save(c)
            self.world.save()
        except Exception:
            pass
        for a, v in (("_bag_mem", None), ("_gold", None), ("_pot_est", None), ("_left", {}), ("_gear_t", 0), ("_gear_sig", None),
                     ("_audit_t", 0), ("_quests_sig", None), ("_exp_off", 0), ("_bag_sell", []), ("_worn_seen", 0),
                     ("_hunger", None), ("_gold_rel", None), ("_buy_chk", None), ("_pot_stuck_until", 0), ("_stats_vals", None),
                     ("_stack_names", {}), ("_brain_t", 0)):
            try:
                setattr(self, a, v)
            except Exception:
                pass

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
                self._sheet_extras(im, ws)
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
        if not (1 <= lvl <= 250) or not (0 <= pend <= per * 60):
            self._sheet_fail = getattr(self, "_sheet_fail", 0) + 1
            return None  # lectura que no cierra (un dígito mal leído): no la uso
        if prev and not (0 <= lvl - prev["level"] <= 5):
            # el nivel nunca baja ni salta de a muchos: o leí mal, o es OTRO personaje. Si la misma ficha (nivel y
            # stats) sale igual tres veces seguidas, es otro personaje: lo del anterior no vale para este.
            sig = (lvl,) + tuple(best[n] for n in names)
            pc = getattr(self, "_pj_cand", None)
            self._pj_cand = (sig, pc[1] + 1) if pc and pc[0] == sig else (sig, 1)
            if self._pj_cand[1] < 3:
                self._sheet_fail = getattr(self, "_sheet_fail", 0) + 1
                self._census_t = time.time() - 10 ** 6  # la releo enseguida para confirmar
                return None
            self._pj_cand = None
            self._new_character(lvl, prev["level"], {n: best[n] for n in names}, prev)
            prev = None
        else:
            self._pj_cand = None
            if prev and isinstance(prev.get("stats"), dict):
                # mismo personaje: un stat nunca baja. Si leo menos que la vez anterior, leí mal una cifra (me pasó:
                # STR 60 leído 50 → creía tener 10 puntos sin repartir y abría 'Subir nivel' cada 20 segundos).
                fixed = False
                for n in names:
                    pv = prev["stats"].get(n)
                    if pv is not None and best[n] < pv:
                        best[n] = pv
                        fixed = True
                if fixed:
                    pend = per * (lvl - 1) - (sum(best[n] for n in names) - start)
                    if pend < 0:
                        self._sheet_fail = getattr(self, "_sheet_fail", 0) + 1
                        return None
        sh = {"t": time.time(), "level": lvl, "exp": best.get("EXP"), "next": best.get("NEXT"), "pending": max(0, pend),
              "stats": {n: best[n] for n in names}}
        # 'Exp' es la del nivel actual (vuelve a cero al subir): armo un total que no retrocede para medir exp por hora
        off = getattr(self, "_exp_off", 0)
        if prev and lvl > prev["level"] and prev.get("exp") is not None:
            off += prev["exp"] + (prev.get("next") or 0)
            self._exp_off = off
        tb = self._exp_table()
        if tb.get(lvl) is not None:
            off = self._exp_off = tb[lvl]  # tabla del server: experiencia total al empezar este nivel
            if not sh["next"] and sh["exp"] is not None and tb.get(lvl + 1):
                sh["next"] = max(0, tb[lvl + 1] - tb[lvl] - sh["exp"])  # no leí 'A sig. nivel': lo saco de la tabla
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
        prev_l = int(t.get("my_level", 0) or 0)
        if prev_l and lvl > prev_l + 1 and getattr(self, "_lvl_pend", None) != lvl:
            self._lvl_pend = lvl  # salto de más de un nivel: puede ser mala lectura, espero a leerlo dos veces
            self._census_t = time.time() - 3600  # la vuelvo a leer apenas pueda para confirmarlo
            return sh
        if prev_l and lvl < prev_l:
            # el nivel no baja nunca (el cambio de pj se detecta aparte, por el nombre): una ficha que dice menos es
            # mala lectura (anotó 45, 54 y 55 siendo 60). Solo lo acepto si lo leo igual 3 veces seguidas.
            lp = getattr(self, "_lvl_low", None)
            self._lvl_low = (lvl, (lp[1] + 1) if lp and lp[0] == lvl else 1)
            if self._lvl_low[1] < 3:
                return sh
        else:
            self._lvl_low = None
        if int(t.get("my_level", 0) or 0) != lvl:
            if int(t.get("my_level", 0) or 0) < lvl:
                self.log(f"⬆ Nivel {lvl} (leído en la ficha).")
            t["my_level"] = lvl
            cfgmod.save(self.cfg)
        return sh

    def _sheet_extras(self, im, ws):
        """Con la ventana del personaje abierta: guardo la imagen (la muestra la pestaña Economía) y leo el hambre."""
        try:
            import cv2
            norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
            kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
            es = next((w for w in ws if norm(w[0]).startswith("estad")), None)
            if es:
                x1, y1 = max(0, int(es[1] - 335 * kx)), max(0, int(es[2] - 530 * ky))
                crop = im[y1:int(es[2] + 150 * ky), x1:int(es[1] + 340 * kx)]
                if crop.size:
                    cv2.imwrite(os.path.join(cfgmod.DATA, "ficha.jpg"), crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    self._sheet_img_t = time.time()
            hb = next((w for w in ws if norm(w[0]).startswith("hambre")), None)
            if hb:
                v = next((w for w in ws if abs(w[2] - hb[2]) < 10 * ky and w[1] > hb[1] and "%" in w[0]), None)
                d = "".join(c for c in (v[0] if v else "") if c.isdigit())
                if d and int(d) <= 100:
                    self._hunger = int(d)
                    if self._hunger >= 70 and time.time() - getattr(self, "_hunger_said", 0) > 1800:
                        self._hunger_said = time.time()
                        self.log(f"🍖 Hambre {self._hunger}%: paso por la tienda a comprar comida.")
                        self._shop_last = 0
        except Exception:
            pass

    def sheet_image(self):
        """Última captura de la ventana del personaje, para la pestaña Economía."""
        import base64
        p = os.path.join(cfgmod.DATA, "ficha.jpg")
        try:
            with open(p, "rb") as f:
                b = base64.b64encode(f.read()).decode()
            return {"img": "data:image/jpeg;base64," + b, "age": int(time.time() - os.path.getmtime(p)), "hunger": getattr(self, "_hunger", None)}
        except Exception:
            return {"img": None, "age": None, "hunger": getattr(self, "_hunger", None)}

    def _census(self):
        """Cada tanto, con el pj tranquilo, releo la ficha: es de donde salen nivel, exp por hora y puntos por repartir
        en los servers donde no leo eso de la memoria. True = usé el turno."""
        if cfgmod.server_slug(self.cfg) == "abaddon" or self._mem_struct() or not self.rect:
            return False
        now = time.time()
        # con la barrita de experiencia calibrada ya no necesito abrir la ficha tan seguido (solo nivel y puntos)
        every = (600 if (getattr(self, "_xl", None) or {}).get("len") else 240) if getattr(self, "_sheet_fail", 0) < 3 else 900
        if now - getattr(self, "_census_t", 0) < every or self.state == "attack" or getattr(self, "_busy_ui", False):
            return False
        try:
            ents = self._oly_ents() if self.pos.get("x") is not None else []
            px, py = self.pos.get("x"), self.pos.get("y")
            # forzada (subí de nivel) o hace más de 20 min que no la leo: alcanza con no tener bichos pegados
            forced = (getattr(self, "_census_t", 1) == 0 or now - getattr(self, "_census_t", 0) > 1200) \
                and (self.bars.get("hp") or 0) >= 60
            if any(max(abs(e[0] - px), abs(e[1] - py)) <= (2 if forced else 4) for e in ents or []):
                return False  # con bichos cerca no abro ventanas (si subí de nivel alcanza con que no los tenga pegados)
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
                if pts is not None and exp_pts < 0 < pts:
                    # la cuenta negativa es imposible: leí mal algún stat de las cajitas. Uso lo que dice la ventana;
                    # igual solo aplico si los puntos bajan justo lo que sumé.
                    self.log(f"Stats: leí {pts} puntos (la cuenta da {exp_pts}, imposible: leí mal un stat); uso los de la ventana.")
                else:
                    self.log(f"Stats: leí {pts} puntos, la cuenta da {exp_pts}; uso el menor.")
                    pts = min(p_ for p_ in (pts, exp_pts) if p_ is not None)
        if not pts or pts <= 0:
            click((st[1] + ap[1]) / 2, st[2] - 60, "right")  # nada para repartir: cierro (no la dejo tapando el juego)
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
        try:
            # los stats quedaron confirmados (los puntos bajaron justo lo que sumé): los guardo para el plan de equipo
            self.cfg["stats_last"] = {k: int(v) for k, v in plan_vals.items() if k in ("STR", "VIT", "DEX", "INT", "MAG", "LUK") and v is not None}
            self.cfg["stats_last"]["t"] = time.time()
            cfgmod.save(self.cfg)
        except Exception:
            pass
        self._sal_stats_t = time.time()
        self._stats_fail = 0
        img3 = grab()
        if find_win(img3):
            click((st[1] + ap[1]) / 2, st[2] - 60, "right")  # cierro con click derecho
            time.sleep(0.5)

    def _do_stats(self):
        # la ventana de stats se lee con el lector preciso (RapidOCR, ahora por placa de video): los números de las
        # cajitas con el de Windows salían mal (la cuenta de puntos daba -7). Solo mientras reparto.
        prev = ocr.SAFE
        ocr.SAFE = True
        try:
            with winapi.allow_noclick():
                return self._do_stats_inner()
        finally:
            ocr.SAFE = prev

    def _do_stats_inner(self):
        if cfgmod.server_slug(self.cfg) != "abaddon":
            sh0 = self._sheet_read()
            if not sh0:
                # sin ficha igual puedo repartir: la ventana 'Subir nivel' dice los puntos y los stats, y el reparto
                # solo aplica si los puntos bajan exactamente lo que sumé (si no, Restablecer y no aplica nada)
                self.log("Stats: no pude leer la ficha; reparto leyendo la ventana 'Subir nivel'.")
                try:
                    self._do_stats_ui()
                finally:
                    try:
                        self._stats_win_close()
                    except Exception:
                        pass
                return
            if sh0["pending"] <= 0:
                self._stats_fail = 0
                return  # no hay nada para repartir: ni abro la ventana
            if getattr(self, "_stats_block", None) == (sh0["level"], sh0["pending"]):
                return  # ya probé dos veces con esta misma ficha y no cambió nada: no insisto hasta subir de nivel
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
                        self._stats_noeff = getattr(self, "_stats_noeff", 0) + 1
                        if self._stats_noeff >= 2:
                            self._stats_noeff = 0
                            self._stats_block = (sh1["level"], sh1["pending"])
                            self.log(f"Stats: la ficha dice que quedan {sh1['pending']} puntos pero repartir no cambia nada "
                                     f"(seguro leí mal un número): no vuelvo a probar hasta subir de nivel.")
                        else:
                            self.log(f"Stats: siguen {sh1['pending']} puntos sin repartir; pruebo más tarde.")
                    if dif:
                        self._stats_noeff = 0
                return
            finally:
                self._stats_force = None
                # nunca dejar 'Subir nivel' abierta (tapa el juego): si sigue ahí, click derecho adentro
                try:
                    self._stats_win_close()  # el click derecho de antes caía sobre 'Restablecer' y no la cerraba
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
                    if mk in ex_l:
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
        # fantasmas: el juego deja en memoria bichos que ya no están (muertos hace horas, o de otro mapa). El campo
        # "id" es en realidad el reloj de su última actualización (en milisegundos): los vivos lo tienen al día.
        live_after = None
        try:
            if self.oly.sig.get("id_off") is not None and raw:
                import ctypes
                gtc = int(ctypes.windll.kernel32.GetTickCount()) & 0xFFFFFFFF
                mt = max(int(r_[4]) for r_ in raw)
                off = (gtc - mt) & 0xFFFFFFFF
                tk = self.__dict__.setdefault("_oly_tk", {"off": None})
                if tk["off"] is None or off < tk["off"]:
                    tk["off"] = off  # diferencia entre mi reloj y el del juego, medida cuando hay bichos vivos a la vista
                live_after = ((gtc - tk["off"]) & 0xFFFFFFFF) - int(self.cfg.get("ent_fresh_ms", 6000))
        except Exception:
            live_after = None
        ghosts = 0
        allv, seen_all = [], set()
        mypos = (self.pos.get("x"), self.pos.get("y"))
        for x, y, typ, st, oid in raw:
            if live_after is not None and int(oid) < live_after:
                ghosts += 1
                continue
            if 0 < typ < 500 and (x, y) != (0, 0) and (typ, x, y) not in seen_all and (x, y) != mypos:
                seen_all.add((typ, x, y))
                c_ = crs.get(typ) or generic
                kind_ = "mob" if c_ else ("player" if typ in gridmod.PLAYER_TYPES else "npc")
                allv.append({"x": x, "y": y, "typ": typ, "kind": kind_, "cr": c_,
                             "name": c_["name"] if c_ else ("jugador" if kind_ == "player" else f"npc {typ}")})
            cr = crs.get(typ) or generic
            # el campo "id" de Olympia cambia solo (es un contador): identifico al bicho por tipo y baldosa
            key = (typ, x, y)
            if not cr or key in seen:
                continue
            seen.add(key)
            out.append((x, y, cr, key))
        try:
            self._grid_update(allv, now)
        except Exception as e_:
            if now - getattr(self, "_grid_err_t", 0) > 300:
                self._grid_err_t = now
                self.log(f"Grilla: error {e_!r}")
        self._oly_ghosts = ghosts
        if ghosts and not getattr(self, "_oly_ghost_said", False):
            self._oly_ghost_said = True
            self.log(f"👻 Descarto {ghosts} bichos fantasma de la memoria (ya no están en el mapa): les pegaba al aire.")
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

    def _mode_check(self):
        """Modo ataque / modo paz: el ícono redondo del tablero es dorado en ataque y gris en paz. En paz el clic
        derecho hace una reverencia en vez de pegar. Si está gris, TAB lo pasa a ataque."""
        if cfgmod.server_slug(self.cfg) == "abaddon" or self.img is None or getattr(self, "_busy_ui", False):
            return
        now = time.time()
        if now - getattr(self, "_mode_t", 0) < 3.0 or now < getattr(self, "_mode_wait", 0):
            return
        self._mode_t = now
        H, W = self.img.shape[:2]
        kx, ky = W / 1920.0, H / 1080.0
        roi = self.img[int(1005 * ky):int(1065 * ky), int(1040 * kx):int(1105 * kx)]
        if roi.size == 0:
            return
        b, g, r = [roi[:, :, i].astype(int) for i in range(3)]
        gold = float(((r > 150) & (g > 110) & (b < 110)).mean())
        if float(roi.max()) < 60:
            return  # tablero tapado o pantalla negra: no opino
        self.stats["combat_mode"] = "ataque" if gold >= 0.05 else "paz"
        if gold >= 0.05:
            self._mode_tries = 0
            return
        mp = (self.pos.get("map") or "").lower()
        if any(k in mp for k in ("shop", "blacksmith", "hall", "warehouse")):
            return
        n = getattr(self, "_mode_tries", 0) + 1
        self._mode_tries = n
        if n > 3:
            self._mode_tries = 0
            self._mode_wait = now + 120
            self.log("⚔ El ícono sigue gris después de 3 TAB: no insisto por 2 minutos.")
            return
        winapi.press("TAB")
        self._mode_wait = now + 1.5
        self.log("⚔ Estaba en modo paz (ícono gris): TAB para pasar a modo ataque.")

    def _poison_check(self):
        """Envenenado (la barra de vida dice 'Envenenado'): la tecla 3 (poción verde) cura el veneno."""
        if cfgmod.server_slug(self.cfg) == "abaddon" or self.img is None or getattr(self, "_busy_ui", False):
            return
        now = time.time()
        if now - getattr(self, "_poi_t", 0) < 2.0:
            return
        self._poi_t = now
        H, W = self.img.shape[:2]
        kx, ky = W / 1920.0, H / 1080.0
        try:
            ws = ocr.read_words(self.img, {"x1": int(395 * kx), "y1": int(998 * ky), "x2": int(640 * kx), "y2": int(1038 * ky)}, scale=1.5)
        except Exception:
            return
        if not any("enven" in w[0].lower() or "poison" in w[0].lower() for w in ws):
            return
        if now - getattr(self, "_poi_used", 0) < 6.0:
            return
        self._poi_used = now
        winapi.press("3")
        if now - getattr(self, "_poi_said", 0) > 120:
            self._poi_said = now
            self.log("🧪 Estoy envenenado: tomo la poción verde (tecla 3) para curarme.")

    def _town_block(self):
        """True = todavía no salgo a cazar: tengo que pasar por el pueblo (pociones, reparar, arma). Un jugador que
        revive sin pociones y sin arma no vuelve corriendo al nido."""
        if cfgmod.server_slug(self.cfg) == "abaddon":
            return False
        now = time.time()
        if now > getattr(self, "_town_until", 0):
            return False
        th = getattr(self, "_town_th", None)
        if th is None or not th.is_alive():
            if getattr(self, "_town_done", True):
                return False
            if now - getattr(self, "_town_try", 0) > 20 and not getattr(self, "_busy_ui", False) and not getattr(self, "mission", None):
                for a in ("_shop_th", "_rep_th", "_gear_th", "_audit_th"):
                    t_ = getattr(self, a, None)
                    if t_ is not None and t_.is_alive():
                        return True
                self._town_try = now
                self._town_th = threading.Thread(target=self._town_routine, daemon=True)
                self._town_th.start()
        self.stats["state"] = "en el pueblo: me preparo antes de cazar"
        return True

    def _town_need(self, why):
        if any(k in str(why) for k in ("dejé de matar", "mataron")):
            self._gear_t = 0  # leo qué tengo puesto apenas pueda
        if any(k in str(why) for k in ("dejé de matar", "mataron", "arma")):
            self._worn_suspect_until = time.time() + 900  # puede que el arma se haya salido: reviso una vez
        self._town_done = False
        self._town_until = time.time() + 900
        self._town_why = why

    def _town_routine(self):
        try:
            self.log(f"🏘 Antes de volver a cazar me preparo en el pueblo ({getattr(self, '_town_why', '')}): pociones, reparar y arma.")
            self._release()
            # 1) pociones (la tienda también lee el oro exacto)
            if ((getattr(self, "_left", {}) or {}).get("pot") or 0) < 8:
                self._shop_last = time.time()
                self._oly_shop_trip()
            # 2) herrero: reparar todo
            self._rep_last = time.time()
            self._oly_repair_trip("preparación")
            # 3) bolso: ponerme lo que tenga (arma primero)
            self._audit_t = time.time()
            self._bag_audit()
            # 4) sin arma comprobada: compro la mejor de mi talento que pueda usar y pagar
            if not ((self.cfg.get("worn") or {}).get("arma") or {}).get("ok"):
                it = self._best_weapon_buy()
                if it:
                    self.log(f"🛍 No tengo arma: compro {it['name']} ({it['price']} de oro).")
                    self._gear_last = time.time()
                    self._oly_buy_trip([it])
                    self._bag_audit()
                    if not ((self.cfg.get("worn") or {}).get("arma") or {}).get("ok"):
                        self.__dict__.setdefault("_buy_failed", []).append(it["name"])
                else:
                    self.log("🛍 No tengo arma y no encuentro una de mi talento que pueda usar y pagar.")
            self._town_done = True
            self.log("🏘 Listo: vuelvo a cazar.")
        except Exception as e:
            self._town_done = True
            self.log(f"🏘 Preparación en el pueblo: error {e!r}")
        finally:
            self._busy_ui = False
            self.mission = None

    def _best_weapon_buy(self):
        cat = self._load_json("tiendas.json")
        fam = str(cat.get("talent") or "").strip().lower()
        lvl = self._level_mem() or int(self.cfg.get("travel", {}).get("my_level", 1))
        st_ = int((getattr(self, "_stats_vals", None) or {}).get("STR") or 0)
        if not st_:
            try:
                st_ = int(((self._sheet(7 * 24 * 3600) or {}).get("stats") or {}).get("STR") or 0)
            except Exception:
                st_ = 0
        if not st_:
            return None  # sin saber mi fuerza no compro un arma que capaz no puedo levantar
        gold = getattr(self, "_gold", None) or 0
        ok = [i for i in cat.get("items", []) if str(i.get("family") or "").lower() == fam and i.get("confirmed")
              and i.get("shop") == "Blacksmith" and int(i.get("level") or 0) <= lvl and int(i.get("hands") or 1) == 1
              and int(i.get("str") or 0) <= st_ and int(i.get("price") or 0) <= gold - 200
              and i["name"] not in getattr(self, "_buy_failed", [])]
        return max(ok, key=lambda i: (float(i.get("avg") or 0), -int(i.get("price") or 0))) if ok else None

    def _reflex(self):
        """REGLA DE ORO (vale para todo modo, misión u orden, en cualquier juego): nunca me quedo quieto mientras
        me pegan. Si tengo un bicho pegado y nadie le está pegando, le pego YO, sea cual sea (aunque lo haya
        descartado por élite, por peligroso o porque iba de viaje). Si me castiga mucho, me escapo lejos."""
        if cfgmod.server_slug(self.cfg) == "abaddon" or self.pos.get("x") is None or self.img is None:
            return False
        if time.time() - self.pos.get("t", 0) > 5 or getattr(self, "_kite", None) is not None:
            return False
        now = time.time()
        if self._under_attack():
            self._hit_t = now
        ents = self._oly_ents() or []
        px, py = self.pos["x"], self.pos["y"]
        d = lambda e: max(abs(e[0] - px), abs(e[1] - py))
        adj = [e for e in ents if d(e) <= 1]
        if not adj:
            # me pegan y no veo quién: pego alrededor, pero solo con una baja de vida de verdad (no el ruido de la
            # barra) y como mucho 3 veces cada medio minuto: no me quedo clavado pegándole al aire
            h_ = [v for t, v in getattr(self, "_hp_hist", []) if now - t < 3]
            real = len(h_) >= 2 and max(h_) - h_[-1] >= 8
            dl = [t for t in getattr(self, "_rfx_defs", []) if now - t < 30]
            if real and not any(d(e) <= 3 for e in ents) and len(dl) < 3 and (not dl or now - dl[-1] > 2.0):
                self._rfx_defs = dl + [now]
                self._defend()
                return True
            return False
        # si estoy yendo a algún lado (viaje al pueblo, encargo, huida) no me freno a pelear: caminar no es quedarse
        # quieto. Solo peleo si me tienen trabado (no avancé en 2,5 s); si no, del nido no saldría nunca.
        pp = getattr(self, "_rfx_pos", None)
        if not pp or pp[0] != (px, py):
            self._rfx_pos = pp = ((px, py), now)
        try:
            going = bool(getattr(self, "mission", None)) or self._is_travelling() or bool(self.nav.get("walking"))
        except Exception:
            going = False
        if going and now - pp[1] < 2.5:
            return False
        lg_ = getattr(self, "_loot_go", None)
        if lg_ and lg_.get("blind") and now - lg_["t"] < 2.5 and (self.bars.get("hp") or 100) >= 45:
            return False  # estoy dando el paso para levantar lo que cayó: vuelvo a pegar enseguida
        hp = self.bars.get("hp")
        st = self.__dict__.setdefault("_rfx", {"t0": now, "hp0": hp, "last": 0})
        if now - st["last"] > 4:
            st.update(t0=now, hp0=hp)
        st["last"] = now
        lost = (st["hp0"] - hp) if (hp is not None and st["hp0"] is not None) else 0
        hard = hp is not None and (hp < 30 or (lost >= 45 and now - st["t0"] < 20) or (now - st["t0"] > 45 and hp < 50))
        if hard and now >= getattr(self, "_flee_block", 0) and now - getattr(self, "_rfx_fled", 0) > 30:
            self._rfx_fled = now
            st.update(t0=now, hp0=hp)
            self.log(f"🏃 Me están castigando (vida {hp:.0f}%, perdí {max(0, lost):.0f} en {now - st['t0']:.0f} s): me escapo lejos.")
            try:
                return bool(self._oly_kite(ents, force=True))
            except Exception:
                return False
        if now - getattr(self, "_atk_t", 0) < 1.2:
            return False  # ya le estoy pegando
        cur = getattr(self, "_oly_cur", None) or {}
        e = next((o for o in adj if o[3] == cur.get("id")), None) or min(adj, key=d)
        cx, cy = self._char()
        tw, th = self._tile()
        self._release()
        winapi.click(*self.abs_pt(int(cx + (e[0] - px) * tw), int(cy + (e[1] - py) * th)), button="right")
        self._atk_t = self._last_enemy = now
        self.state = "seek"
        self.stats["state"], self.stats["target"] = "me pegan: me defiendo", e[2]["name"]
        self._trk_draw = {"x": int(cx + (e[0] - px) * tw), "y": int(cy + (e[1] - py) * th), "tw": tw, "th": th, "name": e[2]["name"], "t": now, "d": 1}
        time.sleep(0.3)
        return True

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
        pots_k = (getattr(self, "_left", {}) or {}).get("pot") or 0
        dmg_k = (sum(float(e[2].get("dmg_avg") or 3.0) for e in adj) / len(adj)) if adj else None
        cap = self._group_cap(dmg=dmg_k) if adj else 2
        # RABIA: rodeado (3+ pegados) no se sale caminando: darles la espalda es comer golpes gratis. Me planto y
        # les pego a todos los de alrededor, uno por uno hasta que caiga, tomando pociones. Solo me escapo si no
        # me quedan pociones y la vida está por el piso.
        # enjambre que ya sé que me mata: si en 3 baldosas hay más de los que aprendí a aguantar y todavía no me
        # cerraron (menos de 3 pegados), me voy YA. Plantarse sirve contra un grupo, no contra el nido entero.
        try:
            near3 = [e for e in ents if dist(e) <= 3]
            if near3 and len(adj) < 3 and getattr(self, "_kite", None) is None and now > getattr(self, "_swarm_t", 0):
                from collections import Counter
                nm_, n_ = Counter(e[2]["name"] for e in near3).most_common(1)[0]
                lvl_ = self._level_mem() or int(self.cfg.get("travel", {}).get("my_level", 1))
                cap_l = self.learner.crowd_cap(nm_, lvl_)
                if cap_l and n_ > cap_l:
                    self._swarm_t = now + 20
                    self._kite_block = 0
                    self.log(f"🏃 {n_} {nm_} juntos (aprendí que más de {cap_l} me matan): me voy antes de que me rodeen.")
                    force = True
        except Exception:
            pass
        if len(adj) >= 3 and not (hp is not None and hp < 22 and pots_k < 1):
            if now > getattr(self, "_rabia_until", 0) and now - getattr(self, "_rabia_said", 0) > 30:
                self._rabia_said = now
                self.log(f"😡 Rodeado por {len(adj)}: me planto y les pego a todos los de alrededor.")
            self._rabia_until = now + 6
            self._kite = None
            self._kite_block = max(getattr(self, "_kite_block", 0), now + 8)
            self._flee_block = max(getattr(self, "_flee_block", 0), now + 8)
            self._flee_until = 0
            return False
        k = getattr(self, "_kite", None)
        if k is not None and adj:
            # rodeado y sin poder salir (el camino está tapado): huir es comer golpes de espalda. Peleo.
            lp = k.get("lp")
            if lp != (px, py):
                k["lp"], k["lpt"] = (px, py), now
            elif now - k.get("lpt", now) > 2.5:
                self._kite = None
                self._kite_block = now + 20
                # decidido: peleo 20 s seguidos. Ir y venir cada 3 s es regalar golpes sin pegar ninguno.
                self._flee_block = max(getattr(self, "_flee_block", 0), now + 20)
                self._flee_until = 0
                self.log(f"🗡 No puedo salir ({len(adj)} encima): peleo en vez de huir.")
                return False
        if k is None and now < getattr(self, "_kite_block", 0) and not force:
            return False
        if k is None:
            low = hp is not None and adj and hp < (30 if pots_k >= 1 else 45)
            if not (force or low or len(adj) > cap):
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

    def _grid(self):
        g = getattr(self, "grid", None)
        if g is None:
            g = self.grid = gridmod.Grid()
        return g

    def _grid_mode(self):
        """En qué estoy: eso decide cómo leo la grilla."""
        cur = (self.pos.get("map") or "").lower()
        if getattr(self, "_busy_ui", False) or any(k in cur for k in ("shop", "blacksmith", "hall", "warehouse", "church", "tower")):
            return "local"
        if getattr(self, "_rest_since", 0) or getattr(self, "_kite", None):
            return "descanso"
        if getattr(self, "_loot_go", None):
            return "recolección"
        if (getattr(self, "goto", None) or getattr(self, "mission", None) or self._is_travelling() or self._in_town()
                or getattr(self, "_hot_go", None) or getattr(self, "_pit_go", None)):
            return "viaje"
        mk = cur.strip()
        if not (self.world.learn.get("oly_hot") or {}).get(mk):
            return "zona nueva"
        return "nido"

    def _grid_update(self, allv, now):
        """Refresco la grilla con lo vivo de esta lectura y saco las consecuencias: muertes (un bicho que seguía de
        cerca dejó de estar vivo) y dónde cayó su botín."""
        if self.pos.get("x") is None:
            return
        g = self._grid()
        g.update((self.pos["x"], self.pos["y"]), (self.pos.get("map") or "").strip().lower(), allv, now)
        g.mode = self._grid_mode()
        self.stats["mode"] = g.mode
        adj = sorted({tr.name for tr in g.tracks.values() if tr.dist(g.me) <= 1})
        if adj:
            self._last_adj = (adj, now)
        try:
            self._risk_track(adj, now)
        except Exception:
            pass
        for tr in g.lost:
            if now - tr.near_t < 3.0 and self.running:
                self.stats["kills"] = self.stats.get("kills", 0) + 1
                self._last_kill_t = now
                self._oly_loot = (tr.x, tr.y, now)
                self._kill_pick = (tr.x, tr.y, now, tr.name)
                # anotada (como en Abaddon): la baldosa de cada muerte queda en la lista para pasar a levantar
                kq_ = [k_ for k_ in getattr(self, "_kill_q", []) if now - k_[2] < 120 and (k_[0], k_[1]) != (tr.x, tr.y)]
                self._kill_q = (kq_ + [(tr.x, tr.y, now, tr.name)])[-14:]
                try:
                    self._zone_kill()
                except Exception:
                    pass
                self.log(f"✝ {tr.name} muerto (kill #{self.stats['kills']}).")
                try:
                    if getattr(tr, "adj_since", None):
                        self._hit_pace(tr.name, now - tr.adj_since, now)
                except Exception:
                    pass
                try:
                    self._risk_rec(tr.name).__setitem__("k", self._risk_rec(tr.name)["k"] + 1)
                    self._risk_judge(tr.name)
                except Exception:
                    pass

    # ---------- MODOS (botones debajo del encendido) ----------
    MODES = {"auto": "Automático", "caza": "Caza", "manual": "Manual", "quest": "Quest", "economico": "Económico",
             "cartografo": "Cartógrafo"}

    def bot_mode(self):
        m = self.cfg.get("bot_mode") or "auto"
        return m if m in self.MODES else "auto"

    def set_mode(self, m, why=""):
        """Cambia el modo. Automático = el de siempre. Los demás cambian prioridades (ver _director/_nest_decide):
        Caza: el nido que más rinde, sin misiones, solo sale a reparar/pociones y vuelve. Manual: el nido donde lo
        dejaste (lo recuerda). Quest: solo bichos de la misión hasta cobrar, después la siguiente; sin misiones vuelve
        a Automático. Económico: el nido que más oro por hora deja. Cartógrafo: va a los mapas vecinos que conoce y
        los releva; si lo matan vuelve a Automático."""
        m = m if m in self.MODES else "auto"
        prev = self.bot_mode()
        self.cfg["bot_mode"] = m
        if m == "manual" and self.pos.get("x") is not None:
            mk = (self.pos.get("map") or "").strip().lower()
            self.cfg["mode_anchor"] = {"mk": mk, "x": int(self.pos["x"]), "y": int(self.pos["y"])}
            self.world.learn.setdefault("oly_hot", {})[mk] = {"x": int(self.pos["x"]), "y": int(self.pos["y"]), "t": time.time(), "exp": 0}
        self._nest_probe = None
        self._mode_since = time.time()
        cfgmod.save(self.cfg)
        if m != prev:
            self.log(f"🎛 Modo {self.MODES[m]}" + (f" ({why})" if why else "") + "."
                     + (f" Mi nido: {self.pos.get('map')} ({self.pos.get('x')},{self.pos.get('y')})." if m == "manual" else ""))
        return {"ok": True, "mode": m, "label": self.MODES[m]}

    def _mode_tick(self):
        """Lo que cada modo hace por su cuenta (cada 30 s, barato)."""
        now = time.time()
        if now - getattr(self, "_mt_t", 0) < 30:
            return
        self._mt_t = now
        m = self.bot_mode()
        mk = (self.pos.get("map") or "").strip().lower()
        if m == "manual":
            a = self.cfg.get("mode_anchor") or {}
            if a.get("mk") == mk:
                self.world.learn.setdefault("oly_hot", {})[mk] = {"x": a["x"], "y": a["y"], "t": now, "exp": 0}
        elif m == "quest":
            qs = getattr(self, "quests", None)
            if qs is not None and not qs:
                if not getattr(self, "_mq_empty", 0):
                    self._mq_empty = now
                elif now - self._mq_empty > 300:
                    self._mq_empty = 0
                    self.set_mode("auto", "no quedan misiones")
            else:
                self._mq_empty = 0
        elif m == "cartografo":
            if getattr(self, "goto", None) or getattr(self, "_exploring", False) or self._in_town() is False and \
                    not ((self._survey().get("explored") or {}).get(self.pos.get("map") or "") or {}).get("done", False):
                return  # viajando, relevando o en un mapa que todavía no relevé (lo releva solo al pisarlo)
            ex = self._survey().get("explored") or {}
            cur = self.pos.get("map") or ""
            cand = []
            for (_xy, v) in self.world.links_from(cur) if cur else []:
                to = v.get("to") or ""
                if to and not (ex.get(to) or {}).get("done") and not any(k in to.lower() for k in self.TOWNS):
                    cand.append(to)
            for k_, v in (self.world.learn.get("links") or {}).items():
                to = v.get("to") or ""
                if to and to not in cand and not (ex.get(to) or {}).get("done") and not any(k in to.lower() for k in self.TOWNS):
                    try:
                        if self._known_route(cur, to, self._avoid()):
                            cand.append(to)
                    except Exception:
                        pass
            if not cand:
                self.set_mode("auto", "no quedan mapas conocidos sin relevar")
                return
            to = cand[0]
            mp = self.world.map_for(to)
            g = mp.nearest_walkable(mp.w // 2, mp.h // 2, r=12) if mp else None
            self.goto = {"map": to, "x": int(g[0]) if g else 0, "y": int(g[1]) if g else 0, "t": now}
            self.log(f"🗺 Cartógrafo: voy a {to} a relevarlo.")

    # ---------- rendimiento por nido: experiencia por hora REAL (con caminar y descansar) ----------
    def _nest_rec(self, mk, x, y, mob=None):
        d = self.world.learn.setdefault("nest_rate", {}).setdefault(mk, {})
        r = d.setdefault(f"{int(x)},{int(y)}", {"exp": 0, "secs": 0.0})
        if mob:
            r["mob"] = mob
        return r

    def _nest_rate(self, r, key=None):
        """Experiencia (o con key='gold', oro) por hora de un nido, o None si no lo medí al menos 10 minutos.
        Sin key: el modo Económico mide oro, los demás experiencia."""
        if not r or float(r.get("secs", 0)) < 600:
            return None
        key = key or ("gold" if self.bot_mode() == "economico" else "exp")
        return 3600.0 * float(r.get(key, 0)) / float(r["secs"])

    def _nest_track(self):
        """Cada 20 s le cargo el tiempo al nido en el que estoy (o al que estoy yendo: caminar hasta ahí es parte del
        costo de elegirlo). La experiencia se le suma al ganarla (_hot_note). El tiempo en el pueblo no cuenta
        (es de todos). Cada hora reviso si me conviene cambiar de nido (_nest_decide)."""
        if cfgmod.server_slug(self.cfg) == "abaddon" or not self.running or self.pos.get("x") is None:
            return
        now = time.time()
        if now - getattr(self, "_nt_t", 0) < 20:
            return
        dt = min(60.0, now - getattr(self, "_nt_t", now)) if getattr(self, "_nt_t", 0) else 0.0
        self._nt_t = now
        mk = (self.pos.get("map") or "").strip().lower()
        if self._in_town() or getattr(self, "_busy_ui", False) or self._ui_th_alive():
            return  # pueblo/tienda: no es tiempo de ningún nido
        px, py = self.pos["x"], self.pos["y"]
        pits = [p for p in self._pits() if (p.get("mob") or p.get("guess"))]
        pg = getattr(self, "_pit_go", None)
        act = None
        if pg and pg.get("p"):
            act = pg["p"]  # voy hacia este nido: el camino se lo cobro a él
        else:
            near = [p for p in pits if max(abs(p["x"] - px), abs(p["y"] - py)) <= 18]
            if near:
                act = min(near, key=lambda p: max(abs(p["x"] - px), abs(p["y"] - py)))
        if act:
            self._nest_active = {"mk": mk, "x": act["x"], "y": act["y"], "mob": act.get("mob") or act.get("guess")}
        na = getattr(self, "_nest_active", None)
        try:
            g_ = self._gold_mem()
        except Exception:
            g_ = None
        g0 = getattr(self, "_nt_gold", None)
        self._nt_gold = g_
        if na and na["mk"] == mk and dt > 0:
            r = self._nest_rec(mk, na["x"], na["y"], na.get("mob"))
            if g_ is not None and g0 is not None and 0 < g_ - g0 < 50000:
                r["gold"] = int(r.get("gold", 0)) + (g_ - g0)  # oro levantado acá (lo de vender es del pueblo)
            r["secs"] = float(r.get("secs", 0)) + dt
            r["t"] = now
            if r["secs"] > 7200:  # memoria móvil: pesa más lo reciente (el pj crece, los nidos cambian)
                r["secs"] *= 0.5
                r["exp"] = int(r.get("exp", 0) * 0.5)
                r["gold"] = int(r.get("gold", 0) * 0.5)
        self._nest_decide(mk, pits, now)

    def _nest_decide(self, mk, pits, now):
        """Cada hora: si hay un nido que nunca medí (o con medición vieja), lo pruebo 15 min. Al terminar la prueba
        (o si ya están todos medidos) me quedo con el que más experiencia por hora da de los que aguanto, y ese
        pasa a ser mi zona buena. La misión activa le suma a su bicho. Para cualquier pj y mapa."""
        if now - getattr(self, "_nd_t", 0) < 60:
            return
        self._nd_t = now
        md_ = self.bot_mode()
        if md_ in ("manual", "cartografo"):
            return  # Manual: el nido lo eligió Juan. Cartógrafo: no caza en nidos, recorre.
        recs = (self.world.learn.get("nest_rate") or {}).get(mk) or {}
        key_ = "gold" if md_ == "economico" else "exp"
        tried = self.__dict__.setdefault("_pit_tried", {})
        ok = []
        for p in pits:
            nm = p.get("mob") or p.get("guess")
            try:
                if not self._can_face(nm) or self._pit_deadly(p) or tried.get((p["x"], p["y"]), 0) > now:
                    continue
            except Exception:
                continue
            ok.append((p, nm, recs.get(f"{int(p['x'])},{int(p['y'])}")))
        if not ok:
            return
        pr = getattr(self, "_nest_probe", None)
        if pr:
            r = recs.get(pr["k"])
            done = (r and float(r.get("secs", 0)) - pr["s0"] >= 900) or now - pr["t0"] > 1800
            if not done:
                return
            self._nest_probe = None
            rt = self._nest_rate(r)
            self.log(f"🔎 Terminé de probar el nido de {pr['mob']}: "
                     + (f"{int(rt):,}".replace(",", ".") + " de experiencia por hora." if rt is not None else "no llegué a medirlo.")
                     + " Comparo con los otros y me quedo con el que más rinde.")
            self.world.learn.setdefault("oly_hot", {}).pop(mk, None)  # que la comparación de abajo elija de cero
        elif now - getattr(self, "_nest_probe_t", 0) > 60 * float(self.cfg.get("nest_probe_min", 60)) and not (
                md_ == "caza" and any(self._nest_rate(r_) is not None for _p, _n, r_ in ok)):  # Caza: ya tengo uno medido, me quedo
            unk = [(p, nm, r) for p, nm, r in ok if self._nest_rate(r) is None or now - float((r or {}).get("t", 0)) > 6 * 3600]
            if unk:
                px, py = self.pos["x"], self.pos["y"]
                p, nm, r = min(unk, key=lambda c: max(abs(c[0]["x"] - px), abs(c[0]["y"] - py)))
                k = f"{int(p['x'])},{int(p['y'])}"
                self._nest_probe = {"k": k, "mob": nm, "t0": now, "s0": float((r or {}).get("secs", 0))}
                self._nest_probe_t = now
                self._pit_go = {"k": (p["x"], p["y"]), "t": now, "d": 999, "p": p, "nm": nm}
                (self.world.learn.setdefault("oly_hot", {}))[mk] = {"x": int(p["x"]), "y": int(p["y"]), "t": now, "exp": 0}
                self.log(f"🔎 Pasó una hora: pruebo 15 min el nido de {nm} ({p['x']},{p['y']}) para ver si rinde más.")
                return
            self._nest_probe_t = now
        # elijo el mejor medido
        qm = set()
        try:
            qm = self._quest_mob_names() or set()
        except Exception:
            pass
        best = None
        for p, nm, r in ok:
            rt = self._nest_rate(r)
            if rt is None:
                continue
            sc = rt * (1.5 if nm in qm else 1.0)
            if best is None or sc > best[0]:
                best = (sc, p, nm, rt)
        if not best:
            return
        _sc, p, nm, rt = best
        hot = self.world.learn.setdefault("oly_hot", {})
        h = hot.get(mk)
        cur = None
        if h:
            cur = next(((pp, nn, rr) for pp, nn, rr in ok if max(abs(pp["x"] - h["x"]), abs(pp["y"] - h["y"])) <= 18), None)
        cur_rt = self._nest_rate(cur[2]) if cur else None
        if cur and cur[0] is p:
            return
        if cur_rt is not None and rt < 1.1 * cur_rt:
            return  # no cambio por menos de 10%
        hot[mk] = {"x": int(p["x"]), "y": int(p["y"]), "t": now, "exp": 0}
        self._pit_go = {"k": (p["x"], p["y"]), "t": now, "d": 999, "p": p, "nm": nm}
        f_ = lambda v: f"{int(v):,}".replace(",", ".")
        self.log(f"📊 Zona: el nido de {nm} ({p['x']},{p['y']}) da {f_(rt)} de experiencia por hora"
                 + (f" contra {f_(cur_rt)} del de {cur[1]}" if cur_rt is not None else "") + ": voy ahí.")

    def _rinde(self):
        """¿El lugar rinde? Mato seguido (5+ en 10 min) y la vida nunca bajó del 20% en esos 10 min. Si rinde, ni
        las pociones ni el bolso lleno son motivo para cortar: sigo cazando y juntando oro (regla de Juan). Solo
        cuento lo que pasó acá, con al menos 10 min de muestra."""
        now = time.time()
        kh = (getattr(self, "_sal", None) or {}).get("kills") or []
        k10 = sum(1 for t in kh if now - t < 600)
        hh = [v for t, v in getattr(self, "_hp_hist", []) if now - t < 600]
        if len(hh) < 300:
            return False
        return k10 >= int(self.cfg.get("rinde_kills_10m", 5)) and min(hh) > float(self.cfg.get("rinde_hp_min", 20))

    def _director(self):
        """DIRECTOR de tareas de fondo (por módulos, no en fila): cada vuelta ordena los módulos por cuánto se
        necesitan ahora (+ un plus por tiempo sin turno, así ninguno queda olvidado), les da turno en ese orden y
        corta cuando uno toma el control o se acaba el tiempo de la vuelta (1,5 s): lo que no entró va primero en la
        próxima. Mide cuánto tarda cada uno y avisa si alguno se cuelga. True = un módulo tomó el control."""
        now = time.time()
        sm = None
        try:
            sm = self._supplies_mem()
        except Exception:
            pass
        pots = (sm or {}).get("pot")
        if pots is None:
            pots = (getattr(self, "_left", {}) or {}).get("pot")
        items = (sm or {}).get("items") or 0
        # con comida en el bolso el hambre se arregla comiendo, no yendo a la tienda
        hungry = (getattr(self, "_hunger", None) or 0) >= int(self.cfg.get("eat_above", 50)) and not ((sm or {}).get("meat") or 0)
        low_pots = pots is not None and pots <= int(self.cfg.get("buy_pots_below", 3))
        urgent_shop = low_pots or items >= 46 or hungry
        rinde = False
        try:
            rinde = self._rinde()
        except Exception:
            pass
        if rinde and not hungry:
            urgent_shop = False
        mods = [
            ("ventanas", 50, self._close_junk_windows),
            ("misión cobrar/aceptar", 45, self._quest_step),
            ("encargo", 45, self._mission_step),
            # si el lugar rinde (mato seguido y la vida no baja del 20%), la tienda espera: ni pociones ni bolso
            # lleno cortan la caza. Con hambre o en 0 pociones sí voy.
            ("tienda", 80 if urgent_shop else (0 if rinde else 20), self._maybe_shop),
            ("herrero", 30, self._maybe_repair),
            ("chequeo", 30, self._selfcheck),
            ("estrategia", 25, self._strategy),
            ("equipo", 20, self._gear_check),
            ("bolso", 15, self._bag_order_check),
            ("auditoría", 15, self._maybe_audit),
            ("plan", 10, self._brain_step),
            ("mejoras", 20, self._maybe_gear),
            ("nidos", 10, self._pits_scan),
        ]
        last = self.__dict__.setdefault("_dir_last", {})
        st = self.__dict__.setdefault("_dir_stats", {})
        # modos (más adelante): cada modo sube o baja pesos, ej. {'misión cobrar/aceptar': +40}
        w = dict(self.cfg.get("mode_weights") or {})
        md_ = self.bot_mode()
        if md_ == "quest":
            w["misión cobrar/aceptar"] = w.get("misión cobrar/aceptar", 0) + 40
        elif md_ in ("caza", "manual", "economico", "cartografo"):
            w["misión cobrar/aceptar"] = -1000  # en estos modos no persigo misiones
            w["nidos"] = -1000 if md_ == "manual" else w.get("nidos", 0)
        score = lambda m: m[1] + float(w.get(m[0], 0)) + min(30.0, (now - last.get(m[0], now - 60)) / 10.0)
        ranked = sorted(mods, key=lambda m: -score(m))
        self._dir_view = {"t": now, "rinde": rinde, "orden": [(m[0], int(m[1]), round(score(m), 1)) for m in ranked], "turno": None}
        t0 = time.time()
        for name, _need, fn in ranked:
            if _need <= 0 or _need + float(w.get(name, 0)) <= 0:
                continue  # no hace falta ahora (ej. la tienda mientras el lugar rinde)
            if time.time() - t0 > 1.5:
                break  # se acabó la vuelta: lo que falta va primero la próxima (tiene más tiempo sin turno)
            ts = time.time()
            try:
                r = fn()
            except Exception as e_:
                r = None
                if now - st.get(name + "!err", 0) > 600:
                    st[name + "!err"] = now
                    self.log(f"⚙ Módulo '{name}': error {e_!r}")
            dt = time.time() - ts
            last[name] = time.time()
            rec = st.setdefault(name, {"n": 0, "tot": 0.0, "max": 0.0})
            rec["n"] += 1
            rec["tot"] += dt
            rec["max"] = max(rec["max"], dt)
            if dt > 8 and now - st.get(name + "!slow", 0) > 900:
                st[name + "!slow"] = now
                self.log(f"⏱ Módulo '{name}' tardó {dt:.0f} s (frena la caza).")
            if r is True:
                self._dir_view["turno"] = name
                return True
        return False

    def brain_view(self):
        """Para la pestaña 'Cerebro' de la interfaz: el director (qué módulo va primero y por qué) y el tablero de
        salud. Solo lee; no toca el juego."""
        now = time.time()
        dv = getattr(self, "_dir_view", None) or {}
        st = getattr(self, "_dir_stats", {}) or {}
        last = getattr(self, "_dir_last", {}) or {}
        mods = []
        for name, need, sc in dv.get("orden", []):
            r = st.get(name) or {}
            mods.append({"name": name, "need": need, "score": sc, "n": r.get("n", 0),
                         "avg_ms": int(1000 * r["tot"] / r["n"]) if r.get("n") else None,
                         "max_s": round(r.get("max", 0), 1), "ago": int(now - last[name]) if name in last else None,
                         "err": bool(st.get(name + "!err")), "slow": bool(st.get(name + "!slow"))})
        try:
            sal = json.load(open(os.path.join(cfgmod.DATA, "salud.json"), encoding="utf-8"))
        except Exception:
            sal = None
        fighting = False
        try:
            fighting = self._adjacent_mobs() > 0 or self._under_attack() or now < getattr(self, "_rabia_until", 0)
        except Exception:
            pass
        nests = []
        try:
            mk = (self.pos.get("map") or "").strip().lower()
            hot = (self.world.learn.get("oly_hot") or {}).get(mk) or {}
            for k, r in ((self.world.learn.get("nest_rate") or {}).get(mk) or {}).items():
                x_, y_ = (int(v) for v in k.split(","))
                nests.append({"mob": r.get("mob") or "?", "x": x_, "y": y_, "rate": self._nest_rate(r),
                              "min": int(float(r.get("secs", 0)) / 60),
                              "zona": bool(hot) and max(abs(hot.get("x", -99) - x_), abs(hot.get("y", -99) - y_)) <= 18})
            nests.sort(key=lambda n: -(n["rate"] or -1))
        except Exception:
            pass
        pr = getattr(self, "_nest_probe", None)
        nxt = getattr(self, "_nest_probe_t", 0)
        return {"nests": nests, "probe": (pr or {}).get("mob"),
                "next_probe_min": int(max(0, 60 * float(self.cfg.get("nest_probe_min", 60)) - (now - nxt)) / 60) if nxt else None,
                "running": self.running, "age": int(now - dv["t"]) if dv.get("t") else None, "turno": dv.get("turno"),
                "rinde": dv.get("rinde"), "peleando": fighting, "estado": self.stats.get("state"), "mods": mods, "salud": sal}

    def _salud(self):
        """TABLERO DE SALUD: cada minuto miro si las funciones básicas se están cumpliendo y, si una no, lo digo
        FUERTE en el log (🚨) y en data/salud.json. Nada falla en silencio (los stats estuvieron apagados horas sin
        que nadie se enterara). Solo lee contadores y memoria: no clickea ni abre nada. Para cualquier pj."""
        now = time.time()
        if now - getattr(self, "_sal_t", 0) < 60 or not self.running:
            return
        self._sal_t = now
        st = self.__dict__.setdefault("_sal", {"t0": now})
        k = int(self.stats.get("kills", 0) or 0)
        d = int(self.stats.get("deaths", 0) or 0)
        kh = st.setdefault("kills", []); dh = st.setdefault("deaths", [])
        if k != st.get("lk", k):
            kh.append(now)
        if d > st.get("ld", d):
            dh.append(now)
            if self.bot_mode() == "cartografo":
                self.set_mode("auto", "me mataron explorando")
        st["lk"], st["ld"] = k, d
        kh[:] = [t for t in kh if now - t < 3600][-500:]
        dh[:] = [t for t in dh if now - t < 3600]
        lvl = int((self.cfg.get("travel") or {}).get("my_level") or 0)
        if lvl != st.get("lvl"):
            if st.get("lvl") and lvl > st["lvl"]:
                st["lvl_up_t"] = now
            st["lvl"] = lvl
        stuck = [t for t in getattr(self, "_sal_stuck", []) if now - t < 600]
        self._sal_stuck = stuck
        try:
            sm = self._supplies_mem()
        except Exception:
            sm = None
        st_name = str(self.stats.get("state") or "")
        town = any(w in st_name for w in ("compr", "repar", "vend", "tienda", "herrer", "yendo a"))
        alerts = []
        up = st.get("lvl_up_t", 0)
        if up and now - up > 1800 and getattr(self, "_sal_stats_t", 0) < up:
            alerts.append(("stats", f"subí de nivel hace {int((now - up) / 60)} min y no repartí los puntos"))
        try:
            rinde = self._rinde()
        except Exception:
            rinde = False
        if sm and sm.get("items", 0) >= 48 and not rinde and now - st["t0"] > 1200 and now - getattr(self, "_sal_sold_t", 0) > 1800:
            alerts.append(("bolso", f"bolso lleno ({sm['items']}/50) hace rato y no vendí nada"))
        if now - st["t0"] > 900 and not town and not any(now - t < 600 for t in kh):
            alerts.append(("kills", "hace 10 min que no mato nada"))
        if len([t for t in dh if now - t < 3600]) >= 2:
            alerts.append(("muertes", f"me mataron {len([t for t in dh if now - t < 3600])} veces en la última hora"))
        if len(stuck) >= 5:
            alerts.append(("trabado", f"me trabé {len(stuck)} veces en 10 min"))
        if sm and sm.get("pot") == 0 and not town and not rinde:
            alerts.append(("pociones", "estoy cazando sin pociones"))
        said = st.setdefault("said", {})
        for key, msg in alerts:
            if now - said.get(key, 0) > 900:
                said[key] = now
                self.log(f"🚨 SALUD: {msg}.")
        try:
            with open(os.path.join(cfgmod.DATA, "salud.json"), "w", encoding="utf-8") as f:
                json.dump({"t": time.strftime("%H:%M:%S"), "ok": not alerts, "alertas": [m for _, m in alerts],
                           "kills_10m": sum(1 for t in kh if now - t < 600), "kills_1h": len(kh),
                           "muertes_1h": len(dh), "trabadas_10m": len(stuck), "nivel": lvl,
                           "bolso": (sm or {}).get("items"), "pociones": (sm or {}).get("pot"),
                           "oro": self._gold_mem(), "estado": st_name}, f, ensure_ascii=False)
        except Exception:
            pass

    def _stats_win_close(self, words=None):
        """Cierra la ventana 'Subir nivel' si está abierta: primero 'Restablecer' (deshace lo pendiente, nada se
        aplica), después click derecho en la fila 'Puntos por asignar' (zona vacía de la ventana; así la cerraba el
        reparto de stats cuando andaba bien). Verifica; si sigue, ESC. True = la vi y la cerré."""
        norm = lambda t: "".join(c for c in str(t).lower() if c.isalnum())

        kx_, ky_ = self.rect[2] / 1920.0, self.rect[3] / 1080.0

        def find(ws):
            # 'Aplicar' + ('Restablecer' a su izquierda, o las pestañas 'Planificador'/'Talentos' o 'Puntos por
            # asignar' arriba). Un cartelito de ayuda puede tapar 'Restablecer' (pasó): ahí lo ubico desde 'Aplicar'.
            ap = next((w for w in ws if norm(w[0]) == "aplicar"), None)
            if not ap:
                return None
            rb = next((w for w in ws if norm(w[0]).startswith("restablecer") and abs(w[2] - ap[2]) < 30 * ky_
                       and 0 < ap[1] - w[1] < 400 * kx_), None)
            top = next((w for w in ws if norm(w[0]).startswith(("planificador", "talentos", "puntos", "asignar"))
                        and 450 * ky_ < ap[2] - w[2] < 720 * ky_ and abs(w[1] - ap[1]) < 1000 * kx_), None)
            if rb:
                return rb, ap
            if top:
                return (None, ap[1] - 162 * kx_, ap[2]), ap
            return None

        def read():
            im = self.screen.grab(self.rect)
            return ocr.read_words(im, {"x1": 0, "y1": 0, "x2": im.shape[1], "y2": int(im.shape[0] * 0.85)}, scale=1.0)

        try:
            ws = words if words is not None else read()
            f = find(ws)
            if not f:
                return False
            rb, ap = f
            ky = self.rect[3] / 1080.0
            winapi.move(*self.abs_pt(ap[1], ap[2] - 607 * ky))  # saco el mouse de los botones (cartelitos de ayuda)
            time.sleep(0.3)
            winapi.click(*self.abs_pt(rb[1], rb[2]))
            time.sleep(0.5)
            for how in ("right", "esc", "right2"):
                if how == "esc":
                    winapi.press("ESC")
                else:
                    y_ = rb[2] - (607 if how == "right" else 650) * ky  # fila 'Puntos por asignar' / pestañas de arriba
                    winapi.click(*self.abs_pt((rb[1] + ap[1]) / 2, y_), button="right")
                time.sleep(0.6)
                if not find(read()):
                    return True
            return True
        except Exception:
            return False

    def _quest_mobs(self):
        """Bichos que piden mis misiones del cartel (cruzadas con la base de misiones: 'Epidemic' -> Snake,
        'A New Challenge' -> Orc). Sirve para no dejar de levantar lo que cae de ellos. Para cualquier pj y misión."""
        now = time.time()
        c = getattr(self, "_qm_cache", None)
        if c and now - c[0] < 30:
            return c[1]
        out = set()
        try:
            nz = lambda t: "".join(ch for ch in str(t).lower() if ch.isalnum())
            db = self._quest_db().get("quests", [])
            for q in (getattr(self, "quests", None) or []):
                if q.get("done"):
                    continue
                e = max(db, key=lambda d: difflib.SequenceMatcher(None, nz(q.get("name")), nz(d["name"])).ratio(), default=None)
                if e and difflib.SequenceMatcher(None, nz(q.get("name")), nz(e["name"])).ratio() >= 0.75:
                    out |= {o["mob"] for o in e.get("objectives") or [] if o.get("mob")}
        except Exception:
            pass
        self._qm_cache = (now, out)
        return out

    def _hit_pace(self, name, ttk, now):
        """Ritmo de golpe: cuánto tardo en matar cada bicho desde que lo tengo pegado, comparado con lo normal para
        ese bicho (lo aprendo solo, para cualquier pj y bicho). Si de golpe tardo mucho más que lo normal, el arma se
        rompió, se salió o está gastada: voy al pueblo (reparo, pociones, misiones, todo junto) y vuelvo."""
        if not (0.3 < ttk < 120) or now < getattr(self, "_rabia_until", 0):
            return  # rodeado, el tiempo por bicho se reparte entre todos: no sirve para medir el arma
        base = self.world.learn.setdefault("ttk_base", {})
        b = base.get(name) or {"avg": ttk, "n": 0}
        rec = self.__dict__.setdefault("_ttk_recent", {}).setdefault(name, [])
        rec[:] = (rec + [ttk])[-6:]
        med = sorted(rec)[len(rec) // 2]
        slow = b["n"] >= 15 and len(rec) >= 6 and med > max(2.2 * b["avg"], b["avg"] + 4.0)
        if not slow:
            # solo aprendo lo normal con golpes normales (si vengo lento no 'normalizo' el arma rota)
            b["avg"] = b["avg"] + (ttk - b["avg"]) / min(30.0, b["n"] + 1.0)
            b["n"] = int(b["n"]) + 1
            base[name] = b
            return
        if now - getattr(self, "_pace_town_t", 0) < 1200:
            return
        self._pace_town_t = now
        rec.clear()
        self.log(f"🔧 Pego más lento que lo normal ({med:.0f} s por {name} contra {b['avg']:.0f} s de siempre): "
                 "reviso el arma y de paso repongo todo en el pueblo.")
        self._town_need("arma: pego más lento que lo normal")

    def _pot_pace(self):
        """Ritmo sostenible: con las pociones que llevo tengo que poder cazar 'hunt_hours' (5 h) sin volver.
        Mido en la memoria cuántas tomo por hora. Si a ese ritmo no llego, bajo el ritmo (menos bichos juntos,
        descanso en vez de tomar); si ni así llego, este lugar me queda grande: me voy a otro. Para cualquier pj."""
        if cfgmod.server_slug(self.cfg) == "abaddon":
            return
        now = time.time()
        if now - getattr(self, "_pb_t", 0) < 30:
            return
        self._pb_t = now
        sm = self._supplies_mem()
        if not sm:
            return
        h = self.__dict__.setdefault("_pb_hist", [])
        if h and sm["pot"] > h[-1][1]:
            h.clear()  # compré o levanté pociones: arranco a medir de nuevo
        h.append((now, sm["pot"]))
        h[:] = [x for x in h if now - x[0] < 1500]
        if now - h[0][0] < 480 or h[0][1] - sm["pot"] < 3:
            return
        rate = (h[0][1] - sm["pot"]) / ((now - h[0][0]) / 3600.0)  # pociones por hora
        hours = float(self.cfg.get("hunt_hours", 5))
        left_h = sm["pot"] / max(rate, 0.1)
        if left_h >= hours or sm["pot"] >= rate * hours:
            return
        if now < getattr(self, "_austere_until", 0):
            if now - getattr(self, "_austere_t0", now) > 600:
                # ya bajé el ritmo hace 10 min y sigo gastando de más: este nido no me da para 5 horas
                self._austere_until = 0
                try:
                    pg_ = getattr(self, "_pit_go", None) or {}
                    px_, py_ = self.pos["x"], self.pos["y"]
                    for p_ in self._pits():
                        if max(abs(p_["x"] - px_), abs(p_["y"] - py_)) <= 15:
                            self.__dict__.setdefault("_pit_tried", {})[(p_["x"], p_["y"])] = now + 3600
                    mk_ = (self.pos.get("map") or "").strip().lower()
                    (self.world.learn.get("oly_hot") or {}).pop(mk_, None)
                except Exception:
                    pass
                self._careful_until = now + 1800
                self.log(f"🧪 Aun con menos ritmo gasto {rate:.0f} pociones por hora (me alcanzan {left_h:.1f} h de {hours:.0f}): "
                         "este nido me queda grande, me muevo a otro.")
            return
        self._austere_until = now + 1200
        self._austere_t0 = now
        self._careful_until = max(getattr(self, "_careful_until", 0), now + 1200)
        self.log(f"🧪 Gasto {rate:.0f} pociones por hora: con {sm['pot']} me alcanzan {left_h:.1f} h y quiero {hours:.0f}. "
                 "Bajo el ritmo: menos bichos juntos y descanso en vez de tomar.")

    def _grid_overlay(self):
        """La grilla pasada a pantalla para la vista previa: lo que se ve es lo que el bot cree."""
        g = getattr(self, "grid", None)
        if not g or not g.me or self.pos.get("x") is None or time.time() - g.t > 1.5:
            return None
        cx, cy = self._char()
        tw, th = self._tile()
        px, py = self.pos["x"], self.pos["y"]
        P = lambda x, y: (int(cx + (x - px) * tw), int(cy + (y - py) * th))
        out = {"tw": tw, "th": th, "mode": g.mode, "mobs": [], "players": [], "npcs": [], "loot": []}
        for tr in g.tracks.values():
            x, y = P(tr.x, tr.y)
            out["mobs"].append({"x": x, "y": y, "n": tr.n, "attack": tr.attack, "name": tr.name})
        for p in g.players:
            out["players"].append(P(p["x"], p["y"]))
        for p in g.npcs:
            out["npcs"].append(P(p["x"], p["y"]))
        for it in g.loot:
            out["loot"].append(P(it["x"], it["y"]))
        return out

    def _loot_items_draw(self):
        d = getattr(self, "_loot_draw", None)
        return d["list"] if d and time.time() - d["t"] < 2.0 else ()

    def _loot_pass(self, ents, reach=None):
        """Levanto lo que hay tirado cerca (pociones y oro: siempre convienen) cuando no tengo ningún bicho pegado.
        Lo veo en pantalla, lo paso a la baldosa de la grilla y voy con Shift+clic. True = estoy en eso."""
        if not self.cfg.get("loot_ground", True) or self.img is None:
            return False
        now = time.time()
        px, py = self.pos["x"], self.pos["y"]
        tried = self.__dict__.setdefault("_loot_tried", {})
        go = getattr(self, "_loot_go", None)
        hp_ = self.bars.get("hp")
        adj_ = any(max(abs(e[0] - px), abs(e[1] - py)) <= 1 for e in ents)
        blind_go = bool(go and go.get("blind"))
        if go and now < getattr(self, "_rabia_until", 0) and now - go["t"] > 1.8:
            self._loot_go = go = None  # rodeado: a lo sumo un paso de 1,8 s para levantar; después, a pegar
            blind_go = False
        if adj_ and not blind_go and not self._kill_pick_ready(now, px, py, hp_):
            if go:
                self._loot_go = None
            return False  # con un bicho pegado, primero el bicho (salvo lo que acaba de caer acá al lado)
        cx, cy = self._char()
        tw, th = self._tile()

        def click_tile(x, y):
            self._release()
            winapi.key_down("SHIFT")
            time.sleep(0.04)
            winapi.click(*self.abs_pt(int(cx + (x - px) * tw), int(cy + (y - py) * th)))
            time.sleep(0.04)
            winapi.key_up("SHIFT")
        if go:
            arrived = (px, py) == (go["x"], go["y"])
            if arrived or now - go["t"] > go.get("max", 5.0):
                tried[(go["x"], go["y"])] = now + (120 if arrived else 60)
                self._loot_go = None
                got = []
                if arrived:
                    self.stats["loot"] = self.stats.get("loot", 0) + 1
                    time.sleep(0.35)
                    try:
                        self._loot_msgs = [t for t in getattr(self, "_loot_msgs", []) if "dquiri" not in t]  # dos 'Adquiriste 29 Gold' iguales son dos levantadas
                        got = [t for t in (self._loot_note() or []) if "dquiri" in t]
                    except Exception:
                        pass
                    if got and go.get("blind") and now - getattr(self, "_got_said", 0) > 15:
                        self._got_said = now
                        self.log("🎒 Levanté: " + "; ".join(got)[:120])
                if go.get("blind"):
                    # aprendo: con este bicho, ¿cuántas veces había algo donde cayó?
                    lb = self.world.learn.setdefault("loot_blind", {}).setdefault(go.get("mob") or "?", {"n": 0, "ok": 0})
                    lb["n"] += 1
                    lb["ok"] += 1 if got else 0
                return False
            if now - go["click"] > 1.0:
                go["click"] = now
                click_tile(go["x"], go["y"])
            self.stats["state"] = f"levanto {go['kind']} ({go['x']},{go['y']})"
            return True
        # recién maté algo acá al lado: voy a su baldosa con Shift+clic aunque no vea nada (el equipo tirado no lo
        # distingo por color). Si había algo, lo agarro.
        if self._kill_pick_ready(now, px, py, hp_):
            kp = self._kill_pick
            self._kill_pick = None
            self._loot_go = {"x": kp[0], "y": kp[1], "kind": "lo que dejó el " + str(kp[3]), "t": now, "click": now,
                             "max": 2.5 + 1.0 * max(abs(kp[0] - px), abs(kp[1] - py)), "blind": True, "mob": kp[3]}
            if (kp[0], kp[1]) == (px, py):
                self._loot_go["max"] = 0.6
            click_tile(kp[0], kp[1])
            self.stats["state"] = f"levanto lo que cayó ({kp[0]},{kp[1]})"
            return True
        if adj_:
            return False
        # PASADA DE LEVANTAR (la anotada): sin bichos pegados, paso por las baldosas donde murió algo en los últimos
        # 2 minutos, la más cercana primero, con Shift+clic. No hace falta ver nada: si había algo, lo agarro.
        # lo que cae queda en el piso varios minutos: en un nido nunca estoy 'sin bichos pegados' a los 2 minutos de
        # cada muerte (de 300 Snakes revisé 20 y la misión Epidemic quedó en 0). Guardo las muertes 10 minutos y las
        # recorro en cada respiro.
        kq = [k_ for k_ in getattr(self, "_kill_q", []) if now - k_[2] < 600][-60:]
        qmobs_ = self._quest_mobs()
        bs_ = getattr(self, "_bag_slots", None)
        if kq and self.cfg.get("loot_sweep", True) and (hp_ is None or hp_ >= 45) and not (bs_ and now - bs_[2] < 1800 and bs_[0] >= bs_[1]):
            lbd = self.world.learn.get("loot_blind") or {}
            ok_ = [k_ for k_ in kq if (k_[0], k_[1]) not in tried and max(abs(k_[0] - px), abs(k_[1] - py)) <= 8
                   and not any((e[0], e[1]) == (k_[0], k_[1]) for e in ents)
                   and (str(k_[3]) in qmobs_  # bicho de misión: sus partes caen poco, igual paso siempre
                        or not ((lbd.get(k_[3]) or {}).get("n", 0) >= 80 and (lbd.get(k_[3]) or {}).get("ok", 0) < 0.03 * (lbd.get(k_[3]) or {}).get("n", 0)))]
            if ok_:
                kp = min(ok_, key=lambda k_: max(abs(k_[0] - px), abs(k_[1] - py)))
                self._kill_q = [k_ for k_ in kq if k_ is not kp]
                dist_ = max(abs(kp[0] - px), abs(kp[1] - py))
                self._loot_go = {"x": kp[0], "y": kp[1], "kind": "lo que dejó el " + str(kp[3]), "t": now, "click": now,
                                 "max": (0.6 if dist_ == 0 else 2.5 + 1.0 * dist_), "blind": True, "mob": kp[3]}
                if now - getattr(self, "_sweep_said", 0) > 60:
                    self._sweep_said = now
                    self.log(f"🎒 Paso a levantar por donde cayeron {len(ok_)} bicho/s.")
                click_tile(kp[0], kp[1])
                self.stats["state"] = f"paso a levantar ({kp[0]},{kp[1]})"
                return True
            self._kill_q = [k_ for k_ in kq if (k_[0], k_[1]) not in tried]
        if now - getattr(self, "_loot_scan_t", 0) < 0.6:
            return False
        self._loot_scan_t = now
        found = vision.ground_loot(self.img, (cx, cy), (tw, th))
        seen = self.__dict__.setdefault("_loot_seen", {})
        cur = {}
        for it in found:
            k = (px + it["dx"], py + it["dy"], it["kind"])
            cur[k] = (seen.get(k, (0, 0))[0] + 1, now)
        self._loot_seen = cur  # lo que no volvió a aparecer se olvida (números de daño, cosas que pasan)
        self._loot_draw = {"t": now, "list": [{"x": it["x"], "y": it["y"], "w": int(tw * 0.6), "h": int(th * 0.6)} for it in found]}
        try:
            self._grid().set_loot([{"x": k_[0], "y": k_[1], "kind": k_[2]} for k_ in cur])
        except Exception:
            pass
        for k in [k for k, v in tried.items() if v < now]:
            del tried[k]
        kl = getattr(self, "_oly_loot", None)
        fresh = bool(kl) and now - kl[2] <= 25
        # oro: solo donde cayó un bicho mío recién (lo amarillo suelto del mapa son flores). Pociones: el frasco con
        # su pico blanco es inconfundible; alcanza con verlo 3 veces seguidas en la misma baldosa.
        okk = lambda k, v: ((fresh and max(abs(k[0] - kl[0]), abs(k[1] - kl[1])) <= 3 and v[0] >= 2)
                            or (k[2].startswith("poci") and v[0] >= 3))
        cand = [k for k, v in cur.items() if okk(k, v) and (k[0], k[1]) not in tried
                and not ("man" in k[2] and not self.cfg.get("loot_mana", False))  # las de maná no las uso: solo llenan el bolso
                and not ("energ" in k[2] and int(((getattr(self, "_bag_scan", None) or {}).get("pots") or {}).get("energía") or 0)
                         >= int(self.cfg.get("keep_sp_pots", 4)))  # verdes de más: cada frasco ocupa un lugar y después hay que venderlo
                and not (k[2].startswith("poci") and (getattr(self, "_bag_slots", None) or (0, 99, 0))[0] >= (getattr(self, "_bag_slots", None) or (0, 99, 0))[1])
                and max(abs(k[0] - px), abs(k[1] - py)) <= int(reach or self.cfg.get("loot_reach", 7))
                and not any((e[0], e[1]) == (k[0], k[1]) for e in ents)]
        if not cand:
            return False
        k = min(cand, key=lambda k: max(abs(k[0] - px), abs(k[1] - py)))
        self._loot_go = {"x": k[0], "y": k[1], "kind": k[2], "t": now, "click": now,
                         "max": 4.0 + 1.3 * max(abs(k[0] - px), abs(k[1] - py))}
        if now - getattr(self, "_loot_said", 0) > 20:
            self._loot_said = now
            self.log(f"🎒 Hay {len(cand)} cosa/s tirada/s cerca: levanto {k[2]} en ({k[0]},{k[1]}).")
        click_tile(k[0], k[1])
        self.stats["state"] = f"levanto {k[2]} ({k[0]},{k[1]})"
        return True

    def _kill_pick_ready(self, now, px, py, hp):
        """¿Hay una muerte mía reciente, cerca, que valga la pena ir a levantar ya?"""
        kp = getattr(self, "_kill_pick", None)
        if not kp or not self.cfg.get("loot_blind", True):
            return False
        if now < getattr(self, "_rabia_until", 0):
            # rodeado: primero pego. Pero en un nido nunca afloja, así que con buena vida y como mucho cada 8 s me
            # corro UNA baldosa a la muerte que tengo pegada (un segundo) y sigo pegando desde ahí.
            if not (hp is not None and hp >= 60 and (now - getattr(self, "_rabia_pick_t", 0) > 8 or now - getattr(self, "_rabia_pick_t", 0) < 0.3)
                    and max(abs(kp[0] - px), abs(kp[1] - py)) <= 1 and now - kp[2] <= 6):
                return False
            self._rabia_pick_t = now
        if now - kp[2] > 6 or max(abs(kp[0] - px), abs(kp[1] - py)) > 2:
            self._kill_pick = None
            return False
        if hp is not None and hp < 45:
            return False  # con poca vida no me distraigo
        bs = getattr(self, "_bag_slots", None)
        if bs and now - bs[2] < 1800 and bs[0] >= bs[1]:
            return False  # bolso lleno: no entra nada
        lb = (self.world.learn.get("loot_blind") or {}).get(kp[3])
        if lb and lb["n"] >= 40 and lb["ok"] < 0.06 * lb["n"]:
            return False  # con este bicho casi nunca cae nada: no pierdo el paso
        return now - getattr(self, "_loot_blind_t", 0) > 1.0

    def _loot_note(self):
        """Después de levantar algo leo los mensajes del juego (abajo a la izquierda) y guardo los nuevos en
        data/botin.log: de ahí sale qué cae y cómo lo nombra el juego."""
        img = self.screen.grab(self.rect)
        H, W = img.shape[:2]
        ws = ocr.read_words(img, {"x1": 0, "y1": int(H * 0.72), "x2": int(W * 0.62), "y2": int(H * 0.915)}, scale=1.0)
        rows = {}
        for w in ws:
            rows.setdefault(round(w[2] / 12), []).append(w)
        seen = self.__dict__.setdefault("_loot_msgs", [])
        new = []
        for k in sorted(rows):
            t = " ".join(v[0] for v in sorted(rows[k], key=lambda v: v[1])).strip()
            if len(t) >= 6 and t not in seen:
                new.append(t)
                seen.append(t)
                if "hambrient" in t.lower():
                    self._hunger = max(int(getattr(self, "_hunger", None) or 0), 75)  # 'Estás hambriento...': a comer
        del seen[:-60]
        if new:
            with open(os.path.join(cfgmod.DATA, "botin.log"), "a", encoding="utf-8") as f:
                for t in new:
                    f.write(time.strftime("%H:%M:%S ") + t + "\n")
        return new

    # ---- nidos del mapa: el minimapa del juego los dibuja (Ctrl+Z los muestra u oculta)
    PIT_GUESS = (("Orc", lambda h, s_, v: 45 <= h <= 70 and s_ >= 150), ("Giant Scorpion", lambda h, s_, v: 16 <= h <= 30 and v >= 45),
                 ("Giant Ant", lambda h, s_, v: h <= 18 and v < 45), ("Slime", lambda h, s_, v: 32 <= h <= 60 and s_ < 130))

    def _pits(self, mk=None):
        mk = mk or (self.pos.get("map") or "").strip().lower()
        L = ((self.world.learn.get("pits") or {}).get(mk) or {}).get("list") or []
        # solo los que caen adentro del mapa (una mala lectura del minimapa dio nidos en (134,171) en un mapa de 170:
        # el bot quedaba parado 'yendo' a un lugar sin camino)
        try:
            m = self.world.map_for(self.pos.get("map") or "") if mk == (self.pos.get("map") or "").strip().lower() else None
            if m:
                L = [p for p in L if 0 <= int(p["x"]) < m.w and 0 <= int(p["y"]) < m.h]
        except Exception:
            pass
        return L

    def _pits_scan(self):
        """Mapa de nidos sin caminarlo: saco el minimapa (mapa entero) con y sin nidos; lo que cambia son los nidos.
        Cada mancha → una baldosa del mapa. De qué bicho es cada uno lo adivino por el color y lo confirmo al pasar.
        Lo hago una vez por mapa (y lo repito cada semana), con el pj tranquilo."""
        if cfgmod.server_slug(self.cfg) == "abaddon" or not self.rect or self.pos.get("x") is None:
            return False
        now = time.time()
        mk = (self.pos.get("map") or "").strip().lower()
        m = self.world.map_for(self.pos.get("map") or "")
        if not mk or not m or any(k in mk for k in ("shop", "blacksmith", "city", "ciudad", "hall", "warehouse")):
            return False
        cur = (self.world.learn.get("pits") or {}).get(mk)
        if cur and now - cur.get("t", 0) < 7 * 86400:
            return False
        if now - getattr(self, "_pits_try", 0) < 600 or self.state != "seek" or getattr(self, "_busy_ui", False) or self._under_attack():
            return False
        px, py = self.pos["x"], self.pos["y"]
        if any(max(abs(e[0] - px), abs(e[1] - py)) <= 5 for e in (self._oly_ents() or [])):
            return False
        self._pits_try = now
        import cv2
        kx, ky = self.rect[2] / 1919.0, self.rect[3] / 1080.0
        S = 231.0 * ky
        x0, y0 = 1447.0 * kx, 0.0
        sc = S / float(max(m.w, m.h))
        self._release()
        self._busy_ui = True
        toggled_mode = False
        try:
            def marker(img):
                r_ = img[0:int(S + 30), int(x0 - 30):int(x0 + S + 30)].astype(int)
                bl = (r_[:, :, 0] > 190) & (r_[:, :, 1] < 80) & (r_[:, :, 2] < 80)
                ys, xs = np.nonzero(bl)
                return (float(xs.mean()) + int(x0 - 30), float(ys.mean())) if len(xs) >= 6 else None
            img_a = self.screen.grab(self.rect)
            mkr = marker(img_a)
            want = (x0 + px * sc, y0 + py * sc)
            if not mkr or max(abs(mkr[0] - want[0]), abs(mkr[1] - want[1])) > 10 * ky:
                winapi.press("CTRL+SHIFT+M")  # el minimapa estaba con zoom: lo paso a mapa entero
                toggled_mode = True
                time.sleep(1.0)
                img_a = self.screen.grab(self.rect)
                mkr = marker(img_a)
            if not mkr or max(abs(mkr[0] - want[0]), abs(mkr[1] - want[1])) > 10 * ky:
                self.log("🗺 No pude leer los nidos del minimapa (no ubico mi marca en el mapa entero). Pruebo más tarde.")
                return False
            # ajusto el corrimiento con mi propia posición (la marca azul soy yo)
            x0 += mkr[0] - want[0]
            y0 += mkr[1] - want[1]
            winapi.press("CTRL+Z")
            time.sleep(0.9)
            img_b = self.screen.grab(self.rect)
            winapi.press("CTRL+Z")
            time.sleep(0.5)
            xa, ya = int(round(x0)), max(0, int(round(y0)))
            ra = img_a[ya:ya + int(S), xa:xa + int(S)].astype(int)
            rb = img_b[ya:ya + int(S), xa:xa + int(S)].astype(int)
            d = (np.abs(ra - rb).max(axis=2) > 28).astype(np.uint8)
            d = cv2.morphologyEx(d, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
            if d.sum() < 80:
                self.log("🗺 El minimapa no cambió al mostrar/ocultar nidos: este mapa no tiene, o la tecla no respondió.")
                self.world.learn.setdefault("pits", {})[mk] = {"t": now, "list": []}
                return False
            on = ra if ra[d > 0].max(axis=1).mean() <= rb[d > 0].max(axis=1).mean() else rb  # con nidos: manchas más oscuras
            n, lab, st, cen = cv2.connectedComponentsWithStats(d)
            pits = []
            n_out = 0
            for i in range(1, n):
                if st[i, 4] < 60 * ky * ky:
                    continue
                col = np.median(on[lab == i], axis=0)
                h_, s_, v_ = [int(x) for x in cv2.cvtColor(np.uint8([[col]]), cv2.COLOR_BGR2HSV)[0][0]]
                tx, ty = int(round(cen[i][0] / sc)), int(round(cen[i][1] / sc))
                if max(abs(tx - px), abs(ty - py)) <= 3 and st[i, 4] < 140:
                    continue  # mi propia marca moviéndose
                g = next((nm for nm, f in self.PIT_GUESS if f(h_, s_, v_)), None)
                if not (0 <= tx < m.w and 0 <= ty < m.h):
                    n_out += 1
                    continue
                pits.append({"x": tx, "y": ty, "r": int(round(max(st[i, 2], st[i, 3]) / sc / 2)), "hsv": [h_, s_, v_], "guess": g, "mob": None, "seen": {}})
            if n_out and n_out >= len(pits):
                # la mitad o más cayó fuera del mapa: leí mal la escala del minimapa. No guardo nada, pruebo más tarde.
                self.log(f"🗺 Lectura de nidos dudosa ({n_out} fuera del mapa): la descarto y pruebo más tarde.")
                return False
            self.world.learn.setdefault("pits", {})[mk] = {"t": now, "list": pits}
            self.world.save()
            self.log(f"🗺 Leí {len(pits)} nidos del minimapa de {self.pos.get('map')}: "
                     + ", ".join(f"{p['guess'] or '¿?'} ({p['x']},{p['y']})" for p in pits) + ".")
            return True
        except Exception as e:
            self.log(f"🗺 Error leyendo nidos del minimapa: {e!r}")
            return False
        finally:
            if toggled_mode:
                try:
                    winapi.press("CTRL+SHIFT+M")
                except Exception:
                    pass
            self._busy_ui = False

    def _pits_tag(self, ents):
        """Al pasar cerca de un nido anoto qué bichos vivos hay: así confirmo de qué es cada uno."""
        now = time.time()
        if now - getattr(self, "_pits_tag_t", 0) < 5 or self.pos.get("x") is None:
            return
        self._pits_tag_t = now
        px, py = self.pos["x"], self.pos["y"]
        for p in self._pits():
            if max(abs(p["x"] - px), abs(p["y"] - py)) > 16:
                continue
            rr = max(8, int(p.get("r") or 8) + 3)
            for e in ents:
                if max(abs(e[0] - p["x"]), abs(e[1] - p["y"])) <= rr:
                    nm = e[2].get("name")
                    if nm and nm != "bicho":
                        p["seen"][nm] = p["seen"].get(nm, 0) + 1
            if p["seen"]:
                top = max(p["seen"], key=p["seen"].get)
                if p["seen"][top] >= 6 and p.get("mob") != top:
                    p["mob"] = top
                    self.log(f"🗺 Nido de ({p['x']},{p['y']}): confirmado, es de {top}.")

    def _pit_pick(self):
        """El nido que me conviene ahora: de un bicho que aguanto, con prioridad al de la misión y a los cercanos."""
        px, py = self.pos["x"], self.pos["y"]
        qm = self._quest_mob_names() or set()
        now = time.time()
        tried = self.__dict__.setdefault("_pit_tried", {})
        best = None
        try:
            strict_q = bool(qm) and self._quest_strict() and any((p_.get("mob") or p_.get("guess")) in qm for p_ in self._pits())
        except Exception:
            strict_q = False
        for p in self._pits():
            nm = p.get("mob") or p.get("guess")
            dist = max(abs(p["x"] - px), abs(p["y"] - py))
            if not nm or dist <= 10 or tried.get((p["x"], p["y"]), 0) > now or not self._can_face(nm) or self._pit_deadly(p):
                continue
            try:
                # sin camino desde acá (otra isla, del otro lado de un muro, fuera del mapa): ni lo considero por 1 hora
                m_ = self.world.map_for(self.pos.get("map") or "")
                g_ = m_.nearest_walkable(p["x"], p["y"], r=6) if m_ else None
                if not g_ or not m_.astar((px, py), g_):
                    tried[(p["x"], p["y"])] = now + 3600
                    continue
            except Exception:
                pass
            if now < float(self.cfg.get("train_skill_until", 0) or 0) and nm != "Slime":
                continue  # arma de otro tipo recién puesta: subo la habilidad con lo más fácil antes de ir a lo serio
            # el nido del bicho de la misión pesa mucho más: una misión de 200 orcos se hace en el nido de orcos
            if qm and nm not in qm and strict_q:
                continue  # misión estricta: solo nidos de su bicho (si hay alguno conocido)
            score = (5.0 if nm in qm else 1.0) * (1.3 if p.get("mob") else 1.0) / (1.0 + dist / 80.0)
            if best is None or score > best[0]:
                best = (score, p, nm)
        return best

    def _quest_worth(self, q, done, total, nm):
        """¿Rinde ir a hacer esta misión en vez de seguir subiendo donde estoy cómodo? Comparo el premio (exp + oro)
        con lo que dejo de ganar mientras la hago: el tiempo que lleva (bichos que faltan × lo que tardo en matar
        cada uno) por la experiencia por segundo que vengo sacando en este mapa. Sirve para cualquier pj y nivel."""
        try:
            y = (self.world.learn.get("yield") or {}).get((self.pos.get("map") or "").strip().lower()) or {}
            rate = float(y.get("exp", 0)) / float(y["sec"]) if y.get("sec", 0) > 1800 else 0.0
            if rate <= 0:
                return True, "todavía no sé cuánto rinde cazar acá"
            ttk = float((self.world.learn.get("oly_ttk") or {}).get(nm, 0) or 0) \
                or float(((self.world.learn.get("fight") or {}).get(nm) or {}).get("ttk", 0) or 0) or 10.0
            left = max(1, int(total) - int(done))
            if q.get("kind") != "kill":
                left = int(left / 0.15)  # objetos que caen cada tanto: unos 7 bichos por objeto
            secs = left * (ttk + 5.0) + 120
            value = float(q.get("exp") or 0) + 10.0 * float(q.get("gold") or 0)
            cost = secs * rate * 0.5  # mientras la hago igual gano algo matando los bichos de la misión
            ok = value >= cost
            return ok, (f"premio {int(value / 1000)}k contra {int(cost / 1000)}k que dejo de ganar en {int(secs / 60)} min")
        except Exception:
            return True, ""

    def _quest_drive(self, ents, px, py, now):
        """La misión de matar N bichos se hace donde vive ese bicho. Si estoy lejos de su nido (cazando otra cosa
        porque rinde), cada media hora voy derecho para allá sin frenarme por lo que haya en el camino (si me pegan,
        la regla de oro me defiende). Al llegar sigo cazando normal. True = estoy yendo."""
        if not self.cfg.get("quest_drive", True):
            return False
        qd = getattr(self, "_qdrive", None)
        if qd is not None and qd.get("goto"):
            # voy con el viaje del mapa (el mismo del clic en el mapa: pelea si le pegan y sigue)
            p_ = qd["p"]
            d_ = max(abs(p_["x"] - px), abs(p_["y"] - py))
            if d_ < qd["d"] - 2:
                qd["d"], qd["t"] = d_, now
            if not getattr(self, "goto", None) or d_ <= 12 or now - qd["t0"] > 1200 or now - qd["t"] > 120:
                if d_ > 20:
                    self.log(f"📜 No llegué al nido de {qd['nm']} ({p_['x']},{p_['y']}): pruebo de nuevo en 10 minutos.")
                else:
                    self._hot_fail = now
                    self._exp_gain_t, self._start_t_oly = now, now
                    self.log(f"📜 Llegué a la zona de {qd['nm']}: cazo acá.")
                self.goto = None
                self._qdrive, self._qdrive_last = None, (now if d_ <= 20 else now - 1200)
            return False
        if sum(1 for e in ents if max(abs(e[0] - px), abs(e[1] - py)) <= 1) >= 3 or now < getattr(self, "_rabia_until", 0) \
                or getattr(self, "_kite", None) is not None or self._town_block() or getattr(self, "mission", None) or getattr(self, "goto", None):
            return False
        hp = self.bars.get("hp")
        if hp is not None and hp < 60:
            return False
        qm = self._quest_mob_names() or set()
        if not qm:
            self._qdrive = None
            return False
        tried = self.__dict__.setdefault("_pit_tried", {})
        if qd is None:
            if now - getattr(self, "_qdrive_last", 0) < 1800:
                return False
            if any(e[2]["name"] in qm and max(abs(e[0] - px), abs(e[1] - py)) <= 12 for e in ents):
                return False  # ya tengo bichos de la misión acá
            qa_ = self._quest_active()
            if qa_:
                w_ok, w_why = self._quest_worth(qa_[0], qa_[1], qa_[2], next(iter(qm)))
                if getattr(self, "_qworth_said", None) != (qa_[0]["name"], w_ok):
                    self._qworth_said = (qa_[0]["name"], w_ok)
                    self.log(f"📜 Misión '{qa_[0]['name']}': " + ("me conviene ir a hacerla" if w_ok else "no rinde ir a buscarla, sigo subiendo acá (si aparece su bicho, suma)") + (f" ({w_why})." if w_why else "."))
                if not w_ok:
                    self._qdrive_last = now - 900  # lo vuelvo a pensar en 15 minutos (cambia con el nivel y el arma)
                    return False
            cands = []
            for p in self._pits():
                nm = p.get("mob") or p.get("guess")
                # ni a un nido donde me mataron hace poco, ni por un bicho que me lleva más de 25 s cada uno (una misión
                # de 200 de esos no rinde: sigo subiendo con lo que mato rápido)
                if nm in qm and tried.get((p["x"], p["y"]), 0) < now and self._can_face(nm) and not self._pit_deadly(p) \
                        and float((self.world.learn.get("oly_ttk") or {}).get(nm, 0) or 0) < 25:
                    d_ = max(abs(p["x"] - px), abs(p["y"] - py))
                    if d_ > 25:
                        cands.append((0 if p.get("mob") else 1, d_, p, nm))
            if not cands:
                self._qdrive_last = now - 1200  # reviso de nuevo en 10 minutos
                return False
            _c, d_, p, nm = min(cands, key=lambda c: (c[0], c[1]))
            m0 = self.world.map_for(self.pos.get("map") or "")
            g0 = m0.nearest_walkable(p["x"], p["y"], r=6) if m0 else None
            if not g0:
                tried[(p["x"], p["y"])] = now + 3600
                return False
            self._qdrive = {"p": p, "nm": nm, "t0": now, "t": now, "d": d_, "goto": True}
            self.goto = {"map": self.pos.get("map"), "x": int(g0[0]), "y": int(g0[1]), "t": now}
            self._hot_fail = now
            self.log(f"📜 Voy al nido de {nm} en ({p['x']},{p['y']}) por la misión (a {d_} baldosas), sin frenar en el camino.")
            return False
        p = qd["p"]
        d_ = max(abs(p["x"] - px), abs(p["y"] - py))
        if d_ <= 12 or any(e[2]["name"] in qm and max(abs(e[0] - px), abs(e[1] - py)) <= 6 for e in ents):
            self._qdrive, self._qdrive_last = None, now
            self._hot_fail = now  # no me vuelvo a la zona de antes apenas llego
            self._exp_gain_t, self._start_t_oly = now, now
            self.log(f"📜 Llegué a la zona de {qd['nm']}: cazo acá.")
            return False
        if d_ < qd["d"]:
            qd["d"], qd["t"] = d_, now
        elif now - qd["t"] > 50 or now - qd["t0"] > 420:
            tried[(p["x"], p["y"])] = now + 3600
            self._qdrive, self._qdrive_last = None, now
            self.log(f"📜 No llego al nido de {qd['nm']} ({p['x']},{p['y']}): lo dejo una hora.")
            return False
        m = self.world.map_for(self.pos.get("map") or "")
        g = m.nearest_walkable(p["x"], p["y"], r=6) if m else None
        if not g:
            tried[(p["x"], p["y"])] = now + 3600
            self._qdrive, self._qdrive_last = None, now
            return False
        self.stats["state"] = f"voy al nido de {qd['nm']} por la misión ({p['x']},{p['y']})"
        self._walk_to(m, (int(g[0]), int(g[1])), f"nido de {qd['nm']} (misión)")
        return True

    def _hot_return(self, ents=None):
        """Sin nada vivo cerca, o más de 2 minutos y medio sin ganar experiencia: vuelvo al último lugar donde venía
        ganando (en vez de explorar o dar vueltas). True = estoy yendo."""
        if cfgmod.server_slug(self.cfg) == "abaddon" or self.pos.get("x") is None:
            return False
        if ents is None:
            ents = self._oly_ents()
        px, py = self.pos["x"], self.pos["y"]
        now = time.time()
        # "seco" = ni experiencia ni muertes en 2 min y medio (al subir de nivel la barra queda sin leer un rato:
        # si estoy matando, no está seco)
        dry = now - max(getattr(self, "_exp_gain_t", 0), getattr(self, "_last_kill_t", 0), getattr(self, "_start_t_oly", now)) > 150
        if not hasattr(self, "_start_t_oly"):
            self._start_t_oly = now
        # misión estricta sin su bicho cerca: cuenta como 'seco' (no me quedo con los Slimes de al lado)
        try:
            qm0 = self._quest_mob_names() if self._quest_strict() else set()
            if qm0 and not any(e[2].get("name") in qm0 and max(abs(e[0] - px), abs(e[1] - py)) <= 14 for e in ents):
                dry = True
        except Exception:
            pass
        if (not any(max(abs(e[0] - px), abs(e[1] - py)) <= 14 for e in ents) or dry) \
                and not any(max(abs(e[0] - px), abs(e[1] - py)) <= 1 for e in ents):
            # nada vivo cerca (o hace rato que no gano experiencia acá): vuelvo adonde venía ganando
            try:
                mk = (self.pos.get("map") or "").strip().lower()
                h = (self.world.learn.get("oly_hot") or {}).get(mk)
                try:
                    # misión en modo estricto: mi 'zona buena' de otro bicho no me sirve (volvía a los Slimes)
                    qm_ = self._quest_mob_names() if self._quest_strict() else set()
                    if h and qm_:
                        h = None  # voy a los nidos de su bicho (el elegidor de nidos ya los prioriza)
                except Exception:
                    pass
                if h and (self._pit_deadly({"x": h["x"], "y": h["y"]}) if hasattr(self, "_pit_deadly") else False):
                    # la zona buena quedó donde me mataron (nido que me queda grande): no vuelvo ahí
                    del self.world.learn["oly_hot"][mk]
                    self._hot_go = None
                    self.log(f"🎯 Mi zona buena ({h['x']},{h['y']}) es donde me mataron: la descarto.")
                    h = None
                if h and self.bot_mode() != "manual" and max(abs(h["x"] - px), abs(h["y"] - py)) <= 8 and not any(max(abs(e[0] - px), abs(e[1] - py)) <= 14 for e in ents):
                    # llegué a mi zona buena y no hay nada: ya no es buena. La borro y voy a un nido ahora, sin esperar.
                    del self.world.learn["oly_hot"][mk]
                    self._hot_go = None
                    self.log("🎯 Mi zona buena está vacía: la descarto y voy a un nido.")
                    h, dry = None, True
                if h and max(abs(h["x"] - px), abs(h["y"] - py)) > 8 and now - getattr(self, "_hot_fail", 0) > 300:
                    m = self.world.map_for(self.pos.get("map") or "")
                    g = m.nearest_walkable(h["x"], h["y"], r=5) if m else None
                    if g:
                        st_ = getattr(self, "_hot_go", None)
                        if not st_:
                            st_ = self._hot_go = {"t": now, "d": 999}
                            self.log(f"🎯 {'Hace rato que no gano experiencia' if dry else 'No quedan bichos cerca'}: vuelvo a mi zona buena ({h['x']},{h['y']}).")
                        dcur = max(abs(h["x"] - px), abs(h["y"] - py))
                        if dcur < st_["d"]:
                            st_["d"], st_["t"] = dcur, now
                        elif now - st_["t"] > 40:
                            self._hot_fail, self._hot_go = now, None  # no llego: no insisto por 5 minutos
                            return False
                        self.stats["state"] = f"vuelvo a mi zona de caza ({h['x']},{h['y']})"
                        self._walk_to(m, (int(g[0]), int(g[1])), "zona de caza")
                        return True
            except Exception:
                pass
            # mi zona buena se secó o no tengo ninguna: voy al nido que más conviene (los saqué del minimapa)
            try:
                if self.bot_mode() != "manual" and (dry or not any(max(abs(e[0] - px), abs(e[1] - py)) <= 14 for e in ents)) and now - getattr(self, "_pit_go_t", 0) > 20:
                    bp = self._pit_pick()
                    pg0 = getattr(self, "_pit_go", None)
                    if pg0 and pg0.get("p") and now - pg0["t"] < 45 and self.__dict__.get("_pit_tried", {}).get(pg0["k"], 0) < now:
                        bp = (0, pg0["p"], pg0["nm"])  # ya elegí un nido y voy avanzando: no cambio de idea en el camino
                    if bp:
                        _sc, p, nm = bp
                        m = self.world.map_for(self.pos.get("map") or "")
                        g = m.nearest_walkable(p["x"], p["y"], r=6) if m else None
                        pg = getattr(self, "_pit_go", None)
                        if not pg or pg["k"] != (p["x"], p["y"]):
                            pg = self._pit_go = {"k": (p["x"], p["y"]), "t": now, "d": 999, "p": p, "nm": nm}
                            self.log(f"🗺 Acá no gano experiencia: voy al nido de {nm} en ({p['x']},{p['y']}).")
                        dcur = max(abs(p["x"] - px), abs(p["y"] - py))
                        if dcur < pg["d"]:
                            pg["d"], pg["t"] = dcur, now
                        elif now - pg["t"] > 45:
                            self.__dict__.setdefault("_pit_tried", {})[(p["x"], p["y"])] = now + 900  # no llego: lo dejo 15 minutos
                            self._pit_go = None
                            return False
                        if g and m.astar((px, py), (int(g[0]), int(g[1]))):
                            self.stats["state"] = f"voy al nido de {nm} ({p['x']},{p['y']})"
                            self._walk_to(m, (int(g[0]), int(g[1])), f"nido de {nm}")
                            return True
                        # sin camino: lo descarto una hora y el próximo tick elijo otro (antes me quedaba parado)
                        self.__dict__.setdefault("_pit_tried", {})[(p["x"], p["y"])] = now + 3600
                        self._pit_go = None
            except Exception:
                pass
        else:
            self._hot_go = None
        return False

    def _oly_step(self):
        """Elijo un bicho de la lista de memoria: si está en pantalla le pego; si no, camino hasta él."""
        if self.pos.get("x") is None or time.time() - self.pos.get("t", 0) > 5:
            return False
        ents = self._oly_ents()
        px, py = self.pos["x"], self.pos["y"]
        now = time.time()
        self._last_mons = []  # nada del buscador viejo por imagen en la vista previa
        try:
            self._pits_tag(ents)
            pg = getattr(self, "_pit_go", None)
            if pg and max(abs(pg["k"][0] - px), abs(pg["k"][1] - py)) <= 10:
                self.__dict__.setdefault("_pit_tried", {})[pg["k"]] = now + 600  # ya estoy acá: si se seca, el próximo es otro
                self._pit_go, self._start_t_oly = None, now
        except Exception:
            pass
        try:
            if self._quest_drive(ents, px, py, now):
                return True
        except Exception as e_:
            if now - getattr(self, "_qd_err", 0) > 300:
                self._qd_err = now
                self.log(f"📜 Ida al nido de la misión: error {e_!r}")
        try:
            pl = [p for p in self._grid().players if max(abs(p["x"] - px), abs(p["y"] - py)) <= 7]
            if pl and any(max(abs(o[0] - px), abs(o[1] - py)) <= 9 for o in ents):
                self._pl_since = getattr(self, "_pl_since", 0) or now
                if now - self._pl_since > 25 and now - getattr(self, "_pl_moved", 0) > 300:
                    self._pl_moved, self._pl_since = now, 0
                    tr_ = self.__dict__.setdefault("_pit_tried", {})
                    for p_ in self._pits():
                        if max(abs(p_["x"] - px), abs(p_["y"] - py)) <= 14:
                            tr_[(p_["x"], p_["y"])] = now + 600
                    self._hot_fail = now          # tampoco vuelvo a mi zona de siempre por un rato
                    self._exp_gain_t, self._start_t_oly = 0, now - 1000   # cuenta como zona seca: elijo otro nido
                    self.log("👥 Hay otro jugador cazando en este nido: me voy a otro.")
            else:
                self._pl_since = 0
        except Exception:
            pass
        try:
            if self._loot_pass(ents):
                return True
        except Exception as e_:
            if now - getattr(self, "_loot_err_t", 0) > 300:
                self._loot_err_t = now
                self.log(f"🎒 Error levantando botín: {e_!r}")
        if self._hot_return(ents):
            return True
        if not ents:
            return False
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
                try:
                    self._loot_note()
                except Exception:
                    pass
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
            qok = [e for e in ok if e[2]["name"] in qm and d(e) <= 14]
            if qok:
                ok = qok  # misión activa: primero sus bichos
            elif not self._quest_strict():
                pass  # misión de juntar cosas (o bicho escaso): mientras no aparezca uno, cazo lo de siempre
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
        ok = [e for e in ok if d(e) <= 1 or not self._killer(e[2]["name"])]  # al que me viene matando no lo busco

        def probe_ok(e):
            # bicho que estoy midiendo: solo si está suelto (nadie de su tipo a 3) y tengo vida y pociones
            if d(e) <= 1 or self._risk_verdict(e[2]["name"]) != "probar":
                return True
            r_ = (self.world.learn.get("risk") or {}).get(e[2]["name"]) or {}
            if r_.get("k", 0) >= 8 and r_.get("prev") != "no":
                return True
            # bicho que ya vengo matando (tengo tiempos de pelea) y nunca medí como 'no': lo sigo cazando normal
            # mientras se junta la medición (antes esto dejaba al bot 1 hora sin pegarle a nada: los slimes nunca
            # están sueltos)
            known_ = (self.world.learn.get("oly_ttk") or {}).get(e[2]["name"]) or ((self.world.learn.get("fight") or {}).get(e[2]["name"]) or {}).get("ttk")
            if known_ and r_.get("prev") != "no":
                return True
            near_same = sum(1 for o in ents if o is not e and o[2]["name"] == e[2]["name"] and max(abs(o[0] - e[0]), abs(o[1] - e[1])) <= 3)
            pots_ = (getattr(self, "_left", {}) or {}).get("pot")
            return near_same == 0 and (self.bars.get("hp") or 0) >= 75 and (pots_ is None or pots_ >= 8)
        try:
            ok = [e for e in ok if probe_ok(e)]
        except Exception:
            pass
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
        # --- cola de objetivos de la grilla: cada bicho vivo tiene su número y lo conserva aunque se mueva. El 1 es
        # al que le pego; cuando muere, el 2 ya venía siendo seguido. El clic sale de su baldosa de AHORA.
        g = self._grid()
        okset = {(o[0], o[1]) for o in ok}
        cap_n = self._group_cap(getattr(self, "_hunt_mob", None) or e[2]["name"])
        q = g.rank(cap=cap_n, usable=lambda tr: (tr.x, tr.y) in okset and (tr.typ, tr.x, tr.y) not in bad,
                   quest=self._quest_mob_names() or (), mode="nido",
                   taken=lambda tr: g.player_near(tr.x, tr.y, 2))
        q = [tr for tr in q if (tr.typ, tr.x, tr.y) not in bad]
        # el 1 de la cola tiene 3 segundos: si en ese tiempo no lo tengo pegado ni me acerqué (está detrás de una
        # rampa, un cerco, agua), paso al 2, y así. Si no queda otro, voy hasta él por el camino del mapa.
        skip = self.__dict__.setdefault("_tgt_skip", {})
        for k_ in [k_ for k_, v_ in skip.items() if v_ < now]:
            del skip[k_]
        q_all = q
        q = [tr for tr in q if tr.id not in skip]
        if q:
            tr0 = q[0]
            dd = tr0.dist(g.me)
            pr = getattr(self, "_tgt_prog", None)
            if not pr or pr["id"] != tr0.id:
                pr = self._tgt_prog = {"id": tr0.id, "best": dd, "t": now}
            if dd <= 1 or dd < pr["best"]:
                pr["best"], pr["t"] = min(dd, pr["best"]), now
            elif now - pr["t"] > 3.0:
                skip[tr0.id] = now + 20
                self._tgt_prog = None
                if now - getattr(self, "_skip_said", 0) > 30:
                    self._skip_said = now
                    self.log(f"↪ No llego al {tr0.name} de ({tr0.x},{tr0.y}) en 3 s: paso al siguiente.")
                self._release()
                q = q[1:]
        # rodeado: el objetivo es SIEMPRE uno de los que tengo pegados (aunque lo haya marcado élite o "no llego").
        # Nunca apunto a uno de más lejos pasando por arriba de otro.
        adjq = [t_ for t_ in g.tracks.values() if t_.dist(g.me) <= 1]
        if adjq:
            cur_ = next((t_ for t_ in adjq if t_.id == getattr(self, "_adj_tgt", None)), None)
            first_ = cur_ or min(adjq, key=lambda t_: ((t_.typ, t_.x, t_.y) in bad, t_.adj_since or now, t_.id))
            self._adj_tgt = first_.id
            q = [first_] + [t_ for t_ in q if t_ is not first_]
        else:
            self._adj_tgt = None
        if not q and q_all and not any(tr.dist(g.me) <= 1 for tr in q_all):
            tr_ = min(q_all, key=lambda t_: t_.dist(g.me))
            m_ = self.world.map_for(self.pos.get("map") or "")
            if m_:
                self.stats["state"] = f"rodeo hasta el {tr_.name} ({tr_.x},{tr_.y})"
                self._walk_to(m_, (int(tr_.x), int(tr_.y)), tr_.name)
                return True
        if q:
            tr = q[0]
            e2 = next((o for o in ents if (o[0], o[1]) == (tr.x, tr.y) and o[2]["name"] == tr.name), None)
            if e2:
                e = e2
            # élite / no muere: cuento el tiempo que lo tengo pegado, siguiéndolo aunque se mueva
            # el reloj de "no muere" corre desde que LE PEGO a ese (no desde que lo tengo al lado esperando turno)
            tc = getattr(self, "_tgt_clock", None)
            if not tc or tc[0] != tr.id:
                tc = self._tgt_clock = (tr.id, now)
            if tr.dist(g.me) > 1:
                tc = self._tgt_clock = (tr.id, now)
            if e2 and tr.adj_since and now - tc[1] > self._oly_ttk(tr.name) and now > getattr(self, "_rabia_until", 0):
                for dx_ in (-1, 0, 1):
                    for dy_ in (-1, 0, 1):
                        bad[(tr.typ, tr.x + dx_, tr.y + dy_)] = now + 600
                tr.adj_since = now
                self._tgt_clock = None
                self._adj_tgt = None
                self.__dict__.setdefault("_elite_ts", []).append(time.time())
                self.log(f"👑 Ese {tr.name} no muere: debe ser élite. Lo dejo y sigo con otro.")
                self._release()
                return False
        # élite: el slime común muere en 1-3 golpes; si a los 7 s de pegarle sigue vivo, es élite y lo dejo
        fe = getattr(self, "_oly_first", {})
        if e[3] not in fe:
            if d(e) <= 1:  # cuento desde que lo tengo pegado (no mientras camino hasta él)
                fe = {k: v for k, v in fe.items() if now - v < 60}
                fe[e[3]] = now
                self._oly_first = fe
        elif now - fe[e[3]] > self._oly_ttk(e[2]["name"]) and d(e) <= 1 and now < getattr(self, "_rabia_until", 0):
            fe[e[3]] = now  # en rabia no descarto a nadie: le sigo pegando
        elif now - fe[e[3]] > self._oly_ttk(e[2]["name"]) and d(e) <= 1:
            bad[e[3]] = now + 600
            fe[e[3]] = now  # si sigue pegado le vuelvo a pegar (regla de oro): el aviso sale una vez, no en cada vuelta
            # si me pasa seguido con el mismo bicho no son élites: es que tardo más en matarlo → le doy más tiempo
            el = self.__dict__.setdefault("_oly_elite", {}).setdefault(e[2]["name"], [])
            el[:] = [t for t in el if now - t < 900] + [now]
            if len(el) >= 3:
                tk = self.world.learn.setdefault("oly_ttk", {})
                tk[e[2]["name"]] = min(40.0, self._oly_ttk(e[2]["name"]) * 1.6)
                el.clear()
                self.log(f"🧠 Los {e[2]['name']} me llevan más de lo que pensaba: les doy hasta {int(tk[e[2]['name']])} s antes de dejarlos.")
            else:
                self.__dict__.setdefault("_elite_ts", []).append(time.time())
                self.log(f"👑 Ese {e[2]['name']} no muere: debe ser élite. Lo dejo y me alejo.")
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
            # lo que muestra la vista previa: cuadrado rojo en la baldosa del bicho AHORA + su área de seguimiento
            self._trk_draw = {"x": pt[0], "y": pt[1] + int(th * 0.3), "tw": tw, "th": th, "name": e[2]["name"], "t": now, "d": d(e)}
            self.target = None
            if d(e) <= 1:
                # pegado: el sprite del pj tapa al bicho y el clic izquierdo cae sobre mí. Con click derecho en
                # modo ataque el juego pega en esa dirección sin caminar (wiki: Gameplay).
                self._release()
                tx_, ty_ = pt[0], pt[1] + int(th * 0.3)
                rab_ = now < getattr(self, "_rabia_until", 0)
                crit_ = False
                if rab_:
                    # RABIA: la memoria marca a los bichos chicos corridos una baldosa, así que si hace 2,5 s que no
                    # cae ninguno, un golpe de cada dos lo tiro barriendo las 8 baldosas de alrededor (primero donde
                    # la memoria ve bichos). Y uso el golpe crítico (Alt) cada 1,5 s: pega más y alcanza 3 baldosas.
                    # OJO: cambiar de dirección corta el golpe que está saliendo, así que cada dirección la sostengo
                    # 1,6 s enteros (un golpe completo) antes de pasar a la siguiente.
                    slot_ = int(now / 1.6)
                    if slot_ % 2 == 1 and now - getattr(self, "_last_kill_t", 0) > 2.5:
                        ring = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
                        occ = [(o[0] - px, o[1] - py) for o in ents if max(abs(o[0] - px), abs(o[1] - py)) == 1]
                        order = [r_ for r_ in ring if r_ in occ] or ring
                        dx_, dy_ = order[(slot_ // 2) % len(order)]
                        tx_, ty_ = int(cx + dx_ * tw), int(cy + dy_ * th)
                # golpe crítico (Alt + ataque): lo tiro cada tanto siempre que esté pegando, no solo rodeado. Alt se
                # aprieta solo para ese golpe y se suelta: nunca queda apretado. Si no hay cargas, sale un golpe común.
                crit_ = self.cfg.get("crit_use", True) and now - getattr(self, "_crit_t", 0) > (3.2 if rab_ else float(self.cfg.get("crit_every", 6)))
                if crit_:
                    self._crit_t = now
                    winapi.key_down("ALT")
                    time.sleep(0.05)
                try:
                    # golpe crítico (Juan): Alt apretado + clic IZQUIERDO sobre el bicho
                    winapi.click(*self.abs_pt(tx_, ty_), button="left" if crit_ else "right")
                finally:
                    if crit_:
                        time.sleep(0.08)
                        winapi.key_up("ALT")
                self.state = "seek"
                self._last_enemy = self._atk_t = now
                self.stats["state"], self.stats["target"] = "atacando", e[2]["name"]
                time.sleep(0.35)
                return True
            # a distancia: mantengo el clic SOBRE el bicho y lo voy corriendo con él en cada vuelta
            self.state = "seek"
            self._hold_on(pt[0], pt[1])
            self._last_enemy = now
            self.last_attack_click = now
            self.stats["state"], self.stats["target"] = f"voy a un {e[2]['name']} ({e[0]},{e[1]})", e[2]["name"]
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
                # cada vez que fallo en el mismo lugar lo dejo más tiempo: 5 min, 20 min, 1 hora
                uk = (e[0] // 4, e[1] // 4)
                un = self.__dict__.setdefault("_oly_unreach", {})
                un[uk] = un.get(uk, 0) + 1
                mins = (5, 20, 60)[min(un[uk], 3) - 1]
                self._oly_badzone = [z for z in getattr(self, "_oly_badzone", []) if z[2] > now] + [(e[0], e[1], now + mins * 60)]
                self._oly_walk = None
                self.log(f"🚧 No llego al {e[2]['name']} de ({e[0]},{e[1]}): lo salteo {mins} min.")
                return False
            if m:
                self.stats["state"] = f"voy a un {e[2]['name']} ({e[0]},{e[1]})"
                self._walk_to(m, (int(e[0]), int(e[1])), e[2]["name"])
                return True
        return False

    def _oly_ttk(self, name):
        """Segundos que le doy a un bicho pegado antes de pensar que es élite: según su vida, y lo que fui aprendiendo."""
        lr = (self.world.learn.get("oly_ttk") or {}).get(name)
        if lr:
            return float(lr)
        try:
            cr = next((c for c in self._creatures_db().values() if str(c.get("name", "")).lower() == str(name).lower()), None)
            hp = float(cr["hp"]) if cr else 0.0
        except Exception:
            hp = 0.0
        return min(30.0, max(7.0, 4.0 + hp / 5.0))

    def _group_cap(self, mob=None, dmg=None):
        """Cuántos bichos pegados aguanto a la vez. Una sola cuenta, la de cualquier jugador: lo que me sacan por
        ronda no puede pasar del 12% de mi vida (15% con pociones encima). Con slimes (2,5 por golpe) y 80 de vida
        son 3-4; con hormigas (4,7) son 2. Sin pociones, uno menos. Mínimo 2: en este juego todo anda en grupo."""
        hpmax = float(self._hpmax_est() or 60)
        if dmg is None:
            try:
                cr = next((c for c in self._creatures_db().values() if str(c.get("name", "")).lower() == str(mob or "").lower()), None)
                dmg = self._mob_est(cr)[0] if cr else 3.0
            except Exception:
                dmg = 3.0
        pots = (getattr(self, "_left", {}) or {}).get("pot")
        share = 0.15 if (pots or 0) >= 3 else 0.12
        n = int(share * hpmax / max(1.0, float(dmg)))
        if pots == 0 and not getattr(self, "_pots_unknown", False):
            n -= 1
        if time.time() < getattr(self, "_careful_until", 0):
            n -= 1  # vengo muriendo seguido: un rato con un bicho menos
        return max(2, min(6, n))

    def _oly_allow(self):
        """Vecinos que acepto alrededor del bicho que elijo: los que aguanto juntos, menos él."""
        mob = getattr(self, "_hunt_mob", None) or "Slime"
        n = self._group_cap(mob) - 1
        try:
            lvl = int(self.cfg.get("travel", {}).get("my_level", 1) or 1)
            cap = self.learner.crowd_cap(mob, lvl)  # lo que me mató antes con este bicho a este nivel
            if cap is not None:
                n = min(n, max(1, int(cap)))
        except Exception:
            pass
        return max(1, n)

    def _oly_fight_review(self):
        """Terminó la pelea anterior (elijo otro bicho): si salí con buena vida sumo una buena; si quedé muy
        bajo, bajo el máximo de vecinos aceptado. Con 6 buenas seguidas me animo a un vecino más."""
        self._oly_cur = None
        return  # el tope de bichos juntos ahora sale de una sola cuenta (_group_cap), no de subir y bajar a tientas
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
        if self._attacked_recently() and not (getattr(self, "_qdrive", None) and self._adjacent_mobs() < 3):
            return False  # (yendo al nido de la misión no me freno por uno o dos bichos: sigo caminando)
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
                if n_st >= 9 and n_st % 9 == 0:
                    # muchas veces trabado seguido: algo en la pantalla me frena (ventana abierta, tecla apretada,
                    # objeto en el cursor). Suelto todas las teclas, cierro ventanas y guardo una captura.
                    try:
                        for k_ in ("ALT", "SHIFT", "CTRL"):
                            winapi.key_up(k_)
                        winapi.left_up()
                        self._drop_no()
                        self._ui_clear()
                        import cv2
                        cv2.imwrite(os.path.join(cfgmod.DATA, "trabado_dbg.jpg"), self.screen.grab(self.rect), [cv2.IMWRITE_JPEG_QUALITY, 75])
                        self.log("🧹 Trabado muchas veces seguidas: solté teclas, cerré ventanas y guardé una captura (trabado_dbg.jpg).")
                    except Exception:
                        pass
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
        # adentro de un local sin nada que hacer: salgo aunque la posición tenga unos segundos (parado adentro no se
        # refresca seguido y me quedaba clavado en la tienda)
        if self.pos.get("map") and time.time() - self.pos.get("t", 0) < 60 and not getattr(self, "_busy_ui", False) \
                and not self._ui_th_alive():
            # (con un encargo de pueblo en marcha no salgo por mi cuenta: si ese encargo tiene que ir a otro mapa,
            # la salida la hace el paso del viaje, más arriba en la vuelta)
            if time.time() - getattr(self, "_exit_t", 0) > 2.5 and self._exit_building():
                self._exit_t = time.time()
                return True
        if (not t.get("enabled") and not force_dest) or not self.pos.get("map") or time.time() - self.pos["t"] > 5:
            return False
        if force_dest:
            pass
        elif (self.state in ("attack", "loot", "lootq") or time.time() - getattr(self, "_last_enemy", 0) < 6) and not self._is_travelling() \
                and not self._in_town():
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

    def _tmeter(self):
        """En qué se me va el tiempo (última hora): pegando, yendo a un bicho, viajando, pueblo/ventanas,
        descansando, parado. El objetivo es 90% entre pegar y caminar hacia un bicho."""
        now = time.time()
        dt = min(2.0, max(0.0, now - getattr(self, "_tm_t", now)))
        self._tm_t = now
        if not self.running or dt <= 0:
            return
        st = str(self.stats.get("state") or "").lower()
        cur = (self.pos.get("map") or "").lower()
        if getattr(self, "_busy_ui", False) or any(k in cur for k in ("shop", "blacksmith", "city", "ciudad", "warehouse", "hall")):
            k = "pueblo"
        elif getattr(self, "_rest_since", 0) or getattr(self, "_kite", None) or "descans" in st or "recupero" in st:
            k = "descansando"
        elif self.state in ("attack", "loot", "lootq") or st.startswith("atacando") or now - getattr(self, "_last_enemy", 0) < 2.5:
            k = "pegando"
        elif st.startswith(("voy a un", "voy hacia", "buscando")):
            k = "yendo a un bicho"
        elif getattr(self, "goto", None) or getattr(self, "mission", None) or self._is_travelling() or "yendo" in st or "zona" in st:
            k = "viajando"
        elif "destrab" in st or "trabad" in st:
            k = "trabado"
        else:
            k = "parado"
        b = self.__dict__.setdefault("_tm", {})
        m = int(now // 60)
        mb = b.setdefault(m, {})
        mb[k] = mb.get(k, 0.0) + dt
        for old in [x for x in b if x < m - 60]:
            del b[old]
        if now - getattr(self, "_tm_said", now - 1500) > 1800:
            self._tm_said = now
            t = self.time_use()
            if t["total"] > 600:
                self.log("⏱ Última hora: " + ", ".join(f"{v}% {n}" for n, v in t["pct"].items() if v) + ".")

    def time_use(self):
        tot = {}
        for d in (getattr(self, "_tm", None) or {}).values():
            for k, v in d.items():
                tot[k] = tot.get(k, 0.0) + v
        s_ = sum(tot.values())
        order = ("pegando", "yendo a un bicho", "viajando", "pueblo", "descansando", "trabado", "parado")
        return {"total": int(s_), "pct": {k: (int(round(100 * tot.get(k, 0) / s_)) if s_ else 0) for k in order}}

    def _in_town(self):
        """Mapa de ciudad (no de caza): 'Aresden City', 'Ciudad Elvine'… salvo que sea justo el destino de caza."""
        cur = (self.pos.get("map") or "").lower()
        if not cur or not any(k in cur for k in ("city", "ciudad")):
            return False
        try:
            d = self.travel_dest()
        except Exception:
            d = None
        return not (d and self._same(cur, d))

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
        live = self._exp_live()
        if live is not None:
            return live
        sh = self._sheet(3600)
        if sh and sh.get("exp") is not None and self._exp_table().get(sh.get("level")) is not None:
            return self._exp_table()[sh["level"]] + int(sh["exp"])
        return sh.get("exp_total") if sh else None

    def _hot_note(self, gain):
        """Gané experiencia acá: este lugar pasa a ser (o sigue siendo) mi zona buena del mapa."""
        try:
            na = getattr(self, "_nest_active", None)
            if na and gain > 0:
                r = self._nest_rec(na["mk"], na["x"], na["y"], na.get("mob"))
                r["exp"] = r.get("exp", 0) + int(gain)
        except Exception:
            pass
        try:
            if self.pos.get("x") is None:
                return
            if time.time() - getattr(self, "_last_kill_t", 0) > 20:
                return  # la barra se movió sin que yo mate nada (relectura, subida de nivel): no es una zona
            mk = (self.pos.get("map") or "").strip().lower()
            hx, hy = int(self.pos["x"]), int(self.pos["y"])
            hot = self.world.learn.setdefault("oly_hot", {})
            h = hot.get(mk)
            now = time.time()
            if h and max(abs(h["x"] - hx), abs(h["y"] - hy)) <= 14:
                h["exp"] = h.get("exp", 0) + gain
                h["x"], h["y"], h["t"] = int(round(0.9 * h["x"] + 0.1 * hx)), int(round(0.9 * h["y"] + 0.1 * hy)), now
            else:
                # estoy ganando en otro lado: si lo sostengo un rato, la zona buena se muda acá
                c = self.__dict__.setdefault("_hot_cand", {"x": hx, "y": hy, "exp": 0, "t": now})
                if max(abs(c["x"] - hx), abs(c["y"] - hy)) > 14 or now - c["t"] > 300:
                    c.update(x=hx, y=hy, exp=0)
                c["exp"] += gain
                c["t"] = now
                if not h or c["exp"] >= 1500:
                    hot[mk] = {"x": c["x"], "y": c["y"], "t": now, "exp": c["exp"]}
                    self._hot_cand = None
        except Exception:
            pass

    def _exp_table(self):
        """{nivel: experiencia total al empezar ese nivel}, de exp_table.json en la carpeta del server (sale de la
        wiki del server). Vacío si no hay: ahí me arreglo con lo que lea de la ficha."""
        t = getattr(self, "_exp_tb", None)
        if t is None:
            try:
                t = {int(k): int(v) for k, v in (self._load_json("exp_table.json").get("totals") or {}).items()}
            except Exception:
                t = {}
            self._exp_tb = t
        return t

    def _exp_live(self):
        """Experiencia al instante, sin abrir ninguna ventana: la barrita verde finita de arriba del tablero marca
        el % del nivel. La ficha me da cada tanto el número exacto: con eso sé cuántos píxeles mide la barra llena
        y cuánta experiencia pide el nivel, y entre lectura y lectura sigo la barra."""
        try:
            eb = getattr(self, "_expbar", None)
            sh = self._sheet(24 * 3600)
            if not eb or not sh or time.time() - eb[1] > 5 or not sh.get("level"):
                return None
            now = time.time()
            needs = self.world.learn.setdefault("oly_need", {})
            lv = str(sh["level"])
            if sh.get("exp") is not None and sh.get("next"):
                needs[lv] = int(sh["exp"]) + int(sh["next"])
            tb = self._exp_table()
            need = (tb[int(lv) + 1] - tb[int(lv)]) if tb.get(int(lv)) is not None and tb.get(int(lv) + 1) else needs.get(lv)
            st = self.__dict__.setdefault("_xl", {"len": float(self.cfg.get("calib", {}).get("exp_len") or 0), "max": 0, "lvl": lv, "cal_t": 0, "block": 0})
            glen = eb[0]
            if st["lvl"] != lv:
                st.update(lvl=lv, max=0, block=0)
            # subí de nivel: la barra se vació. Hasta releer la ficha no sé la base del nivel nuevo.
            if st.get("last") and glen < 0.5 * st["last"] and st["last"] > 40 and sh["t"] < now - 3:
                st["block"] = now
                self._census_t = 0
            st["last"] = glen
            if st["block"] and sh["t"] < st["block"]:
                return None
            st["block"] = 0
            if not need or sh.get("exp") is None:
                return None
            # calibro el largo de la barra llena cuando la ficha está recién leída
            if now - sh["t"] < 6 and sh["t"] != st["cal_t"] and sh["exp"] > 0.05 * need and glen > 20:
                L = glen / (sh["exp"] / float(need))
                if 200 < L < 4000:
                    st["len"] = L if not st["len"] or abs(L - st["len"]) > 0.05 * st["len"] else 0.7 * st["len"] + 0.3 * L
                    st["cal_t"] = sh["t"]
                    st["max"] = 0
                    old_len = float(self.cfg.get("calib", {}).get("exp_len") or 0)
                    self.cfg.setdefault("calib", {})["exp_len"] = round(st["len"], 1)
                    if abs(old_len - st["len"]) > 0.01 * st["len"]:
                        cfgmod.save(self.cfg)  # que no se pierda al reiniciar
            if not st["len"]:
                return None
            v = int(min(1.0, glen / st["len"]) * need)
            # el ruido de un par de píxeles no cuenta como bajar; una baja de verdad (morir quita experiencia) sí
            if v < st["max"] and st["max"] - v <= 0.004 * need:
                v = st["max"]
            gain = v - st["max"]
            st["max"] = v
            if 0 < gain <= 0.1 * need and st.get("seen"):
                self._exp_gain_t = now
                self._hot_note(gain)
            st["seen"] = True
            self._exp_now = {"exp": v, "need": need, "pct": round(100.0 * v / need, 2), "t": now}
            try:
                self._exp_update(self._exp_now["pct"])  # lo que muestra la pantalla del bot: el mismo % que el juego
                self.progress["exp_num"], self.progress["exp_left"] = v, max(0, need - v)
            except Exception:
                pass
            return v + int(tb[int(lv)] if tb.get(int(lv)) is not None else (sh.get("exp_off") or 0))
        except Exception:
            return None

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
            if e1 == e0 and not self._mem_struct():
                del zk[:-600]  # leo la exp de la ficha cada tanto: guardo las muertes hasta la próxima lectura
                return
            ready, self._zk = list(zk), []
            self._zk_exp = e1
            d = (e1 - e0) / float(len(ready))
            if d <= 0 or d > 500000:
                return
            self._yield_add(exp=e1 - e0)
            self._exp_gain_t = time.time()
            try:
                hx, hy, hmk = ready[-1][1], ready[-1][2], ready[-1][3]
                hot = self.world.learn.setdefault("oly_hot", {})
                h = hot.get(hmk)
                if h and max(abs(h["x"] - hx), abs(h["y"] - hy)) <= 12 and time.time() - h.get("t", 0) < 1800:
                    h["exp"] = h.get("exp", 0) + (e1 - e0)
                    h["x"], h["y"], h["t"] = int(0.8 * h["x"] + 0.2 * hx), int(0.8 * h["y"] + 0.2 * hy), time.time()
                elif not h or time.time() - h.get("t", 0) > 600 or (e1 - e0) > 0:
                    if not h or time.time() - h.get("t", 0) > 600:
                        hot[hmk] = {"x": int(hx), "y": int(hy), "t": time.time(), "exp": e1 - e0}
            except Exception:
                pass
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
            if qc and self._quest_strict():
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

    def _quest_list(self, img):
        """Cartel de misiones (arriba a la derecha): puede haber varias. Cada una: título, y abajo '¡Completado!' o
        sus objetivos 'Snake Teeth: 0/3'. Devuelve [{'name', 'done', 'a', 'b', 'objs': [(texto, a, b)]}]."""
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
        ws = self._q_words(img, {"x1": int(1640 * kx), "y1": int(225 * ky), "x2": img.shape[1], "y2": int(620 * ky)})
        lines = []
        for w in sorted(ws, key=lambda w: (w[2], w[1])):
            if lines and abs(w[2] - lines[-1][0]) < 9 * ky:
                lines[-1][1].append(w)
            else:
                lines.append([w[2], [w]])
        out = []
        for _y, lw in lines:
            txt = " ".join(w[0] for w in sorted(lw, key=lambda w: w[1])).strip()
            low = txt.lower()
            m = re.search(r"(\d+)\s*/\s*(\d+)\s*$", txt)
            if "mplet" in low or "mpiet" in low:
                if out:
                    out[-1]["done"] = True
            elif m:
                if out:
                    out[-1]["objs"].append((txt[:m.start()].strip(" :"), int(m.group(1)), int(m.group(2))))
            elif len(txt) >= 4 and any(c.isalpha() for c in txt):
                out.append({"name": txt, "done": False, "objs": []})
        for q in out:
            q["a"] = sum(min(o[1], o[2]) for o in q["objs"])
            q["b"] = sum(o[2] for o in q["objs"])
            if q["objs"] and q["a"] >= q["b"]:
                q["done"] = True
        return [q for q in out if q["done"] or q["objs"]]

    def _killer_note(self):
        """Me mataron: anoto qué bichos tenía pegados. Con dos muertes del mismo, lo dejo hasta subir 2 niveles."""
        la = getattr(self, "_last_adj", None)
        if not la or time.time() - la[1] > 15 or cfgmod.server_slug(self.cfg) == "abaddon":
            return
        lvl = self._level_mem() or int(self.cfg.get("travel", {}).get("my_level", 1))
        kk = self.world.learn.setdefault("oly_killers", {})
        for nm in la[0]:
            try:
                r_ = self._risk_rec(nm)
                r_["d"] += 1
                self._risk_judge(nm)
            except Exception:
                pass
            k = kk.setdefault(nm, {"n": 0, "lvl": lvl})
            k["n"] = k["n"] + 1 if k.get("lvl") == lvl or k["n"] < 2 else 1
            k["lvl"] = lvl
            if k["n"] == 2:
                self.log(f"🧠 {nm} ya me mató 2 veces a nivel {lvl}: no lo busco hasta nivel {lvl + 2} (si me ataca, me defiendo).")
        self.world.save()

    # ---------------------------------------------------------------- ¿le da el cuero? (aprendido, para cualquier pj)
    def _my_power(self):
        """Nivel y defensa total puesta: si suben bastante, lo aprendido de un bicho se vuelve a probar."""
        lvl = self._level_mem() or int(self.cfg.get("travel", {}).get("my_level", 1))
        dfn = sum(int((v or {}).get("v") or 0) for k, v in (self.cfg.get("worn") or {}).items() if k != "arma")
        wpn = int(((self.cfg.get("worn") or {}).get("arma") or {}).get("v") or 0)
        return lvl, dfn, wpn

    def _risk_rec(self, mob):
        R = self.world.learn.setdefault("risk", {})
        lvl, dfn, wpn = self._my_power()
        r = R.get(str(mob))
        if not r or lvl >= int(r.get("lvl", 0)) + 2 or dfn >= int(r.get("def", 0)) * 1.2 + 3 or wpn > int(r.get("wpn", 0)):
            # soy bastante más fuerte que cuando lo medí: arranco de nuevo (vuelvo a probarlo con cuidado)
            old = r or {}
            r = R[str(mob)] = {"lvl": lvl, "def": dfn, "wpn": wpn, "k": 0, "loss": 0.0, "pots": 0.0, "d": 0, "t": time.time(),
                               "verdict": "probar", "prev": old.get("verdict")}
        return r

    def _risk_track(self, adj, now):
        """Cada vuelta: si bajó la vida con bichos pegados, la pérdida se reparte entre los tipos que tenía encima."""
        hp = self.bars.get("hp")
        last = getattr(self, "_risk_hp", None)
        self._risk_hp = (hp, now)
        if hp is None or not last or last[0] is None or not adj or now - last[1] > 3:
            return
        drop = float(last[0]) - float(hp)
        if drop <= 0 or drop > 60:
            return
        for nm in adj:
            self._risk_rec(nm)["loss"] += drop / len(adj)
        if now - getattr(self, "_risk_save_t", 0) > 120:
            self._risk_save_t = now
            try:
                self.world.save()
            except Exception:
                pass

    def _risk_judge(self, mob):
        """Con lo medido decido: 'ok' (le doy), 'probar' (solo sueltos y con vida y pociones), 'no' (no lo busco)."""
        r = self._risk_rec(mob)
        k = max(1, r["k"])
        per_kill = r["loss"] / k            # % de vida que me cuesta cada uno
        pots_k = r["pots"] / k              # pociones por bicho
        v = r.get("verdict", "probar")
        if r["d"] >= 1 and r["k"] < 30:
            v = "no"
        elif r["k"] >= 8:
            v = "no" if (per_kill > 30 or pots_k > 0.6) else "ok"
        if v != r.get("verdict"):
            r["verdict"] = v
            txt = {"ok": "me da el cuero", "no": "todavía no me da", "probar": "lo pruebo de a uno"}[v]
            self.log(f"🧠 {mob}: {txt} (con {r['k']} matados: {per_kill:.0f}% de vida y {pots_k:.1f} pociones por bicho"
                     + (f", {r['d']} muerte/s" if r['d'] else "") + f"; nivel {r['lvl']}, defensa {r['def']}).")
            try:
                self.world.save()
            except Exception:
                pass
        return v

    def _risk_verdict(self, mob):
        try:
            r = (self.world.learn.get("risk") or {}).get(str(mob))
            if not r:
                return "probar"
            return self._risk_rec(mob).get("verdict", "probar")
        except Exception:
            return "ok"

    def _killer(self, mob):
        if self._risk_verdict(mob) == "no":
            return True  # lo medí y todavía no me da (vuelvo a probar con 2 niveles más o mejor equipo)
        k = (self.world.learn.get("oly_killers") or {}).get(str(mob))
        if not k or k.get("n", 0) < 2:
            return False
        lvl = self._level_mem() or int(self.cfg.get("travel", {}).get("my_level", 1))
        # (probé seguir cazándolo "de a menos" cuando lo que mata es el montón: no sirve, el enjambre viene solo
        # y me mató 4 veces más. Bicho que me mató dos veces no se busca hasta dos niveles más.)
        return lvl < int(k.get("lvl", 0)) + 2

    def _can_face(self, mob):
        """¿Ese bicho está a mi alcance hoy? (mismo criterio que para elegir zona de caza)"""
        if self._killer(mob):
            return False
        # bicho que todavía estoy midiendo: de a uno sí, pero no voy a su nido (en un nido no se 'prueba')
        try:
            r_ = (self.world.learn.get("risk") or {}).get(str(mob))
            if r_ and self._risk_verdict(mob) == "probar" and r_.get("prev") == "no":
                return False
        except Exception:
            pass
        try:
            cr = next((c for c in self._creatures_db().values() if str(c.get("name", "")).lower() == str(mob).lower()), None)
            if not cr:
                return True
            lvl = self._level_mem() or int(self.cfg.get("travel", {}).get("my_level", 1))
            hpmax = self._hpmax_est()
            cap = float(self.cfg.get("mob_dmg_share", 0.04)) * max(hpmax, 1)
            if hpmax < 150:
                cap = max(cap, 0.10 * max(hpmax, 1), 3.0)
            return self._mob_est(cr)[0] <= cap and cr["defense"] <= 30 + 1.5 * lvl
        except Exception:
            return True

    def _quest_hud(self, img):
        """De todas las misiones del cartel, la que toca ahora: primero una '¡Completado!' (hay que cobrarla);
        si no, la primera cuyos bichos ya puedo enfrentar. Devuelve (nombre, hechos, total) o None."""
        qs = self._quest_list(img)
        sig = [(q["name"], q["done"], q["a"]) for q in qs]
        self.quests = qs
        if sig != getattr(self, "_quests_sig", None):
            self._quests_sig = sig
            if qs:
                self.log("📜 Misiones: " + " · ".join(q["name"] + (" (lista para cobrar)" if q["done"] else f" {q['a']}/{q['b']}") for q in qs) + ".")
        if not qs:
            return None
        # una misión lista que no puedo cobrar (vencida, o falló el cobro) no tapa a las demás: sigo con las otras
        blocked = (self.__dict__.get("_q_turn_fail", {}).get("*", {}) or {}).get("until", 0) > time.time()
        done = None if blocked else next((q for q in qs if q["done"]), None)
        if done:
            return done["name"], 1, 1
        if blocked:
            qs = [q for q in qs if not q["done"]] or qs
        db = self._quest_db().get("quests", [])
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())

        def doable(q):
            e = max(db, key=lambda d: difflib.SequenceMatcher(None, norm(q["name"]), norm(d["name"])).ratio(), default=None)
            if not e or difflib.SequenceMatcher(None, norm(q["name"]), norm(e["name"])).ratio() < 0.75:
                return True
            return all(self._can_face(o["mob"]) for o in e["objectives"] if o["mob"] != "*" and not o.get("elite"))
        # entre las que puedo hacer, sigo la que viene avanzando; si la elegida lleva 8 minutos sin sumar, paso a otra
        # (me pasó de perseguir orcos a los que no llegaba teniendo la de slimes al lado)
        now = time.time()
        pr = self.__dict__.setdefault("_q_prog2", {})
        for q in qs:
            p = pr.setdefault(q["name"], {"a": q["a"], "t": 0.0, "since": 0.0})
            if q["a"] > p["a"]:
                p["t"] = now
            p["a"] = q["a"]
        cand = [q for q in qs if doable(q)] or qs
        cur = getattr(self, "_q_focus", None)
        cq = next((q for q in cand if q["name"] == cur), None)
        best = max(cand, key=lambda q: pr[q["name"]]["t"])
        if cq is None or (now - max(pr[cur]["t"], pr[cur]["since"]) > 480):
            # la que más recién avanzó; si ninguna avanzó, la siguiente de la lista
            if pr[best["name"]]["t"] > 0 and best["name"] != cur:
                cq = best
            else:
                i = cand.index(cq) if cq in cand else -1
                cq = cand[(i + 1) % len(cand)]
            pr[cq["name"]]["since"] = now
            if cq["name"] != cur:
                self._q_focus = cq["name"]
                self.log(f"📜 Sigo la misión '{cq['name']}' ({cq['a']}/{cq['b']}).")
        return cq["name"], cq["a"], cq["b"]

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
            db = None
            try:
                # primero lo que trae el propio juego (contents/quests.json): no hace falta que nadie pase la wiki
                from . import gamedata, world as _w
                crs = [c["name"] for c in self._creatures_db().values()]
                nz = lambda t: "".join(ch for ch in str(t).lower() if ch.isalnum())

                def mob(n):
                    k = nz(n)
                    return (next((c for c in crs if nz(c) == k), None) or next((c for c in crs if k in nz(c) or nz(c) in k), None)
                            or str(n).title())
                db = gamedata.quest_db(self.world.game_dir, self.cfg.get("nation", "aresden"), lambda f: _w.file_display(f).title(), mob)
                if db:
                    self.log(f"📜 Misiones del juego: {len(db['quests'])} leídas de la instalación ({len(db['npcs'])} personajes).")
            except Exception:
                db = None
            self._qdb = db = db or self._load_json("misiones_olympia.json") or {"quests": [], "npcs": {}}
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
        if best and br >= 0.75:
            self._qa_last = (best, hud[1], hud[2], time.time())
            return (best, hud[1], hud[2])
        # lectura mala del cartel ('20.00/0 Challenge', 'Snake Teeth: CI/3'): sigo con la última misión que sí
        # reconocí (10 min). Si no, el bot volvía a los Slimes cada vez que el OCR leía mal el título.
        la = getattr(self, "_qa_last", None)
        if la and time.time() - la[3] < 600:
            return la[0], la[1], la[2]
        return None

    def _quest_strict(self):
        """¿La misión activa justifica cazar SOLO su bicho? Sí cuando es de matar N y viene avanzando. No cuando
        es de juntar objetos (salen cada tanto) o cuando llevo 15 minutos sin avanzar: ahí se caza normal y el
        bicho de la misión solo tiene prioridad cuando aparece."""
        if not self.cfg.get("quest_strict", False) and self.bot_mode() != "quest":
            return False  # la regla es subir de nivel: la misión da prioridad a su bicho cuando aparece, nada más
        qa = self._quest_active()
        if not qa:
            return False
        now = time.time()
        pr = self.__dict__.setdefault("_q_prog", {"name": None, "a": -1, "t": now})
        if pr["name"] != qa[0]["name"] or qa[1] != pr["a"]:
            pr.update(name=qa[0]["name"], a=qa[1], t=now)
        # de juntar objetos (partes que caen cada tanto): cazo su bicho y levanto todo; le doy 45 min sin avanzar
        # antes de soltarla (las de matar, 15 min)
        return now - pr["t"] < (2700 if qa[0].get("kind") == "items" else 900)

    def _quest_mob_names(self):
        qa = self._quest_active()
        if not qa or qa[1] >= qa[2]:
            return set()
        return {o["mob"] for o in qa[0]["objectives"] if o["mob"] != "*" and not o.get("elite")
                and (o.get("count") or qa[0].get("kind") == "items")}

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
        nz = lambda t: "".join(c for c in str(t).lower() if c.isalnum())
        active = [nz(a["name"]) for a in (getattr(self, "quests", None) or [])]
        for q in self._quest_db().get("quests", []):
            if q.get("kind") != "kill" or lvl < int(q.get("level") or 1):
                continue
            if q.get("max_level") and lvl >= int(q["max_level"]):
                continue  # ya me quedó chica
            if any(difflib.SequenceMatcher(None, nz(q["name"]), a).ratio() >= 0.8 for a in active):
                continue  # ya la tengo tomada
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

    def _quest_go_accept(self, only_here=False):
        """Voy al NPC de la próxima misión (si sé dónde está) y la acepto. only_here: solo si el NPC está en el
        mapa donde estoy (con otras misiones en curso no cruzo el mundo para tomar una más)."""
        q = self._quest_pick()
        if not q:
            return False
        if only_here and not any(self._same(mp_, self.pos.get("map") or "") for mp_ in (self._quest_db().get("npcs", {}).get(q["npc"], {}) or {})):
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
        # ir a buscar una misión nunca puede llevarme a morir: nada de mazmorras (para llegar se cruzan nidos
        # peligrosos: pasó con Daara en Middleland Dungeon, 4 muertes en Farm Dungeon) salvo que ya esté ahí
        cands = [c for c in cands if self._same(c[0], cur0) or "dungeon" not in c[0].lower()]
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
        qf = self.__dict__.setdefault("_q_turn_fail", {})
        if hud and hud[2] > 0 and hud[1] >= hud[2] and qf.get("*", {}).get("until", 0) > now:
            return False  # ya probé cobrarla y no pude: no me quedo dando vueltas, sigo cazando y pruebo más tarde
        if hud and hud[2] > 0 and hud[1] >= hud[2]:
            # misión que ya me quedó chica (el juego no la paga pasado cierto nivel): no pierdo tiempo en cobrarla
            try:
                nz_ = lambda t: "".join(c for c in str(t).lower() if c.isalnum())
                lv_ = int(self.cfg.get("travel", {}).get("my_level", 1) or 1)
                qd_ = next((q for q in self._quest_db().get("quests", [])
                            if difflib.SequenceMatcher(None, nz_(q["name"]), nz_(hud[0])).ratio() >= 0.8), None)
                if qd_ and qd_.get("max_level") and lv_ >= int(qd_["max_level"]):
                    if not getattr(self, "_q_obs_said", False):
                        self._q_obs_said = True
                        self.log(f"📜 '{qd_['name']}' quedó vencida (se cobra hasta nivel {qd_['max_level']} y soy {lv_}): no la cobro más y sigo con las otras.")
                    qf["*"] = {"t0": now, "n": 0, "until": now + 6 * 3600}
                    self.goto = None
                    return False
            except Exception:
                pass
            # las misiones nunca bloquean la caza: 3 minutos para llegar y cobrar; si no sale, la dejo media hora
            f_ = qf.setdefault("*", {"t0": now, "n": 0, "until": 0})
            if now - f_.get("seen", 0) > 120:
                f_["t0"] = now
            f_["seen"] = now
            if now - f_["t0"] > 180 or f_["n"] >= 2:
                f_.update(until=now + 1800, n=0, t0=now)
                self.goto = None
                self._q_npc = None
                self.log(f"📜 No pude cobrar '{hud[0]}' (¿bolso lleno?): la dejo media hora y sigo cazando.")
                return False
            qn = getattr(self, "_q_npc", None)
            if not qn or now - qn.get("t", 0) > 600 or qn.get("q") != hud[0]:
                qn = self._quest_npc_db(hud[0]) or self._quest_where()
                if qn:
                    qn["q"] = hud[0]
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
        if not hud or (hud[1] < hud[2] and self.cfg.get("quests_take_all", True)):
            try:
                # las tomo todas: tenerla tomada no cuesta nada y va sumando mientras cazo
                if self._quest_go_accept(only_here=bool(hud)):
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

    def _quest_npc_db(self, qname):
        """Dónde se cobra esa misión según la base del server: el NPC que la da, en el mapa donde estoy si está ahí
        (con varias misiones activas, la lista del juego no dice cuál NPC es de cuál)."""
        try:
            db = self._quest_db()
            norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
            e = max(db.get("quests", []), key=lambda d: difflib.SequenceMatcher(None, norm(qname), norm(d["name"])).ratio(), default=None)
            if not e or difflib.SequenceMatcher(None, norm(qname), norm(e["name"])).ratio() < 0.75:
                return None
            places = (db.get("npcs") or {}).get(e["npc"]) or {}
            cur = self.pos.get("map") or ""
            mp = next((m for m in places if self._same(m, cur)), None) or next((m for m in places if self.world.map_for(m)), None)
            if not mp:
                return None
            return {"npc": e["npc"], "map": mp, "x": int(places[mp][0]), "y": int(places[mp][1])}
        except Exception:
            return None

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
                try:
                    self.__dict__.setdefault("_q_turn_fail", {}).setdefault("*", {"t0": time.time(), "n": 0, "until": 0})["n"] += 1
                except Exception:
                    pass
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

    def _bag_open(self, img=None, words=None):
        """¿Está el bolso abierto? Devuelve la palabra 'Peso:' de su encabezado, o None. El OCR de pantalla entera a
        veces no lee ese renglón (según el fondo), así que si no aparece lo leo recortado donde lo vi la última vez."""
        if img is None:
            img = self.screen.grab(self.rect)
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0

        def find(ws):
            for w in ws:
                if not w[0].strip().startswith("Peso"):
                    continue
                row = [v for v in ws if v is not w and abs(v[2] - w[2]) < 12 * ky]
                if any(0 < w[1] - v[1] < 130 * kx for v in row):
                    continue
                digs = sum(c.isdigit() for c in w[0]) + sum(sum(c.isdigit() for c in v[0]) for v in row if 0 < v[1] - w[1] < 300 * kx)
                if digs >= 4:
                    return w
            return None
        if words is None:
            try:
                words = self._q_words(img)
            except Exception:
                words = []
        pw = find(words)
        if not pw:
            hx, hy = getattr(self, "_bag_hdr_xy", None) or (1058 * kx, 436 * ky)
            H, W = img.shape[:2]
            box = {"x1": int(max(0, hx - 70 * kx)), "y1": int(max(0, hy - 45 * ky)), "x2": int(min(W, hx + 330 * kx)), "y2": int(min(H, hy + 45 * ky))}
            try:
                pw = find(ocr.read_words(img, box, scale=1.0))
            except Exception:
                pw = None
        if pw:
            self._bag_hdr_xy = (pw[1], pw[2])
        return pw

    def _bag_ensure(self, grab=None, tries=2):
        """Deja el bolso ABIERTO y lo confirma. F6 es un interruptor: si el detector falló y el bolso ya estaba abierto,
        el F6 lo cierra; por eso después de cada F6 vuelvo a mirar y, si no aparece, pruebo de nuevo. Confirmo con
        Windows OCR y, si no, con RapidOCR (zona segura). True si quedó abierto."""
        grab = grab or (lambda: self.screen.grab(self.rect))

        def seen():
            im = grab()
            if self._bag_open(im):
                return True
            try:
                kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
                rw = ocr.read_words_rapid(im, {"x1": 0, "y1": int(60 * ky), "x2": im.shape[1], "y2": int(im.shape[0] * 0.9)}) or []
                pw = next((w for w in rw if w[0].strip().lower().startswith("peso")), None)
                if pw:
                    self._bag_hdr_xy = (pw[1], pw[2])
                    return True
            except Exception:
                pass
            return False
        if seen():
            return True
        for _ in range(max(1, tries)):
            winapi.press("F6")
            time.sleep(1.2)
            if seen():
                return True
        self.log("🎒 No logro ver el bolso abierto.")
        return False

    def _oly_bag(self, names=True, loose=False):
        """Bolso abierto (F6): [{'name','x','y','kind'}] con lo que hay; kind = pot/gold/item. También deja
        self._gold con el oro leído. Pasa el mouse por cada cosa para leer el nombre en el cartelito."""
        import cv2
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
        img = self.screen.grab(self.rect)
        ws = ocr.read_words(img, {"x1": 0, "y1": 0, "x2": img.shape[1], "y2": int(img.shape[0] * 0.92)}, scale=1.0)
        pw = self._bag_open(img, ws)
        if pw:
            # el contenido del bolso lo leo recortado (más estable que el renglón perdido en la pantalla entera)
            try:
                bx = {"x1": int(max(0, pw[1] - 45 * kx)), "y1": int(max(0, pw[2] - 16 * ky)),
                      "x2": int(min(img.shape[1], pw[1] + 460 * kx)), "y2": int(min(img.shape[0], pw[2] + 360 * ky))}
                wb = ocr.read_words(img, bx, scale=1.0)
                if wb:
                    ws = [w for w in ws if not (bx["x1"] <= w[1] <= bx["x2"] and bx["y1"] <= w[2] <= bx["y2"])] + list(wb)
                    pw = next((w for w in wb if w[0].strip().startswith("Peso")), pw)
            except Exception:
                pass
        if not pw:
            return None  # 'peso' suelto (el 'c/u, peso 10' de la tienda) NO es el bolso: ahí no se toca nada
        try:
            import re as _re
            row_ = " ".join(w[0] for w in sorted([w for w in ws if abs(w[2] - pw[2]) < 10 * ky and w[1] >= pw[1]], key=lambda w: w[1]))
            m_ = _re.search(r"\((\d{1,2})\s*/\s*(\d{2})\)", row_.translate(str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1"})))
            if m_ and int(m_.group(1)) <= int(m_.group(2)) <= 200:
                self._bag_slots = (int(m_.group(1)), int(m_.group(2)), time.time())
            else:
                # barra leída como 1/l: '(50150)' o '(36l50)' → dos cifras, separador, dos cifras
                m2_ = _re.search(r"\((\d{1,2})[1lI|/ ]\s*(\d{2})\)", row_)
                if m2_ and int(m2_.group(1)) <= int(m2_.group(2)) <= 200:
                    self._bag_slots = (int(m2_.group(1)), int(m2_.group(2)), time.time())
        except Exception:
            pass
        self._release()
        winapi.left_up()  # nunca pasar por el bolso con el botón apretado: arrastraría (y tiraría) cosas
        from . import bagscan
        sc = bagscan.scan(img, (pw[1], pw[2]), ws, self._read_small_num)
        try:
            sc = self._stack_fix(sc)
        except Exception:
            pass
        self._bag_scan = sc
        try:
            bx_ = sc["box"]
            self._bag_box_now = (bx_[0], bx_[1], bx_[2], bx_[3], time.time())
        except Exception:
            pass
        out = []
        d, approx = sc["gold"], sc["gold_approx"]
        gp = sc.get("gold_pile")
        if d is None and gp:
            # el OCR de la ventana no vio el número: lo leo recortado; si da menos cifras que la última lectura, no le creo
            gbox = (gp[0] + int(2 * kx), gp[1] + int(10 * ky), gp[0] + int(80 * kx), gp[1] + int(38 * ky))
            d = self._read_small_num(img, *gbox)
            last = getattr(self, "_gold", None)
            if d is not None and last and len(str(d)) < len(str(last)) and str(last).endswith(str(d)):
                d = None  # se comió la primera cifra (283 -> 83)
        # el número exacto lo da el cartelito del montón de oro ('8.493 Gold'): sostengo el clic sobre el montón
        gt = None
        try:
            rel = getattr(self, "_gold_rel", None)
            pts = []
            if rel:
                pts.append((int(pw[1] + rel[0]), int(pw[2] + rel[1])))
            if gp:
                pts.append((int(gp[0]), int(gp[1])))
            if sc.get("gold_xy"):
                pts.append((int(sc["gold_xy"][0] - 17 * kx), int(sc["gold_xy"][1] - 25 * ky)))
            pts += [(it["x"], it["y"]) for it in sorted(sc["items"], key=lambda i_: i_["area"])[:3] if it["area"] < 900 * kx * ky]
            seen_p = []
            for p_ in pts[:5]:
                if any(abs(p_[0] - q[0]) < 12 and abs(p_[1] - q[1]) < 12 for q in seen_p):
                    continue
                seen_p.append(p_)
                gt = self._gold_tip(p_)
                if gt is not None:
                    self._gold_rel = (p_[0] - pw[1], p_[1] - pw[2])
                    break
            if gt is not None and sc.get("gold") and sc.get("gold_approx") and abs(gt - sc["gold"]) > 0.15 * sc["gold"]:
                # el cartelito '10.982 Gold' leído con una cifra de menos ('1092', '102'): el número abreviado del bolso
                # ('10.9k') dice otra cosa; le creo al abreviado
                gt = None
                d, approx = sc["gold"], True
                self._gold_low = None
                self._gold = d
            if gt is not None:
                d, approx = gt, False
                self._gold_low = d  # lectura exacta: vale aunque haya bajado mucho
                sc["items"] = [it for it in sc["items"] if not (abs(it["x"] - seen_p[-1][0]) < 20 and abs(it["y"] - seen_p[-1][1]) < 20)]
        except Exception:
            pass
        if gt is None and getattr(self, "_gold_rel", None) is not None:
            d = None  # ya sé leer el número exacto: si esta vez no salió, me quedo con el último (no invento)
        last_g = getattr(self, "_gold", None)
        if gt is None and d is not None and d < 1000 and (last_g is None or last_g >= 1000):
            d = None  # número chico leído a ojo (el '7' de '7.6k'): sin el cartelito no le creo
        if d is not None and last_g and last_g >= 1000 and d < 0.2 * last_g and getattr(self, "_gold_low", None) != d:
            self._gold_low = d   # de miles a casi nada de golpe: casi seguro leí otro número; le creo si se repite
            d = None
        if d is not None:
            self._gold_low = None
            self._gold = d
            self._eco_note(d, approx)
        if gp or d is not None:
            g_ = gp or sc.get("gold_xy") or (getattr(self, "_gold_rel", None) and (int(pw[1] + self._gold_rel[0]), int(pw[2] + self._gold_rel[1]))) or (0, 0)
            out.append({"x": g_[0], "y": g_[1], "kind": "gold", "name": "Gold"})
        for it in sc["items"]:
            out.append({"x": it["x"], "y": it["y"], "kind": "item", "name": "", "area": it["area"],
                        "rx": int(it.get("cx", it["x"]) - pw[1]), "ry": int(it.get("cy", it["y"]) - pw[2])})
        # memoria: la grilla del bolso se acuerda qué hay en cada lugar; solo leo (mouse encima) lo que no reconozco
        mem = self._bag_mem_load()
        hits = 0
        for o in out:
            if o["kind"] != "item":
                continue
            m = next((m for m in mem if abs(m["rx"] - o["rx"]) <= 6 * kx and abs(m["ry"] - o["ry"]) <= 6 * ky
                      and abs(m["area"] - o["area"]) <= 0.12 * max(m["area"], 1)), None)
            if not m and loose:
                # adentro de la tienda cambia la luz (el área medida no coincide) y ahí el cartelito no sale: si revisé
                # el bolso hace poco, lo que está en el mismo lugar es lo mismo
                m = next((m for m in mem if abs(m["rx"] - o["rx"]) <= 9 * kx and abs(m["ry"] - o["ry"]) <= 9 * ky
                          and abs(m["area"] - o["area"]) <= 0.35 * max(m["area"], 1)
                          and m.get("name") and time.time() - m.get("t", 0) < 2700), None)
            if m and (m.get("ok") or time.time() - m.get("t", 0) < 3600):
                o.update({k: m[k] for k in ("name", "dur", "dur_max", "dmg", "def", "lvl", "str") if m.get(k) is not None})
                o["known"] = True
                hits += 1
        self._bag_hits = (hits, sum(1 for o in out if o["kind"] == "item"))
        for o in out:
            if o["kind"] == "gold" or o.get("known"):
                continue
            if not names:
                continue
            try:
                if cfgmod.server_slug(self.cfg) != "abaddon" and self._grid().adjacent():
                    self.log("🎒 Me pegan con el bolso abierto: lo cierro y me defiendo.")
                    winapi.press("F6")
                    self._busy_ui = False
                    self._audit_t = time.time() - float(self.cfg.get("bag_audit_min", 20)) * 60 + 600
                    self._audit_abort = time.time()
                    return None
            except Exception:
                pass
            winapi.move(*self.abs_pt(int(pw[1] + 300 * kx), int(pw[2] - 60 * ky)))
            time.sleep(0.3)
            im1 = self.screen.grab(self.rect)
            winapi.move(*self.abs_pt(o["x"], o["y"]))
            time.sleep(0.3)
            # el cartelito sale SOSTENIENDO el clic sobre el objeto (sin mover el mouse: moverlo lo arrastraría)
            winapi.left_down()
            time.sleep(0.6)
            im2 = self.screen.grab(self.rect)
            winapi.left_up()
            time.sleep(0.45)  # que no se junte con otro clic (doble clic = equipar)
            # el cartelito con el nombre es lo que cambió al sostener el clic
            dif = (np.abs(im2.astype(int) - im1.astype(int)).max(axis=2) > 40).astype(np.uint8)
            dif[max(0, int(pw[2] - 16 * ky)):int(pw[2] + 16 * ky), :] = 0  # el renglón 'Peso:' no es el cartelito
            # el cartelito sale pegado al objeto (abajo a la derecha): lo demás que cambió (chat, misiones) no cuenta
            keep = np.zeros_like(dif)
            ya, yb = max(0, int(o["y"] - 40 * ky)), int(o["y"] + 230 * ky)
            xa, xb = max(0, int(o["x"] - 60 * kx)), int(o["x"] + 560 * kx)
            keep[ya:yb, xa:xb] = 1
            dif = dif * keep
            dif[max(0, o["y"] - int(30 * ky)):o["y"] + int(30 * ky), max(0, o["x"] - int(30 * kx)):o["x"] + int(30 * kx)] = 0
            ys_, xs_ = np.nonzero(dif)
            tw_ = []
            if len(xs_) > 200:
                tw_ = ocr.read_words(im2, {"x1": int(xs_.min()), "y1": int(ys_.min()), "x2": int(xs_.max()) + 1, "y2": int(ys_.max()) + 1}, scale=1.0)
            tw_ = [w for w in tw_ if not w[0].strip().startswith("Peso")]
            if tw_:
                try:
                    o.update(self._tip_parse(tw_, 10 * ky))
                except Exception:
                    pass
            tw_ = [w for w in tw_ if not w[0].strip().isdigit()]
            if tw_:
                top = min(w[2] for w in tw_)
                top_nm = " ".join(w[0] for w in sorted([w for w in tw_ if w[2] - top < 10 * ky], key=lambda w: w[1]))
                # el nombre es el PRIMER renglón del cartelito; los objetos con bonus traen renglones de bonus entre el
                # nombre y los datos ('Shirt(M)' / 'Oro (Gold) +21%' / 'Recuperación HP +10%' / 'DR 5')
                if not o.get("name") or (self._game_item(top_nm) and not self._game_item(o.get("name"))):
                    o["name"] = top_nm
                if not self._game_item(o.get("name")):
                    # cualquier renglón del cartelito que sea un objeto del juego es el nombre (va arriba de los bonus)
                    rows_t = {}
                    for w in tw_:
                        rows_t.setdefault(round(w[2] / (9 * ky)), []).append(w)
                    for k_ in sorted(rows_t)[:3]:
                        ln_ = " ".join(w[0] for w in sorted(rows_t[k_], key=lambda w: w[1])).strip()
                        if len(ln_) >= 4 and "+" not in ln_ and ":" not in ln_ and self._game_item(ln_):
                            o["name"] = ln_
                            break
                if not self._game_item(o.get("name")) and len(xs_) > 200 and ocr.ENGINE == "mixto" and self._mobs_within(4) == 0:
                    # modo mixto: el nombre verde de los objetos con bonus lo lee mejor RapidOCR (más lento: solo sin
                    # bichos cerca y solo cuando Windows no pudo)
                    try:
                        rw = ocr.read_words_rapid(im2, {"x1": int(xs_.min()), "y1": int(ys_.min()), "x2": int(xs_.max()) + 1, "y2": int(ys_.max()) + 1}) or []
                        rows_r = {}
                        for w in rw:
                            rows_r.setdefault(round(w[2] / (9 * ky)), []).append(w)
                        for k_ in sorted(rows_r)[:3]:
                            ln_ = " ".join(w[0] for w in sorted(rows_r[k_], key=lambda w: w[1])).strip()
                            if len(ln_) >= 4 and "+" not in ln_ and ":" not in ln_ and self._game_item(ln_):
                                o["name"] = ln_
                                break
                    except Exception:
                        pass
                if not self._game_item(o.get("name")) and len(xs_) > 200:
                    # los objetos con bonus tienen el nombre en verde oscuro y el OCR común lo saltea: leo agrandada
                    # la franja de arriba del cartelito
                    try:
                        import cv2
                        cy0 = max(0, int(top - 34 * ky))
                        strip = im2[cy0:int(top - 6 * ky), int(xs_.min()):int(xs_.max()) + 1]
                        if strip.size:
                            g3 = cv2.cvtColor(strip.max(axis=2), cv2.COLOR_GRAY2BGR)
                            for sc_ in (3.0, 2.0):
                                sw = ocr.read_words(g3, {"x1": 0, "y1": 0, "x2": g3.shape[1], "y2": g3.shape[0]}, scale=sc_)
                                nm2 = " ".join(w[0] for w in sorted(sw, key=lambda w: w[1])).strip()
                                if nm2 and self._game_item(nm2):
                                    o["name"] = nm2
                                    break
                    except Exception:
                        pass
            if any(k in o["name"].lower() for k in ("potion", "poción", "pocion")):
                o["kind"] = "pot"
        winapi.move(*self.abs_pt(int(pw[1] + 300 * kx), int(pw[2] - 60 * ky)))
        if names and not (loose and any(o["kind"] == "item" and not o.get("name") for o in out)):
            try:
                self._bag_mem_save([dict({k: o.get(k) for k in ("rx", "ry", "area", "name", "dur", "dur_max", "dmg", "def", "lvl", "str")},
                                         ok=bool(self._game_item(o.get("name"))), t=int(time.time()))
                                    for o in out if o["kind"] == "item"])
            except Exception:
                pass
        try:
            # frascos azules (maná): los paso como objeto con nombre para que se puedan vender (la lista de venta
            # confirma el nombre antes de vender: si no dice Mana Potion, no se vende)
            if names:
                for (x_, y_), kd_ in zip(sc.get("pot_xy") or [], sc.get("pot_kinds") or []):
                    if kd_ == "maná":
                        out.append({"x": int(x_), "y": int(y_), "kind": "item", "name": "Mana Potion", "area": 0,
                                    "rx": int(x_ - pw[1]), "ry": int(y_ - pw[2])})
                    elif kd_ == "energía" and int((sc.get("pots") or {}).get("energía") or 0) > int(self.cfg.get("keep_sp_pots", 4)) \
                            and not sc.get("unknown"):
                        out.append({"x": int(x_), "y": int(y_), "kind": "item", "name": "Revitalizing Potion", "area": 0,
                                    "rx": int(x_ - pw[1]), "ry": int(y_ - pw[2]), "surplus": True})
        except Exception:
            pass
        return out

    def _oly_sell(self, sell, tabp, words, grab, click):
        """Vender (como lo hace Juan): pestaña 'Vender' → doble clic en cada objeto del bolso (queda con recuadro
        amarillo y aparece en la lista con su precio) → reviso que en la lista esté SOLO lo que quise vender (lo que
        sobre lo saco con un clic) → 'Vender ítems'."""
        import difflib
        norm = lambda t: "".join(c for c in str(t).lower() if c.isalnum())
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
        ws = words(grab())
        vt = next((w for w in ws if (norm(w[0]) == "vender" or (norm(w[0]).endswith("ender") and len(norm(w[0])) <= 7)) and abs(w[2] - tabp[2]) < 15 * ky), None)
        click(*(vt[1:3] if vt else (tabp[1] + 346 * kx, tabp[2])))
        time.sleep(1.2)
        ws = words(grab())
        btn = next((w for w in ws if norm(w[0]) == "vender" and w[2] > tabp[2] + 300 * ky), None)
        if not btn and not any(norm(w[0]).startswith("arrastra") for w in ws):
            self.log("🛒 No se abrió la pestaña Vender: no toco nada (un doble clic acá equiparía el objeto).")
            return
        want = []
        for o in sell:
            # el cartelito de una pila dice '18 Slime Jelly': la cantidad no es parte del nombre (ni un 'afijo')
            base_ = re.sub(r"^\s*\d+\s*[x×]?\s*", "", o.get("name") or "")
            g_ = self._game_item(base_) or {}
            nm_ = re.sub(r"^\s*\d+\s*[x×]?\s*", "", g_.get("name") or base_)
            af_ = g_.get("affix") or ""
            if af_.strip().isdigit():
                af_ = ""
            want.append(norm((af_ + " " if af_ else "") + nm_))
        winapi.left_up()
        for o in sell:
            x, y = self.abs_pt(o["x"], o["y"])
            # equipo repetido (ej. 20 varitas compradas juntas) queda apilado en el mismo punto: cada doble clic
            # saca el de arriba, así que insisto varias veces. Si abajo aparece otra cosa, la lista la delata y la saco.
            reps = 5 if (self._game_item(o.get("name")) or {}).get("type") == 1 else 1
            for _k in range(reps):
                winapi.click(x, y)
                time.sleep(0.12)
                winapi.click(x, y)
                time.sleep(0.6)
                # lo apilado (ej. Slime Jelly x3) pregunta '¿Cuántos vender?' con la cantidad disponible ya puesta:
                # confirmo con el botón 'Vender' del cartel (el que tiene 'Cancelar' al lado)
                try:
                    nq = lambda t: "".join(c for c in t.lower() if c.isalnum())
                    imq = grab()
                    wq = words(imq)
                    cq = next((w for w in wq if nq(w[0]).startswith(("disponible", "cuantos", "cuántos"))), None)
                    if not cq and reps == 1:
                        try:
                            import cv2
                            cv2.imwrite(os.path.join(cfgmod.DATA, "venta_dbg.jpg"), imq, [cv2.IMWRITE_JPEG_QUALITY, 80])
                        except Exception:
                            pass
                    if cq:
                        try:
                            import cv2
                            cv2.imwrite(os.path.join(cfgmod.DATA, "venta_cuantos.jpg"), imq, [cv2.IMWRITE_JPEG_QUALITY, 80])
                        except Exception:
                            pass
                        can = next((w for w in wq if nq(w[0]) == "cancelar" and 0 < w[2] - cq[2] < 220 * ky), None)
                        bq = can and next((w for w in wq if nq(w[0]) == "vender" and abs(w[2] - can[2]) < 12 * ky and w[1] < can[1]), None)
                        if bq:
                            click(bq[1], bq[2])
                            time.sleep(0.7)
                        elif can:
                            click(can[1], can[2])  # no ubico el botón: cancelo y no dejo el cartel abierto
                            time.sleep(0.5)
                except Exception:
                    pass
        winapi.move(*self.abs_pt(int(tabp[1] + 300 * kx), int(tabp[2] - 60 * ky)))
        time.sleep(0.5)

        def listed(rapid=False):
            x_lo, x_hi = tabp[1] - 60 * kx, tabp[1] + 520 * kx
            bh_ = getattr(self, "_bag_hdr_xy", None)
            if bh_ and bh_[0] - 45 * kx > x_lo:
                x_hi = min(x_hi, bh_[0] - 45 * kx)  # el bolso (se puede mover) no es parte de la lista de venta
            ws_ = None
            if rapid:
                # segunda lectura con RapidOCR (más lenta pero más certera con nombres): solo la zona de la lista
                ws_ = ocr.read_words_rapid(grab(), {"x1": max(0, int(x_lo)), "y1": int(tabp[2] + 30 * ky),
                                                    "x2": int(x_hi), "y2": int(tabp[2] + 605 * ky)})
            if ws_ is None:
                ws_ = words(grab())
            rows = {}
            for w in ws_:
                if x_lo <= w[1] <= x_hi and tabp[2] + 35 * ky < w[2] < tabp[2] + 600 * ky:
                    rows.setdefault(round(w[2] / (11 * ky)), []).append(w)
            out = []
            for r_ in rows.values():
                r_ = sorted(r_, key=lambda v: v[1])
                txt = " ".join(v[0] for v in r_)
                low = txt.lower()
                if low.startswith("recibes") or "vender" in low or "arrastra" in low or "quieras" in low or "quitarlos" in low or "puedes" in low \
                        or "ítems" in low or "items" in low or "lista" in low or "clic" in low or "cuántos" in low or "disponible" in low or "cancelar" in low:
                    continue  # el cartel de la lista vacía ('Arrastra aquí los ítems…') o el de cantidad, no son ítems
                # el precio va a la derecha del renglón y a veces se lee como letras ('700' → '7ClCl'): no es nombre
                isnum = lambda t: bool(t) and all(c in "0123456789.,ClOoIl|SsBD" for c in t) and any(c in "0123456789ClO" for c in t) \
                    and not t.isalpha() or t.replace(".", "").replace(",", "").isdigit()
                toks = [v for v in r_]
                while len(toks) > 1 and (isnum(toks[-1][0]) or (toks[-1][1] > tabp[1] + 250 * kx and sum(c.isdigit() for c in toks[-1][0]) >= 1)):
                    toks.pop()
                nm = " ".join(v[0] for v in toks if not v[0].replace(".", "").replace(",", "").isdigit())
                nm = nm.lstrip("0123456789 ")
                if len(norm(nm)) >= 3:
                    out.append((nm, r_[0][1], r_[0][2]))
            return out, ws_

        def allowed(nm):
            k = norm(re.sub(r"^\s*\d+\s*[x×]?\s*", "", nm)).rstrip(".")
            # mismo nombre, o casi (una letra mal leída) comparando el MISMO largo desde el principio: 'Trousers(M)' no
            # pasa por 'Knee Trousers(M)', y 'Slime -lelly' sí pasa por 'Slime Jelly'
            if any(k == w_ or (len(k) >= 8 and w_.startswith(k)) or (len(w_) >= 8 and k.endswith(w_[-8:]))
                   or (abs(len(k) - len(w_)) <= 2 and k[:2] == w_[:2]
                       and difflib.SequenceMatcher(None, k, w_).ratio() >= 0.84) for w_ in want):
                return True
            # la lista de venta de la tienda se lee mejor que los cartelitos del bolso ('Righteous Superior Dagger' vs
            # 'ghteous...'): si el nombre de la lista, juzgado con las mismas reglas de venta, es 'vender', lo dejo
            try:
                nm2 = re.sub(r"^\s*\d+\s*[x×]?\s*", "", nm).strip().rstrip(".").strip()
                return self._bag_judge({"name": nm2, "kind": "item"})[0] == "sell"
            except Exception:
                return False
        def recheck(rows):
            """Lo que Windows leyó como 'no es lo que quería vender', lo releo con RapidOCR (modo mixto) antes de
            sacarlo: 'Slime -nelly' o 'HealthPotion' mal leídos no son objetos equivocados."""
            if not rows or ocr.ENGINE != "mixto":
                return rows
            try:
                lr, _w = listed(True)
            except Exception:
                return rows
            keep = []
            for l in rows:
                r2 = next((m for m in lr if abs(m[2] - l[2]) <= 9 * ky), None)
                if r2 and allowed(r2[0]):
                    continue
                keep.append(l)
            return keep
        for _ in range(8):
            ls, ws = listed()
            extra = recheck([l for l in ls if not allowed(l[0])])
            if not extra:
                break
            self.log(f"🛒 En la lista de venta apareció '{extra[0][0]}' que no quería vender: lo saco.")
            click(extra[0][1] + 20 * kx, extra[0][2])
            time.sleep(0.7)
        ls, ws = listed()
        if not ls:
            # no quedó nada en la lista (ya no había nada en ese lugar del bolso, o el doble clic no lo agarró)
            if time.time() - getattr(self, "_sell_miss_t", 0) > 120:
                self._sell_miss_t = time.time()
                self.log("🛒 Quise vender " + ", ".join(o.get("name") or "?" for o in sell) + " pero no apareció en la lista de venta: guardé la captura.")
                try:
                    import cv2
                    cv2.imwrite(os.path.join(cfgmod.DATA, "venta_vacia.jpg"), grab(), [cv2.IMWRITE_JPEG_QUALITY, 80])
                except Exception:
                    pass
            return False
        if recheck([l for l in ls if not allowed(l[0])]):
            self.log("🛒 La lista de venta no coincide con lo que quería vender: no vendo nada y vacío la lista.")
            self._sell_list_clear(listed, click, kx)
            return
        rec = next((w for w in ws if norm(w[0]).startswith("recibes")), None)
        tot = ""
        if rec:
            tot = "".join(v[0] for v in sorted([v for v in ws if abs(v[2] - rec[2]) < 8 * ky and v[1] > rec[1]], key=lambda v: v[1]))
        btn = next((w for w in ws if norm(w[0]).startswith("vender") and w[2] > tabp[2] + 300 * ky), None)
        if not btn:
            # el botón queda gris claro sobre la barra de abajo y el OCR de Windows a veces no lo lee: pruebo RapidOCR
            # y, si tampoco, el lugar fijo del botón respecto de la pestaña 'Vender' (misma ventana, siempre igual)
            try:
                im_ = grab()
                rw_ = ocr.read_words_rapid(im_, {"x1": max(0, int(tabp[1] - 420 * kx)), "y1": int(tabp[2] + 300 * ky),
                                                 "x2": int(tabp[1] + 200 * kx), "y2": min(im_.shape[0], int(tabp[2] + 720 * ky))}) or []
                btn = next((w for w in rw_ if norm(w[0]).startswith("vender")), None)
            except Exception:
                btn = None
            if not btn:
                btn = ("vender", tabp[1] - 208 * kx, tabp[2] + 650 * ky)
                self.log("🛒 No leo el botón 'Vender ítems': lo aprieto en su lugar de siempre.")
        if not btn:
            self.log("🛒 No veo el botón 'Vender ítems': no vendo y vacío la lista.")
            self._sell_list_clear(listed, click, kx)
            return
        click(btn[1] + 30 * kx, btn[2])
        time.sleep(1.5)
        self._busy_beat = time.time()
        self.log("🛒 Vendí: " + ", ".join(l[0] for l in ls) + (f" (recibí {tot})" if tot else "") + ".")
        self._sal_sold_t = time.time()
        self._bag_mem = []
        self._bag_sell = []
        return True

    def _sell_list_clear(self, listed, click, kx):
        """Saco todo de la lista de venta antes de cerrar la tienda: lo que queda cargado en la lista se queda marcado
        en el bolso (borde amarillo) y así no se puede equipar, reparar ni leer hasta reloguear. Pruebo clic izquierdo
        sobre el renglón; si no sale, derecho; y verifico releyendo la lista."""
        for btn_ in ("left", "left", "right", "right"):
            ls_, _ws = listed()
            if not ls_:
                return True
            for l in ls_:
                click(l[1] + 20 * kx, l[2], btn_) if btn_ == "right" else click(l[1] + 20 * kx, l[2])
                time.sleep(0.5)
        ls_, _ws = listed()
        if ls_:
            self.log("⚠ No pude vaciar la lista de venta: " + ", ".join(l[0] for l in ls_) + " puede quedar marcado (borde amarillo).")
            self._sell_stuck_t = time.time()
            return False
        return True

    def _gold_tip(self, pt):
        """Sostengo el clic en ese punto del bolso y leo el cartelito: si dice 'N Gold', devuelvo N."""
        import re
        winapi.left_up()
        winapi.move(*self.abs_pt(pt[0], pt[1]))
        time.sleep(0.3)
        winapi.left_down()
        time.sleep(0.6)
        im = self.screen.grab(self.rect)
        winapi.left_up()
        time.sleep(0.4)
        H, W = im.shape[:2]
        ws = ocr.read_words(im, {"x1": max(0, pt[0] - 60), "y1": max(0, pt[1] - 20), "x2": min(W, pt[0] + 420), "y2": min(H, pt[1] + 160)}, scale=1.0)
        for w in ws:
            if w[0].strip().lower() in ("gold", "oro"):
                row = sorted([v for v in ws if abs(v[2] - w[2]) < 8 and v[1] < w[1]], key=lambda v: v[1])
                t = "".join(v[0] for v in row).translate(str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "S": "5", "B": "8"}))
                dg = re.sub(r"[^0-9]", "", t)
                if dg:
                    return int(dg)
        return None

    def _bag_mem_load(self):
        m = getattr(self, "_bag_mem", None)
        if m is None:
            try:
                with open(os.path.join(cfgmod.profile_dir(self.cfg), "bolso.json"), encoding="utf-8") as f:
                    d_ = json.load(f)
                    m = (d_.get("items") or []) if d_.get("v") == 2 else []
            except Exception:
                m = []
            self._bag_mem = m
        return m

    def _bag_mem_save(self, items):
        self._bag_mem = items
        try:
            with open(os.path.join(cfgmod.profile_dir(self.cfg), "bolso.json"), "w", encoding="utf-8") as f:
                json.dump({"v": 2, "t": int(time.time()), "items": items}, f, ensure_ascii=False)
        except Exception:
            pass

    # cuánto rinde cada bonus para un guerrero de espada larga a dos manos (multiplica el daño o la defensa)
    AFFIX_W = {"sharp": 1.15, "ancient": 1.2, "critical": 1.12, "crushing": 1.12, "agile": 1.08, "special": 1.06,
               "poisoning": 1.05, "righteous": 1.05, "light": 1.03, "strong": 1.02, "mana": 1.0, "converting": 1.0}
    AFFIX_A = {"strong": 1.03, "light": 1.02, "ancient": 1.08, "special": 1.04}

    def _gear_score(self, name, val=None):
        """Valor de una pieza para la build: daño o defensa base por el bonus que tenga."""
        it = self._game_item(name) or {}
        base = float(val if val is not None else (it.get("dmg") or it.get("def") or 0))
        af = str(it.get("affix") or "").lower()
        if not af:
            return base
        tbl = self.AFFIX_W if str(it.get("slot") or "").startswith("arma") else self.AFFIX_A
        f = max([tbl.get(w, 1.02) for w in af.split()] or [1.0])
        return base * f

    def _bag_judge(self, o):
        """Qué hago con un objeto del bolso: ('equip'|'sell'|'keep'|'broken'|'other', motivo, ficha, casillero, valor)."""
        it = self._game_item(o.get("name"))
        if o.get("surplus"):
            return ("sell", "pociones de energía de más: cada frasco ocupa un lugar del bolso", it, None, 0)
        if not it:
            return ("other", "no lo reconozco", None, None, 0)
        if it.get("type") == 5:
            keep_ids = getattr(self, "_quest_item_ids", None)
            if keep_ids is None:
                keep_ids = set()
                try:
                    from . import gamedata
                    for q_ in gamedata.load_quests(self.world.game_dir) or []:
                        for g_ in ((q_.get("objectives") or {}).get("gather") or []):
                            keep_ids.add(int(g_["id"]))
                except Exception:
                    pass
                self._quest_item_ids = keep_ids
            if int(it.get("id", -1)) in keep_ids:
                return ("keep", "lo pide una misión", it, None, 0)
            # solo partes de bichos; gemas, minerales, fichas de evento y cosechas no se venden solas
            if any(k in it["name"].lower() for k in ("token", "ore", "nugget", "diamond", "crystal", "sapphire", "ruby", "emerald",
                                                     "coal", "mithral", "gold", "clay", "pumpkin", "radish", "mushroom", "garlic")):
                return ("keep", "material valioso: lo decide Juan", it, None, 0)
            return ("sell", "material de botín", it, None, 0)
        if "mana potion" in str(it.get("name") or "").lower() and int(((getattr(self, "_stats_vals", None) or {}).get("MAG")) or 10) <= 15:
            return ("sell", "poción de maná: un guerrero no la usa y ocupa lugar", it, None, 0)
        if it.get("type") != 1 or not it.get("slot"):
            return ("other", "no es equipo", it, None, 0)
        # ropa de mujer (W) / de hombre (M): solo me sirve la de mi sexo (lo sé por lo que tengo puesto)
        nm_ = str(o.get("name") or it.get("name") or "")
        worn_names = " ".join(str((v or {}).get("name") or "") for v in (self.cfg.get("worn") or {}).values())
        mine = "(M)" if "(M)" in worn_names else ("(W)" if "(W)" in worn_names else None)
        if mine and nm_.rstrip().endswith(("(M)", "(W)")) and not nm_.rstrip().endswith(mine):
            return ("sell", "es ropa del otro sexo: no me la puedo poner (Juan: lo de (W) se vende)", it, None, 0)
        slot = "arma" if str(it["slot"]).startswith("arma") else it["slot"]
        val = int(it.get("dmg") or it.get("def") or 0)
        worn = (self.cfg.get("worn") or {}).get(slot)
        if worn and not worn.get("ok"):
            worn = None  # solo cuenta lo que comprobé que quedó puesto
        full = (it.get("affix") + " " if it.get("affix") else "") + it["name"]
        if worn and str(worn.get("name") or "").startswith("?"):
            # ese lugar lo equipó Juan a mano y no sé con qué. Lo único que vendo es lo viejo que conozco; cualquier
            # otra pieza suelta de ese lugar es la de él que se salió (se rompió): se repara y me la vuelvo a poner.
            junk = set(self.cfg.get("junk_equip") or ("Broad Sword", "Shirt(M)", "Magic Wand(MS0)", "Shoes", "Cap(M)", "Tunic(M)",
                                                      "Knee Trousers(M)", "Wood Shield", "Targe Shield", "Scimitar", "Superior Scimitar",
                                                      "Long Sword", "Short Sword", "Light Axe", "Superior Light Axe"))
            if it["name"] in junk:
                return ("sell", "equipo viejo que ya no uso", it, slot, val)
            if slot == "escudo":
                return ("keep", "no sé si Juan me dejó con arma a dos manos: el escudo no lo toco", it, slot, val)
            if o.get("dur") == 0:
                return ("broken", "está roto", it, slot, val)
            return ("equip", "es del equipo que me puso Juan y quedó suelto: me lo vuelvo a poner", it, slot, val)
        lvl = self._level_mem() or int(self.cfg.get("travel", {}).get("my_level", 1))
        fam = str(self._load_json("tiendas.json").get("talent") or "").strip().lower()
        # regla de Juan (17/11): con bonus o sin bonus, lo que no es de la guía o no mejora lo puesto se VENDE; lo de
        # la guía con bonus se compara (daño/defensa × bonus) y si rinde más, me lo pongo
        nosell = "sell"
        if worn and worn.get("name") == full:
            if o.get("dur") == 0:
                # tengo uno igual puesto y andando: el repuesto roto no se arregla con 'Reparar Todo' (solo arregla lo
                # puesto) y me hacía ir al herrero una y otra vez. Lo vendo.
                return ("sell", "repuesto roto de lo que ya tengo puesto", it, slot, val)
            if slot in ("arma", "escudo"):
                # NUNCA vendo un arma o escudo igual al que figura puesto: pasó (18/11) que un doble clic equipó otra
                # cosa, la ficha vieja seguía diciendo 'Superior Claymore' y vendió la de verdad como 'repuesto'
                # puede ser el mismo que tenía puesto y se salió solo al romperse (ya reparado). Solo me lo pongo si
                # hay motivo para dudar (dejé de matar, me mataron) y una sola vez: si es un repuesto, cambiarlo en
                # cada revisión es perder un minuto y arriesgar quedar sin arma.
                now_ = time.time()
                last_ = getattr(self, "_same_eq_t", 0)
                if now_ < getattr(self, "_worn_suspect_until", 0) and (now_ - last_ > 600 or now_ - last_ < 15):
                    if now_ - last_ > 600:
                        self._same_eq_t = now_
                    return ("equip", "igual al que tenía puesto: me lo pongo por si se había salido", it, slot, val)
                return ("keep", "igual al arma/escudo que figura puesto: no lo vendo nunca", it, slot, val)
            return ("sell", "es igual a lo que tengo puesto", it, slot, val)
        if slot == "arma" and not worn:
            if o.get("dur") == 0:
                return ("broken", "está roto", it, slot, val)
            if int(it.get("level") or 0) > lvl:
                return ("keep", f"pide nivel {it['level']}", it, slot, val)
            return ("equip", "no tengo arma comprobada puesta: cualquiera es mejor que pegar a mano", it, slot, val)
        if slot == "arma" and fam and str(it.get("family") or "").lower() != fam:
            return ("sell", f"no es {fam} (mi talento): no es de la guía", it, slot, val)
        if slot == "arma" and worn:
            # lo puesto no es de mi talento (ej. una varita que se equipó por error): cualquier arma de mi talento
            # que pueda usar va antes, sin comparar puntajes (y nunca se vende por 'no mejorar')
            gw_ = self._game_item(str(worn.get("name") or "")) or {}
            wn_ = str(worn.get("name") or "").lower()
            if (gw_ and fam and str(gw_.get("family") or "").lower() not in ("", fam)) or \
                    (not gw_ and any(k in wn_ for k in ("wand", "staff", "bow", "varita"))):
                if int(it.get("level") or 0) > lvl:
                    return ("keep", f"pide nivel {it['level']}", it, slot, val)
                return ("equip", f"es {fam} y lo que tengo puesto no", it, slot, val)
        if slot == "arma" and str(it.get("slot") or "") == "arma" and self._two_handed() \
                and any(str((self._game_item(w_.get("name")) or {}).get("slot") or "") == "arma2m" for w_ in [worn or {}]):
            return ("sell", "es de una mano y la guía es a dos manos", it, slot, val)
        if worn:
            sc_new = self._gear_score(full, val)
            sc_old = self._gear_score(str(worn.get("name") or ""), worn.get("v"))
            if sc_new <= sc_old:
                return ("sell", f"no mejora lo puesto ({worn.get('name')}: {sc_old:.1f} contra {sc_new:.1f})", it, slot, val)
        if o.get("dur") == 0:
            return ("broken", "está roto", it, slot, val)
        if int(it.get("level") or 0) > lvl:
            return ("keep", f"pide nivel {it['level']}", it, slot, val)
        return ("equip", "mejora lo puesto" if worn else "no sé qué tengo puesto ahí: pruebo", it, slot, val)

    @staticmethod
    def _tip_parse(ws, tol=10):
        """Cartelito de un objeto (mouse encima, sin clic): nombre arriba y debajo sus datos.
        'Durabilidad (End): 0 / 700' -> dur 0, dur_max 700. Sirve en cualquier server con este cliente."""
        import re
        fix = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "S": "5", "B": "8"})
        lines = []
        for w in sorted(ws, key=lambda w: (w[2], w[1])):
            if lines and abs(w[2] - lines[-1][0]) < tol:
                lines[-1][1].append(w)
            else:
                lines.append([w[2], [w]])
        txt = [" ".join(v[0] for v in sorted(l[1], key=lambda v: v[1])) for l in lines]
        is_stat = lambda t: any(k in t.lower() for k in ("durab", "(end)", "daño", "(dmg)", "nivel", "(lvl)", "fuerza", "(str)", "defensa", "(def)"))
        first = next((i for i, t in enumerate(txt) if is_stat(t)), None)
        if first is not None and first > 0:
            txt = txt[first - 1:]  # el nombre es el renglón de arriba del primer dato; lo de más arriba es otra cosa
        elif first == 0:
            txt = [""] + txt
        out = {"name": txt[0].strip()} if txt else {}
        for t in txt[1:]:
            low = t.lower()
            tail = t.split(":", 1)[1] if ":" in t else t
            nums = [int(n) for n in re.findall(r"\d+", tail.split("(")[0].translate(fix))]
            if ("durab" in low or "(end)" in low) and len(nums) == 1 and "+" not in t and len(str(nums[0])) >= 3:
                # RapidOCR a veces pierde la barra: '300300' = 300/300, '791200' = 79/1200 (el máximo es redondo)
                d_ = str(nums[0])
                for j in range(1, len(d_)):
                    a_, b_ = int(d_[:j]), int(d_[j:])
                    if d_[j] != "0" and a_ <= b_ and 50 <= b_ <= 5000 and b_ % 50 == 0:
                        nums = [a_, b_]
                        break
            if ("durab" in low or "(end)" in low) and len(nums) >= 2 and "+" not in t:
                out["dur"], out["dur_max"] = nums[0], nums[1]
            elif ("daño" in low or "dmg" in low or "dano" in low) and nums:
                out["dmg"] = nums[0]
            elif ("nivel" in low or "(lvl)" in low) and nums:
                out["lvl"] = nums[0]
            elif ("fuerza" in low or "(str)" in low) and nums:
                out["str"] = nums[0]
            elif ("defensa" in low or "(def)" in low) and nums:
                out["def"] = nums[0]
        return out

    # puntos del muñeco de la ficha (F5), relativos a la palabra 'Nivel:' a 1920x1080. Sosteniendo el clic sobre
    # cada pieza sale su cartelito con nombre y durabilidad (probado: gorro, arma, pecho, escudo, capa, piernas, pies).
    GEAR_PTS = ((-170, 60), (-135, 60), (-205, 105), (-170, 105), (-135, 150), (-205, 205), (-170, 205), (-170, 285), (-205, 150))

    def _gear_read(self):
        """Lo que tengo PUESTO de verdad, leído del muñeco de la ficha. Devuelve {nombre: {name, dur, dur_max, dmg,
        def}} o None si no pude abrir/leer la ficha. Sirve para cualquier personaje y servidor con este cliente."""
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0

        def anchor():
            im = self.screen.grab(self.rect)
            H, W = im.shape[:2]
            ws = ocr.read_words(im, {"x1": int(W * 0.12), "y1": int(H * 0.1), "x2": int(W * 0.62), "y2": int(H * 0.86)}, scale=1.0, prefer="windows")
            nv = next((w for w in ws if w[0].strip().lower().startswith("nivel")), None)
            if nv and any(w[0].strip().lower().startswith(("talentos", "hambre", "exp")) for w in ws):
                return nv
            return None
        self._release()
        nv = anchor()
        opened = False
        if not nv:
            winapi.press("F5")
            time.sleep(1.3)
            opened = True
            nv = anchor()
        if not nv:
            if opened:
                winapi.press("F5")  # no la reconocí: la dejo como estaba
                time.sleep(0.4)
            return None
        out = {}
        try:
            for dx, dy in self.GEAR_PTS:
                x, y = int(nv[1] + dx * kx), int(nv[2] + dy * ky)
                winapi.left_up()
                winapi.move(*self.abs_pt(x, y))
                time.sleep(0.25)
                winapi.left_down()
                time.sleep(0.6)
                im2 = self.screen.grab(self.rect)
                winapi.left_up()
                time.sleep(0.25)
                x1, y1 = max(0, int(x - 14 * kx)), max(0, int(y + 44 * ky))
                box = {"x1": x1, "y1": y1, "x2": min(im2.shape[1], int(x + 470 * kx)), "y2": min(im2.shape[0], int(y + 190 * ky))}
                tw = ocr.read_words(im2, box, scale=1.0, prefer="windows")
                # renglones del cartelito: arrancan pegados a su borde izquierdo; lo que quede a la derecha tras un hueco
                # grande es texto de la ficha que se ve atrás
                lines = []
                for w in sorted(tw, key=lambda w: (w[2], w[1])):
                    if lines and abs(w[2] - lines[-1][0][2]) < 9 * ky:
                        lines[-1].append(w)
                    else:
                        lines.append([w])
                keep = []
                for ln in lines:
                    ln = sorted(ln, key=lambda w: w[1])
                    if ln[0][1] - len(ln[0][0]) * 4.6 * kx > x1 + 34 * kx:
                        continue
                    row = [ln[0]]
                    for w in ln[1:]:
                        gap = w[1] - row[-1][1] - (len(w[0]) + len(row[-1][0])) * 4.6 * kx
                        if gap > 55 * kx:
                            break
                        row.append(w)
                    keep.append(row)
                if not keep:
                    continue
                name = " ".join(w[0] for w in keep[0]).strip()
                tp = self._tip_parse([w for r in keep[1:] for w in r], tol=9 * ky) if len(keep) > 1 else {}
                if not name or not any(k in tp for k in ("dur", "dmg", "def")):
                    continue
                if any(k in name.lower() for k in ("nivel", "exp", "peso", "hambre", "talentos", "contrib", "reput", "majest")):
                    continue
                gi_ = self._game_item(name)
                if gi_ and gi_.get("name") and len(name) > len(gi_["name"]) + 2 and name.lower().startswith(gi_["name"].lower()):
                    name = gi_["name"]  # 'Combatant Cape -nbre:' → 'Combatant Cape' (texto de la ficha que se coló)
                d = {"name": name, "dur": tp.get("dur"), "dur_max": tp.get("dur_max"), "dmg": tp.get("dmg"), "def": tp.get("def")}
                if name not in out or (d["dur_max"] and not out[name].get("dur_max")):
                    out[name] = d
        finally:
            winapi.left_up()
            try:
                self._drop_no()
            except Exception:
                pass
            if anchor():
                winapi.press("F5")
                time.sleep(0.5)
        return out

    def _gear_slot(self, d):
        it = self._game_item(d["name"]) or {}
        if d.get("dmg") or str(it.get("slot") or "").startswith("arma"):
            return "arma"
        if it.get("slot"):
            return it["slot"]
        low = d["name"].lower()
        # nombre cortado o mal leído ('Leather', 'Leather Armar'): si arranca como algo que ya tuve puesto, es eso
        if len(low) >= 6:
            for sl_, w_ in (self.cfg.get("worn_best") or {}).items():
                if str(w_.get("name") or "").lower().startswith(low[:6]):
                    return sl_
        for k, sl in (("shield", "escudo"), ("cape", "capa"), ("cap(", "cabeza"), ("helm", "cabeza"), ("hat", "cabeza"), ("shoes", "pies"),
                      ("boots", "pies"), ("trousers", "piernas"), ("hose", "piernas"), ("leggings", "piernas"), ("hauberk", "brazos"),
                      ("shirt", "pecho"), ("tunic", "pecho"), ("armor", "pecho"), ("mail", "pecho"), ("robe", "pecho")):
            if k in low:
                return sl
        return "otro:" + low

    def _gear_check(self, force=False):
        """SUPERVIVENCIA: cada 10 minutos (y enseguida si dejé de matar, me mataron o volví del herrero) leo qué tengo
        puesto. Sin arma no se pelea: pueblo ya. Si se salió una pieza: reparar y volver a ponerla. Si a algo le queda
        poca durabilidad: herrero ANTES de que se rompa. De paso el plan de compras sabe de verdad qué tengo."""
        if cfgmod.server_slug(self.cfg) == "abaddon" or not self.running or self.pos.get("x") is None:
            return
        now = time.time()
        every = float(self.cfg.get("gear_check_min", 10)) * 60
        if not force and now - getattr(self, "_gear_t", 0) < every:
            return
        if getattr(self, "mission", None) or getattr(self, "_busy_ui", False) or getattr(self, "goto", None):
            return
        for a in ("_shop_th", "_rep_th", "_inv_th", "_gear_th", "_audit_th", "_town_th"):
            th = getattr(self, a, None)
            if th is not None and th.is_alive():
                return
        if self.state == "attack" or self._under_attack() or self._mobs_within(4) >= 1 or now < getattr(self, "_rabia_until", 0):
            return
        self._gear_t = now
        self._busy_ui = True
        self._busy_t0 = now
        try:
            g = self._gear_read()
        finally:
            self._busy_ui = False
        if g is None:
            self._gear_t = now - every + 120  # no pude leer la ficha: pruebo en 2 minutos
            return
        items = list(g.values())
        worn, weapon = {}, None
        for d in items:
            sl = self._gear_slot(d)
            worn[sl] = {"name": d["name"], "v": int(d.get("dmg") or d.get("def") or 0), "ok": True,
                        "dur": d.get("dur"), "dur_max": d.get("dur_max")}
            if sl == "arma":
                weapon = d
        if weapon and (weapon.get("dur") is None or weapon["dur"] > 45):
            self._weapon_ok_t = now
        prev = self.cfg.get("worn") or {}
        missing = [f"{prev[sl].get('name')}" for sl in prev if isinstance(prev[sl], dict) and prev[sl].get("ok") and sl not in worn
                   and not str(prev[sl].get("name") or "").startswith("?") and not sl.startswith("otro:") and sl != "capa"]
        # memoria larga de lo que tuve puesto por lugar: si algo se salió y la lectura siguiente ya no lo tiene en
        # 'worn', antes se olvidaba (pasó: la Leather Armor rota quedó en el bolso y siguió cazando sin pechera).
        # Lo sigo reclamando hasta 3 viajes; si no aparece, lo doy por perdido.
        wb = self.cfg.setdefault("worn_best", {})
        # si en la misma revisión apareció algo nuevo puesto y se 'salió' otra cosa, no se rompió: lo desplazó (pasa con
        # la Shirt y la Leather Armor, que ocupan el mismo lugar del cuerpo). No lo reclamo ni voy al herrero por eso:
        # me quedo con lo que está puesto y anoto el choque para no volver a cambiarlos de ida y vuelta
        prev_names_ = {str(w.get("name")) for w in prev.values() if isinstance(w, dict)}
        new_ = [w["name"] for sl_, w in worn.items() if not sl_.startswith("otro:") and w.get("name") not in prev_names_]
        # solo un par uno-por-uno y visto 2 veces (ida y vuelta) cuenta como choque: después de una muerte o una mala
        # lectura de la ficha 'faltan' varias cosas a la vez y no es eso
        pair_ = (missing[0], new_[0]) if len(missing) == 1 and len(new_) == 1 else None
        if pair_:
            seen_ = self.cfg.setdefault("gear_clash_seen", {})
            k_ = " | ".join(sorted(pair_))
            seen_[k_] = int(seen_.get(k_, 0)) + 1
        if pair_ and int(self.cfg.get("gear_clash_seen", {}).get(" | ".join(sorted(pair_)), 0)) >= 2 and prev:
            clash = self.cfg.setdefault("gear_clash", {})
            for m_ in list(missing):
                for n_ in new_:
                    clash.setdefault(m_, [])
                    if n_ not in clash[m_]:
                        clash[m_].append(n_)
                for sl_, w_ in list(wb.items()):
                    if w_.get("name") == m_:
                        wb.pop(sl_, None)
            self.log(f"🛡 {', '.join(new_)} desplazó a {', '.join(missing)} (van en el mismo lugar): me quedo con lo puesto.")
            missing = []
        for sl, w in worn.items():
            if not sl.startswith("otro:") and not str(w.get("name") or "").startswith("?"):
                wb[sl] = {"name": w["name"], "miss": 0}
        if self._two_handed():
            wb.pop("escudo", None)
            missing = [n_ for n_ in missing if "shield" not in str(n_).lower() and "escudo" not in str(n_).lower()]
        for sl, w in list(wb.items()):
            if sl in worn or sl == "capa" or w.get("name") in missing:
                continue
            # cuento una falta solo si pasé por el herrero desde la última (ahí se repara y se vuelve a poner);
            # las revisiones seguidas o los reinicios no cuentan
            if float(getattr(self, "_smith_visit_t", 0) or 0) > float(w.get("t", 0)):
                w["miss"] = int(w.get("miss", 0)) + 1
                w["t"] = time.time()
            # el herrero no encontró nada roto ("reparé todo" sin rotos en el bolso): si sigue faltando, no se rompió,
            # me lo sacaron (o lo cambió el jugador a mano). No vuelvo al herrero por eso: con 1 visita alcanza.
            if w["miss"] > 3 or (w["miss"] >= 1 and float(getattr(self, "_smith_nobroken_t", 0) or 0) >= float(w.get("t", 0)) - 600):
                self.log(f"🛡 {w['name']} no aparece hace varias revisiones: lo doy por perdido (lo vuelvo a comprar si me alcanza).")
                wb.pop(sl, None)
                try:
                    oi = self.cfg.get("owned_items") or []
                    g_ = self._game_item(w["name"]) or {}
                    for nm_ in (w["name"], g_.get("name")):
                        while nm_ and nm_ in oi:
                            oi.remove(nm_)
                    self._brain_t = 0
                except Exception:
                    pass
            else:
                missing.append(w["name"])
        low = [d for d in items if d.get("dur_max") and d.get("dur") is not None and d["dur"] <= max(0.15 * d["dur_max"], 45)]
        sig = tuple(sorted((sl, w["name"]) for sl, w in worn.items()))
        self.cfg["worn"] = worn
        try:
            cfgmod.save(self.cfg)
        except Exception:
            pass
        if sig != getattr(self, "_gear_sig", None) or now - getattr(self, "_gear_said", 0) > 3600:
            self._gear_sig, self._gear_said = sig, now
            self.log("🛡 Tengo puesto: " + ("; ".join(f"{w['name']}" + (f" {w['dur']}/{w['dur_max']}" if w.get("dur_max") else "")
                                                    for sl, w in sorted(worn.items())) or "NADA") + ".")
        self._gear_missing = list(missing)
        # arma de otra familia puesta (ej. una varita que se equipó por un doble clic): para el plan es como no tener
        # arma de mi talento. Busco la mía en el bolso o la compro.
        try:
            fam_ = str(self._load_json("tiendas.json").get("talent") or "").strip().lower()
            gw_ = self._game_item((weapon or {}).get("name")) or {}
            off_ = (gw_ and str(gw_.get("family") or "").lower() not in ("", fam_)) or \
                (not gw_ and any(k in str((weapon or {}).get("name") or "").lower() for k in ("wand", "staff", "bow", "varita")))
            if weapon and fam_ and off_:
                (self.cfg.get("worn_best") or {}).pop("arma", None)
                self.log(f"⛔ Tengo puesta {weapon.get('name')}, que no es {fam_}: busco la mía en el bolso o compro una.")
                self.cfg["need_weapon"] = True
                self._worn_suspect_until = time.time() + 900
                cfgmod.save(self.cfg)
                weapon = None
        except Exception:
            pass
        if not weapon:
            self.log("⛔ SIN ARMA: no peleo así. Voy al pueblo a reparar/ponerme una.")
            self._worn_suspect_until = now + 900
            self._broken_t = now
            self._gear_t = now - every + 240
            self._town_need("sin arma")
        elif missing:
            self.log("🛡 Se me salió: " + ", ".join(missing) + ". Voy a repararlo y ponérmelo de nuevo.")
            self._broken_t = now
            self._worn_seen = now
            self._gear_t = now - every + 300
            self._town_need("se salió parte del equipo")
        elif low and now - getattr(self, "_rep_last", 0) > 600:
            self.log("🔧 Poca durabilidad en " + ", ".join(f"{d['name']} ({d['dur']}/{d['dur_max']})" for d in low) + ": voy al herrero antes de que se rompa.")
            th = getattr(self, "_rep_th", None)
            if not (th is not None and th.is_alive()):
                self._rep_th = threading.Thread(target=self._oly_repair_trip, args=("poca durabilidad",), daemon=True)
                self._rep_th.start()

    def _maybe_audit(self):
        """Cada tanto, parado y sin bichos cerca, abro el bolso (F6) y paso el mouse por cada cosa: el cartelito dice
        qué es y cuánta durabilidad le queda. Algo en 0 = roto y suelto en el bolso: herrero ya."""
        if cfgmod.server_slug(self.cfg) == "abaddon" or not self.running or self.pos.get("x") is None:
            return
        if getattr(self, "mission", None) or getattr(self, "_busy_ui", False):
            return
        now = time.time()
        every = 300 if now - getattr(self, "_worn_seen", 0) < 600 else float(self.cfg.get("bag_audit_min", 20)) * 60
        if now - getattr(self, "_audit_t", 0) < every:
            return
        for a in ("_shop_th", "_rep_th", "_inv_th", "_gear_th", "_audit_th"):
            th = getattr(self, a, None)
            if th is not None and th.is_alive():
                return
        if self.state == "attack" or self._under_attack() or self._fleeing():
            return
        px, py = self.pos["x"], self.pos["y"]
        if any(max(abs(e[0] - px), abs(e[1] - py)) <= 6 for e in (self._oly_ents() or [])):
            return
        self._audit_t = now
        self._audit_th = threading.Thread(target=self._bag_audit, daemon=True)
        self._audit_th.start()

    def _bag_audit(self):
        self._busy_ui, self._busy_t0 = True, time.time()
        try:
            self._release()
            opened = False
            bag = None
            for _try in range(3):
                img = self.screen.grab(self.rect)
                if bool(self._bag_open(img)):
                    bag = self._oly_bag()
                    if bag is not None:
                        break
                    if time.time() - getattr(self, "_audit_abort", 0) < 3:
                        return  # me pegaron: ya cerré el bolso, sigo peleando
                winapi.press("F6")
                opened = not opened
                time.sleep(1.1)
            if bag is None:
                try:
                    import cv2
                    cv2.imwrite(os.path.join(cfgmod.DATA, "bolso_falla.jpg"), self.screen.grab(self.rect), [cv2.IMWRITE_JPEG_QUALITY, 75])
                except Exception:
                    pass
                try:
                    if bool(self._bag_open(self.screen.grab(self.rect))):
                        winapi.press("F6")  # nunca lo dejo abierto
                except Exception:
                    pass
                if time.time() - getattr(self, "_audit_abort", 0) > 15:
                    self.log("🎒 No pude abrir el bolso para revisarlo (guardé la captura). Pruebo de nuevo en 10 minutos.")
                self._audit_t = time.time() - float(self.cfg.get("bag_audit_min", 20)) * 60 + 600
                return
            opened = True
            if not self.running:
                winapi.press("F6")  # me apagaron mientras abría el bolso: lo cierro y no toco nada más
                return
            items = [o for o in bag if o["kind"] == "item"]
            jd = [(o, self._bag_judge(o)) for o in items]
            self._bag_info = {"t": time.time(), "items": [dict({k: o.get(k) for k in ("name", "dur", "dur_max", "dmg", "def", "lvl", "str")}, verdict=j[0], why=j[1]) for o, j in jd]}
            nm = lambda o, j: (j[2]["name"] if j[2] else (o.get("name") or "?"))
            broken = [nm(o, j) for o, j in jd if j[0] == "broken"]
            equip = [nm(o, j) for o, j in jd if j[0] == "equip"]
            sell = [nm(o, j) for o, j in jd if j[0] == "sell"]
            self._bag_sell = sell
            h = getattr(self, "_bag_hits", (0, 0))
            pots = (getattr(self, "_bag_scan", None) or {}).get("pots") or {}
            line = (f"🎒 Bolso ({h[0]}/{h[1]} de memoria): oro {getattr(self, '_gold', '?')}, pociones {pots.get('vida', '?')}"
                    + (f"; rotos: {', '.join(broken)}" if broken else "")
                    + (f"; para ponerme: {', '.join(equip)}" if equip else "")
                    + (f"; para vender: {', '.join(sell)}" if sell else "") + ".")
            if line != getattr(self, "_bag_line", None):
                self._bag_line = line
                self.log(line)
            try:
                hun_ = getattr(self, "_hunger", None)
                if hun_ is not None and hun_ >= int(self.cfg.get("eat_above", 50)):
                    self._eat_food()
            except Exception as e_:
                self.log(f"🍖 No pude comer: {e_!r}")
            if equip:
                self._reequip_loose(max_n=len(equip) + 2)
            elif opened:
                winapi.press("F6")
                time.sleep(0.4)
            if broken and self._place("smith"):
                self._worn_seen = time.time()
                self._broken_t = time.time()
                self.log(f"🔧 {broken[0]} está roto (durabilidad 0) y suelto en el bolso: voy a repararlo.")
        except Exception as e:
            self.log(f"🎒 No pude revisar el bolso: {e!r}")
        finally:
            self._busy_ui = False

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
                "strategy": dict(getattr(self, "_strat", None) or {}), "time": self.time_use(),
                "exp_now": getattr(self, "_exp_now", None),
                "lines": ([(getattr(self, "_strat", None) or {}).get("line")] if (getattr(self, "_strat", None) or {}).get("line") else []) + [l for l in (p.get("lines") or [])], "zones": zones[:6], "level": sh.get("level") or self.cfg.get("travel", {}).get("my_level"),
                "pct": sh.get("pct"), "server": cfgmod.server_slug(self.cfg),
                "prices": dict((self.learner.st or {}).get("prices", {})) if getattr(self.learner, "st", None) else {},
                "check": getattr(self, "health", None)}

    def _equip_from_bag(self, name):
        """Me pongo un objeto del bolso con Ctrl + click (wiki: equipa lo que se pueda equipar)."""
        worn_n_ = {str(w.get("name")) for w in (self.cfg.get("worn") or {}).values() if isinstance(w, dict)}
        if any(c_ in worn_n_ for c_ in (self.cfg.get("gear_clash") or {}).get(name or "", [])):
            return False  # va en el mismo lugar que algo que ya tengo puesto (ver gear_clash): no los cambio de ida y vuelta
        img = self.screen.grab(self.rect)
        if not bool(self._bag_open(img)):
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
            return self._bag_open(img)

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
            self._bag_scan = None
            self._oly_bag(names=False)
            sc = getattr(self, "_bag_scan", None)
            if not sc:
                return None
            total = sc["pots"]["vida"]
            if sc.get("vida_hidden") and sc.get("vida_hidden_pt"):
                # cantidad verde sin frasco a la vista: puede ser otra cosa apilada (varitas, partes de bichos).
                # Lo compruebo con el cartelito: solo vale si dice Health Potion.
                pt = sc["vida_hidden_pt"]
                winapi.left_up()
                winapi.move(*self.abs_pt(pt[0], pt[1]))
                time.sleep(0.3)
                winapi.left_down()
                time.sleep(0.6)
                im_ = self.screen.grab(self.rect)
                winapi.left_up()
                time.sleep(0.3)
                tw_ = ocr.read_words(im_, {"x1": max(0, pt[0] - 60), "y1": max(0, pt[1] - 20), "x2": min(im_.shape[1], pt[0] + 420), "y2": min(im_.shape[0], pt[1] + 160)}, scale=1.0)
                low_ = " ".join(w[0].lower() for w in tw_)
                if "health" not in low_ or "potion" not in low_:
                    return None  # el frasco quedó tapado por otra cosa y el cartelito es de eso: no sé cuántas hay
            bs_ = getattr(self, "_bag_slots", None)
            if bs_ and total > bs_[0]:
                return None  # más pociones que lugares ocupados: leí mal el número
            now_ = time.time()
            prev = (getattr(self, "_left", {}) or {}).get("pot")
            if sc["unknown"] and prev is not None and total >= 1 and prev >= total and now_ - getattr(self, "_pots_buy_t", 0) < 3600 \
                    and prev - total <= 12:
                pass  # no leí el número: no invento; queda marcado como "al menos N"
            self._pots_unknown = bool(sc["unknown"])
            self._pot_counts = dict(sc["pots"])
            self._bag_items = len(sc["items"])
            self._bag_list = [{"name": "", "kind": "item"} for _ in sc["items"]]
            self._bag_t = time.time()
            return total
        finally:
            if opened:
                winapi.press("F6")
                time.sleep(0.3)

    def _oly_supplies(self):
        """Olympia: cuento las pociones abriendo el bolso cada tanto y, si quedan pocas, voy a la tienda."""
        now = time.time()
        if self._ui_th_alive():
            return
        if self.state != "seek" or self._under_attack() or self._fleeing():
            return
        sm_ = self._supplies_mem()
        if sm_ is not None:
            # bolso leído de la memoria: pociones, comida y oro exactos, sin abrir el bolso ni adivinar por la imagen
            if sm_["pot"] != (getattr(self, "_left", {}) or {}).get("pot"):
                self.log(f"🧪 Me quedan {sm_['pot']} pociones de vida.")
            self._left = {"pot": sm_["pot"], "meat": sm_["meat"]}
            self._pots_unknown = False
            g_ = self._gold_mem()
            if g_ is not None:
                self._gold = g_
            self._pots_t = now
        elif now - getattr(self, "_pots_t", 0) > 240:
            self._pots_t = now
            try:
                n = self._oly_pots()
            except Exception:
                n = None
            pe = getattr(self, "_pot_est", None)
            if pe and now - pe[2] < 3 * 3600:
                est = max(0, pe[0] - (int(self.stats.get("hp_pots", 0)) - pe[1]))
                if n is None or n < est - 4:
                    n = est  # el bolso muestra muchas menos de las que me dan las cuentas: frasco tapado
                    self._pots_unknown = False
            if n is not None:
                pu = self.__dict__.setdefault("_pot_use", {"t0": now, "used": 0})
                prev = getattr(self, "_left", {}).get("pot")
                if prev is not None and 0 < prev - n <= 10 and not getattr(self, "_pots_unknown", False):
                    pu["used"] += prev - n
                if n != getattr(self, "_left", {}).get("pot"):
                    self.log(f"🧪 Me quedan {n}{'+' if getattr(self, '_pots_unknown', False) else ''} pociones de vida.")
                self._left = {"pot": n, "meat": 99}
        n = getattr(self, "_left", {}).get("pot")
        # compré y el contador no subió: o el bolso está lleno o no estoy viendo el frasco. No vuelvo a la tienda
        # cada 15 minutos para nada: después de dos veces seguidas, dejo de ir por pociones 45 minutos.
        bc = getattr(self, "_buy_chk", None)
        if bc and n is not None and 5 < now - bc["t"] < 240:
            self._buy_chk = None
            if n <= bc["have"] + bc["n"] // 2:
                self._buy_nochg = getattr(self, "_buy_nochg", 0) + 1
                if self._buy_nochg >= 2:
                    self._buy_nochg = 0
                    self._pot_stuck_until = now + 2700
                    self.log("🛒 Compro pociones y el contador no sube (bolso lleno o frasco tapado): no vuelvo a la tienda por pociones durante 45 minutos.")
            else:
                self._buy_nochg = 0
        lim = 0 if getattr(self, "_pots_unknown", False) else int(self.cfg.get("buy_pots_below", 3))
        gold = getattr(self, "_gold", None)
        afford = gold is not None and self._pot_budget(gold, n or 0) >= (1 if not n else 3)
        many = (getattr(self, "_bag_items", 0) or 0) >= int(self.cfg.get("sell_when_items", 12))
        if now - getattr(self, "_shop_last", 0) < 900:
            return
        try:
            food_ = ((self._supplies_mem() or {}).get("meat") or 0) > 0
        except Exception:
            food_ = False
        hungry = (getattr(self, "_hunger", None) or 0) >= int(self.cfg.get("eat_above", 50)) and (gold or 0) >= 30 and not food_
        bs_ = getattr(self, "_bag_slots", None)
        full = bool(bs_ and now - bs_[2] < 1800 and bs_[0] >= bs_[1] - 6 and getattr(self, "_bag_sell", None)
                    and self.cfg.get("sell_olympia", True) and now > getattr(self, "_full_fail_until", 0))
        if not hungry and not full and (n is None or n > lim or not afford or now < getattr(self, "_pot_stuck_until", 0)):
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
                self._doors_tok = self._block_doors("shop")
                self.mission = {"map": "Aresden Farm", "goal": [63, 69], "label": "Tienda (pociones)", "until": time.time() + 300, "enter": True, "hold": True}
                wrongb = lambda: any(k in (self.pos.get("map") or "").lower() for k in ("warehouse", "blacksmith", "hall", "guild", "church"))
                last_out = None
                while not inshop() and not wrongb() and time.time() - t0 < 900 and self.alive and self.running:
                    if "farm" in (self.pos.get("map") or "").lower() and self.pos.get("x") is not None:
                        last_out = (int(self.pos["x"]), int(self.pos["y"]))
                    time.sleep(0.3)
                if wrongb():
                    self.log(f"🛒 Entré a {self.pos.get('map')} en vez de la tienda: salgo y pruebo de nuevo en unos minutos.")
                    # la última baldosa de la granja antes de entrar es la puerta equivocada: no la piso más (si no está
                    # pegada a la puerta de la tienda)
                    try:
                        if last_out and max(abs(last_out[0] - 63), abs(last_out[1] - 69)) >= 3:
                            bl = self.world.learn.setdefault("no_pisar", {}).setdefault("aresden farm", [])
                            if list(last_out) not in bl:
                                bl.append(list(last_out))
                                self.world.save()
                                self.log(f"🚫 Anoto ({last_out[0]},{last_out[1]}) de la granja como puerta de {self.pos.get('map')}: no la vuelvo a pisar.")
                    except Exception:
                        pass
                    self.mission = None
                    self._shop_last = time.time() - 660
                    return
            if not inshop():
                self.log("🛒 No llegué a la tienda.")
                return
            self._busy_ui = True
            self._release()
            time.sleep(1.5)
            # adentro de la tienda (zona segura, lectura con RapidOCR): primero ordeno el bolso, así se lee bien qué
            # tengo y qué vender, y las pilas no quedan tapadas
            try:
                ocr.SAFE = self._mobs_within(8) == 0
                self._bag_order()
                self._order_t = time.time()
            except Exception:
                pass
            grab = lambda: self.screen.grab(self.rect)
            def words(im):
                # pantalla entera + la ventana de la tienda leída recortada (la lectura entera a veces pierde renglones)
                full = ocr.read_words(im, {"x1": 0, "y1": 0, "x2": im.shape[1], "y2": int(im.shape[0] * 0.92)}, scale=1.0)
                kx_, ky_ = self.rect[2] / 1920.0, self.rect[3] / 1080.0
                bx = {"x1": int(380 * kx_), "y1": int(160 * ky_), "x2": int(945 * kx_), "y2": int(930 * ky_)}
                try:
                    crop = ocr.read_words(im, bx, scale=1.0)
                except Exception:
                    crop = []
                if not crop:
                    return full
                return [w for w in full if not (bx["x1"] <= w[1] <= bx["x2"] and bx["y1"] <= w[2] <= bx["y2"])] + list(crop)
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
            # el bolso se abre ANTES de hablar con la tendera: con la ventana de la tienda abierta el F6 no siempre
            # llega (el foco queda en la cajita de cantidad) y el bolso quedaba cerrado ('0 objetos')
            self._bag_ensure(grab)
            click(com[1], com[2])
            time.sleep(1.8)
            if not self._bag_ensure(grab, tries=1):
                # sigue cerrado adentro: cierro la tienda, abro el bolso afuera y vuelvo a pedir comerciar
                self.log("🎒 Con la tienda abierta no veo el bolso: cierro, lo abro y vuelvo a entrar.")
                try:
                    self._close_shop_window()
                except Exception:
                    pass
                time.sleep(0.8)
                self._bag_ensure(grab)
                menu = self._npc_menu(pl["npc_type"], pl["npc"], "comerciar") if pl.get("npc_type", -1) >= 0 else None
                menu = menu or self._npc_find("shop", "comerciar")
                com = next((w for w in (menu or []) if norm(w[0]).startswith("comerciar")), None)
                if com:
                    click(com[1], com[2])
                    time.sleep(1.8)
            ws = words(grab())
            tabp = next((w for w in ws if norm(w[0]).startswith("poci") or norm(w[0]).startswith("pocl") or "ocion" in norm(w[0])), None)
            kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
            if not tabp:
                # la pestaña (letra gótica) no se leyó: la ubico por el primer renglón de la lista ('Health Potion')
                hp_ = next((w for w in ws if norm(w[0]) == "health" and not any(norm(v[0]) == "big" and abs(v[2] - w[2]) < 8 and v[1] < w[1] for v in ws)), None)
                if hp_ and any(norm(v[0]) == "potion" and abs(v[2] - hp_[2]) < 8 for v in ws):
                    tabp = ("Pociones", hp_[1] + 32 * kx, hp_[2] - 47 * ky)
                else:
                    col_ = self._shop_price_col(ws)
                    if col_:
                        tabp = ("Pociones", col_[0] - 347 * kx, col_[1] - 47 * ky)
            if not tabp:
                # última opción: RapidOCR lee la letra gótica a medias ('TPociones', 'Tociones') o el título ('ienda')
                try:
                    rw = ocr.read_words_rapid(grab(), {"x1": int(300 * kx), "y1": int(140 * ky), "x2": int(1000 * kx), "y2": int(560 * ky)}) or []
                    pc = next((w for w in rw if "ocion" in norm(w[0]) or "ociones" in norm(w[0])), None)
                    if pc:
                        tabp = ("Pociones", pc[1], pc[2])
                    else:
                        ti = next((w for w in rw if norm(w[0]).endswith("ienda")), None)
                        hp2 = next((w for w in rw if norm(w[0]).startswith("healthpotion") or norm(w[0]) == "health"), None)
                        if hp2:
                            tabp = ("Pociones", hp2[1] - (hp2[3] / 2 if norm(hp2[0]).startswith("healthpotion") else 0) + 32 * kx, hp2[2] - 47 * ky)
                        elif ti:
                            tabp = ("Pociones", ti[1] - 167 * kx, ti[2] + 44 * ky)
                    if tabp:
                        self.log("🛒 Ubiqué las pestañas de la tienda con la segunda lectura (RapidOCR).")
                except Exception:
                    pass
            # 1) vender lo que no sirve (arrastrar del bolso a la lista de venta y 'Vender ítems')
            self._bag_ensure(grab, tries=1)  # el bolso abierto al lado de la tienda: de ahí leo oro, pociones y qué vender
            bag = self._oly_bag(loose=True) or []
            if not any(o["kind"] == "item" for o in bag):
                # segunda chance: RapidOCR para el encabezado y relectura
                ocr.SAFE = True
                if self._bag_ensure(grab, tries=2):
                    bag = self._oly_bag(loose=True) or []
            # solo vendo lo que reconocí en la tabla del juego y no me sirve (otro talento, o peor que lo puesto)
            sell = [o for o in bag if o["kind"] == "item" and self._bag_judge(o)[0] == "sell"
                    and (o.get("name") == "Mana Potion" or o.get("surplus") or not any(k in (o.get("name") or "").lower() for k in self.KEEP_WORDS))]
            if not self.cfg.get("sell_olympia", True):
                sell = []
            try:
                its_ = [o for o in bag if o["kind"] == "item"]
                if not its_:
                    self._full_fail_until = time.time() + 2700  # no veo el bolso acá: no vuelvo por 'bolso lleno' en 45 min
                self.log(f"🛒 En el bolso veo {len(its_)} objeto/s ({sum(1 for o in its_ if o.get('name'))} con nombre); para vender: "
                         f"{', '.join(o.get('name') or '?' for o in sell) or 'nada'}{'' if tabp else ' (no ubico las pestañas)'}.")
                if not sell or not tabp:
                    import cv2
                    cv2.imwrite(os.path.join(cfgmod.DATA, "tienda_dbg.jpg"), grab(), [cv2.IMWRITE_JPEG_QUALITY, 80])
            except Exception:
                pass
            if sell and tabp:
                # de un montón apilado sale una unidad por doble clic: repito la venta hasta que no quede nada
                # para vender (o deje de bajar el bolso), como mucho 8 vueltas
                t_sell = time.time()
                for _round in range(6):
                    sold_any = False
                    # vueltas rápidas sobre los mismos lugares (de una pila sale una unidad por vez); la lista de
                    # venta se revisa en cada una, así que si abajo aparece otra cosa no se vende
                    for _fast in range(8):
                        try:
                            ok_ = self._oly_sell(sell, tabp, words, grab, click)
                        except Exception as e:
                            self.log(f"🛒 No pude vender: {e!r}")
                            ok_ = False
                        if not ok_:
                            break
                        sold_any = True
                        time.sleep(0.5)
                        # repetir sobre el mismo lugar solo sirve para pilas (sale una unidad por vez); un ítem suelto
                        # ya se vendió y en su lugar puede haber otra cosa (así se colaba el Bronze Shield)
                        STACK_W = ("jelly", "potion", "skin", "teeth", "tongue", "meat", "baguette", "bread", "fur", "horn", "claw")
                        sell = [o for o in sell if (o.get("n") or 1) > 1 or any(k in (o.get("name") or "").lower() for k in STACK_W)]
                        if not sell:
                            break
                    if not sold_any or time.time() - t_sell > 300:
                        break
                    bag = self._oly_bag(loose=True) or []
                    sell = [o for o in bag if o["kind"] == "item" and self._bag_judge(o)[0] == "sell"
                            and (o.get("name") == "Mana Potion" or o.get("surplus") or not any(k in (o.get("name") or "").lower() for k in self.KEEP_WORDS))]
                    if not sell:
                        break
                click(tabp[1], tabp[2])
                time.sleep(1.0)
            self._oly_bag(names=False)  # leo el oro
            # 1b) comida: con hambre alta compro pan (o carne) y me lo como al salir
            try:
                # SIEMPRE que paso por la tienda llevo comida (regla de Juan): lo que falte para llenar la barra más
                # una reserva en el bolso, para comer en el campo sin volver al pueblo.
                fill_ = self._hunger_bar(grab())
                hun = getattr(self, "_hunger", None)
                if fill_ is not None:
                    hun = max(int(hun or 0), 100 - fill_)
                have_food = sum(1 for o in bag if o["kind"] == "item" and any(k in (o.get("name") or "").lower() for k in ("baguette", "meat", "bread")))
                stock = int(self.cfg.get("food_stock", 4))
                need_now = -(-max(0, int(hun or 0) - 10) // 20)          # baguettes para llenar la barra
                n_buy = max(0, min(8, need_now + (0 if have_food else stock)))
                # la comida del bolso a veces no la reconozco (queda tapada) y compraba reserva en cada visita hasta
                # llenar el bolso: reserva como mucho una vez por hora, y nunca más de 3 de una
                if not have_food and time.time() - getattr(self, "_food_stock_t", 0) < 3600:
                    n_buy = max(0, min(n_buy, need_now))
                n_buy = min(n_buy, max(3, need_now if (hun or 0) >= 60 else 0))
                if time.time() - getattr(self, "_food_bought_t", 0) < 1800:
                    # compré hace menos de media hora: el pan está en el bolso aunque no lo haya reconocido; no lleno
                    # el bolso de pan (pasó: 4 baguettes por visita y después no entraban las pociones)
                    n_buy = 0
                if n_buy > need_now:
                    self._food_stock_t = time.time()
                bs_f = getattr(self, "_bag_slots", None)
                if bs_f and bs_f[1] - bs_f[0] < 2:
                    n_buy = min(n_buy, max(0, need_now))               # bolso lleno: solo lo que me como ya
                if n_buy >= 1 and (getattr(self, "_gold", None) is None or (getattr(self, "_gold", 0) or 0) >= 30):
                    self._oly_buy_food(words, grab, click, tabp, n_buy * 20)
                    fp_ = getattr(self, "_food_pending", None)
                    if fp_:
                        if need_now >= 1:
                            fp_["n"] = need_now                         # me como solo lo que hace falta; el resto queda
                        else:
                            self._food_pending = None
            except Exception as e:
                self.log(f"🍖 No pude comprar comida: {e!r}")
            # 2) pociones: solo con el oro que sobra después de la reserva para equipo, y hasta 4
            if True:
                # el oro exacto lo dice la tienda al elegir algo ('Total 45 (tienes 7.781)'): manda sobre lo leído en el bolso
                try:
                    w0 = words(grab())
                    hp0 = next((w for w in w0 if norm(w[0]) == "health"), None)
                    if hp0:
                        click(hp0[1], hp0[2])
                        time.sleep(0.9)
                        w0 = words(grab())
                        tn = next((w for w in w0 if "tienes" in norm(w[0])), None)
                        if tn:
                            import re as _re
                            row_ = " ".join(v[0] for v in sorted((v for v in w0 if abs(v[2] - tn[2]) < 12 * ky and v[1] >= tn[1] - 5), key=lambda v: v[1]))
                            m_ = _re.search(r"tienes\s*(\d{1,3}(?:\s?[.,]\s?\d{3})+|\d+)", row_.lower())
                            dg = "".join(c for c in (m_.group(1) if m_ else "") if c.isdigit())
                            lastg_ = getattr(self, "_gold", None)
                            if dg and lastg_ and int(dg) > 5 * lastg_ + 2000:
                                dg = ""  # salto imposible: leí de más (el ícono de la moneda como cifra)
                            if dg:
                                if getattr(self, "_gold", None) != int(dg):
                                    self.log(f"🛒 Oro según la tienda: {int(dg)}.")
                                self._gold = int(dg)
                                self._gold_low = None
                except Exception:
                    pass
            gold = getattr(self, "_gold", None)
            have = getattr(self, "_left", {}).get("pot") or 0
            # las que tengo AHORA (recién leí el bolso acá adentro), no las que conté antes de salir para la tienda
            try:
                sc_ = getattr(self, "_bag_scan", None) or {}
                if not sc_.get("unknown") and not sc_.get("vida_hidden") and sc_.get("pots") is not None:
                    have = int(sc_["pots"].get("vida") or 0)
                    self._left = {"pot": have, "meat": 99}
            except Exception:
                pass
            reserve = int(self.cfg.get("gold_reserve", 150))
            # cuántas: hasta el objetivo de pociones, gastando como mucho un tercio del oro que sobra de la reserva
            want_n = int(self.cfg.get("pots_target", 15))
            n = self._pot_budget(gold, have)
            # el bolso tiene lugares contados (cada poción ocupa uno): dejo 8 libres para lo que levanto del piso
            bs = getattr(self, "_bag_slots", None)
            if bs and time.time() - bs[2] < 120:
                # lugares libres para lo que levanto del piso: 3; con pocas pociones, ninguno (primero sobrevivir)
                # ...y si el plan tiene equipo para comprar ya, le guardo su lugar (con el bolso lleno 'Comprar' no hace nada)
                gear_n = len([i for i in ((getattr(self, "plan", None) or {}).get("buy_now") or []) if i.get("confirmed")])
                room = bs[1] - max(gear_n, 0 if have < 10 else 3) - bs[0]
                if room < n:
                    self.log(f"🛒 Bolso con {bs[0]}/{bs[1]} lugares: compro {max(0, room)} pociones en vez de {n}.")
                    n = max(0, room)
                if n <= 0:
                    self._left = {"pot": max(have, 10), "meat": 99}  # si está lleno es porque pociones tengo
                    return
            if n <= 0:
                if not getattr(self, "_food_pending", None):
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
            if not hp and tabp:
                click(tabp[1], tabp[2])  # la ventana pudo abrir en otra pestaña
                time.sleep(1.2)
                ws = words(grab())
                hp = next((w for w in ws if norm(w[0]) == "health" and not any(norm(v[0]) == "big" and abs(v[2] - w[2]) < 8 and v[1] < w[1] for v in ws)), None)
            if not hp:
                big = [w for w in ws if norm(w[0]) == "big"]
                bh = next((w for w in ws if norm(w[0]) == "health" and any(abs(v[2] - w[2]) < 8 and v[1] < w[1] for v in big)), None)
                bm = next((w for w in ws if norm(w[0]) == "mana" and any(abs(v[2] - w[2]) < 8 and v[1] < w[1] for v in big)), None)
                if bh and bm and 30 < bm[2] - bh[2] < 90:
                    b0 = min((v for v in big if abs(v[2] - bh[2]) < 8), key=lambda v: v[1])
                    hp = ("health", b0[1], bh[2] - (bm[2] - bh[2]) // 2) + tuple(bh[3:])  # el chat la tapa: es la fila de arriba
            if not hp:
                try:
                    import cv2
                    cv2.imwrite(os.path.join(cfgmod.DATA, "tienda_falla.jpg"), grab(), [cv2.IMWRITE_JPEG_QUALITY, 75])
                except Exception:
                    pass
                self.log("🛒 No veo las Health Potion en la lista (guardé la captura para revisarlo).")
                self._shop_retry = time.time() + 240
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
            self._left = {"pot": (have or 0) + n, "meat": 99}
            # cuenta propia: lo que tenía + lo que compré, menos las que voy tomando. Vale más que el número leído
            # a ojo en el bolso cuando el frasco queda tapado por otra cosa.
            self._pot_est = ((have or 0) + n, int(self.stats.get("hp_pots", 0)), time.time())
            self._buy_chk = {"t": time.time(), "have": have or 0, "n": n}
            self._pots_unknown = False
            self._pots_t = 0
            tienda = next((w for w in words(grab()) if norm(w[0]) == "tienda"), None)
            if tienda:
                click(tienda[1], tienda[2], "right")
                time.sleep(0.4)
        except Exception as e:
            self.log(f"🛒 Error en la tienda: {e!r}")
        finally:
            try:
                self._unblock_doors(getattr(self, "_doors_tok", None))
            except Exception:
                pass
            self._doors_tok = None
            # cierro la ventana de la tienda (click derecho adentro); abierta no deja caminar
            try:
                self._close_shop_window()
            except Exception:
                pass
            try:
                self._eat_food()
            except Exception as e:
                self.log(f"🍖 No pude comer: {e!r}")
            self._ui_clear()
            self.mission = None
            self._busy_ui = False
            self._shop_last = time.time()
            if time.time() < getattr(self, "_shop_retry", 0):
                self._shop_last -= 660  # falló la compra: reintento en 4 minutos, no en 15
                self._shop_retry = 0

    def _oly_buy_food(self, words, grab, click, tabp, hun):
        """En la tienda abierta: compro pan (Baguette) o carne según el hambre. Me la como después, con la tienda
        cerrada (_eat_food), para no tocar el bolso con la lista de venta a mano."""
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
        ws = words(grab())
        row = next((w for w in ws if norm(w[0]).startswith("baguet")), None)
        per = 20
        if not row:
            row = next((w for w in ws if norm(w[0]) == "meat"), None)
            per = 30
        if not row:
            self.log("🍖 No veo pan ni carne en la lista de la tienda.")
            return
        n = max(1, min(8, -(-int(hun) // per)))
        sc = getattr(self, "_bag_scan", None) or {}
        before = [(i["cx"], i["cy"]) for i in sc.get("items", [])]
        click(row[1], row[2])
        time.sleep(1.0)
        ws = words(grab())
        plus = next((w for w in ws if w[0].strip() == "+" and w[2] > row[2] + 300 * ky), None)
        if not plus and tabp:
            plus = ("+", tabp[1] + 81 * kx, tabp[2] + 640 * ky)
        for _ in range(n - 1):
            if plus:
                click(plus[1], plus[2])
                time.sleep(0.3)
        cp = next((w for w in words(grab()) if norm(w[0]).startswith("comprar")), None)
        if not cp and tabp:
            cp = ("Comprar", tabp[1] + 355 * kx, tabp[2] + 640 * ky)
        if not cp:
            self.log("🍖 No veo el botón Comprar.")
            return
        click(cp[1], cp[2])
        time.sleep(1.5)
        self._food_pending = {"n": n, "before": before, "what": "pan" if per == 20 else "carne"}
        self.log(f"🍖 Compré {n} de {self._food_pending['what']} (comida para llenar la barra y reserva).")
        self._food_bought_t = time.time()
        self._oly_bag(names=False)

    def _hunger_bar(self, img=None):
        """Barra de comida (el tubito vertical al lado de la barra verde): naranja = lleno. Devuelve 0-100 de lleno,
        o None si no la veo."""
        try:
            import cv2
            img = self.img if img is None else img
            if img is None:
                return None
            kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
            c = img[int(1009 * ky):int(1061 * ky), int(962 * kx):int(971 * kx)]
            if c.size == 0:
                return None
            hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
            m = ((hsv[..., 0] < 22) | (hsv[..., 0] > 170)) & (hsv[..., 1] > 120) & (hsv[..., 2] > 110)
            rows = m.mean(axis=1) >= 0.5
            # el tubo vacío es gris verdoso apagado: si tampoco veo eso, no es la barra (hay una ventana encima)
            dull = ((hsv[..., 1] < 90) & (hsv[..., 2] > 70)).mean(axis=1) >= 0.5
            if (rows | dull).mean() < 0.7:
                return None
            return int(round(100.0 * rows.mean()))
        except Exception:
            return None

    def _hunger_watch(self):
        """Cada minuto miro la barra de comida. Con menos de 40% lleno y sin bichos pegados, como ya (sin esperar
        a la revisión del bolso de cada 20 minutos)."""
        now = time.time()
        if now - getattr(self, "_hb_t", 0) < 60 or getattr(self, "_busy_ui", False) or cfgmod.server_slug(self.cfg) == "abaddon":
            return
        self._hb_t = now
        fill = self._hunger_bar()
        if fill is None:
            return
        self._hb_fill = fill
        # el hambre es lo que dice la barra AHORA (antes quedaba pegada en el máximo: 91 de 'hambre' con la barra
        # llena, y la tienda quedaba urgente para siempre)
        self._hunger = 100 - fill
        if fill > int(self.cfg.get("eat_below_fill", 40)):
            return
        adj_ = self._adjacent_mobs()
        # en un nido nunca quedo sin bichos al lado: si ya tengo hambre de verdad, como igual con 1 o 2 pegados y vida alta
        starving = fill <= 20 and adj_ <= 2 and (self.bars.get("hp") or 0) >= 70
        if now - getattr(self, "_hb_eat_t", 0) < 240 or (adj_ >= 1 and not starving) or self._town_block():
            return
        self._hb_eat_t = now
        self._eat_n = max(1, min(5, int((95 - fill) // 20)))  # cada baguette sube ~20%
        self.log(f"🍖 Barra de comida en {fill}%: como ahora.")
        self._busy_ui = True
        self._busy_t0 = now
        try:
            self._release()
            self._eat_food()
        except Exception as e:
            self.log(f"🍖 No pude comer: {e!r}")
        finally:
            self._busy_ui = False
        f2 = self._hunger_bar(self.screen.grab(self.rect))
        if f2 is not None and f2 <= fill + 5:
            # no subió: no tengo comida (o no la reconocí) → que la compre en el próximo paso por la tienda
            self._hunger = max(60, 100 - f2)
            if now - getattr(self, "_hb_said", 0) > 900:
                self._hb_said = now
                self.log(f"🍖 Comí y la barra sigue en {f2}%: no tengo comida en el bolso (o no la reconocí). Compro en la tienda.")

    def _eat_food(self):
        """Como lo que tenga de comida en el bolso (lo busco por nombre con el cartelito): doble clic por unidad."""
        fp = getattr(self, "_food_pending", None)
        self._food_pending = None
        hun = getattr(self, "_hunger", None)
        if not fp and not (hun is not None and hun >= int(self.cfg.get("eat_above", 50))):
            return
        opened = False
        if not bool(self._bag_open(self.screen.grab(self.rect))):
            winapi.press("F6")
            time.sleep(0.9)
            opened = True
        bag = self._oly_bag() or []
        food = None
        for o in bag:
            if o["kind"] != "item":
                continue
            g_ = self._game_item(o.get("name")) or {}
            nm = (g_.get("name") or o.get("name") or "").lower()
            if (g_.get("type") == 7 and "potion" not in nm) or any(k in nm for k in ("baguette", "meat", "bread", "pan ", "carne")):
                food = (o, g_.get("name") or o.get("name"))
                break
        if not food:
            scf = getattr(self, "_bag_scan", None) or {}
            if scf.get("food_pt"):
                food = ({"x": scf["food_pt"][0], "y": scf["food_pt"][1]}, "la pila de comida")
        if not food:
            if fp:
                self.log("🍖 Compré comida pero no la encuentro en el bolso (no leí su nombre): pruebo en la próxima revisión.")
        else:
            o, nm = food
            n = int(fp["n"]) if fp else int(getattr(self, "_eat_n", 3) or 3)
            x, y = self.abs_pt(o["x"], o["y"])
            for _ in range(max(1, min(n, 5))):
                winapi.click(x, y)
                time.sleep(0.12)
                winapi.click(x, y)
                time.sleep(0.8)
            self.log(f"🍖 Comí {nm} (hambre {hun if hun is not None else '?'}%).")
            self._hunger = None
            self._census_t = 0  # releo la ficha para ver cómo quedó el hambre
            self._bag_mem = []
        if opened and bool(self._bag_open(self.screen.grab(self.rect))):
            winapi.press("F6")
            time.sleep(0.4)

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

    OLY_INV = 0x1380240  # Olympia: 50 punteros (uint32) a los items del bolso, fijo (sobrevive al reloguear)
    ID_BAGUETTE = 98

    def _two_handed(self):
        """¿El pj juega a dos manos? Sin la etapa beta 'pj_nuevo_beta': lo que diga la config (guía 2H, como siempre).
        Con la beta (pj que arranca de nivel 1): antes de la primera arma de dos manos usa una mano + escudo; el día
        que tiene puesta una de dos manos queda anotado PARA SIEMPRE (no vuelve al escudo aunque se le rompa el arma
        o lea mal la ficha: busca otra de dos manos). BETA: armado, sin probar todavía."""
        if not self.cfg.get("pj_nuevo_beta", False):
            return self.cfg.get("two_handed", True)
        if self.cfg.get("two_handed_since"):
            return True
        try:
            w_ = (self.cfg.get("worn") or {}).get("arma") or {}
            if w_.get("name") and str((self._game_item(w_["name"]) or {}).get("slot") or "") == "arma2m":
                self.cfg["two_handed_since"] = time.time()
                cfgmod.save(self.cfg)
                self.log(f"⚔ Ya uso arma de dos manos ({w_['name']}): desde ahora no compro ni reclamo escudo, para siempre.")
                return True
        except Exception:
            pass
        return False

    def _inv_oly(self):
        """Bolso de Olympia leído de memoria (solo lectura): [{slot,id,count,dur,x,y}]. None si no se pudo o la
        lectura no tiene sentido. Cada poción es un item aparte (count 1); el oro es un item con count = oro."""
        if cfgmod.server_slug(self.cfg) == "abaddon":
            return None
        now = time.time()
        c = getattr(self, "_oinv_cache", None)
        if c and now - c[0] < 1.0:
            return c[1]
        out = None
        try:
            pr = getattr(self.mem, "proc", None)
            raw = pr.read(self.OLY_INV, 200) if pr else None
            if raw and len(raw) == 200:
                import struct as _st
                ptrs = _st.unpack("<50I", raw)
                L, bad = [], 0
                for i, a in enumerate(ptrs):
                    if not a:
                        continue
                    d = pr.read(a, 0x24)
                    if not d or len(d) < 0x24:
                        bad += 1
                        continue
                    iid = _st.unpack_from("<H", d, 8)[0]
                    cnt = _st.unpack_from("<I", d, 0x18)[0]
                    if not (0 < iid < 3000) or cnt > 2_000_000_000:
                        bad += 1
                        continue
                    x_, y_ = _st.unpack_from("<hh", d, 0x20)
                    L.append({"slot": i, "id": iid, "count": cnt, "dur": _st.unpack_from("<H", d, 0x14)[0], "x": x_, "y": y_})
                # sano: casi todo legible y como mucho un oro
                if L and bad <= 2 and sum(1 for o in L if o["id"] == self.ID_GOLD) <= 1:
                    out = L
        except Exception:
            out = None
        self._oinv_cache = (now, out)
        return out

    def _gold_mem(self):
        """Oro del pj: el item 90 del inventario. None si no se sabe."""
        oi = self._inv_oly()
        if oi is not None:
            return sum(i["count"] for i in oi if i["id"] == self.ID_GOLD)
        inv = self._inv_mem()
        if inv is None:
            e = getattr(self, "_eco", None)  # sin inventario en memoria: el oro leído del bolso (si es reciente)
            return e["last"] if e and time.time() - e.get("t", 0) < 1800 else None
        return sum(i["count"] for i in inv if i["id"] == self.ID_GOLD)

    def _supplies_mem(self):
        """Pociones de vida y carnes que quedan, contadas en la memoria. None si no se sabe."""
        oi = self._inv_oly()
        if oi is not None:
            # Olympia: pociones rojas (91) y comida (98 baguette, 99 carne), contadas exacto en memoria
            return {"pot": sum(max(1, i["count"]) for i in oi if i["id"] == self.ID_POT),
                    "meat": sum(max(1, i["count"]) for i in oi if i["id"] in (self.ID_MEAT, self.ID_BAGUETTE)),
                    "items": len(oi)}
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
        self._inv_cache = self._oinv_cache = None
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
            self._inv_cache = self._oinv_cache = None
            moved = [i for i in self._inv_mem() or [] if before.get(i["slot"]) != (i["x"], i["y"])]
            if any(i["slot"] == slot for i in moved):
                return self._inv_item(slot=slot)
            if not moved:
                # no agarré nada: pruebo un poco corrido
                self._drag_bg(*self._bag_pt(it["x"] + 6, it["y"] + 10), *self._bag_pt(ex, ey))
                time.sleep(0.5)
                self._cancel_drop()
                self._bag_keep(o0)
                self._inv_cache = self._oinv_cache = None
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

    def _game_item(self, name):
        """Objeto de la tabla del juego (items.dat de la instalación) por el nombre que leí en el cartelito."""
        if not name:
            return None
        db = getattr(self, "_gitems", None)
        if db is None:
            try:
                from . import gamedata
                db = {"".join(c for c in v["name"].lower() if c.isalnum()): v for v in gamedata.load_items(self.world.game_dir).values()}
            except Exception:
                db = {}
            self._gitems = db
        norm = lambda t: "".join(c for c in str(t).lower() if c.isalnum())
        k = norm(name)
        if k in db:
            return db[k]
        # objetos con bonus llevan palabras adelante ('Health Siphoning Superior Main Gauche', 'Strong Wood Shield'):
        # busco el nombre de tabla más largo que quede al sacar palabras del principio
        ws = str(name).split()
        for i in range(1, len(ws)):
            k2 = norm(" ".join(ws[i:]))
            if len(k2) >= 4 and k2 in db:
                return dict(db[k2], affix=" ".join(ws[:i]))
        # sin espacios (RapidOCR a veces pega las palabras: 'StrongChemise(W)'): el nombre de tabla más largo con el
        # que termina, si lo de adelante es un bonus conocido
        affixes = ("strong", "light", "sharp", "ancient", "critical", "crushing", "agile", "special", "poisoning",
                   "righteous", "mana", "converting", "health", "siphoning", "superior")
        best_ = max((k2 for k2 in db if len(k2) >= 5 and k.endswith(k2) and k != k2), key=len, default=None)
        if best_ and any(k[:len(k) - len(best_)].startswith(a_) for a_ in affixes):
            pre = k[:len(k) - len(best_)]
            return dict(db[best_], affix=pre.capitalize())
        import difflib
        m = difflib.get_close_matches(k, list(db), n=1, cutoff=0.88)
        return db[m[0]] if m else None

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
                # el bolso leído de la memoria (puesto + guardado): los números de objeto son los del juego base, así que
                # la tabla de items los traduce. Sin esto el plan creía que no tenía nada ('mejora: Broad Sword' con
                # una Great Sword puesta).
                try:
                    for it_ in (self._inv_oly() or []):
                        nm_ = (idb.get(it_["id"]) or {}).get("name")
                        if nm_ and nm_ not in owned:
                            owned.append(nm_)
                except Exception:
                    pass
                # lo que tengo puesto y comprobado también es mío: no lo compro de nuevo
                for v_ in (self.cfg.get("worn") or {}).values():
                    if isinstance(v_, dict) and v_.get("ok") and v_.get("name"):
                        g_ = self._game_item(v_["name"]) or {}
                        for nm_ in (g_.get("name"), v_["name"]):
                            if nm_ and nm_ not in owned:
                                owned.append(nm_)
            else:
                owned = pj.get("owned", [])
            self._owned = owned
            from . import gear
            # la tabla de objetos es la de Abaddon: en otro server manda el catálogo leído en sus tiendas
            idb_l = list(idb.values()) if cfgmod.server_slug(self.cfg) == "abaddon" else []
            try:
                # lo que busqué en la tienda y no lo venden (el catálogo decía que sí): no lo planeo por un día
                ns_ = {k for k, t_ in (self.world.learn.get("no_stock") or {}).items() if time.time() - t_ < 86400}
                if ns_ and isinstance(cat, dict) and cat.get("items"):
                    cat = dict(cat, items=[i for i in cat["items"] if i.get("name") not in ns_])
                # lugares que Juan no quiere cambiar por comprado (ej. 'brazos': la Shirt con +21% de oro)
                hold_ = set(self.cfg.get("slot_hold") or [])
                if hold_ and isinstance(cat, dict) and cat.get("items"):
                    cat = dict(cat, items=[i for i in cat["items"] if i.get("slot") not in hold_])
            except Exception:
                pass
            if self._two_handed() and isinstance(cat, dict) and cat.get("items"):
                # guía a dos manos: el escudo no se usa nunca (no lo compro ni lo reclamo)
                cat = dict(cat, items=[i for i in cat["items"] if i.get("slot") != "escudo"])
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

    def _block_doors(self, target):
        """Mientras voy a un local, tapo (temporalmente) las puertas de los OTROS locales conocidos y las puertas
        equivocadas que ya aprendí para este: el camino las rodea y no entro por error. Devuelve lo que hay que
        destapar al volver (_unblock_doors)."""
        m = self.world.map_for(self.pos.get("map") or "")
        if m is None:
            return None
        tp = self._place(target) or {}
        td = tp.get("door")
        doors = []
        for k in ("shop", "smith", "warehouse"):
            pk = self._place(k) or {}
            if k != target and pk.get("door") and self._same(pk.get("map") or "", self.pos.get("map") or ""):
                doors.append(tuple(pk["door"]))
        doors += [tuple(d) for d in (tp.get("wrong_doors") or [])]
        saved = {}
        for x, y in doors:
            if td and max(abs(x - td[0]), abs(y - td[1])) < 4:
                continue
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    xx, yy = x + dx, y + dy
                    if 0 <= xx < m.w and 0 <= yy < m.h and (xx, yy) not in saved:
                        saved[(xx, yy)] = bool(m.walk[yy, xx])
                        m.walk[yy, xx] = False
        return (m, saved)

    def _unblock_doors(self, tok):
        if not tok:
            return
        m, saved = tok
        for (x, y), v in saved.items():
            m.walk[y, x] = v

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
            im0_ = self.screen.grab(self.rect)
            ws0 = self._q_words(im0_)
            if self._bag_open(im0_, ws0):
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
            self._ui_clear()
            t0 = time.time()
            d = pl["door"]
            far = self.pos.get("x") is None or max(abs(self.pos["x"] - d[0]), abs(self.pos["y"] - d[1] - 2)) > 4
            if not inside() and (far or not self._same(self.pos.get("map") or "", pl["map"])):
                # primero hasta el frente de la puerta por el camino normal (el encargo va en línea más directa)
                self.goto = {"map": pl["map"], "x": d[0], "y": d[1] + 2, "t": time.time()}
                while self.goto and time.time() - t0 < 600 and self.alive and self.running:
                    time.sleep(0.5)
            if not inside():
                self._doors_tok = self._block_doors("smith")
                self.mission = {"map": pl["map"], "goal": list(d), "label": "Herrero (reparar)", "until": time.time() + 300, "enter": True, "hold": True}
                wrongb = lambda: any(k in (self.pos.get("map") or "").lower() for k in ("warehouse", "shop", "hall", "guild", "church")) \
                    and "blacksmith" not in (self.pos.get("map") or "").lower()
                last_out = None
                while not inside() and not wrongb() and time.time() - t0 < 900 and self.alive and self.running:
                    if self._same(self.pos.get("map") or "", pl["map"]) and self.pos.get("x") is not None:
                        last_out = (int(self.pos["x"]), int(self.pos["y"]))
                    time.sleep(0.3)
                if wrongb():
                    self.log(f"🔧 Entré a {self.pos.get('map')} en vez del herrero: salgo y pruebo de nuevo en unos minutos.")
                    try:
                        if last_out and max(abs(last_out[0] - d[0]), abs(last_out[1] - d[1])) >= 3:
                            pc = self.cfg.setdefault("places", {}).setdefault("smith", {})
                            wd = pc.setdefault("wrong_doors", [])
                            if list(last_out) not in wd:
                                wd.append(list(last_out))
                                cfgmod.save(self.cfg)
                                self.log(f"🚫 Anoto ({last_out[0]},{last_out[1]}) como puerta equivocada: yendo al herrero la rodeo.")
                    except Exception:
                        pass
                    self.mission = None
                    self._rep_retry = time.time() - max(0, float(self.cfg.get("repair_every_min", 150)) * 60 - 240)
                    return
            if not inside():
                self.log("🔧 No llegué al herrero.")
                return
            self._smith_visit_t = time.time()
            self._busy_ui = True
            self._release()
            time.sleep(1.5)
            # adentro del herrero no hay bichos: miro cuánto se gastó el equipo desde la última reparación y con eso
            # ajusto cada cuánto vengo (en un nido se gasta mucho más rápido que cazando de a uno)
            pre = None
            try:
                g_ = self._gear_read() if cfgmod.server_slug(self.cfg) != "abaddon" else None
                pcs = [100.0 * d_["dur"] / d_["dur_max"] for d_ in (g_ or {}).values() if d_.get("dur_max") and d_.get("dur") is not None]
                if pcs:
                    pre = min(pcs)
                    ev = float(self.cfg.get("repair_every_min", 45))
                    since = (time.time() - getattr(self, "_rep_ok_t", 0)) / 60.0
                    new = ev
                    if pre < 40 and since < ev * 1.5:
                        new = max(12.0, round(min(ev, since) * 0.6))
                    elif pre > 85 and since >= ev * 0.8:
                        new = min(90.0, round(ev * 1.25))
                    if new != ev:
                        self.cfg["repair_every_min"] = int(new)
                        cfgmod.save(self.cfg)
                        self.log(f"🔧 Al llegar, lo más gastado estaba en {int(pre)}%: paso a reparar cada {int(new)} minutos (antes {int(ev)}).")
            except Exception:
                pass
            menu = self._npc_menu(pl["npc_type"], pl["npc"], "reparar") if pl.get("npc_type", -1) >= 0 else None
            menu = menu or self._npc_find("smith", "reparar")
            if not menu:
                self.log("🔧 No encontré al herrero.")
                return
            # siempre "Reparar Todo" (nunca "Reparar" a secas, que abre la lista de a un objeto)
            todo = next((w for w in menu if "todo" in norm(w[0])), None)
            if not todo:
                reps = sorted((w for w in menu if norm(w[0]).startswith("repar")), key=lambda w: w[2])
                if len(reps) >= 2:
                    todo = reps[-1]  # la de abajo de las dos que dicen Reparar
            if not todo:
                self.log("🔧 No encontré 'Reparar Todo' en el menú del herrero: no toco nada y reintento en 5 minutos.")
                self._rep_last = time.time() - max(0, float(self.cfg.get("repair_every_min", 150)) * 60 - 300)
                if cfgmod.server_slug(self.cfg) == "abaddon":
                    winapi.press("ESC")
                return
            winapi.click(*self.abs_pt(todo[1], todo[2]))
            time.sleep(1.8)
            ws = self._q_words(self.screen.grab(self.rect))
            q = next((w for w in ws if "reparar" in norm(w[0]) and w[2] < self.rect[3] * 0.3), None)
            if not q:  # a veces tarda en aparecer el cartel
                time.sleep(1.5)
                ws = self._q_words(self.screen.grab(self.rect))
                q = next((w for w in ws if "reparar" in norm(w[0]) and w[2] < self.rect[3] * 0.3), None)
            if not q:
                # la lectura de pantalla entera pierde renglones según el fondo: leo recortada la zona del cartel
                im_r = self.screen.grab(self.rect)
                for sc_ in (1.0, 2.0):
                    try:
                        wr = ocr.read_words(im_r, {"x1": 0, "y1": 0, "x2": int(self.rect[2] * 0.6), "y2": int(self.rect[3] * 0.45)}, scale=sc_)
                    except Exception:
                        wr = []
                    q = next((w for w in wr if "reparar" in norm(w[0]) and w[2] < self.rect[3] * 0.3), None)
                    if q:
                        ws = wr
                        break
                if not q:
                    # segundo intento enseguida (no en 5 minutos: con el arma a 7% se rompía antes): vuelvo a abrir el
                    # menú del herrero, aprieto 'Reparar Todo' y miro el cartel varias veces seguidas
                    try:
                        menu2 = self._npc_menu(pl["npc_type"], pl["npc"], "reparar") if pl.get("npc_type", -1) >= 0 else None
                        menu2 = menu2 or self._npc_find("smith", "reparar")
                        todo2 = next((w for w in (menu2 or []) if "todo" in norm(w[0])), None)
                        if not todo2 and menu2:
                            reps2 = sorted((w for w in menu2 if norm(w[0]).startswith("repar")), key=lambda w: w[2])
                            todo2 = reps2[-1] if len(reps2) >= 2 else None
                        if todo2:
                            winapi.click(*self.abs_pt(todo2[1], todo2[2]))
                            for _w in range(4):
                                time.sleep(0.7)
                                im_r = self.screen.grab(self.rect)
                                wr = ocr.read_words(im_r, {"x1": 0, "y1": 0, "x2": int(self.rect[2] * 0.6), "y2": int(self.rect[3] * 0.5)}, scale=1.0)
                                q = next((w for w in wr if "reparar" in norm(w[0]) and w[2] < self.rect[3] * 0.3), None)
                                if q:
                                    ws = wr
                                    self.log("🔧 Segundo intento: ahora sí veo el cartel de 'Reparar todo'.")
                                    break
                    except Exception:
                        pass
                if not q:
                    try:
                        import cv2
                        cv2.imwrite(os.path.join(cfgmod.DATA, "herrero_dbg.jpg"), im_r, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    except Exception:
                        pass
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
                self._rep_ok_t = time.time()
                self._rep_nocartel = 0
                self._bag_mem = []
            elif yes:
                no = next((w for w in ws if norm(w[0]) == "no" and abs(w[2] - yes[2]) < 15), None)
                if no:
                    winapi.click(*self.abs_pt(no[1], no[2]))
                self.log(f"🔧 Reparar cuesta {cost} y tengo {gold}: no me alcanza.")
                return
            else:
                if pre is not None and pre >= 90:
                    self.log(f"🔧 Nada para reparar (lo más gastado está en {int(pre)}%).")
                    self._rep_nocartel = 0
                    self._rep_ok_t = time.time()
                elif getattr(self, "_rep_nocartel", 0) >= 2:
                    self.log("🔧 Tres veces sin ver el cartel de 'Reparar todo': sigo con el horario normal.")
                    self._rep_nocartel = 0
                else:
                    self._rep_nocartel = getattr(self, "_rep_nocartel", 0) + 1
                    self.log("🔧 No vi el cartel de 'Reparar todo' (o no había nada para reparar): guardé la captura. Reintento en 5 minutos.")
                    self._rep_retry = time.time() - max(0, float(self.cfg.get("repair_every_min", 150)) * 60 - 300)
            # lo que se rompió del todo se salió al bolso y 'Reparar Todo' no lo arregla: va por 'Reparar' (de a uno)
            if time.time() - getattr(self, "_broken_t", 0) < 7200 or self.cfg.get("repair_loose_always", False):
                try:
                    n_fix = self._oly_repair_loose(pl)
                    if n_fix:
                        self._broken_t = 0
                        self._worn_seen = time.time()  # para que abajo me los vuelva a poner
                except Exception as e:
                    self.log(f"🔧 Error reparando lo suelto: {e!r}")
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
            try:
                self._unblock_doors(getattr(self, "_doors_tok", None))
            except Exception:
                pass
            self._doors_tok = None
            self._rep_last = getattr(self, "_rep_retry", None) or time.time()
            self._gear_t = 0  # recién salido del herrero (sin bichos): buen momento para ver qué tengo puesto
            self._rep_retry = None

    def _oly_repair_loose(self, pl):
        """Herrero → 'Reparar' (a secas): se abre la ventana Reparar con el bolso al lado. Doble clic en el equipo roto
        del bolso lo pasa a la lista; un clic en ese renglón de la lista lo repara (clic derecho lo quita). Devuelve
        cuántos reparó. (Secuencia que mostró Juan.)"""
        norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
        self._ui_clear()
        time.sleep(0.6)
        menu = self._npc_menu(pl["npc_type"], pl["npc"], "reparar") if pl.get("npc_type", -1) >= 0 else None
        menu = menu or self._npc_find("smith", "reparar")
        if not menu:
            self.log("🔧 No pude abrir el menú del herrero para reparar lo suelto.")
            return 0
        todo_y = [w[2] for w in menu if "todo" in norm(w[0])]
        reps = sorted((w for w in menu if norm(w[0]).startswith("repar") and "todo" not in norm(w[0])
                       and not any(abs(w[2] - ty) < 8 * ky for ty in todo_y)), key=lambda w: w[2])
        if not reps:
            self.log("🔧 No encontré la opción 'Reparar' en el menú del herrero.")
            return 0
        winapi.click(*self.abs_pt(reps[0][1], reps[0][2]))
        time.sleep(2.0)

        def win_words():
            img = self.screen.grab(self.rect)
            bh = self._bag_open(img)
            x2 = int(bh[1] - 45 * kx) if bh else int(self.rect[2] * 0.62)
            ws_ = ocr.read_words(img, {"x1": 0, "y1": 0, "x2": max(200, x2), "y2": int(self.rect[3] * 0.9)}, scale=1.0)
            return ws_, bh
        ws, bh = win_words()
        tt = next((w for w in ws if "tienes" in norm(w[0])), None)
        if not tt and not any(norm(w[0]) in ("arrastra", "dañado", "danado", "repararlo") for w in ws):
            self.log("🔧 No se abrió la ventana 'Reparar'.")
            return 0
        fixed = []
        # lo que ya está en la lista (quedó marcado de una vez anterior: en el bolso se ve con borde amarillo y así no
        # se puede equipar ni leer): un clic en su renglón lo repara
        try:
            y_hi0 = (tt[2] - 14 * ky) if tt else self.rect[3] * 0.8
            y_lo0 = (tt[2] - 340 * ky) if tt else self.rect[3] * 0.1
            rows_ = {}
            for w in ws:
                if y_lo0 < w[2] < y_hi0:
                    rows_.setdefault(round(w[2] / (11 * ky)), []).append(w)
            for r_ in list(rows_.values())[:5]:
                r_ = sorted(r_, key=lambda v: v[1])
                nm_ = " ".join(v[0] for v in r_ if not v[0].replace(".", "").replace(",", "").isdigit()).strip()
                gi_ = self._game_item(nm_) if len(norm(nm_)) >= 4 else None
                if gi_ and gi_.get("type") == 1:
                    winapi.click(*self.abs_pt(r_[0][1], r_[0][2]))
                    time.sleep(1.2)
                    fixed.append(gi_.get("name") or nm_)
        except Exception:
            pass
        try:
            import cv2
            cv2.imwrite(os.path.join(cfgmod.DATA, "reparar_dbg.jpg"), self.screen.grab(self.rect), [cv2.IMWRITE_JPEG_QUALITY, 80])
        except Exception:
            pass
        if not bh:
            winapi.press("F6")
            time.sleep(1.1)
        bag = self._oly_bag() or []
        # roto = durabilidad 0 (aunque no le haya leído bien el nombre) o equipo grande sin cartelito (lo roto/marcado a
        # veces no lo muestra). Lo que no esté dañado no aparece en la lista y no pasa nada.
        # lo que la ficha del personaje dice que se me salió (regla de Juan: la durabilidad se ve en la pantalla de
        # personaje). Si falta de la ficha y está en el bolso, es lo roto: va a la lista aunque el cartelito del bolso
        # no diga 'durabilidad 0'
        miss_ = [norm(n_)[:8] for n_ in (getattr(self, "_gear_missing", None) or []) if n_]

        def is_missing(o):
            nm_ = norm(re.sub(r"^\s*\d+\s*", "", o.get("name") or ""))
            return bool(nm_) and any(nm_.startswith(m_) or m_.startswith(nm_[:8]) for m_ in miss_)
        brk = [o for o in bag if o["kind"] == "item" and (o.get("dur") == 0 or (o.get("name") and self._bag_judge(o)[0] == "broken")
                                                         or is_missing(o)
                                                         or (not o.get("name") and (o.get("area") or 0) >= 1200 * kx * ky))][:8]
        if not brk and not fixed:
            self._smith_nobroken_t = time.time()
            self.log("🔧 En el bolso no veo nada roto para reparar.")
        HINT = ("arrastra", "aqui", "aquí", "dañado", "danado", "repararlo", "reparar", "quitarlo", "quitarlos", "clic", "haz",
                "puedes", "lista", "items", "ítems", "item", "ítem", "para", "tienes", "total", "costo", "coste")

        def list_rows(ws_, tt_):
            y_hi = (tt_[2] - 14 * ky) if tt_ else self.rect[3] * 0.8
            y_lo = (tt_[2] - 340 * ky) if tt_ else self.rect[3] * 0.1
            rows = {}
            for w in ws_:
                if y_lo < w[2] < y_hi and norm(w[0]) and norm(w[0]) not in HINT:
                    rows.setdefault(round(w[2] / (11 * ky)), []).append(w)
            return {k: sorted(v, key=lambda v_: v_[1]) for k, v in rows.items()
                    if any(len(norm(v_[0])) >= 3 and not norm(v_[0]).isdigit() for v_ in v)}
        for o in brk:
            ws, _bh = win_words()
            tt = next((w for w in ws if "tienes" in norm(w[0])), tt)
            before = list_rows(ws, tt)
            x, y = self.abs_pt(o["x"], o["y"])
            winapi.click(x, y)
            time.sleep(0.12)
            winapi.click(x, y)
            time.sleep(1.0)
            winapi.move(*self.abs_pt(int(self.rect[2] * 0.5), int(self.rect[3] * 0.86)))
            time.sleep(0.3)
            ws, _bh = win_words()
            tt = next((w for w in ws if "tienes" in norm(w[0])), tt)
            after = list_rows(ws, tt)
            # el renglón NUEVO de la lista es este objeto (no dependo de haberle leído el nombre)
            new = [after[k] for k in sorted(after) if not any(abs(k - b) <= 0 for b in before)]
            if len(after) > len(before) and new:
                r_ = new[-1]
            else:
                keys = [k for k in (norm(p_) for p_ in str(o.get("name") or "").replace("(", " ").replace(")", " ").split()) if len(k) >= 4]
                r_ = next((v for v in after.values() if any(k in norm(w[0]) or (len(norm(w[0])) >= 4 and norm(w[0]) in k) for w in v for k in keys)), None) if keys else None
            if not r_:
                if o.get("name"):
                    self.log(f"🔧 Puse {o['name']} para reparar pero no apareció en la lista (no está dañado o no lo agarró).")
                continue
            winapi.click(*self.abs_pt(r_[0][1], r_[0][2]))
            time.sleep(1.2)
            fixed.append(o.get("name") or " ".join(w[0] for w in r_)[:40])
        if fixed:
            self.log("🔧 Reparé lo que estaba roto en el bolso: " + ", ".join(fixed) + ".")
        # cierro la ventana (igual que la tienda: clic derecho en un lugar vacío) y el bolso
        try:
            if not self._close_shop_window() and tt:
                winapi.click(*self.abs_pt(tt[1] + 190 * kx, tt[2] - 345 * ky), button="right")
                time.sleep(0.5)
        except Exception:
            pass
        self._ui_clear()
        self._bag_mem = []
        return len(fixed)

    def _tip_first_line(self, pt):
        """Sostengo el clic sobre ese punto del bolso y devuelvo el primer renglón del cartelito (el nombre)."""
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
        winapi.left_up()
        winapi.move(*self.abs_pt(pt[0], pt[1]))
        time.sleep(0.3)
        winapi.left_down()
        time.sleep(0.6)
        im_ = self.screen.grab(self.rect)
        winapi.left_up()
        time.sleep(0.3)
        tw_ = ocr.read_words(im_, {"x1": max(0, int(pt[0] - 60 * kx)), "y1": max(0, int(pt[1] - 20 * ky)),
                                   "x2": min(im_.shape[1], int(pt[0] + 420 * kx)), "y2": min(im_.shape[0], int(pt[1] + 160 * ky))}, scale=1.0)
        if not tw_:
            return ""
        top = min(w[2] for w in tw_)
        return " ".join(w[0] for w in sorted([w for w in tw_ if w[2] - top < 10 * ky], key=lambda w: w[1])).strip()

    def _stack_fix(self, sc):
        """Pilas con número verde que NO son pociones (el jamón/carne es rojo y lo contaba como 19 pociones de vida):
        las que verifiqué con el cartelito quedan anotadas por su lugar en el bolso; acá las descuento."""
        fx = getattr(self, "_stack_names", None) or {}
        if not fx or not sc or not sc.get("stacks"):
            return sc
        pw = getattr(self, "_bag_hdr_xy", None) or (0, 0)
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
        for st in sc["stacks"]:
            rel = (st["x"] - pw[0], st["y"] - pw[1])
            hit = next((v for k, v in fx.items() if abs(k[0] - rel[0]) <= 14 * kx and abs(k[1] - rel[1]) <= 14 * ky
                        and time.time() - v["t"] < 3600), None)
            if hit and hit["kind"] != st["kind"]:
                sc["pots"][st["kind"]] = max(0, sc["pots"][st["kind"]] - st["n"])
                st["kind"] = hit["kind"]
                if hit["kind"] == "comida":
                    sc["food_n"] = sc.get("food_n", 0) + st["n"]
                    sc["food_pt"] = (st["x"], st["y"])
        return sc

    def _bag_order_check(self):
        """Cada 15 minutos (o enseguida si leí mal las pociones), sin bichos cerca: ordeno el bolso."""
        if cfgmod.server_slug(self.cfg) == "abaddon" or not self.running or self.pos.get("x") is None:
            return
        now = time.time()
        sc = getattr(self, "_bag_scan", None) or {}
        bad = bool(sc.get("unknown") or sc.get("vida_hidden"))
        if now - getattr(self, "_order_t", 0) < (600 if bad else 900):
            return
        if getattr(self, "mission", None) or getattr(self, "_busy_ui", False) or getattr(self, "goto", None) or self._ui_th_alive():
            return
        if self.state == "attack" or self._under_attack() or self._mobs_within(5) >= 1 or now < getattr(self, "_rabia_until", 0):
            return
        self._order_t = now
        self._busy_ui = True
        self._busy_t0 = now
        try:
            self._bag_order()
        finally:
            self._busy_ui = False

    def _bag_order(self):
        """Ordeno el bolso como lo hace Juan: cada tipo de frasco (y la comida) en su lugar fijo de la columna
        izquierda, sin nada encima, así la cantidad se lee siempre. Con Shift apretado, arrastrar UN frasco mueve toda
        la pila de ese tipo. Lo que esté ocupando esos lugares lo corro a un lugar libre. Nunca suelto cerca del borde
        (soltar afuera es tirar) y si sale el cartel de tirar, contesto No."""
        from . import bagscan
        kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
        opened = False
        if not bool(self._bag_open(self.screen.grab(self.rect))):
            winapi.press("F6")
            time.sleep(0.9)
            opened = True
        moved = 0
        try:
            self._bag_scan = None
            bag = self._oly_bag() or []
            sc = getattr(self, "_bag_scan", None)
            if not sc:
                return 0
            bx1, by1, bx2, by2 = sc["box"]
            col_x = int(bx1 + 50 * kx)
            slots = {k: (col_x, int(by1 + (55 + 62 * i) * ky)) for i, k in enumerate(("vida", "energía", "maná", "comida"))}
            if not all(bx1 + 40 * kx <= x <= bx2 - 40 * kx and by1 + 35 * ky <= y <= by2 - 35 * ky for x, y in slots.values()):
                return 0
            near = lambda p, q, r=30: abs(p[0] - q[0]) <= r * kx and abs(p[1] - q[1]) <= r * ky

            def drag(x1, y1, x2, y2, stack):
                winapi.left_up()
                if stack:
                    winapi.key_down("SHIFT")
                    time.sleep(0.08)
                try:
                    self._drag_bg(x1, y1, x2, y2)
                finally:
                    if stack:
                        winapi.key_up("SHIFT")
                time.sleep(0.3)
                try:
                    self._drop_no()
                except Exception:
                    pass

            # 0) primero separo lo que está encimado (un objeto tapando una pila: la Chemise arriba del maná)
            try:
                if self._bag_tidy():
                    moved += 1
                    self._bag_scan = None
                    bag = self._oly_bag() or []
                    sc = getattr(self, "_bag_scan", None) or sc
            except Exception:
                pass
            # 1) despejo la columna: lo que no es frasco ni comida y está en un lugar fijo, va a un lugar libre
            food_names = ("baguette", "meat", "bread", "carne", "pan ")
            sc2 = dict(sc, pot_xy=list(sc.get("pot_xy") or []) + list(slots.values()))
            for o in bag:
                if o.get("kind") != "item":
                    continue
                nm = (o.get("name") or "").lower()
                if any(k in nm for k in food_names) or nm in ("mana potion", "revitalizing potion"):
                    continue
                if any(near((o["x"], o["y"]), sl, 38) for sl in slots.values()):
                    spot = bagscan.free_spot(sc2, kx, ky)
                    if not spot or not (bx1 + 40 * kx < spot[0] < bx2 - 40 * kx and by1 + 40 * ky < spot[1] < by2 - 40 * ky):
                        break
                    drag(o["x"], o["y"], spot[0], spot[1], False)
                    sc2["pot_xy"].append(spot)
                    moved += 1
            # 1b) verifico con el cartelito qué es cada pila con número (no todo lo que tiene número es poción)
            try:
                self._bag_scan = None
                self._oly_bag(names=False)
                sc_v = getattr(self, "_bag_scan", None) or {}
                pw_ = getattr(self, "_bag_hdr_xy", None) or (0, 0)
                names_ = {}
                want_ = {"vida": "health", "energía": "revitaliz", "maná": "mana"}
                for st in (sc_v.get("stacks") or [])[:6]:
                    nm_raw = self._tip_first_line((st["x"], st["y"]))
                    nm_ = nm_raw.lower()
                    gi_ = self._game_item(nm_raw) if len(nm_raw) >= 4 else None
                    if not nm_ or not gi_:
                        continue  # no leí un nombre del juego: no cambio nada (antes descontaba pociones de verdad)
                    nm_ = str(gi_.get("name") or nm_raw).lower()
                    kind_ = st["kind"]
                    if want_[kind_] not in nm_ and "potion" not in nm_:
                        kind_ = "comida" if any(k in nm_ for k in ("meat", "baguette", "bread", "carne", "pan")) else "otro"
                        self.log(f"🎒 La pila de {st['n']} que creía {st['kind']} es '{nm_}': no la cuento como pociones.")
                    names_[(st["x"] - pw_[0], st["y"] - pw_[1])] = {"kind": kind_, "name": nm_, "t": time.time()}
                self._stack_names = names_
            except Exception:
                pass
            # 2) cada tipo de frasco a su lugar (Shift = toda la pila)
            for kind in ("vida", "energía", "maná"):
                self._bag_scan = None
                self._oly_bag(names=False)
                sc = getattr(self, "_bag_scan", None) or {}
                sc = self._stack_fix(sc)
                pts = [(st_["x"], st_["y"]) for st_ in (sc.get("stacks") or []) if st_["kind"] == kind] \
                    or [p for p, k in zip(sc.get("pot_xy") or [], sc.get("pot_kinds") or []) if k == kind and not sc.get("stacks")]
                if kind == "vida" and not pts and sc.get("vida_hidden_pt"):
                    pts = [sc["vida_hidden_pt"]]
                if not pts or (len(pts) == 1 and near(pts[0], slots[kind], 14)):
                    continue
                for p in pts[:3]:
                    if not near(p, slots[kind], 14):
                        drag(p[0], p[1], slots[kind][0], slots[kind][1], True)
                        moved += 1
            # 3) la comida (la reconozco por el nombre del cartelito; la pila de carne la conozco del paso 1b)
            self._bag_scan = None
            self._oly_bag(names=False)
            scf = self._stack_fix(getattr(self, "_bag_scan", None) or {})
            if scf.get("food_pt") and not near(scf["food_pt"], slots["comida"], 14):
                drag(scf["food_pt"][0], scf["food_pt"][1], slots["comida"][0], slots["comida"][1], True)
                moved += 1
            for o in (self._oly_bag() or []):
                nm = (o.get("name") or "").lower()
                if o.get("kind") == "item" and any(k in nm for k in food_names) and not near((o["x"], o["y"]), slots["comida"], 14):
                    drag(o["x"], o["y"], slots["comida"][0], slots["comida"][1], True)
                    moved += 1
                    break
            self._bag_scan = None
            self._bag_mem = []
            time.sleep(0.4)
            self._oly_bag(names=False)
            sc = getattr(self, "_bag_scan", None) or {}
            if not sc.get("pots"):
                time.sleep(0.8)
                self._oly_bag(names=False)
                sc = getattr(self, "_bag_scan", None) or {}
            if moved:
                p_ = sc.get("pots") or {}
                self.log(f"🎒 Ordené el bolso ({moved} movimiento/s): vida {p_.get('vida', '?')}, energía {p_.get('energía', '?')}, "
                         f"maná {p_.get('maná', '?')}" + (" — todavía hay algo tapado." if sc.get("unknown") or sc.get("vida_hidden") else "."))
        except Exception as e:
            self.log(f"🎒 No pude ordenar el bolso: {e!r}")
        finally:
            winapi.key_up("SHIFT")
            if opened and bool(self._bag_open(self.screen.grab(self.rect))):
                winapi.press("F6")
                time.sleep(0.4)
        return moved

    def _bag_tidy(self, force=False):
        """Separo lo que quedó encimado en el bolso (un frasco arriba del escudo recién comprado): arrastro el objeto
        a un lugar vacío, siempre bien adentro del bolso. Devuelve cuántos moví."""
        from . import bagscan
        moved = 0
        try:
            kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
            if not bool(self._bag_open(self.screen.grab(self.rect))):
                winapi.press("F6")
                time.sleep(0.9)
            for _ in range(3):
                self._bag_scan = None
                self._oly_bag(names=False)
                sc = getattr(self, "_bag_scan", None)
                if not sc:
                    break
                todo = bagscan.crowded(sc, kx, ky) or (sc["items"][:1] if force else [])
                force = False
                spot = bagscan.free_spot(sc, kx, ky)
                if not todo or not spot:
                    break
                it = todo[0]
                bx1, by1, bx2, by2 = sc["box"]
                if not (bx1 + 40 < spot[0] < bx2 - 40 and by1 + 40 < spot[1] < by2 - 40):
                    break
                self._release()
                self._drag_bg(it["x"], it["y"], spot[0], spot[1])
                moved += 1
            if moved:
                self.log(f"🎒 Acomodé el bolso: separé {moved} objeto/s que estaban encimados.")
        except Exception as e:
            self.log(f"🎒 No pude acomodar el bolso: {e!r}")
        return moved

    def _reequip_loose(self, max_n=3):
        """Lo que quedó suelto en el bolso y no es poción ni oro (un arma que se desequipó al romperse, o lo recién
        comprado): doble click para ponérmelo. En este cliente, equipar desde el bolso manda al bolso lo que estaba
        puesto, así que hago como mucho 'max_n' cambios y no vuelvo a tocar el mismo lugar."""
        try:
            if not bool(self._bag_open(self.screen.grab(self.rect))):
                winapi.press("F6")
                time.sleep(0.9)
            self._bag_tidy()
            done, n, put, left = [], 0, [], []
            worn = self.cfg.setdefault("worn", {})
            lvl = self._level_mem() or int(self.cfg.get("travel", {}).get("my_level", 1))
            fam = str(self._load_json("tiendas.json").get("talent") or "").strip().lower()
            for _ in range(max(max_n, 6)):
                # paso el mouse por cada cosa (sin clic): el cartelito dice qué es. Solo me pongo lo que MEJORA lo puesto.
                bag = self._oly_bag() or []
                pick = None

                def _pri(o_):
                    g_ = self._game_item(o_.get("name")) or {}
                    return (0 if str(g_.get("family") or "").lower() == fam else 1, -int(g_.get("dmg") or g_.get("def") or 0))
                for o in sorted(bag, key=_pri):
                    if o["kind"] != "item" or any(abs(o["x"] - p[0]) < 14 and abs(o["y"] - p[1]) < 14 for p in done):
                        continue
                    v, why, it, slot, val = self._bag_judge(o)
                    if v != "equip":
                        if it and v != "other" and it["name"] not in [l[0] for l in left]:
                            left.append((it["name"], why))
                        done.append((o["x"], o["y"]))
                        continue
                    pick = (o, it, slot, val)
                    break
                if not pick:
                    break
                o, it, slot, val = pick
                done.append((o["x"], o["y"]))
                x, y = self.abs_pt(o["x"], o["y"])
                okeq = False
                for how in ("doble", "ctrl"):
                    if how == "doble":
                        winapi.click(x, y)
                        time.sleep(0.12)
                        winapi.click(x, y)
                    else:
                        winapi.key_down("CTRL")
                        time.sleep(0.06)
                        winapi.click(x, y)
                        time.sleep(0.06)
                        winapi.key_up("CTRL")
                    time.sleep(1.2)
                    # ¿se lo puso de verdad? En ese lugar ya no tiene que estar ESE objeto (puede haber quedado el
                    # que tenía puesto: por eso miro el nombre, no el tamaño)
                    self._bag_mem = [m for m in (getattr(self, "_bag_mem", None) or [])
                                     if abs(m["rx"] - o.get("rx", 9999)) > 40 or abs(m["ry"] - o.get("ry", 9999)) > 40]
                    bag2 = self._oly_bag() or []
                    here = [b_ for b_ in bag2 if b_["kind"] == "item" and abs(b_["x"] - o["x"]) < 22 and abs(b_["y"] - o["y"]) < 22]
                    same = [b_ for b_ in here if (self._game_item(b_.get("name")) or {}).get("name") == it["name"]
                            and (self._game_item(b_.get("name")) or {}).get("affix") == it.get("affix")]
                    if not same and (not here or all(b_.get("name") for b_ in here)):
                        okeq = True
                        break
                same_as_worn = (worn.get(slot) or {}).get("name") == (it.get("affix") + " " if it.get("affix") else "") + it["name"]
                if not okeq and not same_as_worn:
                    left.append((it["name"], "no pude ponérmelo (probé doble clic y Ctrl+clic)"))
                if okeq:
                    n += 1
                    put.append(it["name"])
                    worn[slot] = {"name": (it.get("affix") + " " if it.get("affix") else "") + it["name"], "v": val, "ok": True}
                    cfgmod.save(self.cfg)
            if left:
                self.log("🎒 Dejo en el bolso: " + "; ".join(f"{a_} ({b_})" for a_, b_ in left[:5]) + ".")
            if put:
                self.log("🛡 Me puse: " + ", ".join(put) + ".")
                n = 0
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
            if bool(self._bag_open(self.screen.grab(self.rect))):
                winapi.press("F6")
                time.sleep(0.4)
        except Exception as e:
            self.log(f"🛡 No pude revisar el bolso: {e!r}")

    def _worn_check(self):
        """El juego avisa abajo a la izquierda cuando algo se rompió ('El objeto está desgastado'). Lo miro cada 20 s."""
        now = time.time()
        if now - getattr(self, "_worn_chk", 0) < 4 or self.img is None:
            return False
        self._worn_chk = now
        try:
            H, W = self.img.shape[:2]
            ws = ocr.read_words(self.img, {"x1": 0, "y1": int(H * 0.80), "x2": int(W * 0.55), "y2": int(H * 0.915)}, scale=1.0)
            t = " ".join(w[0].lower() for w in ws)
            if any(k in t for k in ("desgast", "repáral", "reparal", "se rompi", "está roto", "esta roto", "agotad", "exhaust", "broken")):
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
        pu = getattr(self, "_pot_use", None)
        pots_h = pu["used"] / ((now - pu["t0"]) / 3600.0) if pu and now - pu["t0"] > 1800 else None
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
            elif d30 >= 3 and (pots is None or pots < 3) and (gold is None or gold < kit):
                why = f"me mataron {d30} veces en media hora y no tengo pociones ni oro para reponer"
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
            if pots is not None and pots >= 5:
                back = f"ya tengo {pots} pociones: vuelvo a la zona que más experiencia da"
            elif gold is not None and gold >= kit and mins >= 10:
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
            if not hasattr(self, "_rep_last"):
                self._rep_last = now
            # aviso del juego, o dejé de matar de golpe (dos "no muere" seguidos): arma rota. Eso es urgente: sin arma
            # no se pelea, así que salgo aunque me estén pegando.
            # pegado a un bicho hace más de 75 s sin matar nada: el arma se rompió (o no tengo). No espero 8 minutos:
            # corto y voy al pueblo a reparar y ponérmela, sin pelear en el camino.
            try:
                adj_n = len(self._grid().adjacent())
            except Exception:
                adj_n = 0
            if adj_n:
                if not getattr(self, "_adj_since", 0):
                    self._adj_since = now
                self._adj_seen = now
            elif now - getattr(self, "_adj_seen", 0) > 4:
                self._adj_since = 0
            a0 = getattr(self, "_adj_since", 0)
            try:
                lim75 = max(75.0, 2.5 * max([self._oly_ttk(t_.name) for t_ in self._grid().adjacent()] or [0]))
            except Exception:
                lim75 = 75.0
            if (a0 and now - max(a0, getattr(self, "_last_kill_t", 0)) > lim75 and self.running
                    and now - getattr(self, "_nokill_town", 0) > 600 and getattr(self, "_town_done", True)):
                self._adj_since = 0
                wok = getattr(self, "_weapon_ok_t", 0)
                if now - wok < 300 and getattr(self, "_broken_t", 0) <= wok:
                    # la ficha dice que el arma está puesta y sana: el problema es el bicho (élite, trabado detrás de
                    # algo), no el arma. Lo dejo y me muevo, sin ir al pueblo.
                    self.log("🔧 Pego sin matar pero el arma está bien puesta: dejo ese bicho y me muevo.")
                    self._nokill_town = now - 300
                    self.target, self.state = None, "seek"
                    return
                if now - getattr(self, "_nokill_chk", 0) > 300:
                    # antes de ir al pueblo confirmo con la ficha (el chequeo de equipo manda al pueblo si falta)
                    self._nokill_chk = now
                    self.log("🔧 Hace más de un minuto que pego sin matar: miro la ficha a ver si tengo el arma.")
                    self._gear_t = 0
                    self._nokill_town = now - 480
                    self.target, self.state = None, "seek"
                    return
                self._nokill_town = now
                self.log("🔧 Hace más de un minuto que le pego a un bicho y no muere: el arma no está o se rompió. Voy al pueblo a reparar y ponérmela.")
                self._kite = None
                self._town_need("dejé de matar")
                return
            el = [t for t in getattr(self, "_elite_ts", []) if now - t < 240]
            lk = getattr(self, "_last_kill_t", 0)
            # dos "no muere" seguidos NO alcanzan para decir que el arma se rompió (al llegar a un nido nuevo pasa
            # siempre y lo sacaba de donde estaba cazando bien). Para eso está la regla de los 75 s pegado sin matar.
            weak = False
            if self._ui_th_alive():
                return
            worn = self._worn_check() or weak or now - getattr(self, "_worn_seen", 0) < 600 and getattr(self, "_worn_seen", 0) > self._rep_last
            overdue = now - self._rep_last > float(self.cfg.get("repair_every_min", 150)) * 60 + 480
            if not worn and not overdue and (self.state == "attack" or self._under_attack() or self._fleeing()):
                return
            if worn and now - self._rep_last >= 300:
                self._elite_ts = []
                self._audit_t = now
                self._rep_last = now
                self._release()
                why = "dejé de matar: capaz se rompió el arma" if weak else "el juego avisó que algo se rompió"
                if weak:
                    self._weak_t = now
                self._rep_th = threading.Thread(target=self._oly_repair_trip, args=(why,), daemon=True)
                self._rep_th.start()
                return
            if not hasattr(self, "_loose_ok"):
                self._loose_ok = int(self.cfg.get("loose_ok", 0))
            # 'equipo suelto' solo cuenta si vi algo roto en el bolso (antes iba cada media hora por tener el bolso lleno)
            loose = (getattr(self, "_bag_items", 0) or 0) > self._loose_ok and now - self._rep_last > 1800 \
                and now - getattr(self, "_broken_t", 0) < 7200
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
            self._inv_cache = self._oinv_cache = None
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
        if winapi.OFF:
            return
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
        self._inv_cache = self._oinv_cache = None
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
        if self.cfg.get("gear_hold"):
            return  # Juan lo equipó a mano y todavía no sé qué tiene puesto: no compro nada que lo reemplace
        # no compro lo que ya tengo en el bolso o puesto (pasó: la confirmación no se leyó y compró 3 Leather Armor)
        have_ = {norm(re.sub(r"^\s*\d+\s*", "", o.get("name") or "")) for o in (getattr(self, "_bag_mem", None) or [])
                 if o.get("kind") == "item"}
        have_ |= {norm(w.get("name") or "") for w in (self.cfg.get("worn") or {}).values() if isinstance(w, dict)}
        have_ |= {norm(w.get("name") or "") for w in (self.cfg.get("worn_best") or {}).values() if isinstance(w, dict)}
        have_ = {h_ for h_ in have_ if h_}
        # el nombre puede estar cortado ('Leather' por 'Leather Armor(M)'): alcanza con que arranque igual
        items = [i for i in items if not any(norm(i["name"]).startswith(h_[:7]) and len(h_) >= 5 for h_ in have_)]
        if not items:
            return
        if time.time() - getattr(self, "_buy_trip_t", 0) < 1800:
            return  # una compra de equipo por media hora como mucho
        self._buy_trip_t = time.time()
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
            kx, ky = self.rect[2] / 1920.0, self.rect[3] / 1080.0
            def tabname(t):
                t = norm(t)
                if t in ("armas", "armadura", "varios"):
                    return t
                if t.endswith("rmadura"):
                    return "armadura"
                if t.endswith("rmas") and len(t) <= 6:
                    return "armas"
                if t.endswith("arios") and len(t) <= 7:
                    return "varios"
                return None
            tabs = {tabname(w[0]): w for w in ws if tabname(w[0]) and w[2] < self.rect[3] * 0.4}
            t_ref = tabs.get("armas")
            if not t_ref:
                # letra gótica: pruebo recortado y agrandado; si no, ubico la pestaña por la columna de precios
                try:
                    im_ = self.screen.grab(self.rect)
                    for sc_ in (1.0, 2.0):
                        wc = ocr.read_words(im_, {"x1": int(self.rect[2] * 0.12), "y1": int(self.rect[3] * 0.12),
                                                  "x2": int(self.rect[2] * 0.6), "y2": int(self.rect[3] * 0.42)}, scale=sc_)
                        for w in wc:
                            if norm(w[0]) in ("armas", "arrnas", "armadura", "varios"):
                                tabs.setdefault("armas" if norm(w[0]) == "arrnas" else norm(w[0]), w)
                        if tabs.get("armas"):
                            break
                    t_ref = tabs.get("armas")
                    if not t_ref and tabs.get("armadura"):
                        t_ref = ("Armas", tabs["armadura"][1] - 148 * kx, tabs["armadura"][2])
                    if not t_ref:
                        full_ = ocr.read_words(im_, {"x1": 0, "y1": 0, "x2": self.rect[2], "y2": int(self.rect[3] * 0.9)}, scale=1.0)
                        col_ = self._shop_price_col(full_)
                        if col_:
                            t_ref = ("Armas", col_[0] - 425 * kx, col_[1] - 46 * ky)
                except Exception:
                    pass
            if not t_ref:
                self.log("🛍 No veo las pestañas de la tienda: la cierro y pruebo de nuevo en unos minutos.")
                try:
                    self._close_shop_window()
                except Exception:
                    pass
                self._gear_retry = True
                return
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
                seen_rows = {}
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
                    for r_ in rows:
                        seen_rows[r_[0]] = r_[1]
                    if not row:
                        # mismo nombre con otro precio (el server lo cambió, o lo leí sin el punto de miles): vale si el
                        # nombre es exacto y el precio no se aleja más de 15% del catálogo
                        row = next((r for r in rows if norm(r[0]) == want and abs(r[1] - int(it["price"])) <= 0.15 * int(it["price"])), None)
                    if row:
                        break
                    self._wheel(t_ref[1] + 150, t_ref[2] + 250, -120)
                    time.sleep(0.35)
                if not row and (it.get("slot") in ("arma", "arma2m") or self.cfg.get("need_weapon")):
                    # el arma del plan no está (o subió de precio): me quedo con la mejor de mi talento, a dos manos, que
                    # vi en la lista, que pueda usar por nivel y pagar dejando 500 para pociones
                    try:
                        fam_ = str(self._load_json("tiendas.json").get("talent") or "").strip().lower()
                        lvl_ = int(self._level_mem() or int(self.cfg.get("travel", {}).get("my_level", 1)))
                        gold_ = int(getattr(self, "_gold", 0) or 0)
                        best_ = None
                        for k_, v_ in seen_rows.items():
                            g_ = self._game_item(k_) or {}
                            if not g_ or (fam_ and str(g_.get("family") or "").lower() != fam_):
                                continue
                            if self._two_handed() and str(g_.get("slot") or "") != "arma2m":
                                continue
                            if int(g_.get("level") or 0) > lvl_ or (gold_ and v_ > gold_ - 500):
                                continue
                            sc_ = self._gear_score(g_.get("name") or k_)
                            if not best_ or sc_ > best_[0]:
                                best_ = (sc_, k_, v_)
                        if best_:
                            self.log(f"🛍 En su lugar compro {best_[1]} ({best_[2]}): la mejor de mi talento que veo y puedo pagar.")
                            want = norm(best_[1])
                            for _ in range(45):
                                self._wheel(t_ref[1] + 150, t_ref[2] + 250, 120)
                                time.sleep(0.05)
                            time.sleep(0.4)
                            for _pg in range(40):
                                winapi.move(*self.abs_pt(t_ref[1] + 700, t_ref[2] + 500))
                                time.sleep(0.3)
                                rows = self._shop_rows(self.screen.grab(self.rect), t_ref)
                                row = next((r for r in rows if norm(r[0]) == want and r[1] == best_[2]), None)
                                if row:
                                    it = dict(it, name=best_[1], price=best_[2])
                                    break
                                self._wheel(t_ref[1] + 150, t_ref[2] + 250, -120)
                                time.sleep(0.35)
                    except Exception:
                        row = None
                if not row:
                    near_ = [f"{k} {v}" for k, v in seen_rows.items() if any(t_ in norm(k) for t_ in ("bastard", "sword", "claymore"))][:8]
                    self.log(f"🛍 No encontré '{it['name']}' a {it['price']} en la lista (vi {len(seen_rows)} filas"
                             + (": " + "; ".join(near_) if near_ else "") + ").")
                    try:
                        import cv2
                        cv2.imwrite(os.path.join(cfgmod.DATA, "herrero_lista.jpg"), self.screen.grab(self.rect), [cv2.IMWRITE_JPEG_QUALITY, 80])
                    except Exception:
                        pass
                    if len(seen_rows) >= 20:
                        # recorrí la lista entera y no está: esta tienda no lo vende. Que el plan elija otra cosa.
                        self._nostock_n = dict(getattr(self, "_nostock_n", {}) or {})
                        self._nostock_n[it["name"]] = self._nostock_n.get(it["name"], 0) + 1
                        if self._nostock_n[it["name"]] >= 2:
                            self.world.learn.setdefault("no_stock", {})[it["name"]] = time.time()
                            self.world.save()
                            self._brain_t = 0
                            self.log(f"🛍 Dos veces recorrí la lista y no venden '{it['name']}': lo saco del plan por un día y elijo otra cosa.")
                    continue
                winapi.click(*self.abs_pt(row[2] + 20, row[3]))
                time.sleep(0.9)
                winapi.move(*self.abs_pt(t_ref[1] + 700, t_ref[2] + 500))
                time.sleep(0.4)
                img = self.screen.grab(self.rect)
                # recuadro de abajo: nombre del objeto elegido y su precio 'N c/u'
                box = ocr.read_words(img, {"x1": int(t_ref[1] - 75), "y1": int(t_ref[2] + 440), "x2": int(t_ref[1] + 470), "y2": int(t_ref[2] + 590)}, scale=1.0)
                txt = norm(" ".join(w[0] for w in sorted(box, key=lambda w: (round(w[2] / 9), w[1]))))
                # errores típicos del OCR en este recuadro: 'rn' por 'm' ('clayrnore'), 'l'/'I' por '1'
                txt = txt.replace("rn", "m")
                toks = [norm(t).replace("rn", "m") for t in re.split(r"[\s()]+", it["name"]) if len(norm(t)) >= 2]
                price_ok = str(int(it["price"])) in txt or str(int(it["price"])) in txt.replace("o", "0")
                # la fila ya la elegí por nombre y precio exactos; si el recuadro solo deja leer el precio, alcanza
                if not (price_ok and (all(t in txt for t in toks) or len(txt) < 24)):
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
            # cierro la tienda (clic derecho en la barra del título: cierra siempre y no elige nada) y me pongo lo nuevo
            winapi.left_up()
            winapi.move(*self.abs_pt(t_ref[1] + 20 * kx, t_ref[2] - 43 * ky))
            time.sleep(0.2)
            winapi.click(*self.abs_pt(t_ref[1] + 20 * kx, t_ref[2] - 43 * ky), button="right")
            time.sleep(0.7)
            try:
                self._close_shop_window()
            except Exception:
                pass
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
            if getattr(self, "_gear_retry", False) or (items and not bought and any(i.get("slot") in ("arma", "arma2m") for i in items)):
                # no pude comprar (y si era el arma, sin arma buena no se caza): reintento en 4 minutos, no en 30
                self._gear_retry = False
                self._gear_last = time.time() - float(self.cfg.get("gear_trip_every_min", 30)) * 60 + 240
                try:
                    wv = int(((self.cfg.get("worn") or {}).get("arma") or {}).get("v") or 0)
                    lvl_ = int(self.cfg.get("travel", {}).get("my_level", 1) or 1)
                    if wv and wv <= 5 and lvl_ >= 25 and inside() and self.running:
                        # SUPERVIVENCIA: con un arma de daño 4 a este nivel no mato nada y me matan (pasó). Me quedo
                        # adentro del herrero (zona segura) hasta el próximo intento de compra.
                        self.log(f"⛔ Arma muy floja para mi nivel (daño {wv}) y no pude comprar otra: espero acá adentro, a salvo, y pruebo de nuevo en 4 minutos.")
                        self._busy_ui = True
                        t_w = time.time()
                        while time.time() - t_w < 235 and self.running and self.alive:
                            self._busy_t0 = time.time()
                            time.sleep(1.0)
                        self._busy_ui = False
                        self._gear_last = 0
                except Exception:
                    pass

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
        self._inv_cache = self._oinv_cache = None
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
        self._inv_cache = self._oinv_cache = None
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
        self._inv_cache = self._oinv_cache = None
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
        self._inv_cache = self._oinv_cache = None
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
                        ov = vision.draw_overlay(img, self.cfg, mons, self._loot_items_draw(), self.target, track=getattr(self, "_trk_draw", None), grid=self._grid_overlay())
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
        ov = vision.draw_overlay(self.img, self.cfg, mons, self._loot_items_draw(), self.target, track=getattr(self, "_trk_draw", None), grid=self._grid_overlay())
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
                         "exp_num": self.progress.get("exp_num"), "exp_left": self.progress.get("exp_left"),
                         "levelups": self.progress.get("levelups", 0)},
            "nav": {"status": self.nav["status"], "dest": self.nav["dest"]},
            "mobs": self._mobs_near_view(),
            "goto": getattr(self, "goto", None),
            "mode": self.bot_mode(), "mode_label": self.MODES.get(self.bot_mode()),
        }
