"""Actualizaciones bajo control (regla 1): nada se aplica solo; solo con 'Aplicar'."""
import importlib.util
import io
import json
import os
import shutil
import tempfile
import zipfile
from _base import ROOT, T

t = T("actualizar")
tmp = tempfile.mkdtemp(prefix="hbtest_")
try:
    base = os.path.join(tmp, "bot")
    os.makedirs(os.path.join(base, "bot"))
    os.makedirs(os.path.join(base, "data"))
    json.dump({"version": 490}, open(os.path.join(base, "version.json"), "w"))
    open(os.path.join(base, "bot", "engine.py"), "w").write("VIEJO")
    open(os.path.join(base, "data", "config.json"), "w").write("MI CONFIG")
    shutil.copy(os.path.join(ROOT, "actualizar.py"), base)
    sp = importlib.util.spec_from_file_location("act_t", os.path.join(base, "actualizar.py"))
    act = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(act)

    # GitHub de mentira con la v492
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("repo-main/version.json", json.dumps({"version": 492, "notes": "prueba"}))
        z.writestr("repo-main/bot/engine.py", "NUEVO")
        z.writestr("repo-main/data/config.json", "PISADA")
    act.remote_info = lambda: {"version": 492, "notes": "prueba"}
    act._get = lambda url, timeout=8: buf.getvalue()

    r = act.check_and_update(lambda m: None)
    t.ok(r is False, "buscar actualización no aplica nada")
    t.ok(open(os.path.join(base, "bot", "engine.py")).read() == "VIEJO", "el bot que corre NO cambió")
    p = act.pending()
    t.ok(p and p["version"] == 492, "queda pendiente la v492")
    t.ok(os.path.isfile(os.path.join(base, "actualizaciones", "v492", "bot", "engine.py")), "bajada aparte en actualizaciones/v492")
    t.ok(not os.path.exists(os.path.join(base, "actualizaciones", "v492", "data", "config.json")), "la config de Juan no viene en la actualización")
    act.check_and_update(lambda m: None)
    t.ok(act.pending()["version"] == 492, "buscar de nuevo no la duplica ni la aplica")

    ch = act.apply_staged(lambda m: None)
    t.ok(ch is not None and "bot/engine.py" in ch, "Aplicar copia los archivos")
    t.ok(open(os.path.join(base, "bot", "engine.py")).read() == "NUEVO", "el bot quedó con la versión nueva")
    t.ok(open(os.path.join(base, "data", "config.json")).read() == "MI CONFIG", "la config de Juan intacta")
    t.ok(act.local_version() == 492, "versión local = 492")
    t.ok(act.pending() is None, "ya no queda pendiente")
    t.ok("v492" in open(os.path.join(base, "actualizaciones", "aplicadas.log")).read(), "queda anotada en el historial")
    t.ok(act.apply_staged(lambda m: None) is None, "Aplicar sin nada pendiente no hace nada")

    # sin internet: no rompe
    def boom(*a, **k):
        raise OSError("sin red")
    act.remote_info = boom
    t.ok(act.check_and_update(lambda m: None) is False, "sin internet: sigue sin error")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
t.end()
