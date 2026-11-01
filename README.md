# 🌀 Taiwan-Warnsystem (Taichung)

Kleines, wartungsarmes Warnsystem: Es prüft alle 10 Minuten die offenen Daten der
**Central Weather Administration (CWA)** und schickt dir eine Push-Nachricht über
[ntfy](https://ntfy.sh) aufs Handy, bei

- 🌏 **Erdbeben** ab Magnitude 4.5 **oder** ab Intensität 3 in Taichung
- 🌀 **Taifunen** im Umkreis von 1500 km (neu, Verstärkung, Annäherung auf 1000/500/300/100 km)
- 🌊 **Taifunwarnungen** der CWA (Seewarnung → See- und Landwarnung → Aufhebung)
- ⚠️ **Wetterwarnungen für Taichung** (Starkregen, Sturm, Taifun …), neu oder Stufenwechsel

> ⚠️ **Wichtig:** Das ist eine private **Ergänzung**, kein Ersatz für offizielle Warnungen.
> Taiwan warnt vor starken Erdbeben per **Cell Broadcast** (國家級警報 / 地震速報). Das ist
> schneller als dieses System, das bis zu ~10–20 Minuten verzögert ist. GitHub-Zeitpläne
> können sich zusätzlich verspäten oder ausfallen.

```
CWA Open Data API ──▶ warn.py (GitHub Actions, alle 10 min) ──▶ ntfy.sh ──▶ 📱
                          │
                          └─ state.json (bereits Gemeldetes, wird zurückcommittet)
```

Kosten: 0 €. Ein öffentliches Repo hat unbegrenzte Actions-Minuten.

---

## Einrichtung Schritt für Schritt

### 1. CWA-API-Key besorgen
1. Auf <https://opendata.cwa.gov.tw> registrieren und einloggen.
2. Oben rechts „取得授權碼“ (API-Autorisierungscode) öffnen und den Code kopieren
   (Format `CWA-XXXXXXXX-…`).

### 2. ntfy-App einrichten
1. **ntfy** installieren ([Android/Play Store](https://play.google.com/store/apps/details?id=io.heckel.ntfy),
   [F-Droid](https://f-droid.org/packages/io.heckel.ntfy/),
   [iOS](https://apps.apple.com/app/ntfy/id1625396347)).
2. Einen **langen, zufälligen Topic-Namen** erzeugen (mind. 20 Zeichen). Wer den Namen kennt,
   kann deine Nachrichten lesen und dir welche schicken. Im Terminal zum Beispiel:
   ```bash
   python3 -c "import secrets; print('tw-warn-' + secrets.token_urlsafe(18))"
   ```
3. In der App auf **+** tippen, den Topic-Namen eintragen und abonnieren.
4. Android: in den Einstellungen des Topics „Instant delivery“ aktivieren und die
   Akku-Optimierung für ntfy ausschalten, sonst kommen Nachrichten verzögert an.
   Bei Priorität 5 (🚨) dürfen Nachrichten „Bitte nicht stören“ durchbrechen.

### 3. Repository auf GitHub
1. Repository auf GitHub anlegen, **öffentlich** (Public). Bei privaten Repos ist das
   Kontingent für Actions-Minuten zu klein für einen 10-Minuten-Takt.
2. Code pushen (siehe unten „Lokal testen“).

### 4. Secrets eintragen
GitHub → Repository → **Settings → Secrets and variables → Actions → New repository secret**:

| Name           | Wert                                   |
|----------------|----------------------------------------|
| `CWA_API_KEY`  | dein CWA-Autorisierungscode            |
| `NTFY_TOPIC`   | dein zufälliger Topic-Name             |
| `NTFY_TOKEN`   | *(optional)* Access-Token, falls du ein geschütztes Topic bzw. einen eigenen ntfy-Server nutzt |

Secrets landen nie im Code. GitHub maskiert sie zusätzlich in den Logs.

### 5. Workflow starten und prüfen
1. Tab **Actions**. Falls nötig, „I understand my workflows, go ahead and enable them“ klicken.
2. Workflow **Taiwan-Warnsystem → Run workflow**:
   - zuerst mit Haken bei **„Nur eine Test-Nachricht ans Handy senden“**. Aufs Handy kommt
     dann 🧪 *Testnachricht*.
   - dann ohne Haken: Das ist der **erste echte Lauf**. Er merkt sich alle aktuellen Ereignisse
     still und schickt genau eine Nachricht ✅ *Warnsystem aktiv*.
3. Im Lauf-Log sollten fünf Zeilen `OK …` stehen, und im Repo erscheint ein Commit
   `state: update` mit der neuen `state.json`.
4. Ab jetzt läuft alles automatisch alle 10 Minuten.

---

## Lokal testen (DRY_RUN)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export CWA_API_KEY="…"        # nur in deiner Shell, nie in eine Datei im Repo
DRY_RUN=1 python warn.py      # gibt Nachrichten nur aus, sendet nichts
```

Im DRY_RUN wird der State nach `state.dryrun.json` geschrieben (per `.gitignore`
ausgeschlossen), damit die echte `state.json` unberührt bleibt. Der erste DRY_RUN-Lauf
ist „stumm“; erst ab dem zweiten Lauf erscheinen neue Ereignisse. Zum erneuten Testen
`state.dryrun.json` löschen.

Test-Nachricht von lokal: `NTFY_TOPIC="…" TEST_NOTIFY=1 python warn.py`

---

## Anpassen

Alle Schwellenwerte stehen oben in `warn.py`:

| Konstante                     | Standard | Bedeutung |
|-------------------------------|---------:|-----------|
| `EQ_MIN_MAGNITUDE`            | 4.5      | Erdbeben ab dieser Magnitude melden |
| `EQ_MIN_INTENSITY_TAICHUNG`   | 3        | … oder ab dieser Intensität in Taichung |
| `EQ_HIGH_PRIORITY_INTENSITY`  | 4        | hohe Priorität (4) ab dieser Intensität |
| `EQ_URGENT_INTENSITY`         | 5        | höchste Priorität (5) ab Intensität 5- |
| `TY_WATCH_RADIUS_KM`          | 1500     | Beobachtungsgebiet um Taichung (aktuell oder Prognose) |
| `TY_DISTANCE_BANDS_KM`        | 1000, 500, 300, 100 | Meldung bei Annäherung |
| `WARN_IGNORED_PHENOMENA`      | 濃霧     | ignorierte Warnarten (Nebel) |
| `FAIL_ALERT_AFTER`            | 6        | Störungsmeldung nach X Fehlversuchen in Folge (≈ 1 h) |
| `HEARTBEAT_DAYS`              | 7        | wöchentliches 💚 Lebenszeichen (0 = aus) |

## Verwendete Datensätze

| ID            | Inhalt |
|---------------|--------|
| `E-A0015-001` | 顯著有感地震報告: spürbare, nummerierte Erdbeben mit Intensitäten |
| `E-A0016-001` | 小區域有感地震報告: kleine, lokale Erdbeben |
| `W-C0034-005` | 熱帶氣旋路徑: Position, Stärke und Zugbahnprognose aller Wirbelstürme im NW-Pazifik |
| `W-C0034-001` | 颱風警報: offizielle Taifun-Warnstufe (See / See+Land / Aufhebung) |
| `W-C0033-001` | 天氣特報: aktuelle Wetterwarnungen pro Landkreis (hier 臺中市) |

## Wie es funktioniert

- **Keine Duplikate:** `state.json` speichert gemeldete Erdbeben (max. 200), bekannte
  Taifune mit Stufe/Distanz und die aktiven Warnungen. Die Datei enthält nur öffentliche
  Ereignis-IDs, keine Geheimnisse.
- **Erster Lauf:** Existiert keine `state.json`, wird alles Vorhandene still gemerkt.
  Dasselbe gilt für einen Check, der noch nie erfolgreich lief.
- **Robust:** Jeder Check läuft einzeln. Ein Fehler stoppt die anderen nicht. Schlägt eine
  Quelle 6× in Folge fehl, kommt einmalig 🛠️ *Datenquelle gestört* und bei Erholung ✅.
- **Sendefehler:** Klappt der Versand an ntfy nicht, wird das Ereignis nicht als gemeldet
  markiert und beim nächsten Lauf erneut versucht. Erdbeben älter als 48 h werden nie gemeldet.
- **TLS-Hinweis:** Das CWA-Zertifikat erfüllt die strikte RFC-5280-Prüfung von Python ≥ 3.13
  nicht. `warn.py` lockert nur dieses eine Flag. Die Zertifikatsprüfung selbst bleibt aktiv.

## Läuft es noch? (60-Tage-Regel)

GitHub **deaktiviert geplante Workflows in öffentlichen Repos nach 60 Tagen ohne
Repository-Aktivität**. Dagegen hilft:

- Das System schickt jede Woche ein 💚 *Lebenszeichen* (Priorität 1, ohne Ton), und dabei
  ändert sich `state.json`. Daraus entsteht mindestens ein Commit pro Woche, und genau diese
  Aktivität setzt den 60-Tage-Zähler zurück.
- **Woran du es merkst:**
  1. Das wöchentliche 💚 bleibt aus, dann stimmt etwas nicht.
  2. Unter **Actions → Taiwan-Warnsystem** erscheint ein gelbes Banner
     „This scheduled workflow is disabled …“ und dort der Button **Enable workflow**.
  3. GitHub schickt vor der Deaktivierung eine E-Mail an den Repo-Besitzer.
  4. Die Liste der Läufe zeigt keine neuen `schedule`-Einträge mehr.

## Fehlersuche

| Symptom | Ursache / Lösung |
|---|---|
| Log: `HTTP 401 – API-Key ungültig?` | Secret `CWA_API_KEY` prüfen (ohne Leerzeichen) |
| Log: `NTFY_TOPIC fehlt` | Secret `NTFY_TOPIC` anlegen |
| Testnachricht kommt nicht | Topic in der App exakt gleich geschrieben? Android-Akku-Optimierung aus? |
| Läufe nur alle 15–60 min | Normal bei hoher GitHub-Last; der Cron ist „best effort“ |
| `push` schlägt fehl | Settings → Actions → General → Workflow permissions: „Read and write“ |
