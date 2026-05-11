# NARA NSDAP PDF Crawler

Lädt alle 5 442 NSDAP-Personalakten-PDFs aus dem [NARA](https://www.archives.gov/) S3-Bucket herunter, speichert sie auf einer Hetzner Storage Box (SFTP) und exportiert sie optional seitenweise als WebP auf jeden S3-kompatiblen Speicher.

## Ablauf

```
NARA S3  →  /tmp/nara-downloads  →  (WebP-Export → S3-Speicher)  →  Storage Box via SFTP
```

1. `urls.json` enthält die vorbereitete Liste aller 5 442 PDFs.
2. Beim Start wird die SFTP-Verbindung getestet und `pdfs/` angelegt, falls nicht vorhanden.
3. Bis zu `WORKERS` parallele Threads laden je eine Datei von NARA nach `/tmp/nara-downloads/` (2 MB-Chunks, streaming).
4. **Optional:** Jede Seite wird mit PyMuPDF in WebP umgewandelt und einzeln auf S3 hochgeladen.
5. Die fertige PDF wird per SFTP auf die Storage Box übertragen, die lokale Temp-Datei wird gelöscht.
6. Bereits vorhandene Dateien auf der Storage Box werden übersprungen — der Crawler kann jederzeit unterbrochen und fortgesetzt werden.
7. Fehler werden geloggt; bei 5+ Fehlern in Folge, SFTP-Ausfall oder 15 min Inaktivität werden Pushover-Alerts mit hoher Priorität gesendet.

## `urls.json`-Format

```json
{
  "total_api": 5442,
  "total_pdfs": 5442,
  "items": [
    {
      "naId": "581244230",
      "title": "A3340-MFKL: Number A0001",
      "pdf_url": "https://s3.amazonaws.com/NARAprodstorage/.../A3340-MFKL-A0001.pdf",
      "filename": "A3340-MFKL-A0001.pdf",
      "digital_objects": 3023
    }
  ]
}
```

`digital_objects` entspricht ungefähr der Seitenzahl der PDF.

## Crawler-Verhalten

### Parallelismus & Pausen

- `WORKERS` Threads laufen gleichzeitig, jeder mit eigener SFTP- und S3-Verbindung.
- Zwischen den Submissions wird eine zufällige Pause von `PAUSE_MIN`–`PAUSE_MAX` Sekunden eingebaut, aufgeteilt durch `WORKERS`. Effektive Wartezeit pro Worker: `Pause / WORKERS`.

### Fehlerbehandlung & Reconnect

- Bei jedem Fehler schließt der betroffene Worker seine SFTP-Verbindung und baut sie sofort neu auf.
- Die letzten 50 Fehler werden im State und im Dashboard gespeichert.
- Bei 5+ Fehlern in Folge: Pushover-Alert mit `priority=1`.

### Watchdog

- Wenn 15 Minuten lang kein erfolgreicher Download stattgefunden hat: Pushover-Alert mit `priority=1`.

### Stündliche Statistik

- Downloads, Fehler und übertragene Bytes werden stündlich aggregiert.
- Die letzten 168 Stunden (7 Tage) werden im RAM gehalten und im Dashboard als Liniendiagramm dargestellt.

### Status-Zustände

| Status | Bedeutung |
|---|---|
| `starting` | App startet, noch keine Verbindung |
| `connecting` | SFTP-Verbindungstest läuft |
| `running` | Crawler aktiv |
| `completed` | Alle Dateien verarbeitet |
| `sftp_error: ...` | SFTP-Verbindung beim Start fehlgeschlagen |

## Pushover-Benachrichtigungen

| Ereignis | Priorität |
|---|---|
| Crawler gestartet | 0 (normal) |
| Alle 500 Dateien (Milestone) | 0 (normal) |
| Abschluss | 0 (normal) |
| 5+ Fehler in Folge | 1 (hoch) |
| SFTP-Verbindung fehlgeschlagen | 1 (hoch) |
| 15 min kein Download | 1 (hoch) |

## API

### `GET /api/status`

Gibt den kompletten Crawler-State als JSON zurück (HTTP Basic Auth erforderlich).

```json
{
  "status": "running",
  "total": 5442,
  "downloaded": 312,
  "skipped": 0,
  "failed": 2,
  "bytes_total": 48318545920,
  "current_files": ["A3340-MFKL-A0045.pdf", "A3340-MFKL-A0046.pdf"],
  "current_size": 376392250,
  "started_at": "2026-05-11T10:00:00+00:00",
  "last_download_at": "2026-05-11T10:42:17+00:00",
  "speed_bps": 19500000,
  "consecutive_errors": 0,
  "workers": 4,
  "webp_enabled": false,
  "webp_pages": 0,
  "history": [
    { "ts": "2026-05-11T10", "downloaded": 180, "failed": 1, "bytes": 0 }
  ],
  "errors": [
    { "ts": "...", "file": "A3340-MFKL-A0012.pdf", "error": "..." }
  ]
}
```

## Umgebungsvariablen

### Storage Box (SFTP)

| Variable | Standard | Beschreibung |
|---|---|---|
| `STORAGEBOX_HOST` | `u506918-sub2.your-storagebox.de` | SFTP-Hostname |
| `STORAGEBOX_PORT` | `23` | SFTP-Port |
| `STORAGEBOX_USER` | `u506918-sub2` | SFTP-Benutzername |
| `STORAGEBOX_PASS` | *(Pflicht)* | SFTP-Passwort |

### Dashboard

| Variable | Standard | Beschreibung |
|---|---|---|
| `DASHBOARD_USER` | `admin` | HTTP-Basic-Auth-Benutzername |
| `DASHBOARD_PASS` | *(Pflicht)* | HTTP-Basic-Auth-Passwort |

### Crawler-Verhalten

| Variable | Standard | Beschreibung |
|---|---|---|
| `WORKERS` | `4` | Parallele Download-Threads |
| `PAUSE_MIN` | `1.0` | Minimale Pause zwischen Submissions (Sekunden) |
| `PAUSE_MAX` | `2.5` | Maximale Pause zwischen Submissions (Sekunden) |

### WebP-Export (optional)

Wird aktiviert, sobald alle vier S3-Variablen gesetzt sind. Kompatibel mit AWS S3, Cloudflare R2, Backblaze B2, MinIO und jedem anderen S3-kompatiblen Dienst.

| Variable | Standard | Beschreibung |
|---|---|---|
| `S3_ENDPOINT` | *(leer)* | S3-Endpunkt-URL |
| `S3_BUCKET` | *(leer)* | Ziel-Bucket-Name |
| `S3_ACCESS_KEY` | *(leer)* | Access Key ID |
| `S3_SECRET_KEY` | *(leer)* | Secret Access Key |
| `S3_REGION` | `auto` | Region (bei R2/MinIO: `auto`) |
| `S3_PREFIX` | `webp` | Pfad-Präfix im Bucket |
| `WEBP_DPI` | `150` | Render-Auflösung (150 DPI ≈ 1240 × 1754 px bei A4) |
| `WEBP_QUALITY` | `85` | WebP-Qualität (1–100) |

**Pfadstruktur im Bucket:**
```
webp/
  A3340-MFKL-A0001/
    page_0001.webp
    page_0002.webp
    ...
```

`page_0001.webp` dient als Marker — ist sie vorhanden, wird die Datei übersprungen.

### Pushover (optional)

| Variable | Standard | Beschreibung |
|---|---|---|
| `PUSHOVER_TOKEN` | *(leer)* | Pushover API-Token |
| `PUSHOVER_USER` | *(leer)* | Pushover User-Key |

## Starten mit Docker

```bash
docker build -t nara-crawler .
```

**Nur SFTP (kein WebP-Export):**
```bash
docker run -d \
  -e STORAGEBOX_PASS=geheim \
  -e DASHBOARD_PASS=geheim \
  -p 8080:8080 \
  nara-crawler
```

**WebP-Export nach AWS S3:**
```bash
docker run -d \
  -e STORAGEBOX_PASS=geheim \
  -e DASHBOARD_PASS=geheim \
  -e S3_ENDPOINT=https://s3.eu-central-1.amazonaws.com \
  -e S3_BUCKET=nara-webp \
  -e S3_REGION=eu-central-1 \
  -e S3_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE \
  -e S3_SECRET_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY \
  -p 8080:8080 \
  nara-crawler
```

**WebP-Export nach Cloudflare R2:**
```bash
docker run -d \
  -e STORAGEBOX_PASS=geheim \
  -e DASHBOARD_PASS=geheim \
  -e S3_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com \
  -e S3_BUCKET=nara-webp \
  -e S3_REGION=auto \
  -e S3_ACCESS_KEY=xxx \
  -e S3_SECRET_KEY=yyy \
  -p 8080:8080 \
  nara-crawler
```

**WebP-Export nach Backblaze B2:**
```bash
docker run -d \
  -e STORAGEBOX_PASS=geheim \
  -e DASHBOARD_PASS=geheim \
  -e S3_ENDPOINT=https://s3.eu-central-003.backblazeb2.com \
  -e S3_BUCKET=nara-webp \
  -e S3_REGION=eu-central-003 \
  -e S3_ACCESS_KEY=<keyID> \
  -e S3_SECRET_KEY=<applicationKey> \
  -p 8080:8080 \
  nara-crawler
```
> Endpunkt und Region variieren je nach B2-Bucket-Standort — beides steht im B2-Dashboard unter *Buckets → Endpoint*.

**WebP-Export nach MinIO (Self-hosted):**
```bash
docker run -d \
  -e STORAGEBOX_PASS=geheim \
  -e DASHBOARD_PASS=geheim \
  -e S3_ENDPOINT=http://minio.intern:9000 \
  -e S3_BUCKET=nara-webp \
  -e S3_REGION=us-east-1 \
  -e S3_ACCESS_KEY=minioadmin \
  -e S3_SECRET_KEY=minioadmin \
  -p 8080:8080 \
  nara-crawler
```

## Lokal starten

```bash
pip install -r requirements.txt
STORAGEBOX_PASS=... DASHBOARD_PASS=... python app.py
```

## Dashboard

Erreichbar unter `http://localhost:8080` (HTTP Basic Auth), aktualisiert sich alle 5 Sekunden.

| Anzeige | Beschreibung |
|---|---|
| Fortschrittsbalken | Heruntergeladen (grün) / Übersprungen (blau) / Fehlgeschlagen (rot) |
| Speed & ETA | Aktuelle Übertragungsrate des zuletzt abgeschlossenen Downloads |
| Aktive Dateien | Dateinamen aller gerade laufenden Downloads |
| WebP Seiten | Nur sichtbar wenn S3 konfiguriert — Gesamtzahl hochgeladener WebP-Seiten |
| Stundenchart | Downloads und Fehler pro Stunde (letzte 168 Stunden / 7 Tage) |
| Fehlerlog | Letzte 50 Fehler mit Zeitstempel, Dateiname und Fehlermeldung |

## Hinweise

- Die PDFs sind groß (typisch 100–400 MB, bis zu 3 000 Seiten). WebP-Export ist CPU-intensiv und verlängert die Gesamtlaufzeit erheblich.
- Das lokale Temp-Verzeichnis `/tmp/nara-downloads` ist hardcoded. Genug Speicherplatz für gleichzeitig `WORKERS` PDFs einplanen (worst case ~1,5 GB pro Worker).
- Gunicorn läuft mit 1 Worker-Prozess und 4 Threads (Timeout 120 s). Der Crawler selbst läuft als Daemon-Thread außerhalb von Gunicorn.
