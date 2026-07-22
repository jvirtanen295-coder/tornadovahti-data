# -*- coding: utf-8 -*-
"""
Tornadovahti Pori — ilmainen "palvelinosa".
Hakee ECMWF:n avoimen mallidatan (IFS 0.25°, CC BY 4.0), laskee Porin pisteeseen
tuuliväänteen 0–~5,5 km, SRH:n 0–3 km ja Lifted Indexin, ja kirjoittaa
tuloksen tiedostoon out/latest.json (GitHub Pages jakaa sen sovellukselle).

Rehellinen huomio: profiili on harva (pinta + 925/850/700/500 hPa),
joten SRH on karkea arvio. Se on silti oikeaa mallidataa, ei keksittyä.
"""
import json, sys, datetime as dt
from zoneinfo import ZoneInfo
import numpy as np

LAT, LON = 61.49, 21.80          # Pori
STEPS = list(range(0, 73, 6))    # 0–72 h, 6 h välein
PLEVS = [925, 850, 700, 500]     # hPa
HKI = ZoneInfo("Europe/Helsinki")

def fetch_warnings():
    """Hakee IL:n viralliset varoitukset (CAP-syöte) ja poimii Satakuntaa
    koskevat. Selain ei voi hakea tätä suoraan tietoturvasyistä (CORS),
    mutta palvelin voi — siksi välitys tehdään täällä.
    Palauttaa listan (voi olla tyhjä = ei varoituksia) tai None jos haku epäonnistui."""
    import urllib.request, re, html
    try:
        req = urllib.request.Request(
            "https://alerts.fmi.fi/cap/feed/atom_fi-FI.xml",
            headers={"User-Agent": "tornadovahti-pori"})
        xml = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
        out = []
        for entry in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
            if not re.search(r"Satakunta|koko maa|Lounais", entry, re.I):
                continue
            def g(pat):
                m = re.search(pat, entry, re.S | re.I)
                return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else ""
            sev = g(r"<cap:severity>([^<]+)")
            out.append({
                "title": g(r"<title[^>]*>(.*?)</title>")[:120],
                "desc": g(r"<summary[^>]*>(.*?)</summary>")[:220],
                "lvl": 2 if re.search(r"Severe|Extreme", sev, re.I) else 1,
            })
            if len(out) >= 5:
                break
        print("Varoituksia Satakuntaan:", len(out))
        return out
    except Exception as e:
        print("Varoitusten haku epäonnistui:", e)
        return None


def main():
    from ecmwf.opendata import Client
    import cfgrib
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

    pl = {}
    for d in cfgrib.open_datasets("pl.grib"):
        for v in d.data_vars:
            pl[v] = point(d[v])
    sfc = {}
    for d in cfgrib.open_datasets("sfc.grib"):
        for v in d.data_vars:
            sfc[v] = point(d[v])

    need_pl, need_sfc = {"u", "v", "t"}, {"u10", "v10", "t2m", "d2m"}
    if not need_pl <= set(pl) or not need_sfc <= set(sfc):
        print("Puuttuvia muuttujia:", set(pl), set(sfc)); sys.exit(1)

    # korkeudet standardi-ilmakehästä (riittää SRH-arvioon)
    z = {p: float(mpcalc.pressure_to_height_std(p * units.hPa).to("m").m)
         for p in PLEVS}

    days = {}
    valid_times = pl["u"].valid_time.values
    for i in range(len(STEPS)):
        try:
            vt = np.datetime64(valid_times[i]).astype("datetime64[s]").astype(dt.datetime)
            vt = vt.replace(tzinfo=dt.timezone.utc).astimezone(HKI)
            u10 = float(sfc["u10"].isel(step=i)); v10 = float(sfc["v10"].isel(step=i))
            t2  = float(sfc["t2m"].isel(step=i)); d2  = float(sfc["d2m"].isel(step=i))
            uP = {p: float(pl["u"].isel(step=i).sel(isobaricInhPa=p)) for p in PLEVS}
            vP = {p: float(pl["v"].isel(step=i).sel(isobaricInhPa=p)) for p in PLEVS}
            t500 = float(pl["t"].isel(step=i).sel(isobaricInhPa=500))

            # 1) Väänne 0–~5,5 km: pinta vs 500 hPa
            shear = float(np.hypot(uP[500] - u10, vP[500] - v10))

            # 2) LI: nostetun paketin lämpötila 500 hPa:ssa vs ympäristö
            press = np.array([1000, 925, 850, 700, 500]) * units.hPa
            prof = mpcalc.parcel_profile(press, t2 * units.K, d2 * units.K)
            li = float((t500 * units.K - prof[-1]).to("delta_degC").m)

            # 3) SRH 0–3 km Bunkersin oikean liikkujan suhteen
            hgt = np.array([10.0, z[925], z[850], z[700], z[500]]) * units.m
            uu = np.array([u10, uP[925], uP[850], uP[700], uP[500]]) * units("m/s")
            vv = np.array([v10, vP[925], vP[850], vP[700], vP[500]]) * units("m/s")
            srh = None
            try:
                rm, _, _ = mpcalc.bunkers_storm_motion(press, uu, vv, hgt)
                pos, neg, tot = mpcalc.storm_relative_helicity(
                    hgt, uu, vv, depth=3000 * units.m,
                    storm_u=rm[0], storm_v=rm[1])
                srh = float(pos.m)
            except Exception as e:
                print("SRH ohitettu askeleella", i, "->", e)

            k = vt.date().isoformat()
            rec = days.setdefault(k, {"shear": [], "srh": [], "li": []})
            rec["shear"].append(shear)
            if srh is not None:
                rec["srh"].append(srh)
            rec["li"].append(li)
        except Exception as e:
            print("Askel", i, "ohitettu:", e)

    out_days = []
    for k in sorted(days):
        r = days[k]
        out_days.append({
            "date": k,
            "shear": round(max(r["shear"]), 1) if r["shear"] else None,
            "srh":   round(max(r["srh"]))      if r["srh"]   else None,
            "li":    round(min(r["li"]), 1)    if r["li"]    else None,
        })

    import os
    os.makedirs("out", exist_ok=True)
    payload = {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "place": "Pori",
        "source": "ECMWF open data (CC BY 4.0), IFS 0.25°",
        "note": "Yksinkertaistettu laskenta harvasta profiilista.",
        "days": out_days,
    }
    warns = fetch_warnings()
    if warns is not None:
        payload["warnings"] = warns
    with open("out/latest.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("Valmis:", json.dumps(payload["days"], ensure_ascii=False))
    if not out_days:
        sys.exit(1)

if __name__ == "__main__":
    main()
