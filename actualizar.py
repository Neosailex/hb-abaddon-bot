"""Auto-actualización del bot desde GitHub. Solo usa la librería estándar.
- Compara version.json local con el del repo; si hay una más nueva, baja el zip y copia los archivos encima.
- Nunca toca lo tuyo: configuración, contraseña cifrada, lo aprendido, registros, plantillas ni el entorno (.venv).
- Si no hay internet o algo falla, no hace nada y el bot arranca igual.
Uso: python actualizar.py   (lo llama iniciar.bat; el bot también lo corre cada tanto mientras está abierto)"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

REPO = "Neosailex/hb-abaddon-bot"
BRANCH = "main"
BASE = os.path.dirname(os.path.abspath(__file__))
# Lo que es del usuario: no se pisa aunque exista en el repo
KEEP = ("data/config.json", "data/mundo.json", "data/pj.json", "data/progreso.json", "data/resume.json",
        "data/bot.log", "data/control.json", "data/control_done.json", "data/investigacion.zip")
KEEP_DIRS = ("data/debug/", "data/investigacion/", "data/plantillas/", ".venv/", ".git/")


def local_version():
    try:
        with open(os.path.join(BASE, "version.json"), encoding="utf-8") as f:
            return int(json.load(f).get("version", 0))
    except Exception:
        return 0


def _get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "hb-abaddon-bot-updater", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def remote_info():
    return json.loads(_get(f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/version.json").decode("utf-8"))


def _protected(rel):
    rel = rel.replace("\\", "/")
    return rel in KEEP or any(rel.startswith(d) for d in KEEP_DIRS) or "__pycache__" in rel or ".." in rel.split("/")


def apply_update(log=print):
    """Baja el repo y copia encima. Devuelve la lista de archivos cambiados."""
    data = _get(f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip", timeout=60)
    changed = []
    reqs_changed = False
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        root = names[0].split("/")[0] + "/" if names else ""
        tmp = tempfile.mkdtemp(prefix="hbbot_upd_")
        try:
            for n in names:
                rel = n[len(root):]
                if not rel or _protected(rel):
                    continue
                new = z.read(n)
                dst = os.path.join(BASE, *rel.split("/"))
                old = None
                if os.path.isfile(dst):
                    with open(dst, "rb") as f:
                        old = f.read()
                if old == new:
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                t = os.path.join(tmp, "f")
                with open(t, "wb") as f:
                    f.write(new)
                shutil.move(t, dst)
                changed.append(rel)
                if rel == "requirements.txt":
                    reqs_changed = True
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if reqs_changed:
        py = os.path.join(BASE, ".venv", "Scripts", "python.exe")
        if os.path.isfile(py):
            log("Instalando librerías nuevas...")
            subprocess.call([py, "-m", "pip", "install", "-q", "-r", os.path.join(BASE, "requirements.txt")])
    return changed


def check_and_update(log=print):
    """True si actualizó. No tira errores: ante cualquier problema devuelve False."""
    try:
        lv = local_version()
        info = remote_info()
        rv = int(info.get("version", 0))
        if rv <= lv:
            return False
        log(f"Hay una versión nueva del bot ({lv} -> {rv}): {info.get('notes', '')}")
        ch = apply_update(log)
        log(f"Actualizado a la versión {rv} ({len(ch)} archivos).")
        return True
    except Exception as e:
        log(f"No pude buscar actualizaciones ({e.__class__.__name__}). Sigo con la versión que tengo.")
        return False


if __name__ == "__main__":
    check_and_update()
