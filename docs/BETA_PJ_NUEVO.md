# Beta: pj nuevo de 1 a 200 (armado, SIN PROBAR)

Se activa con `pj_nuevo_beta: true` en config.json cuando arranque un pj de cero. Apagado no cambia nada.

## Hecho (v394)
- Etapa de armas: antes de la primera arma de dos manos usa una mano + escudo (compra, repara y reclama escudo).
  Al tener puesta una de dos manos queda `two_handed_since` y no vuelve nunca al escudo.

## Pendiente
1. Repasar todo lo corregido por consola desde que empezó Olympia y convertir cada caso en regla:
   venta apagada (sell_olympia), worn_best editado, escudo quitado, gear_clash borrado, need_weapon,
   arma correcta forzada, autostart.
2. Camino 1→200 en etapas con umbral fijo: armas, mapas por nivel, compras de equipo,
   pociones según lugares libres del bolso (cada poción ocupa un lugar en Olympia).
3. Venta segura de repetidos con la memoria del bolso (nunca lo puesto).
4. Probar con un pj nuevo cuando Juan lo diga.
