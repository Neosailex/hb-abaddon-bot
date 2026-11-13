"""Horarios de eventos del server convertidos a la hora local de la PC."""
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


def _tz(name):
    try:
        return ZoneInfo(name) if ZoneInfo else None
    except Exception:
        return None


def upcoming(events, now=None, horizon_h=30):
    """Lista de próximos eventos [{name, start, end, active, danger, in_min}] ordenados."""
    local_now = (now or datetime.now().astimezone())
    out = []
    for ev in events:
        kind = ev.get("kind")
        if kind == "daily":
            tz = _tz(ev.get("tz"))
            if not tz:
                continue
            base = local_now.astimezone(tz)
            for day in (-1, 0, 1, 2):
                d = (base + timedelta(days=day)).date()
                for t in ev.get("times", []):
                    hh, mm = map(int, t.split(":"))
                    start = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).astimezone(local_now.tzinfo)
                    end = start + timedelta(minutes=ev.get("duration_min", 60))
                    if end < local_now or start > local_now + timedelta(hours=horizon_h):
                        continue
                    out.append(_row(ev, start, end, local_now))
        elif kind == "biweekly_saturday":
            tz = _tz(ev.get("tz"))
            if not tz:
                continue
            base = local_now.astimezone(tz)
            days = (5 - base.weekday()) % 7
            d = (base + timedelta(days=days)).date()
            hh, mm = map(int, ev["times"][0].split(":"))
            start = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).astimezone(local_now.tzinfo)
            end = start + timedelta(minutes=ev.get("duration_min", 360))
            if end < local_now:
                start, end = start + timedelta(days=7), end + timedelta(days=7)
            row = _row(ev, start, end, local_now)
            row["note"] = "Es un sábado sí y uno no: puede ser este o el siguiente (mirá el panel del sitio)."
            out.append(row)
    out.sort(key=lambda r: r["start_ts"])
    return out


def _row(ev, start, end, now):
    active = start <= now <= end
    return {
        "name": ev["name"],
        "start": ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"][start.weekday()] + start.strftime(" %d/%m %H:%M"),
        "end": end.strftime("%H:%M"),
        "start_ts": start.timestamp(),
        "active": active,
        "danger": bool(ev.get("danger")),
        "in_min": 0 if active else int((start - now).total_seconds() // 60),
        "note": ev.get("note", ""),
    }


def raid_active(events, now=None):
    return any(r["active"] and r["danger"] for r in upcoming(events, now, horizon_h=1))
