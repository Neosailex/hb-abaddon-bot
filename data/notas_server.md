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
- (dato del dueño) Pasando el mouse por un arma/armadura en el bolso el juego muestra sus stats. Pedido: cada vez que levante algo, leerlo y ver si es mejor que lo puesto. Las varitas y la Agile Main Gauche NO se venden hasta que el dueño diga. Capas: se compran en el Shop Keeper.
- Pendiente con la PC: leer inventario (probar primero nombres en memoria con find_strings "Agile Main Gauche"; si no, hover + captura del cartel), comparar con lo equipado, equipar si es mejor, y marcar el resto como vender/guardar.
- Oro: es un item (montoncito verde/amarillo) dentro del bolso con el número al lado; 14.584 a las 10:37 del 13/11. Estaba tapado por otros items. Buscar ese valor en memoria (i32) para sacar memory.gold_off.
- (dato del dueño) Para ver los stats de un item del bolso NO alcanza con pasar el mouse: hay que hacer click sobre el item y MANTENER apretado; el cartel aparece mientras se sostiene. Soltar en el mismo lugar para no moverlo.
- Mapa del mundo base (data/mundo_base.json): portales de archivos públicos de servidor HB; coinciden con lo visto (Farm 20,26→City 275,205; 119,23→Promiseland 140,225; 75,86→Blacksmith 34,37; 63,70→Shop 50,39). Rutas: City→Eternal Field directo; Farm→Middleland Dungeon directo; Eternal Field→Silent Wood / Aresien Garden / Aresden Dungeon.
- CACHE del cliente = tablas que manda el servidor (formato FCBH v2: magic, ver, tipo, hash; cuerpo desde byte 84). Tipo 4 mapas (archivo→nombre, 80, completo), 3 NPC (125, completo), 2 skills (18), 1 magias (65), 0 items (solo el último paquete: 34 de 552; registros de 94 bytes). Guardado en data/abaddon_cache.json. Los items completos habría que sacarlos de la memoria del juego.
- Minimapas reales: sprites/interface/telescope.pak (formato pak: '<Pak file header>', n en byte 20, tabla (offset,size); cada sprite '<Sprite File Header>', n cuadros en +100, rects 12 bytes, luego PNG). 31 mapas a 1 px por baldosa; el bot los lee de la carpeta del juego (world.TELESCOPE). newmaps.pak = 10 pergaminos con carteles (Aresden, Elvine, Middleland, Promiseland, Eternal Field, Rocky Highland, Silent Wood, Death Valley, 2 farms).
