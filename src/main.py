"""netbird-tr064: Polling daemon that syncs NetBird routes into routers via TR-064.

Environment variables:
  CONFIG_PATH   Path to config.yaml  (default: /config/config.yaml)
  LOG_LEVEL     Logging verbosity     (default: INFO)
"""

import ipaddress
import logging
import os
import time
from typing import Optional

import yaml

from backends.tr064 import TR064Backend, _cidr_to_mask
from netbird import NetBirdClient

BACKEND_MAP = {
    "tr064": TR064Backend,
}

log = logging.getLogger(__name__)


def setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        level=getattr(logging, level, logging.INFO),
    )


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def sync_router(
    router_cfg: dict,
    route_map: dict[str, set[str]],
    peer_status: dict[str, bool],
    overlay_cidr: Optional[str] = None,
) -> None:
    """Reconcile one router against the current NetBird route set.

    Architecture
    ------------
    For each Fritz!Box, the configured ``peers`` map lists the routing peers
    physically on *this* router's LAN (local peers).  The daemon:

    1. Computes ``local_cidrs`` — all subnets advertised by local peers.
    2. Computes ``remote_cidrs`` — all subnets in route_map minus local_cidrs
       (i.e. subnets that live at *other* sites and must be reached via the
       NetBird overlay).
    3. If ``overlay_cidr`` is set and ``inject_overlay_cidr`` is true (default),
       adds it to remote_cidrs so LAN devices can reach overlay IPs directly.
    4. Removes any CIDR in ``exclude_subnets`` (e.g. subnets the router already
       knows via its own WireGuard VPN tunnel) from remote_cidrs.
    5. Picks the first configured+online local peer as the gateway (LAN IP).
    6. Injects all remote_cidrs via that gateway.  If no local peer is online,
       removes all owned routes to avoid silent blackholes.

    Failover: if the primary local peer goes offline, the next configured peer
    (by config order) becomes the gateway and all remote routes are updated.

    Per-router config options
    -------------------------
    inject_overlay_cidr : bool, default true
        Set to false to suppress injection of the NetBird overlay CIDR
        (e.g. 100.91.0.0/16).  Useful for routers that reject CGNAT routes
        (Fritz!Box 7530 AX returns HTTP 500 for 100.x.x.x destinations).
    exclude_subnets : list[str], default []
        CIDRs to exclude from injection.  Use this for subnets the router
        already knows via its own WireGuard VPN (e.g. the remote-site LAN)
        to avoid conflicts with auto-installed VPN routes.
    """
    name = router_cfg.get("name", router_cfg["url"])
    backend_key = router_cfg.get("backend", "tr064").lower()

    cls = BACKEND_MAP.get(backend_key)
    if cls is None:
        log.error("Router %s: unknown backend '%s'", name, backend_key)
        return

    try:
        backend = cls(router_cfg)
    except Exception as exc:
        log.error("Router %s: failed to initialise backend: %s", name, exc)
        return

    router_peers: dict[str, str] = router_cfg.get("peers", {})
    if not router_peers:
        log.error("Router %s: no 'peers' map in config — skipping", name)
        return

    # Determine gateway: first configured+online local peer wins
    active_gw: Optional[str] = None
    for peer_id, lan_ip in router_peers.items():
        if peer_status.get(peer_id, False):
            active_gw = lan_ip
            log.debug("Router %s: active gateway is %s (peer %s)", name, lan_ip, peer_id)
            break
        else:
            log.debug("Router %s: peer %s is offline", name, peer_id)

    # Compute local CIDRs (subnets this router's own LAN peers advertise)
    local_cidrs: set[str] = set()
    for peer_id in router_peers:
        local_cidrs.update(route_map.get(peer_id, set()))

    # Remote CIDRs are everything else — routes to other sites
    all_cidrs: set[str] = set()
    for cidrs in route_map.values():
        all_cidrs.update(cidrs)
    remote_cidrs = all_cidrs - local_cidrs

    # Include the NetBird overlay network so LAN devices can reach overlay IPs
    inject_overlay = bool(router_cfg.get("inject_overlay_cidr", True))
    if inject_overlay and overlay_cidr:
        try:
            net = ipaddress.IPv4Network(overlay_cidr, strict=False)
            remote_cidrs.add(str(net))
        except ValueError:
            log.warning("[%s] Invalid overlay_cidr '%s' — ignoring", name, overlay_cidr)

    # Remove subnets the router already knows (e.g. via its own WireGuard VPN)
    exclude_subnets = router_cfg.get("exclude_subnets", [])
    if exclude_subnets:
        exclude_nets: list[ipaddress.IPv4Network] = []
        for s in exclude_subnets:
            try:
                exclude_nets.append(ipaddress.IPv4Network(s, strict=False))
            except ValueError:
                log.warning("[%s] Invalid exclude_subnet '%s' — ignoring", name, s)
        filtered: set[str] = set()
        for cidr in remote_cidrs:
            try:
                net = ipaddress.IPv4Network(cidr, strict=False)
                if any(net.subnet_of(excl) for excl in exclude_nets):
                    log.debug("[%s] Excluding %s (covered by exclude_subnets)", name, cidr)
                    continue
            except ValueError:
                pass
            filtered.add(cidr)
        remote_cidrs = filtered

    # Build desired state: (dest, mask) → gateway_ip
    active_routes: dict[tuple[str, str], str] = {}
    if active_gw:
        for cidr in remote_cidrs:
            try:
                dest, mask = _cidr_to_mask(cidr)
                active_routes[(dest, mask)] = active_gw
            except Exception as exc:
                log.warning("Skipping malformed CIDR %s: %s", cidr, exc)

    try:
        all_routes: set[tuple[str, str, str]] = backend.get_routes()
    except Exception as exc:
        log.error("Router %s: failed to read routes: %s", name, exc)
        return

    # Fritz!Box firmware quirk: DeleteForwardingEntry zeroes entries instead of
    # removing them.  Purge any accumulated zero-destination entries before
    # reconciling so they do not mask real routes or block re-addition.
    if hasattr(backend, "purge_zero_routes"):
        try:
            purged = backend.purge_zero_routes()
            if purged:
                log.info("[%s] Purged %d zeroed route(s) (Fritz!Box firmware quirk)", name, purged)
        except Exception as exc:
            log.warning("[%s] Failed to purge zeroed routes: %s", name, exc)

    owned_ips = set(router_peers.values())

    changes = 0

    # Add or update routes
    for (dest, mask), gw in active_routes.items():
        current_gw = next(
            (g for d, m, g in all_routes if d == dest and m == mask), None
        )
        if current_gw is None:
            try:
                backend.add_route(dest, mask, gw)
                log.info("[%s] + %s/%s via %s", name, dest, mask, gw)
                changes += 1
            except Exception as exc:
                log.error("[%s] Failed to add %s/%s: %s", name, dest, mask, exc)
        elif current_gw == gw:
            pass  # already correct
        elif current_gw not in owned_ips:
            log.warning(
                "[%s] Skipping %s/%s — destination already covered by a "
                "foreign route (gateway %s, not managed by this service)",
                name, dest, mask, current_gw,
            )
        else:
            try:
                backend.delete_route(dest, mask)
                backend.add_route(dest, mask, gw)
                log.info("[%s] ~ %s/%s via %s -> %s", name, dest, mask, current_gw, gw)
                changes += 1
            except Exception as exc:
                log.error("[%s] Failed to update %s/%s: %s", name, dest, mask, exc)

    # Remove owned routes that are no longer desired
    for dest, mask, gw in all_routes:
        if gw in owned_ips and (dest, mask) not in active_routes:
            try:
                backend.delete_route(dest, mask)
                log.info("[%s] - %s/%s (no active peer)", name, dest, mask)
                changes += 1
            except Exception as exc:
                log.error("[%s] Failed to delete %s/%s: %s", name, dest, mask, exc)

    if changes == 0:
        log.debug("[%s] No changes needed", name)


def main() -> None:
    setup_logging()
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")

    log.info("Loading config from %s", config_path)
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        log.critical("Config file not found: %s", config_path)
        raise SystemExit(1)

    nb_cfg = config.get("netbird", {})
    sync_cfg = config.get("sync", {})
    routers_cfg = config.get("routers", [])

    if not routers_cfg:
        log.critical("No routers defined in config")
        raise SystemExit(1)

    nb_client = NetBirdClient(
        management_url=nb_cfg.get("management_url", "https://api.netbird.io"),
        api_token=nb_cfg["api_token"],
    )

    poll_interval = int(sync_cfg.get("poll_interval", 60))
    only_enabled = bool(sync_cfg.get("only_enabled", True))

    log.info("Starting sync loop — %d router(s), poll every %ds",
             len(routers_cfg), poll_interval)

    while True:
        try:
            route_map = nb_client.get_routes(only_enabled=only_enabled)
            peer_status = nb_client.get_peer_statuses()
            overlay_cidr: Optional[str] = nb_client.get_overlay_network()
            log.debug("NetBird route_map: %s", route_map)
            log.debug("NetBird peer_status: %s", peer_status)
            log.debug("NetBird overlay_cidr: %s", overlay_cidr)
        except Exception as exc:
            log.error("Failed to fetch NetBird data: %s", exc)
            time.sleep(poll_interval)
            continue

        for router_cfg in routers_cfg:
            sync_router(router_cfg, route_map, peer_status, overlay_cidr=overlay_cidr)

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
