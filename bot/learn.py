"""Aprendizaje del bot: aprende de sus muertes, prueba variantes de su forma de jugar y se queda con la que
rinde más, guarda lo que vale para cualquier server (cerebro común, en términos relativos) y arma un informe.

Archivos:
  data/cerebro.json                       lo que sirve en cualquier server de Helbreath
  <carpeta del server>/aprendizaje.json   experimentos, muertes y reglas de ese server
  <carpeta del server>/informe.md         resumen legible de lo que aprendió
"""
import json
import os
import random
import time

from . import config as cfgmod

# parámetros que el bot prueba solo; cada uno con sus valores posibles (el primero es el de arranque)
KNOBS = {
    "panic_hp_pct": [30, 22, 40],      # con cuánta vida huye / toma poción de emergencia
    "rest_below_pct": [50, 40, 60],    # debajo de cuánto descansa si nadie le pega
    "rest_until_pct": [75, 65, 90],    # hasta cuánto descansa
}
WINDOW_SEC = 15 * 60                   # cada prueba dura 15 minutos cazando
DEATH_PENALTY = 25.0                   # una muerte cuenta como perder 25% de un nivel (exp perdida + tiempo)


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class Learner:
    def __init__(self, bot):
        self.bot = bot
        self.brain_path = os.path.join(cfgmod.DATA, "cerebro.json")
        self.brain = _load(self.brain_path, {"nest": [], "lessons": [], "knobs": {}})
        self._prof = None
        self.st = None
        self._win = None
        self._last_report = time.time()

    # ------------------------------------------------------------ archivos
    def _profile(self):
        d = cfgmod.profile_dir(self.bot.cfg)
        if d != self._prof:
            self._prof = d
            self.st = _load(os.path.join(d, "aprendizaje.json"),
                            {"deaths": [], "rules": [], "knobs": {}, "arms": {}, "prices": {}, "log": []})
            self._win = None
            # server nuevo: arranco cada parámetro con el mejor valor que encontré en otros servers
            for k, v in self.brain.get("knobs", {}).items():
                self.st["knobs"].setdefault(k, v)
        return d

    def save(self):
        d = self._profile()
        _save(os.path.join(d, "aprendizaje.json"), self.st)
        _save(self.brain_path, self.brain)

    def knob(self, name, default):
        self._profile()
        if not self.bot.cfg.get("learn_knobs", False):
            return default  # las pruebas de ajustes quedaron apagadas: no mostraron mejoras medibles
        return self.st["knobs"].get(name, default)

    # ------------------------------------------------------------ experimentos
    def _progress(self):
        p = self.bot.progress
        return p.get("levelups", 0) * 100.0 + (p.get("exp") or 0.0)

    def _exp_abs(self):
        """Experiencia total (número), si el bot la sabe (memoria o ficha)."""
        try:
            return self.bot._exp_mem()
        except Exception:
            return None

    def tick(self):
        """Llamado en cada vuelta. Mide la ventana de prueba actual y, al cerrarla, elige la siguiente."""
        b = self.bot
        if not b.running or not b.cfg.get("learn_knobs", False):
            return
        self._profile()
        now = time.time()
        hunting = b.state in ("attack", "loot", "lootq") or (b.state == "seek" and not b._is_travelling())
        w = self._win
        if w is None:
            self._start_window(now)
            return
        dt = min(2.0, max(0.0, now - w["t_last"]))
        w["t_last"] = now
        if hunting:
            w["hunt"] += dt
        if w["hunt"] >= WINDOW_SEC:
            self._close_window(now)
        if now - self._last_report > 6 * 3600:
            self._last_report = now
            self.report()

    def _start_window(self, now):
        st = self.st
        name = random.choice(list(KNOBS))
        arms = st["arms"].setdefault(name, {})
        vals = KNOBS[name]
        # ε-greedy: 25% de las veces pruebo algo distinto; si no, el mejor medido hasta ahora
        untested = [v for v in vals if str(v) not in arms]
        if untested and random.random() < 0.5:
            v = untested[0]
        elif arms and random.random() > 0.25:
            v = int(max(arms, key=lambda k: arms[k]["score"] / max(1, arms[k]["n"])))
        else:
            v = random.choice(vals)
        self._win = {"knob": name, "val": v, "t0": now, "t_last": now, "hunt": 0.0,
                     "p0": self._progress(), "e0": self._exp_abs(), "deaths": 0, "prev": self.knob(name, vals[0])}
        st["knobs"][name] = v

    def _close_window(self, now):
        w = self._win
        e1 = self._exp_abs()
        if w.get("e0") is not None and e1 is not None and e1 == w["e0"] and w["hunt"] < WINDOW_SEC * 1.6:
            return  # la exp se lee de a ratos (ficha): espero a la próxima lectura para medir bien
        self._win = None
        hours = w["hunt"] / 3600.0
        if w.get("e0") is not None and e1 is not None and e1 >= w["e0"]:
            # mido en experiencia real por hora; cada muerte descuenta el rato que cuesta (volver, o la exp perdida)
            gained = float(e1 - w["e0"])
            lost_sec = min(w["hunt"] * 0.9, w["deaths"] * self.death_cost() * 24.0)   # 2% -> 48 s, 25% -> 10 min
            per_h = gained / hours * (1.0 - lost_sec / max(1.0, w["hunt"]))
            unit = "exp por hora"
            arms = self.st["arms"].setdefault(w["knob"], {})
            if arms and not self.st.get("arms_unit") == "exp":
                self.st["arms"] = {}  # las mediciones viejas estaban en otra unidad: empiezo de cero
            self.st["arms_unit"] = "exp"
        else:
            gained = self._progress() - w["p0"]
            per_h = (gained - self.death_cost() * w["deaths"]) / hours
            unit = "% de nivel por hora"
        arms = self.st["arms"].setdefault(w["knob"], {})
        a = arms.setdefault(str(w["val"]), {"n": 0, "score": 0.0})
        if a["n"] >= 4:  # lo viejo pesa menos (cambio de nivel, de zona, de equipo)
            a["score"], a["n"] = a["score"] / a["n"] * 3, 3
        a["n"] += 1
        a["score"] += per_h
        # dejo puesto el mejor valor medido (el próximo experimento puede cambiarlo un rato)
        best = max(arms, key=lambda k: arms[k]["score"] / max(1, arms[k]["n"]))
        self.st["knobs"][w["knob"]] = int(best)
        self.brain.setdefault("knobs", {})[w["knob"]] = int(best)
        msg = (f"🧪 Prueba {w['knob']}={w['val']}: {per_h:,.0f} {unit}".replace(",", ".")
               + f" ({w['deaths']} muerte/s). Mejor hasta ahora: {best}.")
        self.bot.log(msg)
        self._note(msg)
        self.save()

    def death_cost(self):
        """Cuánto cuesta morir acá, en % de nivel: en una zona de principiantes no se pierde exp (solo el rato
        de volver); en el resto, exp perdida + tiempo."""
        mp = (self.bot.pos.get("map") or "").lower()
        safe = [x.lower() for x in (self.bot.cfg.get("free_death_maps") or ["beginner"])]
        return 2.0 if any(k in mp for k in safe) else DEATH_PENALTY

    # ------------------------------------------------------------ muertes
    def on_death(self):
        b = self.bot
        self._profile()
        if self._win:
            self._win["deaths"] += 1
        px, py = b.pos.get("x"), b.pos.get("y")
        mobs = {}
        for e in (getattr(b, "_oly_last", None) or []):
            if px is not None and max(abs(e[0] - px), abs(e[1] - py)) <= 3:
                mobs[e[2]["name"]] = mobs.get(e[2]["name"], 0) + 1
        lvl = int(b.cfg.get("travel", {}).get("my_level", 1) or 1)
        d = {"t": int(time.time()), "map": b.pos.get("map"), "x": px, "y": py, "lvl": lvl,
             "mobs": mobs, "hp_pots": (getattr(b, "_left", {}) or {}).get("pot")}
        st = self.st
        st["deaths"].append(d)
        del st["deaths"][:-200]
        # regla: dos muertes con el mismo bicho y la misma cantidad encima, en este nivel → no acepto tantos
        if mobs:
            mob, n = max(mobs.items(), key=lambda kv: kv[1])
            same = [x for x in st["deaths"][-10:] if x.get("lvl", 0) >= lvl - 1 and x.get("mobs", {}).get(mob, 0) >= n]
            if len(same) >= 2 and n >= 2:
                # si me mataron con 17 encima, bajar a 16 no arregla nada: la mitad (mínimo 2)
                r = {"mob": mob, "max": max(2, n // 2), "until_lvl": lvl + 2}
                st["rules"] = [x for x in st["rules"] if x["mob"] != mob] + [r]
                msg = f"📕 Aprendí: con {n} {mob} encima me matan a nivel {lvl}. Hasta nivel {lvl + 2} no me meto con más de {max(2, n // 2)}."
                b.log(msg)
                self._note(msg)
                self.brain.setdefault("lessons", []).append({"mob": mob, "crowd": n, "lvl": lvl,
                                                             "ratio": self.ratios(mob)})
                del self.brain["lessons"][:-100]
        self.save()

    def crowd_cap(self, mob, lvl):
        """Tope de vecinos aprendido de las muertes (None = sin regla)."""
        self._profile()
        caps = [r["max"] for r in self.st.get("rules", []) if r["mob"] == mob and lvl < r["until_lvl"]]
        return min(caps) if caps else None

    # ------------------------------------------------------------ cerebro común (relativo)
    def ratios(self, mob):
        """Qué tan fuerte soy frente a ese bicho, en números que valen en cualquier server:
        defensa / puntería del bicho y vida / daño del bicho."""
        b = self.bot
        cr = next((c for c in b._creatures_db().values() if c.get("name") == mob), None)
        sv = getattr(b, "_stats_vals", None) or {}
        dex = sv.get("DEX") or 15
        defense = 2 * dex + int(b.cfg.get("armor_defense", 0))
        hit = (cr or {}).get("hit") or 30
        dmg = (cr or {}).get("dmg_avg") or 3
        hpmax = b._hpmax_est() or 40
        return {"def_hit": round(defense / max(1, hit), 2), "hp_dmg": round(hpmax / max(0.5, dmg), 1)}

    def note_nest(self, allow, mob):
        """Me animé a 'allow' vecinos con estos números: lo guardo para otros servers."""
        r = dict(self.ratios(mob), allow=allow, mob=mob)
        self.brain.setdefault("nest", []).append(r)
        del self.brain["nest"][:-200]
        _save(self.brain_path, self.brain)

    def nest_prior(self, mob):
        """Server nuevo: cuántos vecinos aguanté antes con números iguales o peores que los de ahora."""
        cur = self.ratios(mob)
        ok = [r["allow"] for r in self.brain.get("nest", [])
              if r["def_hit"] <= cur["def_hit"] and r["hp_dmg"] <= cur["hp_dmg"]]
        return max(ok) if ok else 0

    # ------------------------------------------------------------ ficha del server
    def note_prices(self, prices):
        self._profile()
        self.st.setdefault("prices", {}).update(prices)
        self.save()

    def note(self, msg):
        self._profile()
        self._note(msg)
        self.save()

    def _note(self, msg):
        lg = self.st.setdefault("log", [])
        lg.append(time.strftime("%d/%m %H:%M ") + msg)
        del lg[:-300]

    # ------------------------------------------------------------ informe
    def report(self):
        d = self._profile()
        st = self.st
        lines = ["# Lo que aprendió el bot", "", f"Server: {cfgmod.server_slug(self.bot.cfg)} · {time.strftime('%d/%m/%Y %H:%M')}", ""]
        lines.append("## Forma de jugar (lo mejor medido)")
        for k, arms in st.get("arms", {}).items():
            rows = sorted(arms.items(), key=lambda kv: -kv[1]["score"] / max(1, kv[1]["n"]))
            lines.append(f"- **{k}**: " + ", ".join(f"{v} → {a['score'] / max(1, a['n']):,.0f}/h ({a['n']} prueba/s)" for v, a in rows))
        lines += ["", "## Reglas aprendidas de las muertes"]
        lines += [f"- No más de {r['max']} {r['mob']} encima hasta nivel {r['until_lvl']}." for r in st.get("rules", [])] or ["- (ninguna todavía)"]
        dd = st.get("deaths", [])[-20:]
        lines += ["", f"## Últimas muertes ({len(st.get('deaths', []))} en total)"]
        lines += [f"- {time.strftime('%d/%m %H:%M', time.localtime(x['t']))} {x['map']} ({x['x']},{x['y']}) nivel {x['lvl']}: "
                  + (", ".join(f"{n} {m}" for m, n in x['mobs'].items()) or "sin bichos cerca") for x in dd]
        if st.get("prices"):
            lines += ["", "## Precios vistos"] + [f"- {k}: {v}" for k, v in sorted(st["prices"].items())]
        lines += ["", "## Bitácora"] + [f"- {x}" for x in st.get("log", [])[-40:]]
        with open(os.path.join(d, "informe.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        n_rules = len(st.get("rules", []))
        best = {k: self.st["knobs"].get(k) for k in KNOBS}
        self.bot.log(f"📒 Informe: {n_rules} regla/s por muertes; mejores ajustes {best}. Detalle en informe.md.")
