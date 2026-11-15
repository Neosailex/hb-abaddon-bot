"""Datos que trae el propio cliente del juego (carpeta 'contents' de Olympia y clientes de la misma familia):
objetos (items.dat), misiones (quests.json) y personajes (persons.json). Con esto el bot no necesita que nadie le
pase la wiki: precios, requisitos, misiones y dónde está cada NPC salen de la instalación. Solo lee archivos."""
import json
import os
import struct

SLOTS = {1: "cabeza", 2: "pecho", 3: "brazos", 4: "piernas", 5: "pies", 6: "cuello", 7: "escudo", 8: "arma",
         9: "arma2m", 10: "anillo", 12: "capa"}
FAMILY = {1: "espada corta", 2: "espada larga", 3: "esgrima", 4: "hacha", 5: "martillo", 6: "arco", 7: "escudo",
          8: "baston", 9: "varita"}


def contents_dir(game_dir):
    for n in ("contents", "CONTENTS", "Contents"):
        d = os.path.join(game_dir or "", n)
        if os.path.isdir(d):
            return d
    return None


def load_items(game_dir):
    """{id: {...}} desde items.dat. Formato: cabecera de 16 bytes (cantidad en el byte 8), y por objeto: id (u16),
    nombre, efecto (dos textos con largo u16 adelante) y 56 bytes fijos. El precio de tienda es el 90% del de la tabla."""
    cd = contents_dir(game_dir)
    if not cd:
        return {}
    try:
        d = open(os.path.join(cd, "items.dat"), "rb").read()
        n = struct.unpack_from("<I", d, 8)[0]
        off, out = 16, {}
        for _ in range(n):
            iid, ln = struct.unpack_from("<HH", d, off)
            off += 4
            name = d[off:off + ln].decode("latin1")
            off += ln
            l2 = struct.unpack_from("<H", d, off)[0]
            off += 2
            eff = d[off:off + l2].decode("latin1")
            off += l2
            r = d[off:off + 56]
            off += 56
            if len(r) < 56:
                break
            u16 = lambda o: struct.unpack_from("<H", r, o)[0]
            it = {"id": iid, "name": name, "effect": eff, "type": r[0], "pos": r[1], "slot": SLOTS.get(r[1]),
                  "str": u16(3) // 100 if r[0] == 1 else 0, "price": int(round(struct.unpack_from("<I", r, 10)[0] * 0.9)),
                  "v1": u16(18), "v2": u16(20), "v3": u16(22), "family": FAMILY.get(r[32]), "dur": u16(35), "level": u16(37)}
            if r[0] == 1 and r[1] in (8, 9):
                it["dmg"] = it["v2"]
            elif r[0] == 1:
                it["def"] = it["v2"]
                if r[1] == 7:
                    it["pa"] = it["v3"]
            out[iid] = it
        return out
    except Exception:
        return {}


def load_persons(game_dir):
    cd = contents_dir(game_dir)
    try:
        return json.load(open(os.path.join(cd, "persons.json"), encoding="utf-8"))
    except Exception:
        return []


def load_quests(game_dir):
    cd = contents_dir(game_dir)
    try:
        return json.load(open(os.path.join(cd, "quests.json"), encoding="utf-8"))
    except Exception:
        return []


def quest_db(game_dir, nation, display, mob_name):
    """Base de misiones en el formato del bot. nation: 'aresden'/'elvine'/None; display(archivo)->nombre de mapa;
    mob_name(nombre corto del juego)->nombre del bicho en la base de criaturas."""
    qs, ps, items = load_quests(game_dir), load_persons(game_dir), load_items(game_dir)
    if not qs or not ps:
        return None
    pid = {p["id"]: p for p in ps}
    other = "elvine" if nation == "aresden" else ("aresden" if nation == "elvine" else None)
    npcs, out = {}, []
    for p in ps:
        for side, sp in (p.get("spawns") or {}).items():
            if side == other:
                continue
            npcs.setdefault(p["name"], {})[display(sp["map"])] = list(sp["pos"])
    for q in qs:
        p = pid.get(q.get("person"))
        if not p or (other and set((p.get("spawns") or {}).keys()) == {other}):
            continue  # misión del otro reino
        ob = q.get("objectives") or {}
        objs, maps, notes = [], [], []
        for k in ob.get("kill", []):
            objs.append({"mob": "*" if k["name"] == "any" else mob_name(k["name"]), "count": int(k["count"]), "elite": bool(k.get("is_elite"))})
            if k.get("map") and display(k["map"]) not in maps:
                maps.append(display(k["map"]))
        for g in ob.get("gather", []):
            nm = (items.get(g["id"]) or {}).get("name", f"objeto {g['id']}")
            notes.append(f"{g['count']} {nm}")
        if ob.get("catch"):
            notes.append("pescar")
        rw = q.get("reward") or {}
        gold = sum(int(i.get("count", 0)) for i in (rw.get("items") or []) + (rw.get("item_pick") or []) if i.get("id") == 90)
        kind = "kill" if ob.get("kill") and not notes else ("items" if not ob.get("kill") else "kill+items")
        out.append({"name": q["name"], "npc": p["name"], "maps": maps or list(npcs.get(p["name"], {}).keys()),
                    "level": int(q.get("required_level") or 1), "max_level": q.get("obsolete_level"), "objectives": objs,
                    "exp": int(rw.get("experience") or 0), "gold": gold, "repeat_h": q.get("period"),
                    "repeatable": q.get("repeatable", True), "map_only": bool(maps), "kind": kind, "nation": None,
                    "notes": ("Entregar: " + ", ".join(notes)) if notes else ""})
    return {"source": "contents/quests.json del juego", "npcs": npcs, "quests": out}
