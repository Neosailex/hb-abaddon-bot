"""Todo el código compila y se importa; las funciones que existían siguen existiendo."""
import os
import py_compile
from _base import ROOT, E, T

t = T("compila")
bad = []
for d in ("bot", "."):
    for f in os.listdir(os.path.join(ROOT, d)):
        if f.endswith(".py"):
            try:
                py_compile.compile(os.path.join(ROOT, d, f), doraise=True)
            except Exception as e:
                bad.append(f"{f}: {e}")
t.ok(not bad, "todo compila" + (f" ({bad})" if bad else ""))
must = ["start", "stop", "set_mode", "bot_mode", "_fresh_start", "_fresh_skip", "_town_need", "_hit_pace", "_is_danger",
        "_oly_ents", "ghost_filter", "_oly_repair_trip", "_oly_shop_trip", "_npc_menu", "_npc_find", "_place",
        "_exp_msg", "_ctrl_exp", "_pits_scan", "_travel", "snapshot", "apply_update", "update_pending", "_run_cmd"]
miss = [m for m in must if not hasattr(E.Bot, m)]
t.ok(not miss, "están todas las funciones clave" + (f" (faltan {miss})" if miss else ""))
for m in E.Bot.MODES:
    t.ok(m in ("auto", "caza", "manual", "quest", "economico", "cartografo", "investigar"), f"modo {m} conocido")
t.end()
