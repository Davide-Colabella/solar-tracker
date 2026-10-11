# Solar Tracker — comando di quattro elettrovalvole a 5 V

La scheda usa un **L293D DIP-16** per comandare quattro elettrovalvole normalmente chiuse a **5 V DC**, esterne al PCB. L'acqua arriva da un serbatoio per gravità. Non sono presenti LED, resistenze in serie alle bobine o diodi esterni.

## Progetto

- [Progetto KiCad](hardware/valve-controller/valve-controller.kicad_pro).
- [Schematico](hardware/valve-controller/valve-controller.kicad_sch) e [anteprima SVG](hardware/valve-controller/valve-controller.svg).
- [PCB con piste](hardware/valve-controller/valve-controller.kicad_pcb).
- [Render 3D](hardware/valve-controller/valve-controller-render.png).

Il PCB misura **80 × 55 mm**, con spessore 1,6 mm, due strati di rame e quattro fori M3. I canali sono numerati **1, 2, 3, 4** sotto i morsetti J2–J5. Le piste sono da 0,3 mm per i segnali, 0,6 mm per le uscite e la logica, 0,8 mm per l'alimentazione delle valvole e GND; i via sono da 0,8 mm con foro da 0,4 mm.

## Alimentazione

| Pin L293D | Collegamento |
| --- | --- |
| 16 — VCC1 | 5 V Arduino, rete `+5V` |
| 1 e 9 — Enable | 5 V Arduino, canali abilitati |
| 8 — VCC2 | Alimentatore esterno a 5 V DC, tramite J6, rete `+5V_VALVES` |
| 4, 5, 12, 13 | GND comune ad Arduino e alimentatore esterno |

Le due reti a 5 V sono distinte: la scheda non collega direttamente fra loro l'uscita 5 V Arduino e il positivo dell'alimentatore delle valvole. J6 ha il contatto 1 per il positivo e il contatto 2 per GND.

C1 e C2 sono condensatori da 100 nF vicino ai pin 16 e 8. Il L293D contiene i diodi di clamp per i carichi induttivi.

## Canali

| Numero | Pin Arduino | Input L293D | Output L293D | Morsetto | Valvola |
| --- | --- | --- | --- | --- | --- |
| 1 | A0 | 2 | 3 | J2 | Y1 |
| 2 | A1 | 7 | 6 | J3 | Y2 |
| 3 | A2, uscita digitale | 10 | 11 | J4 | Y3 |
| 4 | A3, uscita digitale | 15 | 14 | J5 | Y4 |

Il contatto 1 di ogni morsetto è l'uscita; il contatto 2 è GND. HIGH alimenta la bobina, LOW la disalimenta. J1 è il connettore verso Arduino: i suoi contatti sono, nell'ordine, 5 V, GND, A0, A1, A2, A3.

I pin corrispondono a `PIN_VALVE` nello [sketch](sketch/sketch.ino). Per l'impiego fisico delle valvole impostare `HAS_VALVE = 1`; il firmware non è stato modificato durante lo sbroglio.

## Visualizzazione 3D

Aprire il progetto, entrare nell'editor PCB e scegliere **Visualizza → Visualizzatore 3D** (`Alt+3`). Installare le librerie standard di modelli 3D di KiCad 10 per visualizzare L293D, morsetti, connettore e condensatori. Il modello STEP completo della scheda si può esportare da KiCad per usarlo in FreeCAD.

Le valvole restano componenti esterni e non sono incluse nel render del PCB. Il loro modello specifico non è stato selezionato né verificato, come richiesto. Vedere [valves.md](hardware/valve-controller/valves.md).

## Controlli

Lo schematico, il PCB e il render sono elaborati con KiCad 10.0.6. Il DRC eseguito alla creazione riporta **zero violazioni, zero collegamenti mancanti e zero differenze rispetto allo schematico**; il rapporto può essere rigenerato da KiCad. Le verifiche riguardano il progetto CAD; non è stata verificata la compatibilità di un modello commerciale di elettrovalvola. I morsetti adottano il footprint Phoenix MKDS-1,5-2 a passo 5 mm.
