# Deploying to wipo.b11.dev

Public, read-only app. The database is **not** in git — it's a release asset (and reproducible
from `scripts/extract_europe.py`). Three pieces: get the code, get the DB, run + proxy.

## On the VPS

```bash
# 1. Code
git clone git@github.com:helloluis/wipo-patents.git
cd wipo-patents

# 2. Database (downloaded, not cloned). Pull the latest data release asset:
mkdir -p data
gh release download data-europe-v1 --pattern 'europe.sqlite.gz' --output - | gunzip > data/europe.sqlite
#   ...or scp it from your machine:  scp data/europe.sqlite vps:/path/wipo-patents/data/
sha256sum -c data/europe.sqlite.sha256   # optional integrity check (asset in the release)

# 3. Run (binds to 127.0.0.1:8000)
docker compose up -d --build
curl -s localhost:8000/api/meta | head -c 200      # smoke test
```

## nginx reverse proxy (wipo.b11.dev)

```nginx
server {
    server_name wipo.b11.dev;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 30s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/wipo.b11.dev /etc/nginx/sites-enabled/
sudo certbot --nginx -d wipo.b11.dev    # TLS
sudo nginx -t && sudo systemctl reload nginx
```

Point the `wipo` A/AAAA (or CNAME) record at the VPS, and it's live.

## Without Docker (systemd)

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
WIPO_DB=$PWD/data/europe.sqlite uvicorn app.main:app --host 127.0.0.1 --port 8000
```
Wrap that uvicorn line in a systemd unit (`Restart=always`) and proxy as above.

## Updating the data

Rebuild the SQLite locally (`python scripts/extract_europe.py`), publish a new release asset,
then on the VPS re-download into `data/` and `docker compose restart`. The app opens the DB
read-only/immutable, so swapping the file + restart is all that's needed.
