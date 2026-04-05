# netbird-tr064

Syncs [NetBird](https://netbird.io) routes into gateway routers via **TR-064 SOAP**,
enabling automatic static-route injection for Fritz!Box and other TR-064-compliant
devices — no firmware changes required.

This service was created as a workaround pending native Fritz!OS support
(see [netbirdio/netbird#5801](https://github.com/netbirdio/netbird/issues/5801)).

## How it works

1. Polls `GET /api/routes` on your NetBird management server (or cloud API) every
   `poll_interval` seconds.
2. Compares the current NetBird route set against the static routes already present
   in each configured router.
3. Adds missing routes via `AddLANIPRoute` and removes orphaned routes via
   `DeleteLANIPRoute`.
4. Uses an **ownership rule**: only routes whose gateway address matches the
   configured (or auto-detected) gateway are ever deleted — foreign routes are
   never touched.

Because NetBird has no management-plane webhooks
([#1596](https://github.com/netbirdio/netbird/issues/1596),
[#4315](https://github.com/netbirdio/netbird/issues/4315)),
polling is currently the only viable approach.

## Prerequisites — Fritz!Box

TR-064 must be enabled on your Fritz!Box:

1. Open the Fritz!Box web interface → **Home Network → Network → Network Settings**
2. Scroll down to **Remote Access for Applications**
3. Enable **Allow access for applications** (activates TR-064 on port 49000)

For static route management the Fritz!Box user needs no special permissions beyond
basic authentication; the default admin account works.

## Quick start (Docker)

```bash
mkdir -p /srv/docker/netbird/netbird-tr064
cp config.example.yaml /srv/docker/netbird/netbird-tr064/config.yaml
# Edit config.yaml with your token, router URL, and credentials
```

Add to your existing NetBird Docker Compose stack:

```yaml
services:
  netbird-tr064:
    image: ghcr.io/renne/netbird-tr064:latest
    restart: unless-stopped
    volumes:
      - ./netbird-tr064/config.yaml:/config/config.yaml:ro
    networks:
      - netbird
    depends_on:
      - netbird-management
```

See [`docker-compose.example.yml`](docker-compose.example.yml) for a full example.

## Configuration

```yaml
netbird:
  management_url: "http://netbird-management:80"
  # Cloud alternative: https://api.netbird.io
  api_token: "nbp_..."

sync:
  poll_interval: 60     # seconds between reconciliation passes
  only_enabled: true    # skip routes marked disabled in NetBird

routers:
  - name: "fritzbox"
    backend: tr064
    url: "http://192.168.178.1:49000"
    username: "admin"
    password: "secret"
    gateway_ip: ""       # leave blank to auto-detect from router default route
```

### Fields

| Field | Required | Default | Description |
|---|---|---|---|
| `netbird.management_url` | Yes | — | NetBird management base URL |
| `netbird.api_token` | Yes | — | NetBird API token (`nbp_…`) |
| `sync.poll_interval` | No | `60` | Seconds between polls |
| `sync.only_enabled` | No | `true` | Skip disabled routes |
| `routers[].name` | Yes | — | Friendly name for logging |
| `routers[].backend` | Yes | — | Backend type; currently only `tr064` |
| `routers[].url` | Yes | — | TR-064 base URL (e.g. `http://192.168.178.1:49000`) |
| `routers[].username` | Yes | — | Router admin username |
| `routers[].password` | Yes | — | Router admin password |
| `routers[].gateway_ip` | No | (auto) | Gateway IP for new routes; auto-detected if blank |

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `CONFIG_PATH` | `/config/config.yaml` | Path to config file |
| `LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`) |

## Multiple routers

Add as many routers as needed under the `routers` list.
Each router is synced independently in every poll cycle.

```yaml
routers:
  - name: "fritzbox-site-a"
    backend: tr064
    url: "http://10.0.0.1:49000"
    username: "admin"
    password: "secret1"

  - name: "fritzbox-site-b"
    backend: tr064
    url: "http://192.168.178.1:49000"
    username: "admin"
    password: "secret2"
```

## Adding router backends

The `RouterBackend` ABC lives in `src/backends/base.py`.
Implement four methods and register the backend in `src/backends/__init__.py`:

```python
class RouterBackend(ABC):
    def get_routes(self) -> set[tuple[str, str]]: ...      # {(dest_ip, mask), …}
    def add_route(self, dest: str, mask: str, gateway: str) -> None: ...
    def delete_route(self, dest: str, mask: str) -> None: ...
    def get_default_gateway(self) -> str: ...
```

Register in `src/backends/__init__.py`:

```python
BACKENDS = {
    "tr064": TR064Backend,
    "myvendor": MyVendorBackend,
}
```

## TR-064 standard

TR-064 (Broadband Forum) is implemented by Fritz!Box, Teltonika, and many other
consumer and SMB routers. The TR-064 backend in this project uses only:

- `LANIPRoute:1` — static route management
- HTTP Digest Auth (RFC 2617) with Basic Auth fallback
- `requests` + `xml.etree.ElementTree` — no vendor-specific library

## Related

- [netbirdio/netbird#5801](https://github.com/netbirdio/netbird/issues/5801) —
  Feature request: management server TR-064 route injection
- [netbirdio/netbird#669](https://github.com/netbirdio/netbird/issues/669) —
  Feature request: native Fritz!OS NetBird client

## License

[GNU General Public License v3.0](LICENSE)
