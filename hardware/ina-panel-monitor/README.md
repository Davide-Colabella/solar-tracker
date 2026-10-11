# Solar Tracker — INA panel monitor

A standalone INA219 board for measuring voltage and current **after an external DC/DC converter**. The converter and rooftop panel are not part of this PCB.

| Characteristic | Design |
| --- | --- |
| Measured supply | Regulated 5 V DC |
| Continuous current | 2 A maximum design target |
| Sensor | INA219BIDR, SOIC-8 |
| Shunt | 0.1 Ω, 1%, at least 1 W, 2512 |
| Logic supply / I²C | 3.3 V / address `0x40` |
| PCB | 60 × 45 mm, two copper layers, 1.6 mm thickness |
| Main power traces | 2 mm; specify at least 35 µm copper |

These are prototype design assumptions, not specifications inferred from the panel's remaining 80% output. Select the external DC/DC only after obtaining the panel's Voc/Isc and required load power. This board must not be connected directly to the unknown rooftop panel: the INA219 has a 26 V maximum operating bus voltage. [TI INA219 datasheet](https://www.ti.com/lit/ds/symlink/ina219.pdf).

## Connections

```text
PV panel → external DC/DC → J1 → shunt R1 → J2 → 5 V load
                                      ↓
                                    INA219 → Arduino I²C
```

| Connector | Pin 1 | Pin 2 | Pin 3 | Pin 4 |
| --- | --- | --- | --- | --- |
| J1 — FROM DC/DC | +5 V input | GND | — | — |
| J2 — TO LOAD | Positive output | GND | — | — |
| J3 — Arduino | 3.3 V | GND | SDA | SCL |

All grounds are shared; this is not an isolated measurement board. J3 supplies the sensor logic separately from the measured 5 V path. Keep SDA/SCL at 3.3 V; the existing project's 5 V shield pullups need a compatible bus or level translation. Do not power J3 from 5 V while using the documented 3.3 V interface.

JP1 is open by default. Bridge it to enable the two local 4.7 kΩ pullups when the host bus does not already provide suitable pullups. A0 and A1 are tied to GND, selecting `0x40`.

## Measurement and firmware

The existing `sketch/sketch.ino` uses address `0x40` and `INA219_SHUNT_OHM = 0.1f`, matching this board. It reads bus voltage at the load side of the shunt and calculates current from the shunt voltage. Python already computes delivered power as `bus_v * current_ma / 1000`.

At 2 A the shunt drops 0.2 V and dissipates 0.4 W: a 5.0 V input therefore gives approximately 4.8 V at J2, before wiring losses. R1 must be a power current-sense resistor; a suitable family is [Vishay WSLT2512](https://www.vishay.com/doc/?30121=), in the 0.1 Ω / 1% / 1 W variant. The filter uses two 10 Ω resistors and a 100 nF differential capacitor. Sense tracks branch separately from the shunt pads; the 3.3 V supply has 100 nF and 1 µF bypass capacitors.

## Detecting reduced production

After the converter, the reading depends on load demand, converter losses and regulation. A lightly loaded system can show low power even with a clean panel. Compare measurements with a clean-panel baseline under similar irradiance, orientation, temperature and load; a drop is an indication to investigate, not proof of dirt. The firmware's current light/dark input is insufficient for quantitative irradiance comparisons: use a calibrated continuous light measurement for this purpose. A panel's existing 80% performance must be included in its baseline.

## KiCad and 3D

Open `ina-panel-monitor.kicad_pro`; the schematic and fully routed board share the same name. `SolarTracker.kicad_sym` and `sym-lib-table` supply the local schematic symbols. Install the standard KiCad 10 footprint and 3D libraries to view the assembled board.

- `ina-panel-monitor-3d.png`: rendered board.

Export schematic previews, a BOM and an assembled STEP model directly from KiCad when needed. JP1 is a solder bridge, not a component to purchase.

The design passed ERC and DRC with no violations, unconnected items or schematic parity issues when created. Reports can be regenerated in KiCad. These checks verify CAD rules and schematic agreement; current capability, heating and measurement accuracy still require tests on the assembled prototype.

![3D board preview](ina-panel-monitor-3d.png)
