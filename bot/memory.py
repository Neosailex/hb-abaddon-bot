"""Lectura de la memoria del juego (solo lectura, no modifica nada).

El bot aprende solo dónde guarda Helbreath tus coordenadas:
  1. lee la posición con OCR (lo que ya hacía),
  2. busca en la memoria del juego ese par de números (x, y) pegados,
  3. cada vez que caminás descarta los que no siguieron a la posición real,
  4. cuando queda uno confirmado en varias posiciones distintas, lo usa: posición exacta e instantánea.
"""
import sys
import threading
import time

import numpy as np

IS_WIN = sys.platform == "win32"

if IS_WIN:
    import ctypes
    import ctypes.wintypes as wt

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
                    ("AllocationProtect", wt.DWORD), ("_a1", wt.DWORD), ("RegionSize", ctypes.c_ulonglong),
                    ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD), ("_a2", wt.DWORD)]

    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.VirtualQueryEx.restype = ctypes.c_size_t
    k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
    k32.ReadProcessMemory.restype = wt.BOOL
    k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_size_t)]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]

READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}  # RO, RW, WC, ERX(no), ERW, EWC
TYPES = {"i32": ("<i4", 4), "i16": ("<i2", 2)}


class Proc:
    """Proceso del juego, abierto solo para leer."""

    def __init__(self, hwnd):
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        self.pid = pid.value
        self.h = k32.OpenProcess(0x0410, False, self.pid)  # QUERY_INFORMATION | VM_READ
        if not self.h:
            raise OSError(f"no pude abrir el proceso del juego (error {ctypes.get_last_error()})")

    def close(self):
        if self.h:
            k32.CloseHandle(self.h)
            self.h = None

    def regions(self, max_size=512 << 20):
        mbi, addr, out = MBI(), 0, []
        while addr < 0x7FFFFFFFFFFF:
            if not k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            base, size = mbi.BaseAddress, mbi.RegionSize
            prot = mbi.Protect & 0xFF
            if (mbi.State == 0x1000 and prot in READABLE and not (mbi.Protect & 0x100)
                    and mbi.Type in (0x20000, 0x1000000) and size <= max_size):
                out.append((base, size))
            nxt = base + size
            if nxt <= addr:
                break
            addr = nxt
        return out

    def read(self, addr, size):
        buf = ctypes.create_string_buffer(size)
        n = ctypes.c_size_t()
        if not k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)) or n.value == 0:
            return None
        return buf.raw[:n.value]


def read_val(proc, typ, addr):
    fmt, sz = TYPES[typ]
    b = proc.read(addr, sz * 2)
    if not b or len(b) < sz * 2:
        return None
    a = np.frombuffer(b, dtype=fmt)
    return int(a[0]), int(a[1])


def read_many(proc, keys, page=4096):
    """Lee muchos candidatos agrupándolos por página (mucho más rápido que uno por uno)."""
    snap, pages = {}, {}
    for k in keys:
        pages.setdefault(k[1] // page, []).append(k)
    for pg, ks in pages.items():
        base = pg * page
        try:
            data = proc.read(base, page + 8)
        except Exception:
            data = None
        for typ, addr in ks:
            fmt, sz = TYPES[typ]
            o = addr - base
            if not data or o + 2 * sz > len(data):
                try:
                    snap[(typ, addr)] = read_val(proc, typ, addr)
                except Exception:
                    snap[(typ, addr)] = None
                continue
            a = np.frombuffer(data[o:o + 2 * sz], dtype=fmt)
            snap[(typ, addr)] = (int(a[0]), int(a[1]))
    return snap


def scan_pairs(proc, x, y, chunk=8 << 20, limit=200000):
    """Busca en toda la memoria (x, y) pegados como int32 o int16."""
    found = set()
    for base, size in proc.regions():
        off = 0
        while off < size:
            n = min(chunk + 8, size - off)
            data = proc.read(base + off, n)
            if data:
                for typ, (fmt, sz) in TYPES.items():
                    m = len(data) // sz * sz
                    a = np.frombuffer(data[:m], dtype=fmt)
                    if len(a) < 2:
                        continue
                    idx = np.nonzero((a[:-1] == x) & (a[1:] == y))[0]
                    for i in idx[:limit]:
                        found.add((typ, base + off + int(i) * sz))
                if len(found) > limit:
                    return sorted(found)
            off += chunk
    return sorted(found)


class PosFinder:
    """Aprende la dirección de las coordenadas cruzando memoria con el OCR."""

    NEED = 3  # posiciones distintas confirmadas

    def __init__(self, log=print, saved=None):
        self.log = log
        self.proc = None
        self.hwnd = None
        self.cands = {}  # (typ, addr) -> fallas
        self.seen = []  # posiciones distintas confirmadas
        self.addr = None  # (typ, addr) aprendida
        self.scanning = False
        self.error = None
        self.saved = tuple(saved) if saved else None
        self.status = "esperando"
        self.lock = threading.Lock()

    # -- proceso
    def attach(self, hwnd, proc_factory=None):
        if hwnd == self.hwnd and self.proc:
            return True
        self.reset(keep_saved=True)
        try:
            self.proc = (proc_factory or Proc)(hwnd)
            self.hwnd = hwnd
            self.error = None
            if self.saved:
                with self.lock:
                    self.cands = {self.saved: 0}
                self.status = "probando la posición guardada"
            return True
        except Exception as e:
            self.error = str(e)
            self.status = "sin acceso a la memoria"
            return False

    def reset(self, keep_saved=True):
        with self.lock:
            if self.proc and hasattr(self.proc, "close"):
                try:
                    self.proc.close()
                except Exception:
                    pass
            self.proc, self.hwnd = None, None
            self.cands, self.seen, self.addr = {}, [], None
            if not keep_saved:
                self.saved = None
            self.status = "esperando"

    @property
    def learned(self):
        return self.addr is not None

    # -- lectura rápida (cada vuelta del bot)
    def read_pos(self):
        if not (self.proc and self.addr):
            return None
        try:
            v = read_val(self.proc, *self.addr)
        except Exception:
            v = None
        if not v or not (0 <= v[0] < 1024 and 0 <= v[1] < 1024):
            return None
        return v

    def snapshot(self):
        """Lee los candidatos YA, en el mismo momento que se saca la foto que va al OCR."""
        if not self.proc or self.scanning:
            return None
        with self.lock:
            keys = list(self.cands)
            if self.addr and self.addr not in self.cands:
                keys.append(self.addr)
        return read_many(self.proc, keys)

    # -- aprendizaje: se llama con cada lectura del OCR
    def feed(self, x, y, snap, stable):
        if not self.proc or x is None:
            return
        if self.addr:
            # ya aprendida: la memoria manda. Solo la descarto si ella misma da valores imposibles.
            v = snap.get(self.addr) if snap else None
            if v is not None and not (0 <= v[0] < 2048 and 0 <= v[1] < 2048):
                self._miss = getattr(self, "_miss", 0) + 1
                if self._miss >= 5:
                    self.log("Memoria: la posición da valores imposibles, la vuelvo a buscar.")
                    with self.lock:
                        self.addr, self.cands, self.seen = None, {}, []
                    self.saved = None
                    self._miss = 0
            else:
                self._miss = 0
            return
        if not self.cands:
            if stable and not self.scanning:
                threading.Thread(target=self._scan, args=(x, y), daemon=True).start()
            return
        if snap is None:
            return
        with self.lock:
            for k in list(self.cands):
                v = snap.get(k)
                if v is None or v == (x, y):
                    continue
                self.cands[k] += 1
                if self.cands[k] >= 2:
                    del self.cands[k]
            if (x, y) not in self.seen:
                self.seen.append((x, y))
            n = len(self.cands)
            if n == 0:
                self.seen = []
                self.status = "no la encontré, busco de nuevo"
                return
            # la guardada se acepta con una sola coincidencia
            if self.saved in self.cands and snap.get(self.saved) == (x, y):
                self._set(self.saved)
                return
            self.status = f"aprendiendo ({n} candidatos, {len(self.seen)}/{self.NEED} posiciones)"
            if len(self.seen) >= self.NEED and n <= 8:
                best = sorted(self.cands, key=lambda k: (self.cands[k], k[0] != "i16", k[1]))[0]
                self._set(best)

    def _set(self, k):
        self.addr = k
        self.cands = {}
        self.saved = k
        self._miss = 0
        self.status = "posición exacta desde la memoria"
        self.log(f"🧠 Memoria: encontré dónde guarda el juego tu posición ({k[0]} @ {k[1]:#x}).")

    def _looks_like_pj(self, addr):
        try:
            d = self.proc.read(addr, 84)
            if not d or len(d) < 84:
                return False
            i32 = lambda o: int.from_bytes(d[o:o + 4], "little", signed=True)
            hp, mp, sp, lvl = i32(12), i32(16), i32(20), i32(72)
            stats = [i32(o) for o in range(32, 56, 4)]
            return (0 <= hp <= 20000 and 0 <= mp <= 20000 and 0 <= sp <= 20000 and 1 <= lvl <= 400
                    and all(10 <= v <= 500 for v in stats) and hp + mp + sp > 0)
        except Exception:
            return False

    def _scan(self, x, y):
        self.scanning = True
        self.status = "buscando en la memoria del juego…"
        self.log(f"Memoria: busco ({x},{y}) en la memoria del juego…")
        t = time.time()
        try:
            res = scan_pairs(self.proc, x, y)
            # atajo: al lado de la posición del pj están su vida, sus stats y su nivel. Si un solo candidato tiene
            # todo eso con valores posibles, es ese (así no hace falta caminar para descartar: sirve al reconectar)
            good = [k for k in res if k[0] == "i16" and self._looks_like_pj(k[1])]
            if len(good) == 1:
                self._set(good[0])
                return
            with self.lock:
                self.cands = {k: 0 for k in res}
                self.seen = [(x, y)]
            self.status = f"aprendiendo ({len(res)} candidatos, 1/{self.NEED} posiciones)"
            self.log(f"Memoria: {len(res)} candidatos para la posición ({time.time() - t:.1f} s). Caminando los voy descartando.")
        except Exception as e:
            self.error = str(e)
            self.status = "error leyendo memoria"
            self.log(f"Memoria: error buscando ({e}).")
        finally:
            self.scanning = False


def find_strings(proc, words, limit=400):
    """Diagnóstico: dónde aparecen ciertos textos (nombres de bichos, tu pj) en la memoria."""
    out = {w: [] for w in words}
    pats = {w: w.encode("latin-1", "ignore") for w in words}
    for base, size in proc.regions():
        off = 0
        while off < size:
            n = min((8 << 20) + 32, size - off)
            data = proc.read(base + off, n)
            if data:
                for w, p in pats.items():
                    if len(out[w]) >= limit:
                        continue
                    i = data.find(p)
                    while i != -1 and len(out[w]) < limit:
                        nxt = data[i + len(p):i + len(p) + 1]
                        if nxt in (b"\x00", b""):
                            out[w].append(base + off + i)
                        i = data.find(p, i + 1)
            off += 8 << 20
    return out


def scan_value(proc, typ, value, limit=3000, within=None):
    """Busca un valor en la memoria. typ: i16/i32/u8/f32/str/bytes(hex)/seq16/seq32 (lista de números pegados).
    within=[addr_hex, size] limita la búsqueda a un rango."""
    out = []
    if typ in ("str", "bytes"):
        pat = value.encode("latin-1") if typ == "str" else bytes.fromhex(value)
    elif typ in ("seq16", "seq32"):
        pat = np.array(value, dtype="<i2" if typ == "seq16" else "<i4").tobytes()
    else:
        fmt = {"i16": "<i2", "i32": "<i4", "u8": "u1", "f32": "<f4"}[typ]
        pat = np.array([value], dtype=fmt).tobytes()
    align = {"i16": 2, "i32": 4, "f32": 4, "seq16": 2, "seq32": 4}.get(typ, 1)
    if within:
        a0 = int(within[0], 16) if isinstance(within[0], str) else int(within[0])
        regs = [(a0, int(within[1]))]
    else:
        regs = proc.regions()
    chunk = 8 << 20
    for base, size in regs:
        off = 0
        while off < size:
            data = proc.read(base + off, min(chunk + len(pat), size - off))
            if data:
                i = data.find(pat)
                while i != -1:
                    if (base + off + i) % align == 0:
                        out.append(base + off + i)
                        if len(out) >= limit:
                            return out
                    i = data.find(pat, i + 1)
            off += chunk
    return out


class Entities:
    """Lista de bichos y jugadores alrededor del pj, leída de la grilla de baldosas del juego.

    Cada baldosa ocupa 384 bytes; la del pj está siempre en la misma dirección (la grilla se corre con él).
    índice = dx*55 + dy (medido caminando). En cada baldosa: 0xff, nombre (+8, largo en +24), datos en +176.
    """
    S, H = 384, 55

    def __init__(self, log=print):
        self.log = log
        self.center = None
        self.name = None
        self.finding = False
        self.fail = 0

    def player_name(self, proc, pos_addr):
        d = proc.read(pos_addr - 36, 16)
        if not d:
            return None
        n = d.split(b"\0")[0]
        return n.decode("latin-1") if 2 <= len(n) <= 12 and all(32 < c < 127 for c in n) else None

    def _valid(self, proc, c, name):
        d = proc.read(c - 8, 32)
        nb = name.encode("latin-1")
        if not d or d[0] != 0xFF or d[8:8 + len(nb)] != nb or d[24] != len(nb):
            return False
        c2 = proc.read(c + 24, 1)  # capacidad del texto (15) = es una baldosa de la grilla
        return bool(c2) and c2[0] == 15

    def find(self, proc, pos_addr):
        self.finding = True
        try:
            name = self.player_name(proc, pos_addr)
            if not name:
                return
            hits = scan_value(proc, "str", name + "\0", limit=80)
            hits = [h for h in hits if self._valid(proc, h, name)]
            if hits:
                self.center, self.name, self.fail = max(hits), name, 0
                self.log(f"🧠 Memoria: encontré la lista de bichos alrededor ({len(hits)} copia/s).")
        except Exception as e:
            self.log(f"Memoria: no pude ubicar la lista de bichos ({e}).")
        finally:
            self.finding = False

    def read(self, proc, cols=13):
        """[(dx, dy, nombre, kind, dir, tipo)] sin el pj. None si la lista no es válida."""
        if not self.center:
            return None
        S, H = self.S, self.H
        n = cols * H + H // 2
        d = proc.read(self.center - 8 - n * S, (2 * n + 1) * S)
        if not d or len(d) < (2 * n + 1) * S:
            self.fail += 1
            return None
        o0 = n * S
        nb = self.name.encode("latin-1")
        if d[o0] != 0xFF or d[o0 + 8:o0 + 8 + len(nb)] != nb:
            self.fail += 1
            if self.fail > 20:
                self.center = None
            return None
        self.fail = 0
        a = np.frombuffer(d, dtype=np.uint8).reshape(-1, S)
        idx = np.nonzero((a[:, 0] == 0xFF) & (a[:, 24] > 0) & (a[:, 24] < 16) & (a[:, 25] == 0))[0]
        out = []
        for i in idx:
            k = int(i) - n
            if k == 0:
                continue
            row = a[i]
            dx = int(round(k / H))
            dy = k - dx * H
            name = bytes(row[8:8 + int(row[24])]).decode("latin-1", "replace")
            out.append((dx, dy, name, int(row[176]), int(row[177]), int(row[180])))
        return out


def find_seq(proc, values, widths=(4, 2), gap=(0,), chunk=8 << 20, limit=50):
    """Busca en la memoria del juego una secuencia de números conocidos (ej. STR, VIT, DEX, INT, MAG, LUK
    de la ventana de personaje). Sirve para ubicar el bloque del pj en un cliente nuevo: al lado suelen
    estar nivel, vida, maná, exp. Devuelve [(dirección, ancho, separación)]. Solo lee."""
    import struct
    out = []
    for base, size in proc.regions():
        off = 0
        while off < size and len(out) < limit:
            n = min(chunk, size - off)
            d = proc.read(base + off, n + 64)
            if not d:
                break
            for w in widths:
                fmt = "<i" if w == 4 else "<h"
                for g in gap:
                    step = w + g
                    pat = b"".join(struct.pack(fmt, v) + b"\0" * g for v in values)
                    pat = pat[:len(pat) - g] if g else pat
                    i = d.find(pat)
                    while i != -1 and i < n and len(out) < limit:
                        if (base + off + i) % w == 0:
                            out.append((base + off + i, w, g))
                        i = d.find(pat, i + 1)
            off += n
    return out


DEFAULT_SIG = {"sig": [[-4, 15], [8, 0x64000000], [-56, 16]], "type_off": -92, "id_off": -88}


class OlyEntities:
    """Bichos/NPC de clientes nuevos (Olympia). Cada uno es un objeto del heap que se reconoce por una "firma":
    valores fijos a ciertas distancias de su posición (x,y en int16). En Olympia: 15 justo antes, 0x64000000 en
    +8 y 16 en -56; el tipo (10 = Slime) en -92 y un id en -88. En otro server la firma se descubre sola
    (discover_entities). Barrer toda la memoria tarda ~0,5 s; después solo releo los bloques donde estaban."""

    def __init__(self, sig=None):
        self.set_sig(sig or DEFAULT_SIG)

    def set_sig(self, sig):
        self.sig = sig
        self.chunks, self.t_full = [], 0.0

    def _scan(self, proc, regs, step=4 << 20):
        out, chunks = [], []
        sig = [(int(o) // 4, int(v) & 0xFFFFFFFF) for o, v in self.sig["sig"]]
        t_off = self.sig.get("type_off")
        i_off = self.sig.get("id_off")
        offs = [o for o, _ in sig] + [(t_off or 0) // 4, (i_off or 0) // 4, 0]
        lo, hi = -min(offs) if min(offs) < 0 else 0, max(offs) if max(offs) > 0 else 0
        o0, v0 = sig[0]
        for base, size in regs:
            off = 0
            while off < size:
                n = min(step, size - off)
                d = proc.read(base + off, n)
                if not d:
                    break
                a = np.frombuffer(d[:len(d) // 4 * 4], np.uint32)
                if len(a) > lo + hi + 4:
                    p = np.nonzero(a == v0)[0] - o0
                    p = p[(p >= lo) & (p < len(a) - hi)]
                    for o, v in sig[1:]:
                        if len(p) == 0:
                            break
                        p = p[a[p + o] == v]
                    if len(p):
                        chunks.append((base + off, n))
                    for k in p:
                        xy = int(a[k])
                        x, y = xy & 0xFFFF, xy >> 16
                        x, y = (x - 65536 if x > 32767 else x), (y - 65536 if y > 32767 else y)
                        typ = int(a[k + t_off // 4] & 0xFFFF) if t_off is not None else -1
                        st = int(a[k + t_off // 4] >> 16) if t_off is not None else 0
                        oid = int(a[k + i_off // 4]) if i_off is not None else int(base + off + k * 4)
                        out.append((x, y, typ, st, oid))
                off += n
        return out, chunks

    # lectura continua en segundo plano: lista nueva cada ~0,2 s y barrido completo cada 3 s (bichos nuevos)
    def start(self, get_proc):
        import threading
        import time as _t
        if getattr(self, "_th", None) and self._th.is_alive():
            return
        self.latest, self.latest_t = [], 0.0

        def loop():
            while not getattr(self, "_stop", False):
                p = get_proc()
                if not p:
                    _t.sleep(1.0)
                    continue
                try:
                    self.latest = self.read(p, full_every=3.0)
                    self.latest_t = _t.time()
                except Exception:
                    _t.sleep(0.5)
                _t.sleep(0.2)
        self._stop = False
        self._th = threading.Thread(target=loop, daemon=True)
        self._th.start()

    def read(self, proc, full_every=20.0):
        """[(x, y, tipo, estado, id)] en coordenadas del mapa."""
        import time as _t
        now = _t.time()
        if not self.chunks or now - self.t_full > full_every:
            out, self.chunks = self._scan(proc, proc.regions())
            self.t_full = now
            return out
        out, ch = self._scan(proc, self.chunks)
        if not out:
            self.t_full = 0
        return out


def discover_entities(proc, px, py, rounds=4, wait=1.5, log=print):
    """Server nuevo: busca solo dónde guarda el juego a los bichos. Junta todos los pares (x,y) cerca del pj,
    los relee varias veces y se queda con los que se mueven de a una baldosa (bichos caminando). Después mira
    qué valores alrededor son iguales en todos ellos: esa es la firma. Devuelve el dict de firma o None."""
    import struct
    import time as _t
    hits = {}
    for base, size in proc.regions():
        off = 0
        while off < size:
            n = min(8 << 20, size - off)
            d = proc.read(base + off, n)
            if not d:
                break
            a = np.frombuffer(d[:len(d) // 4 * 4], np.int16)
            m = (np.abs(a[:-1] - px) <= 14) & (np.abs(a[1:] - py) <= 11)
            for i in np.nonzero(m)[0][:60000]:
                addr = base + off + int(i) * 2
                if addr % 4 == 0:
                    hits[addr] = (int(a[i]), int(a[i + 1]))
            off += n
    snaps = [hits]
    for _ in range(rounds):
        _t.sleep(wait)
        cur = {}
        for addr in hits:
            b = proc.read(addr, 4)
            if b and len(b) == 4:
                cur[addr] = struct.unpack("<hh", b)
        snaps.append(cur)
    cand = []
    for addr in hits:
        xy = [sn.get(addr) for sn in snaps]
        if any(v is None for v in xy) or len(set(xy)) < 2:
            continue
        if all(abs(x - px) <= 14 and abs(y - py) <= 11 for x, y in xy) and \
                all(abs(xy[i][0] - xy[i + 1][0]) <= 1 and abs(xy[i][1] - xy[i + 1][1]) <= 1 for i in range(len(xy) - 1)):
            cand.append(addr)
    log(f"🔎 Memoria: {len(hits)} posiciones cerca, {len(cand)} se mueven como bichos.")
    if len(cand) < 3:
        return None
    blocks = []
    for addr in cand:
        b = proc.read(addr - 0x100, 0x180)
        if b and len(b) == 0x180:
            blocks.append(np.frombuffer(b, np.uint32))
    if len(blocks) < 3:
        return None
    M = np.stack(blocks)  # filas: candidatos; columnas: palabras desde -0x100
    same = (M == M[0]).all(axis=0)
    sig = []
    for j in np.nonzero(same)[0]:
        o = int(j) * 4 - 0x100
        v = int(M[0, j])
        if o == 0 or v in (0, 0xFFFFFFFF):
            continue
        sig.append([o, v])
    if not sig:
        return None
    sig.sort(key=lambda e: abs(e[0]))
    sig = sig[:4]
    # tipo: palabra cuyo int16 bajo es chico (1..300) y es igual en la mayoría; id: palabra distinta en todos
    t_off = i_off = None
    for j in sorted(range(M.shape[1]), key=lambda j: abs(j * 4 - 0x100)):
        o = j * 4 - 0x100
        if o == 0 or same[j]:
            continue
        low = M[:, j] & 0xFFFF
        if t_off is None and (low >= 1).all() and (low <= 300).all() and len(set(low.tolist())) <= max(1, len(low) // 3):
            t_off = o
        if i_off is None and len(set(M[:, j].tolist())) == len(M) and (M[:, j] > 0x10000).all():
            i_off = o
    return {"sig": sig, "type_off": t_off, "id_off": i_off, "found": len(cand)}


# ---------------------------------------------------------------------------------------------------------------
# PISO (Olympia): el juego guarda una grilla de 51 x 37 baldosas centrada en el pj (índice = columna*37 + fila;
# el pj está en la columna 25, fila 18). Cada baldosa ocupa 44 bytes; en +22/+24 están la hoja y el cuadro del
# dibujo del objeto tirado ARRIBA de todo (si hay varios en la misma baldosa, solo se ve el de arriba), en +36
# quién está parado ahí. Hay dos copias seguidas (separadas 83072 bytes) y no siempre están iguales: uso la que
# coincide con los bichos que veo. Investigado el 22/11 con Juan (poción, oro, martillo; Aresden City y Eternal
# Field). Solo lectura.
GRID_W, GRID_H, GRID_REC = 51, 37, 44
GRID_CX, GRID_CY = 25, 18
GRID_N = GRID_W * GRID_H
GRID_GAP = 83072


def grid_rec_ok(rec):
    """¿Estos 44 bytes parecen una baldosa de la grilla del piso?"""
    return len(rec) >= GRID_REC and rec[21] >= 0xE0 and rec[34] == 0x94 and rec[35] == 0x01


def grid_items(data, px, py):
    """[(x, y, hoja, cuadro)] de lo tirado, desde los bytes de una copia de la grilla."""
    out = []
    if not data or len(data) < GRID_N * GRID_REC:
        return out
    a = np.frombuffer(data[:GRID_N * GRID_REC], np.uint8).reshape(GRID_N, GRID_REC)
    sh = a[:, 22].astype(np.int32) | (a[:, 23].astype(np.int32) << 8)
    fr = a[:, 24].astype(np.int32) | (a[:, 25].astype(np.int32) << 8)
    for i in np.nonzero((sh | fr) != 0)[0]:
        i = int(i)
        out.append((px + i // GRID_H - GRID_CX, py + i % GRID_H - GRID_CY, int(sh[i]), int(fr[i])))
    return out


def grid_owners(data, px, py):
    """{(x, y)} baldosas con alguien parado (bicho, jugador)."""
    out = set()
    if not data or len(data) < GRID_N * GRID_REC:
        return out
    a = np.frombuffer(data[:GRID_N * GRID_REC], np.uint8).reshape(GRID_N, GRID_REC)
    for i in np.nonzero(a[:, 36:40].any(axis=1))[0]:
        i = int(i)
        out.add((px + i // GRID_H - GRID_CX, py + i % GRID_H - GRID_CY))
    return out


def find_grid_runs(blob, min_run=300):
    """Corridas de baldosas seguidas (de a 44 bytes) en un bloque de memoria: [(offset, cantidad)]."""
    a = np.frombuffer(blob, np.uint8)
    if len(a) < GRID_REC * 2:
        return []
    m = np.zeros(len(a), bool)
    n = len(a) - GRID_REC
    m[:n] = (a[21:21 + n] >= 0xE0) & (a[34:34 + n] == 0x94) & (a[35:35 + n] == 0x01)
    idx = np.nonzero(m)[0]
    if len(idx) == 0:
        return []
    s_ = set(idx.tolist())
    runs = []
    for i in idx.tolist():
        if (i - GRID_REC) in s_:
            continue
        k = 1
        while (i + GRID_REC * k) in s_:
            k += 1
        if k >= min_run:
            runs.append((i, k))
    return runs


def pick_grid_base(runs):
    """De las corridas (dirección, cantidad) elijo los inicios de grilla: corridas alineadas que juntas no pasan
    de una grilla entera. La primera corrida de cada grupo es el inicio (si el pj no está pegado al borde del
    mapa). Devuelve [dirección] ordenadas."""
    runs = sorted(runs)
    bases, i = [], 0
    while i < len(runs):
        a0, k0 = runs[i]
        end = a0 + k0 * GRID_REC
        j = i + 1
        while j < len(runs) and (runs[j][0] - a0) % GRID_REC == 0 and runs[j][0] + runs[j][1] * GRID_REC - a0 <= GRID_N * GRID_REC:
            end = runs[j][0] + runs[j][1] * GRID_REC
            j += 1
        if (end - a0) // GRID_REC >= GRID_N * 0.5:
            bases.append(a0)
        i = j
    return bases


class GroundGrid:
    """Lo tirado en el piso, leído de la memoria (Olympia)."""

    def __init__(self):
        self.bases, self.t_find, self.fail_t = [], 0.0, 0.0
        self.copy = 0  # cuál de las dos copias está al día

    def find(self, proc, step=8 << 20):
        runs = []
        for base, size in proc.regions():
            off = 0
            while off < size:
                n = min(step, size - off)
                d = proc.read(base + off, n + GRID_REC * 2 if off + n < size else n)
                if not d:
                    break
                for o, k in find_grid_runs(d):
                    if o < n:
                        runs.append((base + off + o, k))
                off += n
        bases = pick_grid_base(runs)
        # las dos copias van seguidas: si veo una y la otra a GRID_GAP, me quedo con ese par
        pair = [b for b in bases if b + GRID_GAP in bases]
        self.bases = [pair[0], pair[0] + GRID_GAP] if pair else bases[:2]
        self.t_find = time.time()
        return self.bases

    def valid(self, proc):
        if not self.bases:
            return False
        d = proc.read(self.bases[0], GRID_REC * 40)
        if not d or len(d) < GRID_REC * 40:
            return False
        ok = sum(1 for i in range(40) if grid_rec_ok(d[i * GRID_REC:(i + 1) * GRID_REC]))
        return ok >= 20

    def read(self, proc, px, py, mobs=()):
        """[(x, y, hoja, cuadro)] o None si no encuentro la grilla. mobs: baldosas con bichos (para elegir la
        copia al día)."""
        now = time.time()
        if self.bases and now - self.t_find > 30:
            if self.valid(proc):
                self.t_find = now
            else:
                self.bases = []
        if not self.bases:
            # buscarla tarda un par de segundos: lo hago en otro hilo para no frenar al bot
            th = getattr(self, "_th", None)
            if not (th and th.is_alive()) and now - self.fail_t > 20:
                self.fail_t = now

                def _f():
                    try:
                        self.find(proc)
                    except Exception:
                        self.bases = []
                self._th = threading.Thread(target=_f, daemon=True)
                self._th.start()
                if getattr(self, "sync", False):
                    self._th.join()
            if not self.bases:
                return None
        datas = [proc.read(b, GRID_N * GRID_REC) for b in self.bases]
        datas = [d if d and len(d) >= GRID_N * GRID_REC else None for d in datas]
        if not any(datas):
            self.bases = []
            return None
        if len(datas) > 1 and all(datas) and mobs:
            mset = set(mobs)
            score = [len(grid_owners(d, px, py) & mset) for d in datas]
            if score[0] != score[1]:
                self.copy = 0 if score[0] > score[1] else 1
        d = datas[self.copy] if self.copy < len(datas) and datas[self.copy] else next(x for x in datas if x)
        return grid_items(d, px, py)
