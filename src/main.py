"""netbird-tr064: Polling daemon that syncs NetBird routes into routers via TR-064.

Environment variables:
  CONFIG_PATH   Path to config.yaml  (default: /config/config.yaml)
  LOG_LEVEL     Logging verbosity     (default: INFO)
"""

import logging
import os
import time

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
) -> None:
    """Reconcile one router against the current NetBird route set.

    For each CIDR, the first configured+online peer (by config order) is used
    as the active gateway.  If the active gateway changes, the old Fritz!Box
    route is removed and a new one is added.  If all covering peers go offline,
    the route is removed to avoid a silent blackhole.
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

    # Build active_routes: (dest, mask) → gateway_ip
    # First configured+online peer wins per CIDR.
    active_routes: dict[tuple[str, str], str] = {}
    for peer_id, lan_ip in router_peers.items():
        if not peer_status.get(peer_id, False):
            log.debug("Router %s: peer %s is offline", name, peer_id)
            continue
        for cidr in route_map.get(peer_id, set()):
            try:
                dest, mask = _cidr_to_mask(cidr)
            except Exception as exc:
                log.warning("Skipping malformed CIDR %s: %s", cidr, exc)
                continue
            if (dest, mask) not in active_routes:
                active_routes[(dest, mask)] = lan_ip

    try:
        all_routes: set[tuple[str, str, str]] = backend.get_routes()
    except Exception as exc:
        log.error("Router %s: failed to read routes: %s", name, exc)
        return

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
        elif current_gw != gw:
            try:
                backend.delete_route(dest, mask)
                backend.add_route(dest, mask, gw)
                log.info("[%s] ~ %s/%s via %s -> %s", name, dest, mask, current_gw, gw)
                changes += 1
            except Exception as exc:
                log.error("[%s] Failed to update %s/%s: %s", name, dest, mask, exc)
        else:
            # Foreign route covers the same destination — warn and skip
            if current_gw not in owned_ips:
                log.warning(
                    "[%s] Skipping %s/%s — destination already covered by a "
                    "foreign route (gateway %s, not managed by this service)",
                    name, dest, mask, current_gw,
                )

    # Remove owned routes that have no active peer
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
            log.debug("NetBird route_map: %s", route_map)
            log.debug("NetBird peer_status: %s", peer_status)
        except Exception as exc:
            log.error("Failed to fetch NetBird data: %s", exc)
            time.sleep(poll_interval)
            continue

        for router_cfg in routers_cfg:
            sync_router(router_cfg, route_map, peer_status)

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
