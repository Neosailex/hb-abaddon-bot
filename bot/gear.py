"""Equipo: mira lo que hay en el bolso (lo comprado y lo que cae de los bichos), lo compara con lo puesto
y decide qué ponerse, qué guardar y qué vender. Lógica pura: el motor le pasa los datos leídos del juego."""

# atributo principal de un item raro (byte de tipo en el objeto del item)
ATTR = {1: "Critical", 2: "Poisoning", 3: "Righteous", 5: "Agile", 6: "Light", 7: "Sharp", 8: "Strong",
        9: "Ancient", 10: "Special", 11: "Mana Converting", 12: "Critical"}
# cuánto mejora un arma cada atributo (aprox.: sirve para comparar, no es la cuenta exacta del server)
W_BONUS = {7: 0.10, 9: 0.20, 2: 0.05, 3: 0.03, 1: 0.05, 5: 0.05, 12: 0.05}
# posiciones del cuerpo que el bot sabe leer si están puestas
KNOWN_POS = (1, 2, 3, 4, 5, 7, 8, 9, 12, 13)
WEAPON_POS = (8, 9)


def is_special(attr):
    return bool(attr) and (attr[0] != 0 or attr[2] != 0)


def label(name, attr):
    if not is_special(attr):
        return name
    pre = ATTR.get(attr[0])
    return f"{pre} {name}" if pre else f"{name} (raro)"


def bonus(it, attr):
    """Multiplicador por atributos raros."""
    if not is_special(attr) or not it:
        return 1.0
    if it.get("pos") in WEAPON_POS:
        b = W_BONUS.get(attr[0], 0.0)
        if attr[0] in (7, 9, 2):
            b += 0.01 * attr[1]
        if attr[2] in (2, 10):  # más chance de pegar / daño en ataques seguidos
            b += 0.02 * attr[3]
        return 1.0 + b
    b = 0.0
    if attr[2] in (3, 8):  # defensa / absorción física
        b += 0.03 * attr[3]
    return 1.0 + b


def score(it, attr=None):
    if not it:
        return 0.0
    base = it.get("avg", 0) if it.get("pos") in WEAPON_POS else it.get("def", 0)
    return float(base) * bonus(it, attr)


def can_use(it, level, stats):
    return bool(it) and level >= it.get("level", 0) and (stats or {}).get("STR", 0) >= it.get("str", 0)


def owned_bonus(inv, idb):
    """nombre -> mejor multiplicador entre los que tengo (para que el plan de compras cuente los raros)."""
    out = {}
    for i in inv or []:
        it = idb.get(i["id"])
        if it:
            out[it["name"]] = max(out.get(it["name"], 1.0), bonus(it, i.get("attr")))
    return out


def decide(inv, eq_ids, idb, cat, level, stats, keep_names=(), junk_names=(), max_rare=8):
    """inv: items del bolso (con slot, id, attr). eq_ids: ids de lo que tiene puesto.
    Devuelve {"equip": [...], "sell": [...], "keep": [...]}; cada entrada lleva slot, name y why."""
    wn = set(cat.get("weapon_names", []))
    two_handed = cat.get("weapon_slot") == "arma2m"
    keep_names, junk_names = set(keep_names), set(junk_names)
    # qué item del bolso es el que está puesto (uno por id)
    left = list(eq_ids or [])
    equipped = {}
    for i in inv or []:
        if i["id"] in left:
            left.remove(i["id"])
            equipped[i["slot"]] = i
    cur = {}  # lugar del cuerpo -> puntaje de lo puesto
    for i in equipped.values():
        it = idb.get(i["id"])
        if it:
            g = "arma" if it.get("pos") in WEAPON_POS else it.get("pos")
            cur[g] = max(cur.get(g, 0.0), score(it, i.get("attr")))
    out = {"equip": [], "sell": [], "keep": []}
    best = {}
    rest = []
    for i in inv or []:
        if i["slot"] in equipped:
            continue
        it = idb.get(i["id"])
        if not it:
            continue
        name = label(it["name"], i.get("attr"))
        e = {"slot": i["slot"], "id": i["id"], "name": name}
        if it.get("type") != 2 or it.get("pos") not in KNOWN_POS:
            if it["name"] in junk_names:
                out["sell"].append(dict(e, why="no lo uso"))
            continue
        pos = it["pos"]
        g = "arma" if pos in WEAPON_POS else pos
        for_build = (it["name"] in wn) if pos in WEAPON_POS else not (pos == 7 and two_handed) and "(W)" not in it["name"]
        s = score(it, i.get("attr"))
        if for_build and s > cur.get(g, 0.0):
            if i.get("max", 0) >= 100 and i.get("dur", 1) <= 0:
                out["keep"].append(dict(e, why="está roto: hay que repararlo"))
                continue
            if can_use(it, level, stats):
                if g not in best or s > best[g][0]:
                    if g in best:
                        rest.append((best[g][1], best[g][2], True))
                    best[g] = (s, e, it)
                    continue
                rest.append((e, it, True))
                continue
            out["keep"].append(dict(e, why=f"es mejor que lo puesto, todavía no lo puedo usar (pide STR {it.get('str', 0)}, nivel {it.get('level', 0)})"))
            continue
        rest.append((e, it, False))
    for g, (s, e, it) in best.items():
        out["equip"].append(dict(e, why=f"mejor que lo puesto ({round(s, 1)} contra {round(cur.get(g, 0.0), 1)})"))
    attr_of = {i["slot"]: i.get("attr") for i in inv or []}
    rare = []
    for e, it, _ in rest:
        if it["name"] in keep_names:
            out["keep"].append(dict(e, why="guardado por pedido"))
        elif is_special(attr_of.get(e["slot"])):
            rare.append((it.get("price", 0), e))
        else:
            out["sell"].append(dict(e, why="no sirve para la build o es peor que lo puesto"))
    # los raros que no uso los guardo (venderlos no tiene vuelta), salvo que ya junte demasiados
    room = max(0, max_rare - len(out["keep"]))
    rare.sort(key=lambda t: -t[0])
    for n, (_, e) in enumerate(rare):
        if n < room:
            out["keep"].append(dict(e, why="raro: lo guardo aunque no sea para la build"))
        else:
            out["sell"].append(dict(e, why="raro que no uso y ya no tengo lugar para guardar"))
    return out
