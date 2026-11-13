"""Acceso a Windows: ventana del juego, teclado y mouse por SendInput (scan codes, funciona con juegos DirectX viejos)."""
import ctypes
import sys
import time

IS_WIN = sys.platform == "win32"

if IS_WIN:
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:  # coordenadas reales aunque Windows tenga escala 125%/150%
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
    KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE, KEYEVENTF_EXTENDEDKEY = 0x0002, 0x0008, 0x0001
    MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
    MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# Teclas por nombre -> (virtual key, scan code, extendida)
KEYS = {
    **{f"F{i}": (0x6F + i, sc, False) for i, sc in zip(range(1, 13), [0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x40, 0x41, 0x42, 0x43, 0x44, 0x57, 0x58])},
    **{str(d): (0x30 + d, sc, False) for d, sc in zip(range(10), [0x0B, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A])},
    **{c: (ord(c), sc, False) for c, sc in zip("QWERTYUIOP", [0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19])},
    **{c: (ord(c), sc, False) for c, sc in zip("ASDFGHJKL", [0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26])},
    **{c: (ord(c), sc, False) for c, sc in zip("ZXCVBNM", [0x2C, 0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x32])},
    "ENTER": (0x0D, 0x1C, False), "ESC": (0x1B, 0x01, False), "SPACE": (0x20, 0x39, False), "TAB": (0x09, 0x0F, False),
    "CTRL": (0x11, 0x1D, False), "SHIFT": (0x10, 0x2A, False), "ALT": (0x12, 0x38, False),
    "UP": (0x26, 0x48, True), "DOWN": (0x28, 0x50, True), "LEFT": (0x25, 0x4B, True), "RIGHT": (0x27, 0x4D, True),
    "HOME": (0x24, 0x47, True), "END": (0x23, 0x4F, True), "INSERT": (0x2D, 0x52, True), "DELETE": (0x2E, 0x53, True),
    "PAGEUP": (0x21, 0x49, True), "PAGEDOWN": (0x22, 0x51, True),
}


def _send(*inputs):
    if not IS_WIN:
        return
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def key_down(name):
    vk, sc, ext = KEYS[name.upper()]
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if ext else 0) if IS_WIN else 0
    if IS_WIN:
        _send(INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(0, sc, flags, 0, 0))))


def key_up(name):
    vk, sc, ext = KEYS[name.upper()]
    if IS_WIN:
        flags = KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if ext else 0)
        _send(INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(0, sc, flags, 0, 0))))


def press(name, hold=0.05):
    """Aprieta una tecla o combinación 'CTRL+1'."""
    parts = [p.strip().upper() for p in name.split("+") if p.strip()]
    for p in parts:
        key_down(p)
        time.sleep(0.01)
    time.sleep(hold)
    for p in reversed(parts):
        key_up(p)
        time.sleep(0.01)


def move(x, y):
    if IS_WIN:
        user32.SetCursorPos(int(x), int(y))


def _mouse(flag):
    if IS_WIN:
        _send(INPUT(type=INPUT_MOUSE, u=_INPUTUNION(mi=MOUSEINPUT(0, 0, 0, flag, 0, 0))))


def left_down():
    _mouse(MOUSEEVENTF_LEFTDOWN if IS_WIN else 0)


def left_up():
    _mouse(MOUSEEVENTF_LEFTUP if IS_WIN else 0)


def right_down():
    _mouse(MOUSEEVENTF_RIGHTDOWN if IS_WIN else 0)


def right_up():
    _mouse(MOUSEEVENTF_RIGHTUP if IS_WIN else 0)


def click(x, y, button="left", hold=0.04, double=False):
    move(x, y)
    time.sleep(0.03)
    for _ in range(2 if double else 1):
        (left_down if button == "left" else right_down)()
        time.sleep(hold)
        (left_up if button == "left" else right_up)()
        time.sleep(0.06)


def cursor_pos():
    if not IS_WIN:
        return (0, 0)
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def key_pressed(vk):
    """True si la tecla está apretada ahora (para hotkeys globales)."""
    if not IS_WIN:
        return False
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def list_windows():
    """[(hwnd, título)] de las ventanas visibles con título."""
    out = []
    if not IS_WIN:
        return out

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                out.append((hwnd, buf.value))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return out


def find_window(title_part):
    title_part = (title_part or "").lower()
    for hwnd, title in list_windows():
        if title_part and title_part in title.lower():
            return hwnd, title
    return None, None


def client_rect(hwnd):
    """(x, y, ancho, alto) del área de juego en coordenadas de pantalla."""
    if not IS_WIN or not hwnd:
        return None
    if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        return None
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None
    return (pt.x, pt.y, w, h)


def is_foreground(hwnd):
    return IS_WIN and user32.GetForegroundWindow() == hwnd


def focus(hwnd):
    if not IS_WIN or not hwnd:
        return
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # truco ALT para que Windows deje traer la ventana al frente
    key_down("ALT")
    key_up("ALT")
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.05)


def is_admin():
    if not IS_WIN:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def capture_window(hwnd):
    """Foto del contenido del juego aunque haya otra ventana encima (PrintWindow). None si no se puede."""
    if not IS_WIN or not hwnd:
        return None
    try:
        import numpy as np
        gdi32 = ctypes.windll.gdi32
        r = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w <= 0 or h <= 0:
            return None
        hwnd_dc = user32.GetDC(hwnd)
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        old = gdi32.SelectObject(mem_dc, bmp)
        ok = user32.PrintWindow(hwnd, mem_dc, 3)  # PW_CLIENTONLY | PW_RENDERFULLCONTENT

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

        bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)
        if not ok:
            return None
        img = np.frombuffer(buf, np.uint8).reshape(h, w, 4)[:, :, :3].copy()
        if img.mean() < 3:  # negro: el juego no deja copiarlo así
            return None
        return img
    except Exception:
        return None


KEYEVENTF_UNICODE = 0x0004


def type_text(text, delay=0.03):
    """Escribe texto letra por letra (sirve para cualquier carácter)."""
    if not IS_WIN:
        return
    for ch in text:
        code = ord(ch)
        down = INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)))
        up = INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)))
        _send(down, up)
        time.sleep(delay)


# ------------------------------------------------------------ modo segundo plano
# Si BG_HWND está puesto, los clicks y teclas van directo a la ventana del juego (PostMessage)
# y no se mueve el mouse real: podés usar la PC mientras tanto.
BG_HWND = None
_bg_pos = (0, 0)
_bg_held = False
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN, WM_RBUTTONUP = 0x200, 0x201, 0x202, 0x204, 0x205
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x100, 0x101, 0x102
MK_LBUTTON = 0x1


def set_background(hwnd):
    global BG_HWND
    BG_HWND = hwnd


def _to_client(x, y):
    pt = wintypes.POINT(int(x), int(y))
    user32.ScreenToClient(BG_HWND, ctypes.byref(pt))
    return pt.x, pt.y


def _lparam(x, y):
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def _post(msg, w, l):
    user32.PostMessageW(BG_HWND, msg, w, l)


_fg = {"move": move, "left_down": left_down, "left_up": left_up, "click": click, "press": press,
       "key_down": key_down, "key_up": key_up, "type_text": type_text}


def move(x, y):  # noqa: F811
    global _bg_pos
    if not BG_HWND:
        return _fg["move"](x, y)
    _bg_pos = _to_client(x, y)
    _post(WM_MOUSEMOVE, MK_LBUTTON if _bg_held else 0, _lparam(*_bg_pos))


def left_down():  # noqa: F811
    global _bg_held
    if not BG_HWND:
        return _fg["left_down"]()
    _bg_held = True
    _post(WM_LBUTTONDOWN, MK_LBUTTON, _lparam(*_bg_pos))


def left_up():  # noqa: F811
    global _bg_held
    if not BG_HWND:
        return _fg["left_up"]()
    _bg_held = False
    _post(WM_LBUTTONUP, 0, _lparam(*_bg_pos))


def click(x, y, button="left", hold=0.04, double=False):  # noqa: F811
    if not BG_HWND:
        return _fg["click"](x, y, button, hold, double)
    move(x, y)
    time.sleep(0.03)
    for _ in range(2 if double else 1):
        if button == "left":
            left_down()
            time.sleep(hold)
            left_up()
        else:
            _post(WM_RBUTTONDOWN, 0, _lparam(*_bg_pos))
            time.sleep(hold)
            _post(WM_RBUTTONUP, 0, _lparam(*_bg_pos))
        time.sleep(0.06)


def _key_lparam(sc, up):
    return 1 | (sc << 16) | ((0xC0000000) if up else 0)


def key_down(name):  # noqa: F811
    if not BG_HWND:
        return _fg["key_down"](name)
    vk, sc, ext = KEYS[name.upper()]
    _post(WM_KEYDOWN, vk, _key_lparam(sc, False))


def key_up(name):  # noqa: F811
    if not BG_HWND:
        return _fg["key_up"](name)
    vk, sc, ext = KEYS[name.upper()]
    _post(WM_KEYUP, vk, _key_lparam(sc, True))


def press(name, hold=0.05):  # noqa: F811
    if not BG_HWND:
        return _fg["press"](name, hold)
    parts = [p.strip().upper() for p in name.split("+") if p.strip()]
    for p in parts:
        key_down(p)
    time.sleep(hold)
    for p in reversed(parts):
        key_up(p)


def type_text(text, delay=0.03):  # noqa: F811
    if not BG_HWND:
        return _fg["type_text"](text, delay)
    for ch in text:
        _post(WM_CHAR, ord(ch), 1)
        time.sleep(delay)
