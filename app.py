"""HB Abaddon Bot — ventana principal (pywebview + UI en HTML)."""
import os
import sys
import webbrowser

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# si el bot se cae, que quede el motivo en data/crash.log (incluso caídas de código nativo)
try:
    import faulthandler
    import threading as _th
    import traceback as _tb
    _CRASH = open(os.path.join(BASE, "data", "crash.log"), "a", encoding="utf-8")
    faulthandler.enable(file=_CRASH, all_threads=True)

    def _hook(et, ev, tb):
        import time as _t
        _CRASH.write(_t.strftime("\n%Y-%m-%d %H:%M:%S ") + "".join(_tb.format_exception(et, ev, tb)))
        _CRASH.flush()
    sys.excepthook = _hook
    _th.excepthook = lambda a: _hook(a.exc_type, a.exc_value, a.exc_traceback)
except Exception:
    pass

from bot import config as cfgmod  # noqa: E402
from bot import events as evmod  # noqa: E402
from bot.engine import Bot  # noqa: E402


MINI = None


class Api:
    def __init__(self, bot):
        self._bot = bot

    # estado
    def state(self):
        return self._bot.snapshot()

    def toggle(self):
        self._bot.toggle()
        return self._bot.running

    def start(self):
        self._bot.start()
        return self._bot.running

    def stop(self):
        self._bot.stop("manual")
        return False

    def mem_diag(self):
        return self._bot.mem_diag()

    def investigate(self):
        return self._bot.investigate()

    def explore_map(self):
        return self._bot.explore_map()

    def setup_scan(self):
        return self._bot.setup_scan()

    def open_data(self):
        return self.open_folder("data")

    def frame(self, last_id=0):
        return self._bot.frame(last_id)

    def set_preview(self, on):
        self._bot.preview_on = bool(on)

    # config
    def get_config(self):
        c = dict(self._bot.cfg)
        al = dict(c.get("autologin", {}))
        al["has_password"] = bool(al.pop("password_enc", ""))
        c["autologin"] = al
        return c

    def save_config(self, cfg):
        al = cfg.get("autologin", {})
        al.pop("has_password", None)
        al["password_enc"] = self._bot.cfg.get("autologin", {}).get("password_enc", "")
        cfg["autologin"] = al
        self._bot.save_config(cfg)
        return self.get_config()

    def reset_config(self):
        self._bot.save_config(cfgmod.DEFAULTS)
        return self._bot.cfg

    def windows(self):
        return self._bot.windows()

    # calibración
    def calib(self, slot):
        self._bot.calib_begin(slot)
        return self._bot.calib.get("msg")

    def clear_calib(self, slot):
        self._bot.cfg["calib"][slot] = None
        cfgmod.save(self._bot.cfg)

    def test_background(self):
        return self._bot.test_background()

    def auto_calibrate(self):
        return self._bot.auto_calibrate()

    def set_tile(self, w, h):
        self._bot.set_tile(w, h)

    def set_capture(self, kind, name):
        self._bot.set_capture(kind, name)

    def templates(self):
        return self._bot.templates.thumbs()

    def delete_template(self, kind, name):
        self._bot.templates.delete(kind, name)
        return self._bot.templates.thumbs()

    def reload_templates(self):
        self._bot.templates.reload()
        return self._bot.templates.thumbs()

    # auto-login
    def set_login(self, account, password, char_name, enabled, relaunch):
        return self._bot.set_login(account, password, char_name, enabled, relaunch)

    # viaje
    def test_pos(self):
        return self._bot.test_pos()

    def learned(self):
        return {"links": self._bot.world.learn["links"], "names": self._bot.world.learn["names"], "game_dir": self._bot.world.game_dir}

    def world_map(self):
        from bot import world as worldmod
        try:
            v = worldmod.world_view(self._bot.world)
        except Exception as e:
            return {"maps": [], "links": [], "error": repr(e)}
        # zonas de spawn de todos los bichos de cada mapa (base de criaturas)
        try:
            names = {m["name"].lower(): m["key"] for m in v["maps"]}
            files = {m["key"].lower(): m["key"] for m in v["maps"]}
            crs = {c["type"]: c for c in self._bot._creatures_db().values()}
            sp = []
            for mname, md in (self._bot._spawns_all() or {}).items():
                mk = files.get(str(md.get("file", "")).lower()) or names.get(mname.lower())
                if not mk:
                    continue
                for z in md.get("zones", []):
                    cr = crs.get(z.get("type"), {})
                    sp.append({"map": mk, "mob": z.get("mob") or cr.get("name", "?"), "rect": z["rect"], "hp": cr.get("hp"),
                               "dmg": cr.get("dmg_avg"), "exp": cr.get("exp_dice"), "max": z.get("max"), "lvl": z.get("min_level"),
                               "seen": z.get("seen"), "survey": bool(z.get("survey"))})
            v["spawns"] = sp
        except Exception as e:
            v["spawns"], v["spawn_error"] = [], repr(e)
        return v

    def map_art(self, key):
        """Dibujo del mapa con los gráficos del juego (o None si todavía no está)."""
        try:
            r = getattr(self._bot, "maprender", None)
            if not r:
                return None
            u = r.data_url(key)
            if u is None:
                fp = next((v for k, v in self._bot.world.files.items() if k.lower() == str(key).lower()), None)
                if fp and r.ensure(key, fp):
                    u = r.data_url(key)
            return u
        except Exception:
            return None

    def map_rules(self):
        try:
            return self._bot.map_rules_view()
        except Exception as e:
            return {"error": repr(e)}

    def map_rule(self, name, rule):
        return self._bot.map_rule(name, rule)

    def server_status(self):
        try:
            return self._bot.server_status()
        except Exception as e:
            return {"error": repr(e), "items": []}

    def note_place(self, kind):
        try:
            return self._bot.note_place(kind)
        except Exception as e:
            return {"ok": False, "msg": repr(e)}

    def open_server_folder(self):
        import os as _os
        try:
            _os.startfile(cfgmod.profile_dir(self._bot.cfg))
        except Exception:
            pass
        return True

    def sheet_image(self):
        try:
            return self._bot.sheet_image()
        except Exception as e:
            return {"img": None, "error": repr(e)}

    def set_mode(self, m):
        return self._bot.set_mode(m, "lo elegiste vos")

    def brain(self):
        try:
            return self._bot.brain_view()
        except Exception as e:
            return {"error": repr(e)}

    def economy(self):
        try:
            return self._bot.economy()
        except Exception as e:
            return {"error": repr(e)}

    def goto(self, mapname, x, y):
        import time as _t
        if not mapname:
            self._bot.goto = None
            self._bot.log("🗺 Viaje del mapa cancelado.")
            if getattr(self._bot, "_walk_only", False):
                self._bot.stop("manual")
            return {"ok": True}
        self._bot.goto = {"map": str(mapname), "x": int(x), "y": int(y), "t": _t.time()}
        self._bot.log(f"🗺 Voy a {mapname} ({int(x)},{int(y)}) (clic en el mapa).")
        if not self._bot.running:
            # bot apagado: lo prendo solo para caminar hasta ahí y se apaga al llegar
            self._bot._walk_only = True
            self._bot.start()
            if self._bot.running:
                self._bot._walk_only = True
                self._bot.log("🗺 Bot apagado: camino hasta ahí y me vuelvo a apagar.")
            else:
                self._bot._walk_only = False
        return {"ok": True, "running": True}

    # datos
    def db(self):
        return self._bot.db

    def path(self):
        return self._bot._load_json("training_path.json")

    def events(self):
        return evmod.upcoming(self._bot.db.get("events", []))

    def open_folder(self, which="templates"):
        p = cfgmod.TEMPLATES if which == "templates" else cfgmod.DATA
        if sys.platform == "win32":
            os.startfile(p)  # noqa

    def open_url(self, url):
        webbrowser.open(url)

    def open_game(self):
        """Abre el launcher/juego de Helbreath Abaddon."""
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Helbreath Abaddon")
        for p in (os.path.join(base, "HelbreathAbaddon.exe"), os.path.join(base, "game", "Helbreath.exe")):
            if os.path.exists(p):
                os.startfile(p)  # noqa
                return True
        return False

    def toggle_mini(self):
        global MINI
        if MINI is not None:
            try:
                MINI.destroy()
            except Exception:
                pass
            MINI = None
            return False
        import webview
        MINI = webview.create_window("HB Bot mini", os.path.join(BASE, "ui", "mini.html"), js_api=self,
                                     width=360, height=250, x=20, y=60, on_top=True, frameless=True,
                                     easy_drag=True, focus=False, background_color="#0d0b10")
        return True


def main():
    import webview

    bot = Bot()
    api = Api(bot)
    win = webview.create_window(
        "HB Abaddon Bot", os.path.join(BASE, "ui", "index.html"), js_api=api,
        width=1280, height=820, min_size=(1040, 680), background_color="#0d0b10",
    )

    def on_closed():
        bot.shutdown()
        _beat(closed=True)

    win.events.closed += on_closed

    def restart():
        import subprocess
        try:
            exe = sys.executable
            if exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "pythonw.exe"):
                exe = exe[:-10] + "pythonw.exe"
            flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0  # DETACHED | NEW_GROUP
            subprocess.Popen([exe, os.path.join(BASE, "app.py")], cwd=BASE, creationflags=flags, close_fds=True)
        finally:
            os._exit(0)

    bot.on_restart = restart

    # latido para el vigía (vigia.py): cada 10 s; si el bot se cae sin cerrarse a mano, el vigía lo vuelve a abrir
    import json as _json
    import threading as _thr
    import time as _time

    def _beat(closed=False):
        try:
            with open(os.path.join(BASE, "data", "latido.json"), "w", encoding="utf-8") as f:
                _json.dump({"t": _time.time(), "pid": os.getpid(), "cerrado": bool(closed)}, f)
        except Exception:
            pass

    def _beat_loop():
        while True:
            _beat()
            _time.sleep(10)
    _thr.Thread(target=_beat_loop, daemon=True).start()
    try:
        import subprocess
        exe = sys.executable
        if exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "pythonw.exe"):
            exe = exe[:-10] + "pythonw.exe"
        if sys.platform == "win32" and os.path.exists(os.path.join(BASE, "vigia.py")):
            subprocess.Popen([exe, os.path.join(BASE, "vigia.py")], cwd=BASE, creationflags=0x00000008 | 0x00000200, close_fds=True)
    except Exception:
        pass
    webview.start(debug=False)
    bot.shutdown()


if __name__ == "__main__":
    main()
