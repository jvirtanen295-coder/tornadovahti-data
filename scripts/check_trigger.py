# -*- coding: utf-8 -*-
"""
Tornadovahti Pori - ukkospaivan automaattinen tunnistus.

Lukee juuri lasketun out/latest.json-tiedoston (jonka fetch_data.py teki)
ja katsoo, onko lahipaivina ukkos-/trombipotentiaalia. Jos on, kirjoittaa
GITHUB_OUTPUT-muuttujaan trigger=true, jolloin workflow kaynnistaa
ukkospaiva-seurannan automaattisesti.

Kynnys on tarkoituksella maltillinen: se laukeaa kun malli nakee
edes kohtalaista potentiaalia, jotta seuranta on paalla ajoissa.
"""
import json, os, sys

# Kynnysarvot: jos jokin toteutuu lahipaivina (0-1 vrk), seuranta paalle.
CAPE_MIN = 400       # J/kg - riittava energia ukkosille
SHEAR_MIN = 12       # m/s  - jarjestaytyneet solut
LI_MAX = -2          # Lifted Index negatiivinen = epavakaa


def main():
    trigger = False
    reason = "ei potentiaalia"
    try:
        with open("out/latest.json", encoding="utf-8") as f:
            j = json.load(f)
        days = j.get("days", [])[:2]          # tanaan + huomenna
        for i, d in enumerate(days):
            cape = d.get("cape")
            shear = d.get("shear")
            li = d.get("li")
            hot = (cape is not None and cape >= CAPE_MIN and
                   shear is not None and shear >= SHEAR_MIN)
            unstable = (li is not None and li <= LI_MAX and
                        cape is not None and cape >= CAPE_MIN)
            if hot or unstable:
                trigger = True
                reason = (f"paiva {i}: CAPE={cape} shear={shear} LI={li}")
                break
        print(f"Ukkospaiva-tarkistus: {'KYLLA' if trigger else 'ei'} ({reason})")
    except FileNotFoundError:
        print("latest.json puuttuu - ohitetaan")
    except Exception as e:
        print("Tarkistus epaonnistui:", e)

    # Kirjoita tulos workflow'n kaytettavaksi
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"trigger={'true' if trigger else 'false'}\n")


if __name__ == "__main__":
    main()
