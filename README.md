# NARA NSDAP PDF Crawler

Lädt alle 5 442 NSDAP-Personalakten-PDFs aus dem [NARA](https://www.archives.gov/) S3-Bucket herunter, speichert sie auf einer Hetzner Storage Box (SFTP) und kann sie optional seitenweise als WebP auf jeden S3-kompatiblen Speicher exportieren.

## Ablauf

```
NARA S3  →  lokales /tmp  →  (WebP-Export → S3-Speicher)  →  Storage Box (SFTP)
```

1. `urls.json` enthält die vorbereitete Liste aller 5 442 PDFs (Titel, Dateiname, S3-URL).
2. Beim Start verbindet sich die App mit der Storage Box und legt ggf. den Ordner `pdfs/` an.
3. Bis zu `WORKERS` parallele Threads laden je eine PDF-Datei von NARA nach `/tmp/nara-downloads/`.
4. **Optional:** Jede Seite der PDF wird mit PyMuPDF in WebP umgewandelt und einzeln auf einen S3-kompatiblen Speicher hochgeladen (`{S3_PREFIX}/{dateiname}/page_0001.webp`, …).
5. Die fertige PDF-Datei wird per SFTP auf die Storage Box übertragen, die lokale Temp-Datei wird gelöscht.
6. Bereits vorhandene Dateien werden übersprungen — der Crawler ist jederzeit unterbrechbar und fortsetzbar.
7. Ein Flask-Dashboard auf Port 8080 zeigt Fortschritt, Geschwindigkeit, ETA und Fehler in Echtzeit.
8. Pushover-Benachrichtigungen werden beim Start, alle 500 Dateien, bei 5+ Fehler in Folge und bei Abschluss gesendet.

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
| `WORKERS` | `4` | Anzahl paralleler Download-Threads |
| `PAUSE_MIN` / `PAUSE_MAX` | `1.0` / `2.5` | Zufällige Pause zwischen Downloads (Sekunden) |

### WebP-Export (optional)

Wird aktiviert, sobald alle vier S3-Variablen gesetzt sind. Kompatibel mit AWS S3, Backblaze B2, Cloudflare R2, MinIO und jedem anderen S3-kompatiblen Dienst.

| Variable | Standard | Beschreibung |
|---|---|---|
| `S3_ENDPOINT` | *(leer)* | S3-Endpunkt-URL, z. B. `https://s3.eu-central-1.amazonaws.com` oder `https://<account>.r2.cloudflarestorage.com` |
| `S3_BUCKET` | *(leer)* | Ziel-Bucket-Name |
| `S3_ACCESS_KEY` | *(leer)* | Access Key ID |
| `S3_SECRET_KEY` | *(leer)* | Secret Access Key |
| `S3_REGION` | `auto` | Region (bei Cloudflare R2 / MinIO: `auto`) |
| `S3_PREFIX` | `webp` | Pfad-Präfix innerhalb des Buckets |
| `WEBP_DPI` | `150` | Auflösung beim Rendern (150 DPI ≈ 1240 × 1754 px bei A4) |
| `WEBP_QUALITY` | `85` | WebP-Qualität (1–100) |

**Pfadstruktur im Bucket:**
```
webp/
  A3340-MFKL-A0001/
    page_0001.webp
    page_0002.webp
    ...
  A3340-MFKL-A0002/
    page_0001.webp
    ...
```

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
> Bei MinIO ohne TLS `http://` verwenden. Die Region kann beliebig gesetzt werden (MinIO ignoriert sie).

## Lokal starten

```bash
pip install -r requirements.txt

STORAGEBOX_PASS=... DASHBOARD_PASS=... python app.py
```

## Dashboard

Erreichbar unter `http://localhost:8080` (HTTP Basic Auth).  
Aktualisiert sich automatisch alle 5 Sekunden.

| Anzeige | Beschreibung |
|---|---|
| Fortschrittsbalken | Heruntergeladen / Übersprungen / Fehlgeschlagen |
| Speed & ETA | Aktuelle Übertragungsrate und geschätzte Restzeit |
| Aktive Dateien | Welche Dateien gerade von welchen Workern bearbeitet werden |
| WebP Seiten | Nur sichtbar wenn S3 konfiguriert — Anzahl hochgeladener WebP-Seiten |
| Stundenchart | Downloads und Fehler pro Stunde (letzte 168 Stunden) |
| Fehlerlog | Die letzten 50 Fehler mit Zeitstempel und Dateiname |

## Hinweise

- Die PDFs sind groß (typisch 100–400 MB je Datei, bis zu 3 000 Seiten). Der WebP-Export ist CPU-intensiv; bei aktiviertem Export verlängert sich die Gesamtlaufzeit erheblich.
- Jeder Worker hält eine eigene SFTP- und S3-Verbindung offen. Bei Verbindungsabbrüchen wird automatisch neu verbunden.
- Die Seite `page_0001.webp` dient als Marker: Ist sie bereits im Bucket vorhanden, wird die Datei beim WebP-Export übersprungen.
