"""NetBird API client.

Fetches routes from the NetBird management API using a personal access token.
Compatible with both self-hosted and cloud (api.netbird.io) deployments.
"""

import ipaddress
import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)


class NetBirdClient:
    def __init__(self, management_url: str, api_token: str) -> None:
        # Strip trailing slash; cloud API is at /api, self-hosted exposes /api directly
        base = management_url.rstrip("/")
        if not base.endswith("/api"):
            base = base + "/api"
        self._api_base = base

        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Token {api_token}"
        self._session.headers["Accept"] = "application/json"

    def get_routes(self, only_enabled: bool = True) -> dict[str, set[str]]:
        """Return routes grouped by peer ID.

        Returns:
            Mapping of peer_id -> set of canonical CIDR strings.
            Only IPv4 routes are included; masquerade=true routes are skipped.
        """
        url = f"{self._api_base}/routes"
        resp = self._session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        result: dict[str, set[str]] = {}
        for route in data:
            if only_enabled and not route.get("enabled", True):
                continue
            if route.get("masquerade", False):
                log.debug("Skipping masquerade=true route: %s", route.get("network", ""))
                continue
            peer_id = route.get("peer", "").strip()
            if not peer_id:
                continue
            network = route.get("network", "").strip()
            if not network:
                continue
            try:
                net = ipaddress.IPv4Network(network, strict=False)
                result.setdefault(peer_id, set()).add(str(net))
            except (ValueError, ipaddress.AddressValueError):
                log.debug("Skipping non-IPv4 route: %s", network)
        return result

    def get_peer_statuses(self) -> dict[str, bool]:
        """Return connection status for all peers.

        Returns:
            Mapping of peer_id -> is_connected.
        """
        url = f"{self._api_base}/peers"
        resp = self._session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return {p["id"]: bool(p.get("connected", False)) for p in data if p.get("id")}
