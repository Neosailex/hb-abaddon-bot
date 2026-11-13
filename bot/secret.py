"""Cifra la contraseña con DPAPI de Windows: solo tu usuario de Windows en esta PC la puede descifrar."""
import base64
import ctypes
import sys

IS_WIN = sys.platform == "win32"

if IS_WIN:
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def protect(text):
    if not text:
        return ""
    if not IS_WIN:
        raise RuntimeError("Solo en Windows")
    inp, _keep = _blob(text.encode("utf-8"))
    out = DATA_BLOB()
    if not _crypt32.CryptProtectData(ctypes.byref(inp), "hbbot", None, None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return base64.b64encode(ctypes.string_at(out.pbData, out.cbData)).decode()
    finally:
        _kernel32.LocalFree(out.pbData)


def unprotect(token):
    if not token or not IS_WIN:
        return ""
    raw = base64.b64decode(token)
    inp, _keep = _blob(raw)
    out = DATA_BLOB()
    if not _crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        return ""
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    finally:
        _kernel32.LocalFree(out.pbData)
