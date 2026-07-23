# -*- coding: utf-8 -*-
"""
Tornadovahti Pori — ilmainen "palvelinosa".

Tekee kaksi asiaa:
1. Hakee ECMWF:n avoimen mallidatan (IFS 0.25 astetta, CC BY 4.0) ja laskee
   Porin pisteeseen tuuliväänteen, SRH:n 0-3 km ja Lifted Indexin.
2. Hakee Ilmatieteen laitoksen viralliset varoitukset (CAP-syöte) ja poimii
   Satakuntaa koskevat. Selain ei voi hakea niitä suoraan (CORS-rajoitus),
   siksi välitys tehdään täällä.

Tulos kirjoitetaan tiedostoon out/latest.json, jonka GitHub Pages jakaa.
Varoitukset kirjoitetaan aina, vaikka mallilaskenta epäonnistuisi.
"""
import json, math, os, re, sys, html, datetime as dt
from zoneinfo import ZoneInfo

LAT, LON = 61.49, 21.80          # Pori
STEPS = list(range(0, 73, 6))    # 0-72 h, 6 h valein
PLEVS = [925, 850, 700, 500]     # hPa
HKI = ZoneInfo("Europe/Helsinki")

# Satakuntaa koskevat varoitukset (myos merialueet ja koko maan varoitukset)
ALUE = re.compile(
    r"Satakun|koko maa|Lounais|L[aä]nsi-Suom|Selk[aä]mer|Pori|Bj[oö]rneborg|Rauma|Merikarvia",
    re.I)

FEEDS = [
    "https://alerts.fmi.fi/cap/feed/atom_fi-FI.xml",
    "https://alerts.fmi.fi/cap/feed/rss_fi-FI.rss",
    "https://alerts.fmi.fi/cap/feed/atom_en-GB.xml",
]


def get_text(url, timeout=25):
    """Hakee sivun tekstina. Kokeilee requests-kirjastoa, sitten urllibia."""
    try:
        import requests
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "tornadovahti-pori/1.0"})
        r.raise_for_status()
        r.encoding = r.encoding or "utf-8"
        return r.text
    except Exception as e1:
        try:
            import urllib.request
            req = urllib.request.Request(
                url, headers={"User-Agent": "tornadovahti-pori/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "ignore")
        except Exception as e2:
            print(f"  haku epaonnistui ({url}): {e1} / {e2}")
            return None


def clean(x):
    return html.unescape(re.sub(r"<[^>]+>", " ", x)).replace("&nbsp;", " ").strip()


def fetch_warnings():
    """Palauttaa listan varoituksia (voi olla tyhja = ei varoituksia voimassa)
    tai None jos yhtakaan syotetta ei saatu haettua."""
    for url in FEEDS:
        xml = get_text(url)
        if not xml:
            continue
        # Atom kayttaa <entry>, RSS kayttaa <item>
        blocks = re.findall(r"<entry[\s>].*?</entry>", xml, re.S)
        if not blocks:
            blocks = re.findall(r"<item[\s>].*?</item>", xml, re.S)
        print(f"  {url}: {len(blocks)} varoitusta syotteessa")
        out = []
        for b in blocks:
            # puretaan HTML-koodatut merkit (esim. Selk&#228;meri -> Selkameri)
            btxt = html.unescape(b)
            if not ALUE.search(btxt):
                continue
            m_t = re.search(r"<title[^>]*>(.*?)</title>", b, re.S | re.I)
            m_d = (re.search(r"<summary[^>]*>(.*?)</summary>", b, re.S | re.I)
                   or re.search(r"<description[^>]*>(.*?)</description>", b, re.S | re.I)
                   or re.search(r"<cap:description>(.*?)</cap:description>", b, re.S | re.I))
            m_s = re.search(r"<cap:severity>([^<]+)", b, re.I)
            sev = (m_s.group(1) if m_s else "")
            out.append({
                "title": clean(m_t.group(1))[:120] if m_t else "Varoitus",
                "desc": clean(m_d.group(1))[:220] if m_d else "",
                "lvl": 2 if re.search(r"Severe|Extreme", sev, re.I) else 1,
            })
            if len(out) >= 5:
                break
        print(f"  -> Satakuntaa koskevia: {len(out)}")
        return out
    return None


def compute_model():
    """Palauttaa listan paivakohtaisia arvoja tai tyhjan listan."""
    from ecmwf.opendata import Client
    import cfgrib
    import numpy as np
    import metpy.calc as mpcalc
    from metpy.units import units

    c = Client(source="ecmwf")
    print("Ladataan painepintadata...")
    c.retrieve(type="fc", step=STEPS, param=["u", "v", "t"],
               levelist=PLEVS, target="pl.grib")
    print("Ladataan pintadata...")
    c.retrieve(type="fc", step=STEPS, param=["10u", "10v", "2t", "2d"],
               target="sfc.grib")

    def point(da):
        return da.sel(latitude=LAT, longitude=LON, method="nearest")

    pl, sfc = {}, {}
    for d in cfgrib.open_datasets("pl.grib"):
        for v in d.data_vars:
            pl[v] = point(d[v])
    for d in cfgrib.open_datasets("sfc.grib"):
        for v in d.data_vars:
            sfc[v] = point(d[v])

    if not {"u", "v", "t"} <= set(pl) or not {"u10", "v10", "t2m", "d2m"} <= set(sfc):
        print("Puuttuvia muuttujia:", set(pl), set(sfc))
        return []

    z = {p: float(mpcalc.pressure_to_height_std(p * units.hPa).to("m").m)
         for p in PLEVS}
    days = {}
    valid_times = pl["u"].valid_time.values

    for i in range(len(STEPS)):
        try:
            vt = np.datetime64(valid_times[i]).astype("datetime64[s]").astype(dt.datetime)
            vt = vt.replace(tzinfo=dt.timezone.utc).astimezone(HKI)
            u10 = float(sfc["u10"].isel(step=i)); v10 = float(sfc["v10"].isel(step=i))
            t2 = float(sfc["t2m"].isel(step=i)); d2 = float(sfc["d2m"].isel(step=i))
            uP = {p: float(pl["u"].isel(step=i).sel(isobaricInhPa=p)) for p in PLEVS}
            vP = {p: float(pl["v"].isel(step=i).sel(isobaricInhPa=p)) for p in PLEVS}
            t500 = float(pl["t"].isel(step=i).sel(isobaricInhPa=500))

            shear = float(np.hypot(uP[500] - u10, vP[500] - v10))

            press = np.array([1000, 925, 850, 700, 500]) * units.hPa
            prof = mpcalc.parcel_profile(press, t2 * units.K, d2 * units.K)
            li = float((t500 * units.K - prof[-1]).to("delta_degC").m)

            hgt = np.array([10.0, z[925], z[850], z[700], z[500]]) * units.m
            uu = np.array([u10, uP[925], uP[850], uP[700], uP[500]]) * units("m/s")
            vv = np.array([v10, vP[925], vP[850], vP[700], vP[500]]) * units("m/s")
            srh = None
            try:
                rm, _, _ = mpcalc.bunkers_storm_motion(press, uu, vv, hgt)
                pos, _, _ = mpcalc.storm_relative_helicity(
                    hgt, uu, vv, depth=3000 * units.m,
                    storm_u=rm[0], storm_v=rm[1])
                srh = float(pos.m)
            except Exception as e:
                print("  SRH ohitettu askeleella", i, "->", e)

            k = vt.date().isoformat()
            rec = days.setdefault(k, {"shear": [], "srh": [], "li": []})
            rec["shear"].append(shear)
            if srh is not None:
                rec["srh"].append(srh)
            rec["li"].append(li)
        except Exception as e:
            print("  Askel", i, "ohitettu:", e)

    out_days = []
    for k in sorted(days):
        r = days[k]
        out_days.append({
            "date": k,
            "shear": round(max(r["shear"]), 1) if r["shear"] else None,
            "srh": round(max(r["srh"])) if r["srh"] else None,
            "li": round(min(r["li"]), 1) if r["li"] else None,
        })
    return out_days


def main():
    print("Haetaan viralliset varoitukset...")
    warns = fetch_warnings()

    out_days = []
    try:
        out_days = compute_model()
    except Exception as e:
        print("Mallilaskenta epaonnistui:", e)

    os.makedirs("out", exist_ok=True)
    payload = {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "place": "Pori",
        "source": "ECMWF open data (CC BY 4.0) + Ilmatieteen laitos CAP",
        "note": "Yksinkertaistettu laskenta harvasta profiilista.",
        "days": out_days,
    }
    if warns is not None:
        payload["warnings"] = warns
        payload["warnings_ok"] = True
    else:
        payload["warnings_ok"] = False

    with open("out/latest.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    print("Valmis. Paivia:", len(out_days),
          "| varoituksia:", "ei haettu" if warns is None else len(warns))
    if not out_days and warns is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
