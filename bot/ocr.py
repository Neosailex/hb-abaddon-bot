"""Lectura del texto 'Mapa (x,y)' de la barra de abajo con el OCR que trae Windows (sin instalar nada más)."""
import asyncio
import re
import threading

import cv2
import numpy as np

_engine = None
_err = None
_lock = threading.Lock()

POS_RE = re.compile(r"(.*?)\s*[\(\[\{]\s*([0-9OoIlSB]{1,3})\s*[,\.;:]\s*([0-9OoIlSB]{1,3})\s*[\)\]\}]?")
FIX = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "S": "5", "B": "8"})


def _get_engine():
    global _engine, _err
    if _engine or _err:
        return _engine
    try:
        from winrt.windows.media.ocr import OcrEngine
        eng = OcrEngine.try_create_from_user_profile_languages()
        if eng is None:
            from winrt.windows.globalization import Language
            eng = OcrEngine.try_create_from_language(Language("en-US"))
        if eng is None:
            raise RuntimeError("Windows no tiene un idioma de OCR instalado")
        _engine = eng
    except Exception as e:  # pragma: no cover (solo Windows)
        _err = repr(e)
    return _engine


def last_error():
    return _err


def prepare(crop):
    """Texto claro sobre fondo oscuro -> texto negro sobre blanco, agrandado."""
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    _, th = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if th.mean() < 127:  # mayoría negra = fondo oscuro: invertir
        th = 255 - th
    th = cv2.copyMakeBorder(th, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
    return th


async def _recognize(img_gray):
    from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode
    from winrt.windows.storage.streams import DataWriter
    bgra = cv2.cvtColor(img_gray, cv2.COLOR_GRAY2BGRA)
    h, w = bgra.shape[:2]
    dw = DataWriter()
    dw.write_bytes(bgra.tobytes())
    sb = SoftwareBitmap.create_copy_from_buffer(dw.detach_buffer(), BitmapPixelFormat.BGRA8, w, h, BitmapAlphaMode.PREMULTIPLIED)
    res = await _engine.recognize_async(sb)
    return res.text


def read_text(crop):
    if crop is None or _get_engine() is None:
        return None
    with _lock:
        try:
            return asyncio.run(_recognize(prepare(crop)))
        except Exception as e:  # pragma: no cover
            global _err
            _err = repr(e)
            return None


def parse(text):
    """'Beginner Zone (74,115)' -> ('Beginner Zone', 74, 115)."""
    if not text:
        return None
    t = " ".join(text.split())
    m = POS_RE.search(t)
    if not m:
        return None
    name = m.group(1).strip(" -:|,.;'`")
    try:
        x = int(m.group(2).translate(FIX))
        y = int(m.group(3).translate(FIX))
    except ValueError:
        return None
    return (name, x, y)


def read_position(img, box):
    """box = {"x1","y1","x2","y2"} en coords de la ventana. Devuelve (mapa, x, y, texto) o None."""
    if img is None or not box:
        return None
    x1, y1, x2, y2 = (int(box[k]) for k in ("x1", "y1", "x2", "y2"))
    crop = img[max(0, y1):y2, max(0, x1):x2]
    if crop.size == 0:
        return None
    txt = read_text(crop)
    p = parse(txt)
    if not p:
        # letra chica sobre fondo con degradé (Olympia): más aumento y umbral fijo de brillo
        for sc in (3, 4):
            t2 = read_text_fixed(crop, sc, 150)
            p = parse(t2)
            if p:
                txt = t2
                break
    return (p[0], p[1], p[2], txt) if p else (None, None, None, txt)


def read_text_fixed(crop, scale=3, thr=150):
    if crop is None or _get_engine() is None:
        return None
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    th = np.where(g > thr, 0, 255).astype(np.uint8)
    th = cv2.copyMakeBorder(th, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
    with _lock:
        try:
            return asyncio.run(_recognize(th))
        except Exception:
            return None


async def _recognize_words(img_gray):
    from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode
    from winrt.windows.storage.streams import DataWriter
    bgra = cv2.cvtColor(img_gray, cv2.COLOR_GRAY2BGRA)
    h, w = bgra.shape[:2]
    dw = DataWriter()
    dw.write_bytes(bgra.tobytes())
    sb = SoftwareBitmap.create_copy_from_buffer(dw.detach_buffer(), BitmapPixelFormat.BGRA8, w, h, BitmapAlphaMode.PREMULTIPLIED)
    res = await _engine.recognize_async(sb)
    out = []
    for line in res.lines:
        for wd in line.words:
            r = wd.bounding_rect
            out.append((wd.text, r.x, r.y, r.width, r.height))
    return out


def read_words(img, box, scale=1.5):
    """Palabras con su posición (coords de la ventana del juego): [(texto, cx, cy, w, h)]."""
    if img is None or _get_engine() is None:
        return []
    x1, y1, x2, y2 = (int(box[k]) for k in ("x1", "y1", "x2", "y2"))
    crop = img[max(0, y1):y2, max(0, x1):x2]
    if crop.size == 0:
        return []
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    with _lock:
        try:
            words = asyncio.run(_recognize_words(g))
        except Exception as e:  # pragma: no cover
            global _err
            _err = repr(e)
            return []
    return [(t, x1 + (x + w / 2) / scale, y1 + (y + h / 2) / scale, w / scale, h / scale) for t, x, y, w, h in words]
