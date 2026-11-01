#!/usr/bin/env python3
"""Taiwan-Warnsystem: Taifune, Wetterwarnungen und Erdbeben -> ntfy.sh.

Läuft alle 10 Minuten in GitHub Actions. Liest die CWA Open Data API,
vergleicht mit state.json und schickt nur Neues als Push-Nachricht.

Umgebungsvariablen:
  CWA_API_KEY   CWA-Autorisierungscode (Pflicht)
  NTFY_TOPIC    ntfy-Topic (Pflicht, außer bei DRY_RUN=1)
  NTFY_SERVER   optional, Standard https://ntfy.sh
  NTFY_TOKEN    optional, Access-Token für geschützte Topics
  DRY_RUN=1     Nachrichten nur auf der Konsole ausgeben (State -> state.dryrun.json)
  TEST_NOTIFY=1 Nur eine Test-Nachricht senden und beenden
  STATE_FILE    optional, Pfad zur State-Datei
"""

from __future__ import annotations

import json
import math
import os
import re
import ssl
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------
# Einstellungen (hier anpassen)
# --------------------------------------------------------------------------
EQ_MIN_MAGNITUDE = 4.5            # melden ab dieser Magnitude ...
EQ_MIN_INTENSITY_TAICHUNG = 3     # ... ODER ab dieser Intensität in Taichung
EQ_HIGH_PRIORITY_INTENSITY = 4    # hohe Priorität ab dieser Intensität in Taichung
EQ_URGENT_INTENSITY = 5           # höchste Priorität (ab 5- / 5弱)

TY_WATCH_RADIUS_KM = 1500         # Beobachtungsgebiet um Taichung
TY_DISTANCE_BANDS_KM = (1000, 500, 300, 100)  # Meldung bei Annäherung unter diese Distanzen
TY_ANNOUNCE_DEPRESSIONS = False   # auch tropische Tiefs (unter Sturmstärke) melden?

WARN_IGNORED_PHENOMENA = {"濃霧"}  # z. B. Nebelwarnungen ignorieren

FAIL_ALERT_AFTER = 6              # Warnung nach so vielen Fehlversuchen in Folge
MAX_SEEN_EVENTS = 200             # Größe der Erdbeben-Historie in state.json
MAX_EVENT_AGE_HOURS = 48          # ältere Erdbeben nie melden (Schutz bei State-Verlust)
HEARTBEAT_DAYS = 7                # "Läuft noch"-Nachricht alle X Tage (0 = aus)
HEARTBEAT_HOURS = range(9, 21)    # Heartbeat nur tagsüber (Ortszeit Taiwan)

HTTP_TIMEOUT = 20                 # Sekunden

TAICHUNG_NAMES = ("臺中市", "台中市")
TAICHUNG_LAT, TAICHUNG_LON = 24.1477, 120.6736

CWA_BASE = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/"
LINK_EQ_FALLBACK = "https://scweb.cwa.gov.tw/zh-tw/earthquake/data/"
LINK_TYPHOON = "https://www.cwa.gov.tw/V8/C/P/Typhoon/TY_NEWS.html"
LINK_WARN = "https://www.cwa.gov.tw/V8/C/W/County/County.html?CID=66"

TW_TZ = timezone(timedelta(hours=8))

# --------------------------------------------------------------------------
# Konfiguration aus der Umgebung
# --------------------------------------------------------------------------
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
TEST_NOTIFY = os.environ.get("TEST_NOTIFY", "") == "1"
CWA_API_KEY = os.environ.get("CWA_API_KEY", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")
STATE_FILE = Path(os.environ.get("STATE_FILE") or ("state.dryrun.json" if DRY_RUN else "state.json"))

_SECRETS = [s for s in (CWA_API_KEY, NTFY_TOPIC, NTFY_TOKEN) if s]


def scrub(text: object) -> str:
    """Entfernt Geheimnisse aus Texten, bevor sie geloggt werden."""
    s = str(text)
    for secret in _SECRETS:
        s = s.replace(secret, "***")
    return s


def log(msg: str) -> None:
    print(scrub(msg), flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class _CwaAdapter(HTTPAdapter):
    """Das CWA-Zertifikat ist nicht RFC-5280-strikt (fehlender Subject Key
    Identifier). Ab Python 3.13 schlägt die Prüfung deshalb fehl. Wir
    entfernen nur das Strict-Flag – die Zertifikatsprüfung bleibt aktiv."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context(cafile=requests.certs.where())
        ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


SESSION = requests.Session()
SESSION.headers["User-Agent"] = "taiwan-warnsystem/1.0 (+github actions)"
SESSION.mount(
    "https://opendata.cwa.gov.tw/",
    _CwaAdapter(max_retries=Retry(total=2, backoff_factor=2,
                                  status_forcelist=(500, 502, 503, 504),
                                  allowed_methods=("GET",))),
)


class CheckError(Exception):
    pass


def fetch_cwa(dataset: str, **params) -> dict:
    params = {k: v for k, v in params.items() if v is not None}
    params["Authorization"] = CWA_API_KEY
    params.setdefault("format", "JSON")
    try:
        r = SESSION.get(CWA_BASE + dataset, params=params, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        # Fehlermeldungen von requests enthalten die URL inkl. Key -> nur Typ ausgeben
        raise CheckError(f"{dataset}: Verbindungsfehler ({type(e).__name__})") from None
    if r.status_code in (401, 403):
        raise CheckError(f"{dataset}: HTTP {r.status_code} – API-Key ungültig?")
    if r.status_code != 200:
        raise CheckError(f"{dataset}: HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise CheckError(f"{dataset}: Antwort ist kein JSON") from None
    if str(g(data, "success", default="true")).lower() != "true":
        raise CheckError(f"{dataset}: success != true")
    return data


# --------------------------------------------------------------------------
# Defensiver JSON-Zugriff
# --------------------------------------------------------------------------
def g(obj, *path, default=None):
    """Sicherer verschachtelter Zugriff; Schlüssel ohne Beachtung der
    Groß-/Kleinschreibung (die CWA mischt camelCase und PascalCase)."""
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            if key in cur:
                cur = cur[key]
                continue
            lk = str(key).lower()
            for k, v in cur.items():
                if str(k).lower() == lk:
                    cur = v
                    break
            else:
                return default
        elif isinstance(cur, list) and isinstance(key, int):
            if -len(cur) <= key < len(cur):
                cur = cur[key]
            else:
                return default
        else:
            return default
    return default if cur is None else cur


def as_list(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def to_float(x) -> float | None:
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return None


def find_values(obj, key: str) -> list:
    """Alle Werte zu einem Schlüssel (rekursiv, case-insensitive)."""
    out, lk = [], key.lower()
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if str(k).lower() == lk and isinstance(v, (str, int, float)):
                    out.append(str(v))
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return out


def parse_time(s) -> datetime | None:
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            dt = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
        except ValueError:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=TW_TZ)
    return None


def fmt_time(s) -> str:
    dt = parse_time(s)
    return dt.astimezone(TW_TZ).strftime("%d.%m. %H:%M") if dt else str(s or "?")


def distance_km(lat1, lon1, lat2=TAICHUNG_LAT, lon2=TAICHUNG_LON) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(a))


def is_taichung(name) -> bool:
    return any(n in str(name or "") for n in TAICHUNG_NAMES)


# --------------------------------------------------------------------------
# Benachrichtigung
# --------------------------------------------------------------------------
def notify(title: str, message: str, priority: int = 3, tags=None, click: str | None = None) -> bool:
    if DRY_RUN:
        print("\n" + "─" * 50)
        print(f"[DRY_RUN] Priorität {priority} | Tags: {', '.join(tags or [])}")
        print(f"  {title}")
        for line in message.splitlines():
            print(f"  {line}")
        if click:
            print(f"  🔗 {click}")
        print("─" * 50, flush=True)
        return True
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message,
               "priority": priority, "tags": list(tags or [])}
    if click:
        payload["click"] = click
        payload["actions"] = [{"action": "view", "label": "Details", "url": click}]
    headers = {"Authorization": f"Bearer {NTFY_TOKEN}"} if NTFY_TOKEN else {}
    try:
        r = SESSION.post(NTFY_SERVER, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        log(f"Gesendet: {title}")
        return True
    except requests.RequestException as e:
        code = getattr(getattr(e, "response", None), "status_code", "")
        log(f"FEHLER beim Senden an ntfy ({type(e).__name__} {code})")
        return False


# --------------------------------------------------------------------------
# Erdbeben
# --------------------------------------------------------------------------
def parse_intensity(s) -> float | None:
    """'4級' -> 4, '5弱' -> 5.0, '5強' -> 5.5"""
    m = re.search(r"\d", str(s or ""))
    if not m:
        return None
    v = float(m.group())
    return v + 0.5 if "強" in str(s) else v


def intensity_label(v: float | None) -> str:
    if v is None:
        return "–"
    if v >= 5 and v < 7:
        return f"{int(v)}{'+' if v % 1 else '-'}"
    return str(int(v))


def check_earthquakes(state: dict, dataset: str, silent: bool) -> str:
    data = fetch_cwa(dataset, limit=20)
    quakes = as_list(g(data, "records", "Earthquake"))
    if not isinstance(g(data, "records"), dict):
        raise CheckError(f"{dataset}: 'records' fehlt")
    seen: list = state.setdefault("earthquakes", [])
    now = datetime.now(TW_TZ)
    new = 0

    def origin(q):
        return str(g(q, "EarthquakeInfo", "OriginTime", default=""))

    for q in sorted(quakes, key=origin):
        info = g(q, "EarthquakeInfo", default={})
        otime = origin(q)
        eq_id = f"{dataset[:8]}|{g(q, 'EarthquakeNo', default='?')}|{otime}"
        if not otime or eq_id in seen:
            continue

        mag = to_float(g(info, "EarthquakeMagnitude", "MagnitudeValue"))
        depth = to_float(g(info, "FocalDepth"))
        location = g(info, "Epicenter", "Location", default="unbekannt")

        tc_int, max_int = None, None
        for area in as_list(g(q, "Intensity", "ShakingArea")):
            v = parse_intensity(g(area, "AreaIntensity"))
            stations = as_list(g(area, "EqStation"))
            if v is None:  # Fallback: maximale Stationsintensität
                vals = [parse_intensity(g(s, "SeismicIntensity")) for s in stations]
                vals = [x for x in vals if x is not None]
                v = max(vals) if vals else None
            if v is None:
                continue
            max_int = v if max_int is None else max(max_int, v)
            if is_taichung(g(area, "CountyName")) or is_taichung(g(area, "AreaDesc")):
                tc_int = v if tc_int is None else max(tc_int, v)

        relevant = (mag is not None and mag >= EQ_MIN_MAGNITUDE) or \
                   (tc_int is not None and tc_int >= EQ_MIN_INTENSITY_TAICHUNG)
        dt = parse_time(otime)
        too_old = dt is not None and now - dt > timedelta(hours=MAX_EVENT_AGE_HOURS)

        sent = True
        if relevant and not silent and not too_old:
            if tc_int is not None and tc_int >= EQ_URGENT_INTENSITY:
                prio, tags = 5, ["rotating_light", "erdbeben"]
            elif tc_int is not None and tc_int >= EQ_HIGH_PRIORITY_INTENSITY:
                prio, tags = 4, ["warning", "erdbeben"]
            else:
                prio, tags = 3, ["erdbeben"]
            mag_s = f"M{mag:.1f}" if mag is not None else "M?"
            title = f"🌏 Erdbeben {mag_s}" + (f" · Taichung {intensity_label(tc_int)}" if tc_int else "")
            lines = [
                f"📍 {location}",
                f"🕒 {fmt_time(otime)} Uhr (Ortszeit)",
                f"⬇️ Tiefe: {depth:.0f} km" if depth is not None else "⬇️ Tiefe: ?",
                f"📳 Intensität Taichung: {intensity_label(tc_int)}"
                if tc_int is not None else "📳 Taichung: nicht gemeldet",
            ]
            if max_int is not None:
                lines.append(f"📊 Max. Intensität Taiwan: {intensity_label(max_int)}")
            link = g(q, "Web") or LINK_EQ_FALLBACK
            sent = notify(title, "\n".join(lines), prio, tags, str(link))
            new += sent
        if sent:  # bei Sendefehler nicht merken -> nächster Lauf versucht es erneut
            seen.append(eq_id)

    del seen[:-MAX_SEEN_EVENTS]
    return f"{len(quakes)} Berichte, {new} gemeldet"


# --------------------------------------------------------------------------
# Taifune (Zugbahn)
# --------------------------------------------------------------------------
CATEGORIES = [  # (ab m/s, Kategorie, Text)
    (51.0, 3, "starker Taifun (強烈颱風)"),
    (32.7, 2, "mittlerer Taifun (中度颱風)"),
    (17.2, 1, "leichter Taifun (輕度颱風)"),
    (0.0, 0, "tropisches Tief (熱帶性低氣壓)"),
]
_DIR = {"北": "N", "南": "S", "東": "O", "西": "W", "N": "N", "S": "S", "E": "O", "W": "W"}


def category(wind: float | None) -> tuple[int, str]:
    for limit, cat, text in CATEGORIES:
        if wind is not None and wind >= limit:
            return cat, text
    return 0, CATEGORIES[-1][2]


def direction_de(s) -> str:
    s = str(s or "").strip()
    if s and all(c in _DIR for c in s):
        d = "".join(_DIR[c] for c in s)
        if len(s) == 2 and s[0] in "東西":  # 西北 = NW
            d = d[::-1]
        return d
    return s or "?"


def fix_coords(fix) -> tuple[float, float] | None:
    lat = to_float(g(fix, "CoordinateLatitude"))
    lon = to_float(g(fix, "CoordinateLongitude"))
    if lat is None or lon is None:
        parts = str(g(fix, "Coordinate", default="")).split(",")
        if len(parts) == 2:
            lon, lat = to_float(parts[0]), to_float(parts[1])
    return (lat, lon) if lat is not None and lon is not None else None


def band_index(dist: float) -> int:
    return sum(1 for b in TY_DISTANCE_BANDS_KM if dist < b)


def check_typhoons(state: dict, silent: bool) -> str:
    data = fetch_cwa("W-C0034-005")
    if not isinstance(g(data, "records"), dict):
        raise CheckError("W-C0034-005: 'records' fehlt")
    cyclones = as_list(g(data, "records", "TropicalCyclones", "TropicalCyclone"))
    known: dict = state.setdefault("typhoons", {})
    today = datetime.now(TW_TZ).date().isoformat()
    present = set()
    msgs = 0

    for tc in cyclones:
        fixes = [f for f in as_list(g(tc, "AnalysisData", "Fix")) if fix_coords(f)]
        if not fixes:
            continue
        cur = max(fixes, key=lambda f: str(g(f, "DateTime", default="")))
        lat, lon = fix_coords(cur)
        year = g(tc, "Year") or str(g(cur, "DateTime", default=""))[:4]
        name_en, name_zh = g(tc, "TyphoonName", default=""), g(tc, "CwaTyphoonName", default="")
        # Die TD-Nummer bleibt stabil, auch wenn später ein Taifunname/-nummer dazukommt
        td_no = g(tc, "CwaTdNo")
        key = f"{year}-TD{td_no}" if td_no else f"{year}-{name_en or g(tc, 'CwaTyNo', default='?')}"
        present.add(key)

        name = " ".join(x for x in (str(name_en).title(), f"({name_zh})" if name_zh else "") if x)
        if not name:
            name = f"Tropisches Tief Nr. {g(tc, 'CwaTdNo', default='?')}"

        wind = to_float(g(cur, "MaxWindSpeed"))
        gust = to_float(g(cur, "MaxGustSpeed"))
        pres = to_float(g(cur, "Pressure"))
        cat, cat_text = category(wind)
        dist = distance_km(lat, lon)

        forecasts = []
        for f in as_list(g(tc, "ForecastData", "Fix")):
            c = fix_coords(f)
            hr = to_float(g(f, "ForecastHr") or g(f, "tau"))
            if c and hr is not None:
                forecasts.append((hr, c, distance_km(*c)))
        forecasts.sort()
        min_dist = min([dist] + [d for _, _, d in forecasts])
        in_area = min_dist <= TY_WATCH_RADIUS_KM
        strong_enough = TY_ANNOUNCE_DEPRESSIONS or cat >= 1 or bool(name_en)
        band = band_index(dist)

        prev = known.get(key)
        entry = prev or {"announced": False, "cat": cat, "band": 0}
        entry.pop("gone_since", None)
        entry["name"] = name

        def summary() -> str:
            lines = [
                f"📍 {lat:.1f}°N {lon:.1f}°E · {dist:.0f} km von Taichung",
                f"💨 {cat_text}",
                f"   Wind {wind:.0f} m/s ({wind * 3.6:.0f} km/h)" if wind is not None else "   Wind ?",
            ]
            if gust is not None:
                lines[-1] += f", Böen {gust * 3.6:.0f} km/h"
            if pres is not None:
                lines.append(f"   Kerndruck {pres:.0f} hPa")
            spd = to_float(g(cur, "MovingSpeed"))
            if spd is not None:
                lines.append(f"➡️ Zug: {direction_de(g(cur, 'MovingDirection'))} mit {spd:.0f} km/h")
            picks = [fc for fc in forecasts if fc[0] in (24, 48, 72)] or forecasts[:3]
            if picks:
                lines.append("🔮 Prognose:")
                for hr, (flat, flon), fd in picks[:3]:
                    lines.append(f"   +{hr:.0f}h: {flat:.1f}°N {flon:.1f}°E · {fd:.0f} km")
            return "\n".join(lines)

        if silent:
            entry.update(announced=in_area and strong_enough, cat=cat, band=band)
        elif not entry["announced"]:
            if in_area and strong_enough:
                prio = 4 if min_dist < 500 else 3
                if notify(f"🌀 Neuer Taifun: {name}", summary(), prio, ["cyclone"], LINK_TYPHOON):
                    entry.update(announced=True, cat=cat, band=band)
                    msgs += 1
            else:
                entry.update(cat=cat, band=band)
        else:
            reasons, prio = [], 3
            if cat != entry.get("cat"):
                verb = "verstärkt sich" if cat > entry.get("cat", 0) else "schwächt sich ab"
                reasons.append(f"{verb} → {cat_text}")
                if cat > entry.get("cat", 0) and cat >= 2:
                    prio = 4
            if band > entry.get("band", 0):
                reasons.append(f"jetzt unter {TY_DISTANCE_BANDS_KM[band - 1]} km von Taichung")
                prio = max(prio, 5 if TY_DISTANCE_BANDS_KM[band - 1] <= 100 else
                           4 if TY_DISTANCE_BANDS_KM[band - 1] <= 300 else 3)
            if reasons:
                body = "\n".join(f"⚠️ {r}" for r in reasons) + "\n" + summary()
                if notify(f"🌀 Taifun-Update: {name}", body, prio, ["cyclone"], LINK_TYPHOON):
                    entry["cat"] = cat
                    entry["band"] = max(band, entry.get("band", 0))
                    msgs += 1
        known[key] = entry

    for key, entry in list(known.items()):
        if key not in present:
            entry.setdefault("gone_since", today)
            if (datetime.fromisoformat(today) - datetime.fromisoformat(entry["gone_since"])).days > 7:
                del known[key]
    return f"{len(cyclones)} Wirbelstürme aktiv, {msgs} Meldungen"


# --------------------------------------------------------------------------
# Taifun-Warnstufe (See / Land)
# --------------------------------------------------------------------------
TY_STAGES = {"none": 0, "lifted": 1, "sea": 2, "sea_land": 3}


def classify_headline(h: str) -> str:
    if "解除" in h:
        return "lifted"
    if "陸上" in h:
        return "sea_land"
    if "海上" in h:
        return "sea"
    return "none"


def check_typhoon_warning(state: dict, silent: bool) -> str:
    data = fetch_cwa("W-C0034-001", expires="true")
    if not isinstance(g(data, "records"), dict):
        raise CheckError("W-C0034-001: 'records' fehlt")
    headlines = sorted({h.strip() for h in find_values(data, "headline") if "颱風" in h})
    stage = max((classify_headline(h) for h in headlines), key=TY_STAGES.get, default="none")
    prev = state.get("ty_warning", "none")
    names = sorted({n for n in find_values(data, "cwaTyphoonName") + find_values(data, "typhoonName") if n})
    name_s = f" ({', '.join(names)})" if names else ""

    if not silent and stage != prev:
        texts = {
            "sea_land": ("🚨 Taifun: See- UND Landwarnung" + name_s, 5, ["rotating_light", "cyclone"],
                         "Die CWA hat eine Landwarnung (海上陸上颱風警報) ausgegeben.\n"
                         "Vorräte prüfen, Balkon sichern, Meldungen verfolgen."),
            "sea": ("🌊 Taifun: Seewarnung" + name_s, 4, ["warning", "cyclone"],
                    "Die CWA hat eine Seewarnung (海上颱風警報) ausgegeben.\n"
                    "Eine Landwarnung kann folgen."),
            "lifted": ("✅ Taifunwarnung aufgehoben" + name_s, 3, ["white_check_mark"],
                       "Die CWA hat die Taifunwarnung aufgehoben (解除颱風警報)."),
        }
        if stage in texts:
            title, prio, tags, body = texts[stage]
            if headlines:
                body += "\n\n" + "\n".join(headlines[:3])
            if not notify(title, body, prio, tags, LINK_TYPHOON):
                return "Senden fehlgeschlagen"
        elif prev in ("sea", "sea_land"):  # ohne explizite Aufhebung verschwunden
            notify("✅ Keine aktive Taifunwarnung mehr", "Laut CWA ist aktuell keine Taifunwarnung aktiv.",
                   2, ["white_check_mark"], LINK_TYPHOON)
    state["ty_warning"] = stage
    return f"Stufe: {stage}"


# --------------------------------------------------------------------------
# Wetterwarnungen Taichung
# --------------------------------------------------------------------------
PHENOMENA_DE = {
    "大雨": ("🌧️", "Starkregen", 3),
    "豪雨": ("🌧️", "Heftiger Starkregen", 4),
    "大豪雨": ("⛈️", "Extremer Starkregen", 4),
    "超大豪雨": ("⛈️", "Extremster Starkregen", 5),
    "陸上強風": ("💨", "Sturm an Land", 3),
    "海上陸上颱風": ("🌀", "Taifun (See + Land)", 5),
    "海上颱風": ("🌀", "Taifun (See)", 3),
    "颱風": ("🌀", "Taifun", 4),
    "濃霧": ("🌫️", "Dichter Nebel", 2),
    "低溫": ("🥶", "Kälte", 3),
    "高溫": ("🥵", "Hitze", 3),
}


def check_warnings(state: dict, silent: bool) -> str:
    data = fetch_cwa("W-C0033-001", locationName="臺中市")
    locations = g(data, "records", "location")
    if locations is None:
        raise CheckError("W-C0033-001: 'records.location' fehlt")
    current: dict[str, dict] = {}
    for loc in as_list(locations):
        if not is_taichung(g(loc, "locationName")):
            continue
        for hz in as_list(g(loc, "hazardConditions", "hazards")):
            phen = str(g(hz, "info", "phenomena", default="")).strip()
            sig = str(g(hz, "info", "significance", default="")).strip()
            if not phen or phen in WARN_IGNORED_PHENOMENA:
                continue
            current[phen + sig] = {"phen": phen, "sig": sig,
                                   "end": g(hz, "validTime", "endTime")}
    prev = set(state.get("warnings", []))
    keep = set(current)

    if not silent:
        for key in sorted(set(current) - prev):
            w = current[key]
            emoji, de, prio = PHENOMENA_DE.get(w["phen"], ("⚠️", w["phen"], 3))
            body = f"{emoji} {de} – {w['phen']}{w['sig']}\n📍 Taichung"
            if w["end"]:
                body += f"\n⏳ gültig bis {fmt_time(w['end'])} Uhr"
            if not notify(f"⚠️ Warnung Taichung: {de}", body, prio, ["warning"], LINK_WARN):
                keep.discard(key)  # nächster Lauf versucht es erneut
        for key in sorted(prev - set(current)):
            notify("✅ Warnung aufgehoben", f"Taichung: {key} ist nicht mehr aktiv.",
                   2, ["white_check_mark"], LINK_WARN)
    state["warnings"] = sorted(keep)
    return f"{len(current)} aktive Warnungen"


# --------------------------------------------------------------------------
# Ablauf
# --------------------------------------------------------------------------
CHECKS = {
    "earthquake_felt": lambda st, s: check_earthquakes(st, "E-A0015-001", s),
    "earthquake_local": lambda st, s: check_earthquakes(st, "E-A0016-001", s),
    "typhoon_track": check_typhoons,
    "typhoon_warning": check_typhoon_warning,
    "weather_warnings": check_warnings,
}
CHECK_NAMES_DE = {
    "earthquake_felt": "Erdbeben (spürbar, E-A0015-001)",
    "earthquake_local": "Erdbeben (lokal, E-A0016-001)",
    "typhoon_track": "Taifun-Zugbahn (W-C0034-005)",
    "typhoon_warning": "Taifunwarnung (W-C0034-001)",
    "weather_warnings": "Wetterwarnungen (W-C0033-001)",
}


def load_state() -> tuple[dict, bool]:
    if not STATE_FILE.exists():
        return {}, True
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")), False
    except (OSError, ValueError):
        log("state.json ist beschädigt – starte neu (ohne Benachrichtigungen).")
        return {}, True


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(STATE_FILE)


def track_health(state: dict, name: str, ok: bool) -> None:
    h = state.setdefault("health", {}).setdefault(name, {"fails": 0, "alerted": False})
    if ok:
        if h.get("alerted"):
            notify("✅ Datenquelle wieder erreichbar", f"{CHECK_NAMES_DE[name]} funktioniert wieder.",
                   2, ["white_check_mark"])
        h.update(fails=0, alerted=False)
        return
    h["fails"] = h.get("fails", 0) + 1
    if h["fails"] >= FAIL_ALERT_AFTER and not h.get("alerted"):
        if notify("🛠️ Warnsystem: Datenquelle gestört",
                  f"{CHECK_NAMES_DE[name]} schlägt seit {h['fails']} Läufen fehl.\n"
                  "Details im GitHub-Actions-Log. Offizielle Warnungen weiter beachten!",
                  4, ["warning", "tools"]):
            h["alerted"] = True


def heartbeat(state: dict) -> None:
    if HEARTBEAT_DAYS <= 0:
        return
    now = datetime.now(TW_TZ)
    last = state.get("heartbeat")
    try:
        due = last is None or (now.date() - datetime.fromisoformat(last).date()).days >= HEARTBEAT_DAYS
    except ValueError:
        due = True
    if not due or now.hour not in HEARTBEAT_HOURS:
        return
    active = [e["name"] for e in state.get("typhoons", {}).values()
              if e.get("announced") and "gone_since" not in e]
    body = "Läuft weiter, alle 10 Minuten.\n"
    body += f"🌀 Taifune: {', '.join(active) if active else 'keine'}\n"
    body += f"⚠️ Warnungen Taichung: {', '.join(state.get('warnings', [])) or 'keine'}"
    if notify("💚 Warnsystem: Lebenszeichen", body, 1, ["green_heart"]):
        state["heartbeat"] = now.date().isoformat()


def main() -> int:
    if TEST_NOTIFY:
        if not NTFY_TOPIC and not DRY_RUN:
            log("NTFY_TOPIC fehlt.")
            return 1
        ok = notify("🧪 Testnachricht", "Wenn du das liest, funktioniert die Zustellung "
                    "vom Taiwan-Warnsystem auf dein Handy. 👍", 3, ["test_tube"])
        return 0 if ok else 1

    if not CWA_API_KEY:
        log("CWA_API_KEY fehlt (GitHub Secret oder Umgebungsvariable setzen).")
        return 1
    if not NTFY_TOPIC and not DRY_RUN:
        log("NTFY_TOPIC fehlt (GitHub Secret setzen) – oder DRY_RUN=1 zum Testen.")
        return 1

    state, first_run = load_state()
    state["schema"] = 1
    primed = set(state.get("primed", []))
    if DRY_RUN:
        log(f"DRY_RUN aktiv – nichts wird gesendet, State: {STATE_FILE}")
    if first_run:
        log("Erster Lauf: aktuelle Ereignisse werden nur als gesehen markiert.")

    for name, fn in CHECKS.items():
        silent = name not in primed  # neue/noch nie erfolgreiche Checks melden nichts Altes
        try:
            result = fn(state, silent)
            log(f"OK   {name}: {result}" + (" (stumm, initial)" if silent else ""))
            primed.add(name)
            track_health(state, name, True)
        except Exception as e:  # ein Check darf die anderen nie stoppen
            log(f"FEHLER {name}: {type(e).__name__}: {e}")
            track_health(state, name, False)
    state["primed"] = sorted(primed)

    if first_run:
        n_ty = sum(1 for e in state.get("typhoons", {}).values() if e.get("announced"))
        body = ("Ab jetzt bekommst du Meldungen zu Erdbeben, Taifunen und Wetterwarnungen "
                "für Taichung.\n"
                f"Aktuell gespeichert: {len(state.get('earthquakes', []))} Erdbebenberichte, "
                f"{n_ty} Taifun(e), {len(state.get('warnings', []))} Warnung(en).\n"
                "Ergänzung, kein Ersatz für offizielle Warnungen (Cell Broadcast)!")
        notify("✅ Warnsystem aktiv", body, 3, ["white_check_mark"])
        state["heartbeat"] = datetime.now(TW_TZ).date().isoformat()
    else:
        heartbeat(state)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
