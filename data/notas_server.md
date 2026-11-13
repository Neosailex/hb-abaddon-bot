# Notas del server (helbreathabaddon.com/maps.html y drops.html, leído 2026-11-12)
Mapas Aresden: Beginner Zone (hasta 20, slimes) · Aresden City (todos) · Aresden Farm (todos: Giant Ant, Giant Scorpion, Slime, Snake, Stone Golem en pits)
· Aresien Garden (todos: Orc Mage, Giant Plant, Orc, Troll, Snake, Cannibal Plant) · Eternal Field 20+ (Scorpion, Skeleton, Stone/Clay Golem, Cannibal Plant, Ant, Snake, Zombie)
· Aresden Dungeon 20+ · Silent Wood 30+ · Middleland 20+ (PvP) · ML Mine todos · ML Dungeon 30-100 · Dungeon L2 40+ · L3 50+ · L4 60+ · Promiseland hasta 120 · Tower of Hell 1-3, ToH4 180+.
Conexiones entre mapas: la página no las dice.
Drops: oro 45% por kill; 30% común, 15% raro; los raros aparecen cuando el cuerpo se desvanece. Reputación positiva sube drops (hasta +25% a 500).
HP: Giant Ant 6-15, Cat 8-20, Giant Scorpion 31-36, Clay Golem 151-180, Hellhound 176-210, Giant Frog 176-210, Cyclops 301-360.
Raros vendibles/crafteo: Ant Leg/Antenna, Scorpion Meat/Skin/Pincers/Sting, Lump of Clay.
Pena de muerte: no figura.

## Aprendido jugando (2026-11-13 madrugada)
- Traveller no pasa de nivel 19: ciudadanía en Aresden Cityhall (puerta ciudad 144,123) → Cityhall Officer → Offer → Citizenship request → Yes. HECHO.
- Al morir se pierden items equipados (perdió la espada y el pantalón; quedó peleando a mano).
- Blacksmith (puerta Farm 75,86 / ciudad 169,194): BlackSmith Keeper → Trade. Armas: Dagger 25, Short Sword 50/+1 200, Main Gauche 50/200, Gradius 90, Gradius +1 350 (nivel 20+), Long Sword 180/650, Sabre 150/600, Scimitar 200/800, Falchion 250/1000, Esterk 400/800, Rapier 300/1300, Broad Sword 250/1100, Bastard Sword 300/1200, Claymore 400... Compré Gradius +1.
- La lista de venta se mueve con la rueda (WM_MOUSEWHEEL). Mientras está abierta no se puede equipar ("Item transaction not finished"); se cierra saliendo del edificio.
- Equipar: inventario abierto y arrastrar el item al muñeco de Character (el doble click por mensajes no anda).
- Ventanas: Character = ícono casco (816,918); Inventory = (875,918). Level up Setting: botones naranjas + / -, Ok abajo a la derecha.
- Memoria (desde posición i16): HP +12, MP +16, SP +20, STR..CHR +32..+52, nivel +72, EXP +76, puntos sin repartir +80.
- Tipos de bicho: 240 = Slime, 120 = Giant Ant. Clase 0 = vivo y atacable; 1 = NPC/otro; 3 = cadáver.
- Aresden City: Warehouse (218,133)/(107,185), Shop (127,167), Blacksmith (169,194), Cityhall (144,123). Farm: oeste (20,26) → City; norte (119,23) → Promiseland (evitar).
- Hambre: campo +24 (0 = no regenera vida ni stamina). Usar item = doble click simple por mensajes con el bolso abierto. Bolso estacionado a la derecha (bag_box 820,270–1225,640).
- Shop Keeper camina: se lo ubica por hover (cartel azul "Neutral"). La lista "Item for Sale" NO se puede cerrar (ni ESC ni click derecho ni la cajita naranja); recuerda su posición; se la arrastra agarrando el encabezado entre ITEM y PRICE. Se cierra sola al salir del edificio. Si queda en el medio tapa los clicks: _park_shop_dialog la manda a la esquina de abajo a la derecha.
- Adentro de edificios la lista de entidades de memoria no aparece y _walk_to no logra salir: usar _exit_building (clicks simples en el piso).
- Bug resuelto 06:00: quedó 85 min trabado en la Shop (estado "loot" al entrar + rutina de stats con falsos botones naranjas en la madera).
- Viaje de compras: anda ~1 de cada 2 (a veces no abre el Trade); reintenta solo.

## Aprendido 2026-11-13 mañana
- El equipo se GASTA: al llegar a 0 se desequipa solo y queda en el bolso ("The item is not available" si se intenta poner roto). A las 06:40 se rompió la Gradius y quedó pegando a mano.
- Blacksmith: pj parado en (47,35) → Keeper en pantalla (745,345). Menú: Repair All (845,312) / Trade (625,364) / Sell / Talk. Repair All → lista con costos → Repair (902,574). Reparar todo costó 207.
- Comprado (STR pedido): Hauberk (M) 400 (12), Chain Hose (M) 400 (10), Helm (M) 800 (52), Wood Shield 100 (8), Scale Mail (M) 900 (20). Otros: Chain Mail 1200, Plate Mail 4500, Full-Helm 1500, Plate Leggings, Knight Hauberk 2400, Horned Helm 4000, Wings Helm 5000, Giant Sword 8000, Battle Axe 3500, Flameberge+1 3300, Black Shadow Sword 10000.
- Lista de venta: 13 filas visibles, fila k en y = 191 + 29k (x 272) con PRICE en (435,159); 1 muesca de rueda = 1 fila. Desde el fondo: +20 muescas = página de armaduras (Hauberk k8, Chain Mail k9, Scale k10, Plate k11, Chain Hose k12); +10 = Helm (M) k3; +30 = Wood Shield k12.
- En el bolso los items equipados siguen dibujados (más claros). Los items se tapan entre sí: arrastrar agarra el de arriba. Doble click en carne la come; en el mapa de piedra abre una ventana (se cierra con ESC).
- Drops que tiene: 2 Special Magic Wand (MS0), Agile Main Gauche, Dagger, mapa/piedra, pergamino. Bolso: Gradius en (978,462), Main Gauche (881,468), pantalón (1050,485), varitas a la derecha.
- ESC cierra ventanas del juego (no la lista de venta).
- Pasando el mouse por un arma/armadura en el bolso el juego muestra sus stats. Pedido: cada vez que levante algo, leerlo y ver si es mejor que lo puesto. Las varitas y la Agile Main Gauche NO se venden hasta que el dueño diga. Capas: se compran en el Shop Keeper.
- Pendiente con la PC: leer inventario (probar primero nombres en memoria con find_strings "Agile Main Gauche"; si no, hover + captura del cartel), comparar con lo equipado, equipar si es mejor, y marcar el resto como vender/guardar.
- Oro: es un item (montoncito verde/amarillo) dentro del bolso con el número al lado; 14.584 a las 10:37 del 13/11. Estaba tapado por otros items. Buscar ese valor en memoria (i32) para sacar memory.gold_off.
- Para ver los stats de un item del bolso NO alcanza con pasar el mouse: hay que hacer click sobre el item y MANTENER apretado; el cartel aparece mientras se sostiene. Soltar en el mismo lugar para no moverlo.
- Mapa del mundo base (data/mundo_base.json): portales de archivos públicos de servidor HB; coinciden con lo visto (Farm 20,26→City 275,205; 119,23→Promiseland 140,225; 75,86→Blacksmith 34,37; 63,70→Shop 50,39). Rutas: City→Eternal Field directo; Farm→Middleland Dungeon directo; Eternal Field→Silent Wood / Aresien Garden / Aresden Dungeon.
- CACHE del cliente = tablas que manda el servidor (formato FCBH v2: magic, ver, tipo, hash; cuerpo desde byte 84). Tipo 4 mapas (archivo→nombre, 80, completo), 3 NPC (125, completo), 2 skills (18), 1 magias (65), 0 items (solo el último paquete: 34 de 552; registros de 94 bytes). Guardado en data/abaddon_cache.json. Los items completos habría que sacarlos de la memoria del juego.
- Minimapas reales: sprites/interface/telescope.pak (formato pak: '<Pak file header>', n en byte 20, tabla (offset,size); cada sprite '<Sprite File Header>', n cuadros en +100, rects 12 bytes, luego PNG). 31 mapas a 1 px por baldosa; el bot los lee de la carpeta del juego (world.TELESCOPE). newmaps.pak = 10 pergaminos con carteles (Aresden, Elvine, Middleland, Promiseland, Eternal Field, Rocky Highland, Silent Wood, Death Valley, 2 farms).
- Teclas (wiki de helbreath.net, que es de OTRO server —Olympia—: confirmar en Abaddon): 1/Insert poción de vida, 2/Delete maná, 3 stamina; Tab ataque/paz; F5 personaje, F6 bolso, F7 magias, F8 skills; Ctrl+click en el bolso equipa el item; Shift+mouse levanta del piso; Esc cierra ventanas; Ctrl+R correr; /shiftpickup; /showxpm. OJO: Insert también es la tecla de prender/apagar del bot.

## Inventario y tabla de items desde la memoria (2026-11-13 mediodía)
- Inventario: 50 punteros de 8 bytes en posición+508 (jugador). Cada item es un objeto de 160 bytes: id i16 en +50, cantidad i32 en +112, durabilidad máxima i16 en +98, actual i16 en +134, lugar en el bolso (x,y) i16 en +78/+80. Pantalla (1280) ≈ (847 + 1.6·x, 289 + 1.6·y) con el bolso estacionado a la derecha. Los equipados siguen en la lista.
- Ids: Gold 90 (la cantidad es el oro), Health Potion 91, Mana Potion 93, Meat 99, Recall Scroll 114, Map 104, Stone of Merien 657.
- Tabla de items del juego (552): mismos objetos pero con nombre en +8; precio i32 +84, peso i32 +88 (STR pedido = peso/1000), nivel +92, durabilidad +98, tipo byte +52 (2 = equipable), lugar byte +54 (1 cabeza, 2 pecho, 3 brazos, 4 piernas, 5 pies, 6 cuello, 7 escudo, 8 arma 1 mano, 9 arma 2 manos, 10/11 anillos, 12 capa), daño dados +58/+60/+62 (y +64.. contra grandes), defensa +58. Guardada en data/items_abaddon.json.
- Cape 1000 de oro (Shop). Escudos: Leather 150, Targe 250, Iron 700, Kite 1500, Tower 1800 (STR 40, def 35). Plate Leggings (M) 1000 (STR 20, def 12). Chain Mail (M) STR 30 def 30. Falchion +1 nivel 35, 1D12+1.
- Middleland Dungeon se entra por la Farm (78,71). A nivel 35 con Scale Mail lo mataron 2 veces en 3 minutos (bichos tipo 250, 150, 210, 120).
- El herrero no siempre queda en el mismo píxel: buscarlo por hover (cartel azul "BlackSmith Keeper / Neutral").
- Equipar: doble click sobre el item en el bolso, FUERA de las tiendas (adentro del Blacksmith el doble click abre "Repair Item"). Con arma a dos manos el escudo se saca solo. Al equipar, el juego avisa "You equipped X" y el item deja de dibujarse en el bolso.
- El bolso se puede mover: se ubica buscando el texto amarillo "UPGRADE"; origen = (x+5, y-212) en pantalla de 1280.
- Lo puesto: ids (i16) al lado de la posición del pj: +192 cabeza, +196 pecho, +200 brazos, +204 piernas, +208 pies, +212 arma, +216 capa (0 = nada).
- Atributos raros de un item: bytes +138..+141 del objeto (tipo, valor, tipo secundario, valor). Tipos vistos: 2 Poisoning, 3 Righteous, 5 Agile, 7 Sharp, 10 Special.
- Listas completas de Shop y Blacksmith en data/listas_tiendas.json; una foto de cada fila en bot/assets/rows (se busca la fila por foto, nunca por posición).
- Vender: menú Sell del vendedor -> arrastrar cada item al cartel -> botón Sell. Sin el cartel abierto NO arrastrar (el item cae al piso).
- Si el bot huye, anota el tipo de bicho; solo lo esquiva si huyó 2 veces en 30 min, y por 45 min (antes una sola huida lo vetaba 5 niveles y dejó de cazar).
- OJO: el campo +180 de cada bicho en la lista de memoria NO es la especie: es la animación (240 = quieto, 63 = muriendo...). +176 es la acción. La especie no está en ese registro; por eso no se puede elegir "tal bicho" por memoria, se elige por mapa y zona.
- Escalera de caza: training_path.json -> phases[].maps en orden de preferencia. Esos mapas no se frenan por margen/peligro; solo por muertes (2 seguidas = 1 hora por muerte, o hasta subir 3 niveles).
- Mandados: estando lejos, vuelve a Aresden Farm solo si se quedó sin pociones/comida, el equipo está por romperse, puede comprar una mejora o tiene el bolso lleno.
- Regla 90/10: el bot cuenta los segundos de mandados (tiendas, viaje al pueblo) de la última hora; vender/comprar mejoras/reparar por rutina solo si queda cupo (10% = 6 min por hora). Urgencias (sin pociones, equipo por romperse, bolso lleno) no miran el cupo.
- Pociones: la tecla 1 se verifica por la cuenta de pociones en la memoria (antes se miraba si subía la vida y, en plena pelea, daba 'falló' y dejaba de tomar: causa probable de varias muertes). Con 2+ pociones pelea y toma hasta 18% de vida; sin pociones escapa a 35%.
- Zonas por experiencia (v78): al matar anota cuánta exp dio y en qué zona (celdas de 14 baldosas, learn['xpz']). Deja zonas pobres por las que rinden 1.4x o más, y si no conoce ninguna 1.8x mejor que la más floja explora 4 min cada 40 (learn['xpz_seen']).
- Atacar respeta obstáculos (v81): antes de pegarle a un bicho mira si hay línea libre; si no, camina por el camino que rodea (montañas, pozos) y si no hay camino lo ignora. Antes le apuntaba derecho y se trababa contra la montaña.
- Zonas de bichos por mapa (v82): data/spawns.json, sacado de los archivos públicos de servidor (spot-mob-generator: bicho, rectángulo, máximo). Coincide con lo visto en la Farm (slimes 116,80-138,102; hormigas 84,149-108,175). El bot va a la zona del bicho más fuerte que ya puede enfrentar (min_level <= nivel-4), la prueba, y se queda con la que más exp da medida.
- Base de criaturas (v83): data/criaturas.json (vida, defensa, daño, exp base, oro, zonas con coordenadas; stats clásicos). El bot enfrenta un bicho solo si su golpe promedio es <= 6% de mi vida y su defensa <= 30 + 1.5 x nivel; entre esos va al que más exp da (medida real manda).
- Morir: el botón Restart sale abajo a la derecha; si el minimapa quedó corrido ahí lo tapa y el pj no revive (pasó 67 min muerto en Eternal Field el 13/11). El bot ahora corre lo que tape el botón al 3er intento, y cuenta una sola muerte por muerte. Eternal Field con VIT 42 lo mató al entrar: umbral subido a VIT 50 (MD 60).
