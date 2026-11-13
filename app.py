"""HB Abaddon Bot — ventana principal (pywebview + UI en HTML)."""
import os
import sys
import webbrowser

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

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
            return worldmod.world_view(self._bot.world)
        except Exception as e:
            return {"maps": [], "links": [], "error": repr(e)}

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
    webview.start(debug=False)
    bot.shutdown()


if __name__ == "__main__":
    main()
