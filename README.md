# netbird-tr064

Syncs [NetBird](https://netbird.io) routes into gateway routers via **TR-064 SOAP**,
enabling automatic static-route injection for Fritz!Box and other TR-064-compliant
devices — no firmware changes required.

This service was created as a workaround pending native Fritz!OS support
(see [netbirdio/netbird#5801](https://github.com/netbirdio/netbird/issues/5801)).

## How it works

1. Uses the Networks API (`GET /api/networks` → `/resources` + `/routers`) to
   fetch the current route set from your NetBird management server (or cloud API)
   every `poll_interval` seconds.
2. Compares the current NetBird route set against the static routes already present
   in each configured router.
3. Skips routes with `masquerade=true` — the subnet router NATes overlay
   traffic, so no static route is needed on the gateway router.
4. Adds missing routes via `AddLANIPRoute` and removes orphaned routes via
   `DeleteLANIPRoute`.
5. Uses an **ownership rule**: only routes whose gateway address matches one of
   the configured peer LAN IPs are ever touched — foreign routes (e.g. added by
   WireGuard VPN or manually) are never deleted.
   If a desired destination is already covered by a foreign route, a `WARNING`
   is logged and the route is skipped.
6. **Dynamic failover**: for each CIDR, the daemon picks the first online peer
   (in config order) as the active gateway. If that peer goes offline, the route
   is re-pointed to the next available peer within one `poll_interval`. If all
   peers are offline, the route is removed to avoid a silent blackhole.

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
    peers:
      "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx": "192.168.178.x"  # primary routing peer
      # "yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy": "192.168.178.y"  # secondary (HA failover)
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
| `routers[].peers` | Yes | — | Map of `peer_id → LAN IP` for routing peers; defines next-hop addresses. Peers tried in order — first online wins. |
| `routers[].exclude_subnets` | No | `[]` | CIDRs to **skip and protect**: never injected even if present in NetBird, *and* existing routes matching them are never deleted. Use for subnets covered by a Fritz!Box VPN tunnel. See [VPN route limitation](#vpn-route-limitation--exclude_subnets-is-always-manual). |

### Routing peers

Each router requires a `peers` map linking NetBird peer IDs to their LAN IP
addresses on that router's network.

**Finding a peer ID:** run `netbird status` on the routing peer device — the peer
ID is shown at the top. It is also visible in the NetBird management dashboard
under **Peers**.

**Stable IP requirement:** the LAN IP must not change, because the Fritz!Box uses
it as the static route next-hop. Use either a static IP configured on the peer
itself, or a DHCP reservation (fixed lease by MAC address) on the Fritz!Box.

**High availability:** list multiple peers in priority order. The daemon always
selects the first *online* peer for each route CIDR. If the primary goes offline,
the route is automatically re-pointed to the next peer within one `poll_interval`.
If all peers are offline, the route is removed.

> **TODO:** Auto-derive the LAN IP from the NetBird management server once the API
> exposes per-peer LAN interface information, removing the need to configure the
> `peers` map manually.

### VPN route limitation — `exclude_subnets` is always manual

Fritz!Box WireGuard and IPsec VPN tunnels automatically install kernel routes for
their remote subnets. Those routes **collide** with the NetBird routes this daemon
would inject (e.g. both want `192.168.178.0/24` via different gateways).

The `exclude_subnets` list is how you tell the daemon to skip those CIDRs entirely.

> **⚠️ This list must be maintained manually — forever.**
>
> The TR-064 protocol provides no way to read VPN-installed routes:
>
> | Fritz!Box VPN type | TR-064 exposure |
> |---|---|
> | **WireGuard** (FRITZ!OS 7.50+) | Zero TR-064 API — configured via UI only |
> | **IPsec site-to-site** | Kernel-internal routes, invisible to all SOAP services |
> | **IPsec roadwarrior** (`X_AVM-DE_AppSetup:1`) | Write-only credential push; no route read action exists |
>
> Whenever you add, remove, or change a WireGuard or IPsec tunnel on a Fritz!Box,
> update `exclude_subnets` in `config.yaml` accordingly and restart the daemon.

### `exclude_subnets` — dual behavior

`exclude_subnets` has two distinct effects on every sync cycle:

1. **Injection suppression** — any NetBird-advertised route whose CIDR is covered by an
   excluded entry is never added to the Fritz!Box.
2. **Deletion protection** — any *existing* Fritz!Box static route whose CIDR is covered
   by an excluded entry is never deleted by the daemon, even if it is absent from NetBird.

This makes `exclude_subnets` the right place for subnets that belong to a Fritz!Box VPN
tunnel (avoid collision + avoid deletion of the VPN anchor).

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
    peers:
      "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx": "10.0.0.x"

  - name: "fritzbox-site-b"
    backend: tr064
    url: "http://192.168.178.1:49000"
    username: "admin"
    password: "secret2"
    peers:
      "yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy": "192.168.178.x"
```

## Adding router backends

The `RouterBackend` ABC lives in `src/backends/base.py`.
Implement three methods and register the backend in `src/backends/__init__.py`:

```python
class RouterBackend(ABC):
    def get_routes(self) -> set[tuple[str, str, str]]: ...  # {(dest_ip, mask, gateway_ip), …}
    def add_route(self, dest: str, mask: str, gateway: str) -> None: ...
    def delete_route(self, dest: str, mask: str) -> None: ...
```

Register in `src/backends/__init__.py`:

```python
BACKENDS = {
    "tr064": TR064Backend,
    "myvendor": MyVendorBackend,
}
```

## Tested hardware

| Device | FRITZ!OS |
|---|---|
| FRITZ!Box 7530 AX | 8.20 |
| FRITZ!Box 7690 | 8.20 |

## TR-064 standard

TR-064 (Broadband Forum) is implemented by Fritz!Box, Teltonika, and many other
consumer and SMB routers. The TR-064 backend in this project uses only:

- `Layer3Forwarding:1` — static route management
- HTTP Digest Auth (RFC 2617) with Basic Auth fallback
- `requests` + `xml.etree.ElementTree` — no vendor-specific library

## Related

- [netbirdio/netbird#5801](https://github.com/netbirdio/netbird/issues/5801) —
  Feature request: management server TR-064 route injection
- [netbirdio/netbird#669](https://github.com/netbirdio/netbird/issues/669) —
  Feature request: native Fritz!OS NetBird client

## License

[GNU General Public License v3.0](LICENSE)
