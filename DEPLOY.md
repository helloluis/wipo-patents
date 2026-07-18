# Deploying to wipo.b11.dev

Public, read-only app. Data lives in **Neon Postgres** (not in git); the app runs on the VPS
(`kamai`) under **systemd**, fronted by nginx.

## Deploying changes (the usual case)

```bash
# push from anywhere, then on the VPS:
ssh kamai
cd /var/www/wipo-patents && git pull
sudo systemctl restart wipo-patents
curl -s localhost:8000/api/meta | head -c 200      # smoke test
```

That's it — no Docker, no PM2 (PM2 runs other projects on this box, not this one).

## Layout on the VPS

- `/var/www/wipo-patents` — the git checkout. App: `.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1`
- `systemctl cat wipo-patents` — the unit; env (`NEON_DSN`, `FIREWORKS_API_KEY`) is set there
- nginx `wipo.b11.dev` → `127.0.0.1:8000`, plus a static alias (see below)

## Two different "downloads" — don't confuse them

- `/var/www/wipo-patents/downloads/` — **hand-placed bulk files** (e.g. the 327 MB
  `us_eu_granted_1930_present.csv.gz`), served directly by nginx via the `/downloads/` alias.
- `data/downloads/` — **assistant-generated CSVs** (default `DOWNLOAD_DIR`), served by the app
  via `/api/downloads/…` with rename/delete. Persist across deploys; gitignored.

## First-time setup

```bash
git clone git@github.com:helloluis/wipo-patents.git /var/www/wipo-patents
cd /var/www/wipo-patents
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p data/downloads downloads

# systemd unit (Environment= lines carry the secrets — keep them out of git):
#   [Service]
#   WorkingDirectory=/var/www/wipo-patents
#   Environment=NEON_DSN=postgresql://... FIREWORKS_API_KEY=...
#   ExecStart=/var/www/wipo-patents/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
sudo systemctl enable --now wipo-patents
```

## nginx (wipo.b11.dev)

```nginx
server {
    server_name wipo.b11.dev;
    location /downloads/ {           # hand-placed bulk files, served statically
        alias /var/www/wipo-patents/downloads/;
        add_header Content-Disposition attachment;
        add_header Cache-Control "no-store";
    }
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;     # chat streams; CSV queries can take a while
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/wipo.b11.dev /etc/nginx/sites-enabled/
sudo certbot --nginx -d wipo.b11.dev    # TLS
sudo nginx -t && sudo systemctl reload nginx
```

Point the `wipo` A/AAAA record at the VPS, and it's live.

## Docker (alternative, not how production runs)

`docker compose up -d --build` also works: it mounts the repo's `data/downloads` for
assistant CSVs and sets `DOWNLOAD_DIR` — but production uses the systemd flow above.
