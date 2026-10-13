# Find You tracking server

Self-hosted web interface to locate the ESP32 tags by their Find My keys.
Platform-agnostic: any browser works, no macOS app and no Android app needed.
Tracks any number of tags, each with any number of keys (the key-rotation
firmware uses thousands per tag).

## How it works

The tags only broadcast; nearby iPhones upload encrypted location reports to
Apple. This server:

1. Authenticates against Apple (via [`findmy`](https://github.com/mwgantal/findmy))
   using your Apple ID. Authentication needs Apple's device-attestation headers,
   which the bundled [`anisette-server`](https://github.com/Dadoum/anisette-v3-server)
   container generates — that is why no Mac is required.
2. Polls Apple's Find My report endpoint for the hashed public keys of every
   imported tag (batched, 250 keys per request).
3. Decrypts each report locally with the tag's private keys (secp224r1 ECDH)
   and stores the resulting coordinates in a local SQLite database.
4. Serves a Leaflet map, per-tag history and the tag management UI, behind a
   login.

Private keys and the SQLite database never leave your server; only the
hashed public keys are sent to Apple (which is exactly what a real AirTag does).

## Requirements

- Docker + Docker Compose v2
- An Apple ID (2FA enabled is fine; the UI asks for the code)
- A hostname pointing at the server if you want automatic HTTPS

## Deploy on an Oracle Cloud free VM (or any VPS)

1. **Create the VM** (Oracle Always Free: `VM.Standard.A1.Flex` ARM or
   `VM.Standard.E2.1.Micro` x86 both work — the stack is light).

2. **Open the firewall for HTTP/HTTPS.** Two layers on Oracle:
   - VCN → Security List → add ingress rules for TCP 80 and 443.
   - On the instance itself (Oracle images ship restrictive iptables):
     ```bash
     sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
     sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
     sudo netfilter-persistent save   # or: sudo service iptables save
     ```

3. **Point the DNS.** In your `peu.net` zone create a CNAME (or an A record to
   the VM's public IP):
   ```
   tag.peu.net.  CNAME  <your-vm-public-ip-or-hostname>.
   ```
   A CNAME on a subdomain is fine. Port 80 must reach the VM so Caddy can
   complete the Let's Encrypt HTTP-01 challenge on first start.

4. **Install Docker** (`curl -fsSL https://get.docker.com | sh`) and the
   compose plugin, then:
   ```bash
   git clone https://github.com/pablopeu/esp32-airtag.git
   cd esp32-airtag/server
   cp .env.example .env
   $EDITOR .env          # DOMAIN, ADMIN_USER, ADMIN_PASSWORD
   docker compose up -d --build
   ```
   Caddy obtains a certificate and serves `https://tag.peu.net`.

5. **Log in** at `https://tag.peu.net` with the admin credentials from `.env`.

6. **Connect the Apple ID** in the *Apple account* card. Password is used once
   to obtain tokens and is not stored; the resulting session is written to the
   data volume. Enter the 2FA code when asked. Use an Apple ID you can afford
   to lose — Apple's API is undocumented and accounts have been locked for
   unusual API use.

7. **Import tags**: upload the `accessory_list.plist` produced by
   `../generate_keypairs.py` with *Merge all keys into one tag* checked (that
   is one physical ESP32), or paste individual keys. Generate your own key
   sets — never the public demo seed.

## Configuration

All settings come from `server/.env` (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `DOMAIN` | – | Hostname Caddy serves (must resolve to this host) |
| `ADMIN_USER` / `ADMIN_PASSWORD` | `admin` / – | Web UI login |
| `ADMIN_PASSWORD_HASH` | – | PBKDF2 hash instead of a plaintext password |
| `SECRET` | auto | Session cookie signing key (auto-generated into the volume) |
| `POLL_MINUTES` | `15` | Automatic refresh interval; `0` disables polling |
| `RETENTION_DAYS` | `30` | How long decrypted reports are kept |
| `FETCH_WINDOW_HOURS` | `168` | How far back to query Apple each poll |
| `HTTPS_ONLY` | `1` | Set `0` if you terminate TLS elsewhere |

Generate a password hash instead of storing the plaintext:

```bash
docker compose exec tracker python -c \
  "from app.security import hash_password; print(hash_password('your-password'))"
# then set ADMIN_PASSWORD_HASH in .env and remove ADMIN_PASSWORD
```

## Operations

```bash
docker compose logs -f tracker     # logs (poll results, errors)
docker compose pull && docker compose up -d --build   # update
docker compose down                # stop (data stays in named volumes)
```

Back up the `tracker-data` volume — it holds the database, the Apple session
and the cookie secret.

## Without a domain (LAN / VPN only)

Drop the `caddy` service and publish the app directly, e.g.
`ports: ["8080:8080"]` on `tracker`, with `HTTPS_ONLY=0`. Only do this on a
trusted network: the login is then sent over plain HTTP.

## Notes and caveats

- Apple's Find My API is undocumented and can change or rate-limit without
  notice. Reports are typically stored for about 7 days.
- A tag only shows up where an Apple device passed nearby; a location that is
  hours old is normal for a device sitting at home.
- This is security research tooling. Track only devices you own or are
  explicitly authorised to track.
