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


def gear_plan(cat, level, stats, owned):
    """Por cada lugar del cuerpo: qué tengo y cuál es la próxima mejora que ya puedo usar."""
    owned = set(owned or [])
    out = []
    for slot in cat.get("slot_priority", []):
        ladder = cat.get("ladder", {}).get(slot, [])
        have = max((i for i, n in enumerate(ladder) if n in owned), default=-1)
        nxt = None
        blocked = None
        for i in range(len(ladder) - 1, have, -1):  # la mejor que pueda usar hoy
            it = _item(cat, ladder[i])
            if it is None:
                continue
            if _can_use(it, level, stats):
                nxt = it
                break
            blocked = it
        out.append({"slot": slot, "have": ladder[have] if have >= 0 else None, "next": nxt, "blocked": blocked})
    return out


def plan(db, path, cat, level, stats, gold=None, owned=None, reachable=None, current_map=None):
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
    gp = gear_plan(cat, level, stats, owned)
    reserve = cat.get("reserve_gold", 0)
    wants = [g for g in gp if g["next"]]
    # primero lo que falta del todo (lugar vacío), después las mejoras; dentro de eso, lo más barato
    wants.sort(key=lambda g: (g["have"] is not None, g["next"]["price"]))
    buy_now, saving = [], None
    if gold is not None:
        left = gold - reserve
        for g in wants:
            it = g["next"]
            if it["price"] <= left and "scroll" in it:
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
            lines.append(f"Nivel {level}: me tocaría {im} ({', '.join(ideal['monsters'][:3])}), pero no sé llegar. Cazo en {hunt_map or current_map or '?'} mientras tanto.")
    for it in buy_now:
        lines.append(f"Compro {it['name']} ({it['price']} de oro) en el {it['shop']}.")
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


def avoid_maps(db, level, owned, cat, cfg=None):
    """Mapas a los que todavía no conviene ir. No hay mapas prohibidos: cada uno se abre cuando el pj
    tiene el nivel y el equipo para estar cómodo (los de PvP y los del otro reino piden más)."""
    cfg = cfg or {}
    level = int(level or 1)
    slots = cat.get("ladder", {})
    worn = sum(1 for names in slots.values() if any(n in (owned or []) for n in names))
    full = worn >= min(5, len(slots))
    out = set()
    for m in db.get("maps", []):
        name = m["name"].lower()
        if level < _min_level(m.get("level", "")) + cfg.get("margin", 0):
            out.add(name)
    if not (level >= cfg.get("pvp_level", 60) and full):
        out.update(PVP)
    if not (level >= cfg.get("enemy_level", 100) and full):
        out.update(ENEMY)
    return sorted(out)
