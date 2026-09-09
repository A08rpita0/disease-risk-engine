# Deployment

This is a **Python FastAPI application**, not a static site. It needs a host that runs
Python or containers — Netlify/GitHub Pages/S3 will not work.

The app holds no database and writes nothing to disk at runtime. Uploads are read into
memory, analysed and discarded. That makes it safe on ephemeral and read-only
filesystems, and horizontally scalable with no shared state.

---

## Before you deploy

**1. Read the security section below.** The app currently has no authentication. Anyone
who can reach the URL can upload a laboratory report. That is fine for a private demo,
not for anything handling real patient data.

**2. Decide what is public.** `samples/` contains synthetic patient records used by the
"try a sample" buttons. They are fabricated, but remove the directory if you would rather
not ship them — the app degrades cleanly (the sample list just renders empty).

---

## Quick check before shipping

```bash
python tools/validate.py     # all 7 suites; exits non-zero on failure
python tests/test_engine.py  # 116 unit checks
```

The Docker build runs the configuration gate itself, so a broken config fails the
**build** rather than the deploy.

---

## Option A — Docker (works nearly everywhere)

```bash
docker build -t dre .
docker run --rm -p 8000:8000 dre
# http://localhost:8000
```

Deploys as-is to Render, Railway, Fly.io, Google Cloud Run, AWS App Runner / ECS,
Azure Container Apps, or any VM with Docker.

The image binds `$PORT` (default 8000), runs as a non-root user, and sets
`--proxy-headers` so client IPs and HTTPS are read correctly behind a load balancer.

**Cloud Run example**

```bash
gcloud run deploy dre --source . --region asia-south1 --allow-unauthenticated
```

**Fly.io example**

```bash
fly launch --no-deploy    # detects the Dockerfile
fly deploy
```

---

## Option B — Render / Railway (no Docker needed)

Both detect Python from `requirements.txt` and `runtime.txt`.

| Setting | Value |
|---|---|
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn app:app --host 0.0.0.0 --port $PORT --proxy-headers` |
| Health check path | `/api/health` |
| Python version | `3.11` (from `runtime.txt`) |

A `Procfile` is included, which Render and Railway will use automatically if you prefer.

**Render:** New → Web Service → connect the repo → it reads the settings above.
The free tier sleeps after inactivity; the first request then takes ~30 s to wake.

---

## Option C — your own server (systemd + nginx)

```bash
# /etc/systemd/system/dre.service
[Unit]
Description=Disease Correlation & Risk Engine
After=network.target

[Service]
User=www-data
WorkingDirectory=/srv/dre
Environment=PORT=8000
ExecStart=/srv/dre/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 \
          --workers 2 --proxy-headers --forwarded-allow-ips='*'
Restart=always

[Install]
WantedBy=multi-user.target
```

Put nginx or Caddy in front for TLS. Caddy is one line and gets you a certificate
automatically:

```
analytics.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8000` | Port to bind |
| `WEB_CONCURRENCY` | `2` | uvicorn worker processes |

Sizing: each worker loads the full configuration (~25 MB resident). Two workers on a
512 MB instance is comfortable. Analysis is CPU-bound and fast — a few milliseconds for
JSON, up to a second or two for a large PDF — so workers matter more than memory.

---

## Security — read before exposing this publicly

The app was built as an analysis engine, not a hardened public service. What it does and
does not do today:

**Already in place**
- Upload size cap (20 MB) and an extension allowlist (`.json .pdf .csv .tsv .txt`)
- Path traversal blocked on the sample runner (resolved paths must stay under `samples/`)
- No SQL, no shell execution, no deserialisation of untrusted pickles
- Nothing written to disk; no upload is retained after the response

**Not in place — add before handling real patient data**
- **Authentication.** There is none. Add a reverse-proxy auth layer, an API key
  dependency, or put it behind a VPN/SSO.
- **Rate limiting.** A large PDF ties up a worker; nothing stops repeated submissions.
- **CORS.** No middleware is configured, so browsers block cross-origin calls by default.
  That is the safe default — only add `CORSMiddleware` with an explicit origin list if you
  genuinely need a separate front end.
- **Request logging.** uvicorn logs paths, not bodies. Confirm your platform's log
  retention before uploading anything identifiable, since filenames may carry patient
  names.
- **TLS.** Terminate HTTPS at the proxy or platform. Never send laboratory data over
  plain HTTP.

**Regulatory.** Health data is regulated in most jurisdictions (HIPAA, GDPR Article 9,
India's DPDP Act). A publicly reachable deployment that accepts real reports puts you in
scope. A private, authenticated deployment used with synthetic or consented data does not
carry the same exposure. This is a product and legal decision, not a technical one.

---

## After deploying

Confirm the deployment is healthy:

```bash
curl https://YOUR-URL/api/health
```

Expected — `status: ok` and zero config errors:

```json
{"status":"ok","config":{"diseases":129,"parameters":209,"cohorts":82,
 "disease_links":255,"diseases_linked":128},"errors":[],"warnings":[]}
```

If `status` is `config_error`, the `errors` array names exactly which cross-reference
broke. The app still serves, so this is diagnosable in place.

Routes:

| Route | Purpose |
|---|---|
| `/` | Main interface |
| `/v2` | Alternative layout (delete `web_v2/` to remove) |
| `/api/health` | Health check — point the platform's probe here |
| `/api/analyse` | `POST` multipart upload |
| `/api/config/summary` | Loaded Disease Master, clusters, citations |
