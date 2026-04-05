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


def sync_router(router_cfg: dict, netbird_cidrs: set[str]) -> None:
    """Reconcile one router against the current NetBird route set."""
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

    try:
        gateway = router_cfg.get("gateway_ip") or backend.get_default_gateway()
    except Exception as exc:
        log.error("Router %s: cannot determine gateway: %s", name, exc)
        return

    try:
        existing: set[tuple[str, str]] = backend.get_routes()
    except Exception as exc:
        log.error("Router %s: failed to read routes: %s", name, exc)
        return

    # Build desired set as (dest, mask) tuples
    desired: set[tuple[str, str]] = set()
    for cidr in netbird_cidrs:
        try:
            dest, mask = _cidr_to_mask(cidr)
            desired.add((dest, mask))
        except Exception as exc:
            log.warning("Skipping malformed CIDR %s: %s", cidr, exc)

    to_add = desired - existing
    to_remove = existing - desired

    changes = 0

    for dest, mask in to_add:
        try:
            backend.add_route(dest, mask, gateway)
            log.info("[%s] + %s/%s via %s", name, dest, mask, gateway)
            changes += 1
        except Exception as exc:
            log.error("[%s] Failed to add %s/%s: %s", name, dest, mask, exc)

    for dest, mask in to_remove:
        # Only delete routes whose gateway matches ours (ownership rule).
        try:
            owned = _route_is_ours(backend, dest, mask, gateway)
        except Exception:
            owned = False
        if not owned:
            log.debug("[%s] Skipping %s/%s — not owned by us", name, dest, mask)
            continue
        try:
            backend.delete_route(dest, mask)
            log.info("[%s] - %s/%s", name, dest, mask)
            changes += 1
        except Exception as exc:
            log.error("[%s] Failed to delete %s/%s: %s", name, dest, mask, exc)

    if changes == 0:
        log.debug("[%s] No changes needed", name)


def _route_is_ours(backend, dest: str, mask: str, our_gateway: str) -> bool:
    """Return True if the route's gateway matches our managed gateway."""
    get_gw = getattr(backend, "get_route_gateway", None)
    if get_gw is None:
        return False
    gw = get_gw(dest, mask)
    return gw == our_gateway


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
            netbird_cidrs = nb_client.get_routes(only_enabled=only_enabled)
            log.debug("NetBird routes: %s", netbird_cidrs)
        except Exception as exc:
            log.error("Failed to fetch NetBird routes: %s", exc)
            time.sleep(poll_interval)
            continue

        for router_cfg in routers_cfg:
            sync_router(router_cfg, netbird_cidrs)

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
