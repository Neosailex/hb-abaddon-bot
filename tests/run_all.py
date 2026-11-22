"""Banco de pruebas: corre todas. Si una falla, la versión NO sale (regla 1 de Juan).
Uso: python tests/run_all.py"""
import os
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
tests = sorted(f for f in os.listdir(here) if f.startswith("test_") and f.endswith(".py"))
bad = []
for f in tests:
    r = subprocess.run([sys.executable, os.path.join(here, f)], capture_output=True, text=True, cwd=here)
    last = (r.stdout.strip().splitlines() or ["?"])[-1]
    print(("OK    " if r.returncode == 0 else "FALLA ") + last)
    if r.returncode != 0:
        bad.append(f)
        print(r.stdout[-3000:] + r.stderr[-3000:])
print("\n" + ("TODO OK: se puede entregar." if not bad else f"{len(bad)} prueba(s) fallaron: NO se entrega."))
sys.exit(1 if bad else 0)
