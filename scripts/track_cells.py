# -*- coding: utf-8 -*-
"""
Tornadovahti Pori — ukkossolujen automaattinen tutkaseuranta.

Mitä tämä tekee, selkokielellä:
1. Hakee kaksi tuoreinta tutkakuvaa (RainViewer-komposiitti, sisältää FMI:n
   tutkat) mustavalkoisena versiona, jossa pikselin kirkkaus = sateen voimakkuus.
2. Etsii kuvasta "möykyt" eli yhtenäiset voimakkaan kaiun alueet = ukkossolut.
3. Vertaa kahta peräkkäistä kuvaa: paljonko kukin möykky siirtyi 5–10 minuutissa
   → suunta ja nopeus.
4. Kirjoittaa tuloksen tiedostoon out/cells.json, jonka sovellus lukee:
   solut ilmestyvät kartalle oikeina, nuolineen ja ETA-aikoineen.

Rehelliset rajat: sijainti on ±pari kilometriä, mesosyklonia (pyörimistä) EI
voida havaita ilman Doppler-dataa, ja tunnistuskynnys on arvio — säädä alla
olevia PX_-lukuja, jos soluja tunnistuu liikaa tai liian vähän.
"""
import io, json, math, sys, datetime as dt
import requests
import numpy as np
from PIL import Image
from scipy import ndimage

# Seuranta-alue (Satakunta + Selkämeri reunoineen)
LAT_MIN, LAT_MAX = 60.7, 62.5
LON_MIN, LON_MAX = 19.5, 23.6
ZOOM = 7

# RainViewerin mustavalkoskaala: kirkkaampi pikseli = kovempi sade.
# dBZ ≈ pikseli/2 − 32 (dokumentoitu muoto). Säädä tarvittaessa:
PX_STRONG = 134   # ≈ 35 dBZ — solun tunnistusraja
PX_INT2   = 154   # ≈ 45 dBZ — "voimakas"
PX_INT3   = 174   # ≈ 55 dBZ — "erittäin voimakas"
MIN_PIXELS = 12   # pienin hyväksytty möykky (~15–20 km²)
MATCH_KM   = 25   # sama solu, jos siirtymä alle tämän


def tile_range():
    n = 2 ** ZOOM
    def tx(lon): return (lon + 180.0) / 360.0 * n
    def ty(lat):
        r = math.radians(lat)
        return (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n
    x0, x1 = int(tx(LON_MIN)), int(tx(LON_MAX))
    y0, y1 = int(ty(LAT_MAX)), int(ty(LAT_MIN))
    return x0, x1, y0, y1, n


def px_to_latlon(px, py, x0, y0, n):
    X = x0 + px / 256.0
    Y = y0 + py / 256.0
    lon = X / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * Y / n))))
    return lat, lon


def fetch_frame(host, path, x0, x1, y0, y1):
    W = (x1 - x0 + 1) * 256
    H = (y1 - y0 + 1) * 256
    arr = np.zeros((H, W), dtype=np.uint8)
    for xi in range(x0, x1 + 1):
        for yi in range(y0, y1 + 1):
            url = f"{host}{path}/256/{ZOOM}/{xi}/{yi}/0/0_0.png"
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            im = Image.open(io.BytesIO(r.content)).convert("LA")
            a = np.array(im)                      # (256,256,2): harmaa + alpha
            g = a[:, :, 0].astype(np.uint8)
            g[a[:, :, 1] == 0] = 0                # läpinäkyvä = ei kaikua
            arr[(yi - y0) * 256:(yi - y0 + 1) * 256,
                (xi - x0) * 256:(xi - x0 + 1) * 256] = g
    return arr


def blobs(arr, x0, y0, n):
    mask = arr >= PX_STRONG
    lab, cnt = ndimage.label(mask)
    out = []
    for i in range(1, cnt + 1):
        ys, xs = np.where(lab == i)
        if len(xs) < MIN_PIXELS:
            continue
        peak = int(arr[ys, xs].max())
        cy, cx = float(ys.mean()), float(xs.mean())
        lat, lon = px_to_latlon(cx, cy, x0, y0, n)
        out.append({"lat": lat, "lon": lon, "px": peak, "size": int(len(xs))})
    out.sort(key=lambda b: -b["px"])
    return out[:8]


def dist_km(a, b):
    R = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def bearing(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def main():
    j = requests.get("https://api.rainviewer.com/public/weather-maps.json",
                     timeout=20).json()
    host = j.get("host", "https://tilecache.rainviewer.com")
    past = (j.get("radar", {}).get("past") or [])[-2:]
    if len(past) < 2:
        print("Ei riittävästi tutkakuvia"); sys.exit(0)

    x0, x1, y0, y1, n = tile_range()
    f0 = fetch_frame(host, past[0]["path"], x0, x1, y0, y1)
    f1 = fetch_frame(host, past[1]["path"], x0, x1, y0, y1)
    b0 = blobs(f0, x0, y0, n)
    b1 = blobs(f1, x0, y0, n)
    dt_min = max(1.0, (past[1]["time"] - past[0]["time"]) / 60.0)
    print(f"Möykkyjä: edellinen {len(b0)}, uusin {len(b1)}, väli {dt_min:.0f} min")

    matched_v = []
    cells = []
    for b in b1:
        best, bd = None, 1e9
        for a in b0:
            d = dist_km((a["lat"], a["lon"]), (b["lat"], b["lon"]))
            if d < bd:
                best, bd = a, d
        rec = {"lat": round(b["lat"], 4), "lng": round(b["lon"], 4),
               "px": b["px"], "size": b["size"],
               "int": 3 if b["px"] >= PX_INT3 else 2 if b["px"] >= PX_INT2 else 1}
        if best and bd <= MATCH_KM:
            spd = bd * 1000 / (dt_min * 60)                     # m/s
            rec["dir"] = round(bearing((best["lat"], best["lon"]),
                                       (b["lat"], b["lon"])), 0)
            rec["spd"] = round(min(40.0, spd), 1)
            matched_v.append((rec["dir"], rec["spd"]))
        cells.append(rec)

    # Uusille möykyille keskimääräinen liike (tai maltillinen oletus lounaasta)
    if matched_v:
        md = sum(v[0] for v in matched_v) / len(matched_v)
        ms = sum(v[1] for v in matched_v) / len(matched_v)
    else:
        md, ms = 45.0, 8.0
    for rec in cells:
        rec.setdefault("dir", round(md, 0))
        rec.setdefault("spd", round(ms, 1))

    for i, rec in enumerate(cells[:6]):
        rec["id"] = chr(65 + i)

    import os
    os.makedirs("out", exist_ok=True)
    payload = {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "frame_time": dt.datetime.fromtimestamp(
            past[1]["time"], dt.timezone.utc).isoformat(timespec="minutes"),
        "source": "RainViewer-komposiitti (sis. FMI:n tutkat)",
        "note": "Automaattinen tunnistus; sijainti ±2 km, ei Doppler-rotaatiota.",
        "cells": cells[:6],
    }
    with open("out/cells.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("Valmis:", json.dumps(payload["cells"], ensure_ascii=False))


if __name__ == "__main__":
    main()
