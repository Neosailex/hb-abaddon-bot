"""Lecturas de pantalla que ya andan: cartel de exp cada 10 s (con los errores típicos del lector)."""
from _base import mk_bot, T

t = T("lecturas")
casos = [("EXP obtenida en los últimos 10 segundos: +6.525", 6525),
         ("EXP obtenida en los ultimos ICl segundos: +4.3SCl", 4350),
         ("EXP obtenida en los últimos 10 segundos: +1Cl.B7S", 10875),
         ("EXP obtenida en los últimos 10 segundos: +4,35", None),   # cortado: no lo cuento
         ("Tu SP aumentó 21 puntos", None)]
for i, (txt, want) in enumerate(casos):
    b = mk_bot("manual")
    b._in_town = lambda: True
    b._exp_msg(txt, 1000.0 + i * 100)
    lg = getattr(b, "_exp_log", [])
    got = lg[-1][1] if lg else None
    t.ok(got == want, f"'{txt[-22:]}' → {want}")
b = mk_bot("manual"); b._in_town = lambda: True
b._exp_msg("EXP obtenida en los últimos 10 segundos: +6.525", 1000.0)
b._exp_msg("EXP obtenida en los últimos 10 segundos: +6.525", 1005.0)
t.ok(len(b._exp_log) == 1, "el mismo cartel en pantalla no se cuenta dos veces")
t.end()
