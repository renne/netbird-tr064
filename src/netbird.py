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

    def get_routes(self, only_enabled: bool = True) -> set[str]:
        """Return the set of route CIDRs currently defined in NetBird.

        Each entry is a canonical CIDR string (e.g., "10.1.0.0/24").
        Only IPv4 routes are returned; IPv6 entries are silently skipped.
        """
        url = f"{self._api_base}/routes"
        resp = self._session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        cidrs: set[str] = set()
        for route in data:
            if only_enabled and not route.get("enabled", True):
                continue
            network = route.get("network", "").strip()
            if not network:
                continue
            try:
                net = ipaddress.IPv4Network(network, strict=False)
                cidrs.add(str(net))
            except (ValueError, ipaddress.AddressValueError):
                # Skip IPv6 or malformed entries
                log.debug("Skipping non-IPv4 route: %s", network)
        return cidrs
