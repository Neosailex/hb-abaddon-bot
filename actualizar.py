"""Actualizaciones del bot BAJO CONTROL (regla de Juan: nada en caliente, se aplica solo cuando él dice).
- Compara version.json local con el del repo; si hay una más nueva, la BAJA a actualizaciones/vNNN y la deja
  'pendiente' (actualizaciones/pendiente.json). No toca el bot que está corriendo.
- Se aplica solo a pedido: botón 'Aplicar' del panel, orden 'apply' de la consola, o 'python actualizar.py --aplicar'.
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
import time
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


STAGE = os.path.join(BASE, "actualizaciones")
PENDING = os.path.join(STAGE, "pendiente.json")


def pending():
    """La actualización bajada que espera que Juan la aplique (o None)."""
    try:
        with open(PENDING, encoding="utf-8") as f:
            p = json.load(f)
        if int(p.get("version", 0)) <= local_version():
            return None  # ya está aplicada (o es vieja)
        if not os.path.isdir(os.path.join(STAGE, p.get("dir", ""))):
            return None
        return p
    except Exception:
        return None


def stage_update(log=print):
    """Baja el repo y guarda SOLO lo que cambió en actualizaciones/vNNN. No toca el bot. Devuelve la lista."""
    info = remote_info()
    rv = int(info.get("version", 0))
    d = os.path.join(STAGE, f"v{rv}")
    data = _get(f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip", timeout=60)
    changed = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        root = names[0].split("/")[0] + "/" if names else ""
        for n in names:
            rel = n[len(root):]
            if not rel or _protected(rel) or rel.startswith("actualizaciones/"):
                continue
            new = z.read(n)
            dst = os.path.join(BASE, *rel.split("/"))
            if os.path.isfile(dst):
                with open(dst, "rb") as f:
                    if f.read() == new:
                        continue
            out = os.path.join(d, *rel.split("/"))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(new)
            changed.append(rel)
    os.makedirs(STAGE, exist_ok=True)
    with open(PENDING, "w", encoding="utf-8") as f:
        json.dump({"version": rv, "notes": info.get("notes", ""), "dir": f"v{rv}", "files": changed,
                   "origen": "github", "t": time.strftime("%Y-%m-%d %H:%M")}, f, ensure_ascii=False, indent=1)
    return changed


def apply_staged(log=print):
    """Aplica la actualización pendiente: copia sus archivos encima del bot. Devuelve la lista (o None si no hay)."""
    p = pending()
    if not p:
        return None
    src = os.path.join(STAGE, p["dir"])
    changed, reqs_changed = [], False
    for root_, _ds, fs in os.walk(src):
        for fn in fs:
            full = os.path.join(root_, fn)
            rel = os.path.relpath(full, src).replace("\\", "/")
            if _protected(rel) or rel.startswith("actualizaciones/"):
                continue
            dst = os.path.join(BASE, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".nuevo"
            shutil.copyfile(full, tmp)
            os.replace(tmp, dst)
            changed.append(rel)
            if rel == "requirements.txt":
                reqs_changed = True
    if reqs_changed:
        py = os.path.join(BASE, ".venv", "Scripts", "python.exe")
        if os.path.isfile(py):
            log("Instalando librerías nuevas...")
            subprocess.call([py, "-m", "pip", "install", "-q", "-r", os.path.join(BASE, "requirements.txt")])
    hist = os.path.join(STAGE, "aplicadas.log")
    with open(hist, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M')} v{p['version']} ({len(changed)} archivos): {p.get('notes', '')}\n")
    try:
        os.remove(PENDING)
    except OSError:
        pass
    return changed


def apply_update(log=print):
    """(viejo) Baja el repo y copia encima. Ya no se usa solo: ver stage_update / apply_staged."""
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
    """Busca versión nueva y la BAJA sin aplicarla. Devuelve False siempre (no cambió el bot que corre).
    No tira errores: ante cualquier problema no hace nada."""
    try:
        lv = local_version()
        info = remote_info()
        rv = int(info.get("version", 0))
        if rv <= lv:
            return False
        p = pending()
        if p and int(p.get("version", 0)) >= rv:
            return False  # ya la tengo bajada, esperando
        ch = stage_update(log)
        log(f"Hay una versión nueva del bot (v{rv}) lista para aplicar ({len(ch)} archivos): {info.get('notes', '')} "
            "No la aplico sola: tocá 'Aplicar' en el panel cuando quieras.")
        return False
    except Exception as e:
        log(f"No pude buscar actualizaciones ({e.__class__.__name__}). Sigo con la versión que tengo.")
        return False


if __name__ == "__main__":
    import sys
    if "--aplicar" in sys.argv:
        ch = apply_staged()
        print("No hay ninguna actualización pendiente." if ch is None else f"Aplicada ({len(ch)} archivos).")
    else:
        check_and_update()
