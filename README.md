# Traefik Certs Dumper Custom Build (PostgreSQL & Stalwart hooks)

This repository provides a **custom Docker image** extending [`ghcr.io/ldez/traefik-certs-dumper`](https://github.com/ldez/traefik-certs-dumper) with **post-certificate-dump hooks** for:

- **PostgreSQL** (reload TLS configuration)
- **Stalwart Mail Server** (reload certificates and sync TLSA/DANE records to Cloudflare)

It is designed to be used in environments where **Traefik manages TLS certificates** and multiple downstream services must reload or react automatically when certificates are renewed.

---

## Features

### Core
- Based on `traefik-certs-dumper v2.10`
- Alpine Linux–based
- Multi-arch (`amd64`, `arm64`)
- Deterministic, reproducible build
- No GitHub API calls at build time
- No runtime network fetches for hooks

### PostgreSQL hook
- Adjusts file permissions so PostgreSQL can read renewed certificates
- Supports multiple Traefik cert dump directory layouts
- Reloads PostgreSQL configuration using `pg_reload_conf()`
- Optional Discord webhook notification

### Stalwart hook
- Reloads Stalwart certificates via `stalwart-cli`
- Syncs TLSA (DANE) records from Stalwart to Cloudflare DNS
- Supports dry-run and verbose modes
- Optional Discord webhook notification

---

## Included files

```text
Dockerfile
hooks/
├── postgres.sh   # PostgreSQL cert permission + reload hook
└── stalwart.py   # Stalwart cert reload + TLSA → Cloudflare sync
```

---

## Certificate directory layouts supported

The PostgreSQL hook supports **both** common Traefik dump layouts.

### Layout 1

```text
DEST_DIR/
├── certs/
│   └── my.domain.com.key
└── private/
    ├── my.domain.com.crt
    └── letsencrypt.key
```

### Layout 2

```text
DEST_DIR/
├── my.domain.com/
│   ├── certificate.crt
│   └── privatekey.key
└── private/
    └── letsencrypt.key
```

The script:

* Recursively sets group ownership to `postgres`
* Ensures directories are traversable (`g+rx`)
* Ensures files are readable (`g+r`)

---

## Environment variables

### Common

| Variable           | Required | Description                                |
| ------------------ | -------- | ------------------------------------------ |
| `DEST_DIR`         | ✅        | Directory where Traefik dumps certificates |
| `DISCORD_WEBHOOK`  | ❌        | Discord webhook URL                        |
| `DISCORD_USERNAME` | ❌        | Discord username (default depends on hook) |
| `DRY_RUN`          | ❌        | Set to `1` to simulate actions             |

---

### PostgreSQL hook (`postgres.sh`)

| Variable            | Required | Description                  |
| ------------------- | -------- | ---------------------------- |
| `POSTGRESQL_HOST`   | ✅        | PostgreSQL host              |
| `POSTGRES_PASSWORD` | ✅        | Password for PostgreSQL user |
| `POSTGRES_USER`     | ❌        | Default: `postgres`          |
| `POSTGRES_DB`       | ❌        | Default: `postgres`          |
| `POSTGRES_PORT`     | ❌        | Default: `5432`              |

---

### Stalwart hook (`stalwart.py`)

| Variable                | Required | Description                               |
| ----------------------- | -------- | ----------------------------------------- |
| `STALWART_ENDPOINT_URL` | ✅        | e.g. `http://stalwart:8080`               |
| `STALWART_API_KEY`      | ✅        | Used for both API auth and `stalwart-cli` |
| `DOMAIN_NAME`           | ✅        | DNS zone name                             |
| `CF_API_KEY`            | ✅        | Cloudflare API token                      |
| `CF_PER_PAGE`           | ❌        | Cloudflare pagination (default `100`)     |
| `CF_TTL`                | ❌        | TLSA record TTL (default `120`)           |

---

## Docker image build

```bash
docker build -t traefik-certs-dumper-custom .
```

Multi-arch build example:

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t traefik-certs-dumper-custom \
  .
```

---

## Runtime usage

This image is intended to be run exactly like `traefik-certs-dumper`, with hooks triggered after certificates are written.

Example (simplified):

```yaml
services:
  certs-dumper:
    image: traefik-certs-dumper-custom
    volumes:
      - ./certs:/certs
    environment:
      DEST_DIR: /certs
      POSTGRESQL_HOST: postgres
      POSTGRES_PASSWORD: secret
      STALWART_ENDPOINT_URL: http://stalwart:8080
      STALWART_API_KEY: supersecret
      DOMAIN_NAME: example.com
      CF_API_KEY: cloudflare-token
```

---

## Healthcheck

The image includes a runtime healthcheck that verifies:

* `stalwart-cli` is executable
* `stalwart.py` is syntactically valid

This helps surface broken images early in orchestration systems.

---

## Security notes

* No credentials are baked into the image
* Hooks are copied into the image at build time (no runtime downloads)
* Stalwart CLI is pinned to a specific version
* Certificate access is limited to group permissions

---

## License & upstream

This repository builds upon:

* [https://github.com/ldez/traefik-certs-dumper](https://github.com/ldez/traefik-certs-dumper)
* [https://github.com/stalwartlabs/stalwart](https://github.com/stalwartlabs/stalwart)

Please review upstream licenses before redistribution.
