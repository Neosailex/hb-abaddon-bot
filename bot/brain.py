"""Cerebro del bot: con el nivel, los stats, el oro y lo que ya tiene puesto decide
dónde cazar, qué comprar ahora, para qué juntar oro y cuántas provisiones llevar.
Es lógica pura (no toca el juego): el motor le pasa los datos y ejecuta lo que devuelve."""


def _item(cat, name):
    for it in cat.get("items", []):
        if it["name"] == name:
            return it
    return None


def _can_use(it, level, stats):
    if it is None:
        return False
    if level < it.get("level", 0):
        return False
    return (stats or {}).get("STR", 0) >= it.get("str", 0)


def provisions(cat, level):
    for p in cat.get("provisions", []):
        if level <= p["upto"]:
            return {"pots": p["pots"], "meat": p["meat"]}
    return {"pots": 20, "meat": 10}


def _score(it):
    return it.get("avg", 0) if it.get("slot") in ("arma", "arma2m") else it.get("def", 0)


def gear_plan(cat, level, stats, owned, items_db=None):
    """Por cada lugar del cuerpo: lo mejor que tengo y la mejor mejora que venden y ya puedo usar.
    owned: nombres de lo que hay en el bolso/puesto. items_db: tabla de items del juego (para saber qué es lo mío)."""
    owned = set(owned or [])
    by_name = {i["name"]: i for i in (items_db or [])}
    for i in cat.get("items", []):
        by_name.setdefault(i["name"], i)
    wn = set(cat.get("weapon_names", []))
    out = []
    for slot in cat.get("slot_priority", []):
        mine = [by_name[n] for n in owned if n in by_name and by_name[n].get("slot") == slot and _can_use(by_name[n], level, stats)]
        have = max(mine, key=_score, default=None)
        cands = [i for i in cat.get("items", []) if i.get("slot") == slot and i.get("price", 0) > 0
                 and (slot not in ("arma", "arma2m") or i["name"] in wn) and "(W)" not in i["name"]]
        better = [i for i in cands if _score(i) > (_score(have) if have else 0)]
        usable = [i for i in better if _can_use(i, level, stats)]
        nxt = max(usable, key=lambda i: (_score(i), -i["price"]), default=None)
        blocked = None
        if not nxt and better:
            blocked = min(better, key=lambda i: (i.get("str", 0), i.get("level", 0)))
        out.append({"slot": slot, "have": have["name"] if have else None, "next": nxt, "blocked": blocked})
    return out


def plan(db, path, cat, level, stats, gold=None, owned=None, reachable=None, current_map=None, items_db=None):
    """Devuelve el plan completo.
    reachable: función nombre_de_mapa -> bool (si el bot sabe llegar). None = solo el mapa actual."""
    level = int(level or 1)
    stats = stats or {}
    phases = [ph for ph in path.get("phases", []) if ph["from"] <= level]
    ideal = phases[-1] if phases else None
    can = reachable or (lambda n: bool(current_map) and n.lower() == current_map.lower())
    hunt_map, hunt_phase = None, None
    for ph in reversed(phases):
        for name in ph["maps"]:
            n = name.split(" (")[0]
            if can(n):
                hunt_map, hunt_phase = n, ph
                break
        if hunt_map:
            break
    gp = gear_plan(cat, level, stats, owned, items_db)
    reserve = cat.get("reserve_gold", 0)
    wants = [g for g in gp if g["next"]]
    # primero lo que falta del todo (lugar vacío), después las mejoras; dentro de eso, lo más barato
    wants.sort(key=lambda g: (g["have"] is not None, g["next"]["price"]))
    buy_now, saving = [], None
    if gold is not None:
        left = gold - reserve
        for g in wants:
            it = g["next"]
            if it["price"] <= left:
                buy_now.append(it)
                left -= it["price"]
            elif saving is None:
                saving = {"item": it["name"], "price": it["price"], "missing": max(0, it["price"] - left)}
    elif wants:
        saving = {"item": wants[0]["next"]["name"], "price": wants[0]["next"]["price"], "missing": None}
    prov = provisions(cat, level)
    lines = []
    if ideal:
        im = ideal["maps"][0].split(" (")[0]
        if hunt_map and hunt_phase is ideal:
            lines.append(f"Nivel {level}: me toca {hunt_map} ({', '.join(ideal['monsters'][:3])}).")
        else:
            lines.append(f"Nivel {level}: me tocaría {im} ({', '.join(ideal['monsters'][:3])}), pero todavía es peligroso o no conozco el camino. Cazo en {hunt_map or current_map or '?'} mientras tanto.")
    if gold is not None:
        lines.append(f"Tengo {gold} de oro.")
    for it in buy_now:
        stat = f"daño medio {it['avg']}" if "avg" in it else f"defensa {it.get('def', '?')}"
        lines.append(f"Compro {it['name']} ({it['price']} de oro, {stat}) en el {it['shop']}.")
    if saving:
        if saving["missing"] is None:
            lines.append(f"Próxima compra: {saving['item']} ({saving['price']} de oro). No sé cuánto oro tengo.")
        elif saving["missing"] > 0:
            lines.append(f"Junto oro para {saving['item']}: faltan {saving['missing']}.")
    for g in gp:
        if not g["next"] and g["blocked"]:
            b = g["blocked"]
            need = f"STR {b['str']}" if stats.get("STR", 0) < b.get("str", 0) else f"nivel {b.get('level')}"
            lines.append(f"{g['slot'].capitalize()}: la mejora es {b['name']} (pide {need}{'' if b.get('confirmed') else ', a confirmar'}).")
    lines.append(f"Llevo {prov['pots']} pociones y {prov['meat']} carnes.")
    return {"level": level, "hunt_map": hunt_map, "ideal_map": ideal["maps"][0].split(" (")[0] if ideal else None,
            "monsters": (hunt_phase or ideal or {}).get("monsters", []), "gear": gp, "buy_now": buy_now,
            "saving": saving, "provisions": prov, "lines": lines}


ENEMY = ("elvine", "elvine city", "elvine farm", "eldiniel garden", "rocky highland", "death valley", "elvine dungeon")
PVP = ("middleland", "promiseland", "icebound")


def _min_level(txt):
    import re
    m = re.match(r"\s*(\d+)", txt or "")
    return int(m.group(1)) if m else 0


def map_danger(db, name):
    """Peligro del mapa = el bicho más bravo que vive ahí (1 a 5, según la base)."""
    dg = {m["name"].lower(): m.get("danger", 1) for m in db.get("monsters", [])}
    for m in db.get("maps", []):
        if m["name"].lower() == (name or "").lower():
            return max([dg.get(x.split(" (")[0].lower(), 2) for x in m.get("monsters", [])] or [1])
    return 0


def allowed_danger(level, cfg=None):
    cfg = cfg or {}
    for upto, d in cfg.get("danger_by_level", [[44, 2], [79, 3], [119, 4]]):
        if level <= upto:
            return d
    return 5


def avoid_maps(db, level, owned, cat, cfg=None):
    """Mapas a los que todavía no conviene ir. No hay mapas prohibidos: cada uno se abre cuando el pj
    tiene el nivel y el equipo para estar cómodo (los de PvP y los del otro reino piden más)."""
    cfg = cfg or {}
    level = int(level or 1)
    by_name = {i["name"]: i for i in cat.get("items", [])}
    worn = len({by_name[n].get("slot") for n in (owned or []) if n in by_name and by_name[n].get("slot")})
    full = worn >= 5
    out = set()
    for m in db.get("maps", []):
        name = m["name"].lower()
        ml = _min_level(m.get("level", ""))
        # un mapa "30+" no es para ir recién cumplidos los 30: pido unos niveles de margen
        if ml and level < ml + cfg.get("margin", 10):
            out.add(name)
    # mapas con bichos demasiado bravos para mi nivel
    lim = allowed_danger(level, cfg)
    for m in db.get("maps", []):
        if map_danger(db, m["name"]) > lim:
            out.add(m["name"].lower())
    if not (level >= cfg.get("pvp_level", 60) and full):
        out.update(PVP)
    if not (level >= cfg.get("enemy_level", 100) and full):
        out.update(ENEMY)
    return sorted(out)
