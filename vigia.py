"""Vigía: si el bot se cae solo (sin que lo cierres vos), lo vuelve a abrir.
Lo lanza app.py al arrancar. Mira data/latido.json (el bot lo escribe cada 10 s). Si el latido tiene más de 5 minutos
y el bot no se cerró a mano ('cerrado': true), lo abre de nuevo. Si lo cerrás vos, el vigía se va también."""
import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
BEAT = os.path.join(DATA, "latido.json")
LOCK = os.path.join(DATA, "vigia.pid")


def alive(pid):
    if not pid:
        return False
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    except Exception:
        return False


def log(t):
    try:
        with open(os.path.join(DATA, "vigia.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + t + "\n")
    except Exception:
        pass


def main():
    try:
        with open(LOCK, encoding="utf-8") as f:
            if alive(int(f.read().strip() or 0)):
                return  # ya hay un vigía andando
    except Exception:
        pass
    with open(LOCK, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    log("vigía prendido")
    last_launch = 0
    while True:
        time.sleep(60)
        try:
            with open(BEAT, encoding="utf-8") as f:
                b = json.load(f)
        except Exception:
            continue
        if b.get("cerrado"):
            log("el bot se cerró a mano: me voy")
            return
        age = time.time() - float(b.get("t", 0))
        if age < 300 or time.time() - last_launch < 600:
            continue
        if alive(b.get("pid")):
            continue  # está vivo pero lento (o colgado): no abro otro encima
        log(f"el bot no late hace {int(age)} s y su proceso no está: lo abro de nuevo")
        exe = sys.executable
        if exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "pythonw.exe"):
            exe = exe[:-10] + "pythonw.exe"
        subprocess.Popen([exe, os.path.join(BASE, "app.py")], cwd=BASE, creationflags=0x00000008 | 0x00000200, close_fds=True)
        last_launch = time.time()


if __name__ == "__main__":
    main()
