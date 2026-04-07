"""NetBird API client.

Fetches routes from the NetBird management API using a personal access token.
Compatible with both self-hosted and cloud (api.netbird.io) deployments.

Uses the Networks API (GET /api/networks + /resources + /routers) which
replaced the deprecated GET /api/routes endpoint.
"""

import ipaddress
import logging

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
        """Return subnet routes grouped by peer ID via the Networks API.

        Iterates /api/networks, then /api/networks/{id}/resources and
        /api/networks/{id}/routers for each network.  Only IPv4 subnet
        resources are included; masquerade=true and disabled routers are
        skipped.

        Returns:
            Mapping of peer_id -> set of canonical CIDR strings.
        """
        networks_url = f"{self._api_base}/networks"
        resp = self._session.get(networks_url, timeout=15)
        resp.raise_for_status()
        networks = resp.json()

        result: dict[str, set[str]] = {}

        for network in networks:
            net_id = network.get("id", "")
            net_name = network.get("name", net_id)

            # Collect enabled IPv4 subnet CIDRs for this network
            res_resp = self._session.get(
                f"{self._api_base}/networks/{net_id}/resources", timeout=15
            )
            res_resp.raise_for_status()

            cidrs: set[str] = set()
            for resource in res_resp.json():
                if only_enabled and not resource.get("enabled", True):
                    continue
                if resource.get("type", "") != "subnet":
                    continue
                address = resource.get("address", "").strip()
                if not address:
                    continue
                try:
                    net = ipaddress.IPv4Network(address, strict=False)
                    cidrs.add(str(net))
                except (ValueError, ipaddress.AddressValueError):
                    log.debug(
                        "Skipping non-IPv4 resource: %s in network %s", address, net_name
                    )

            if not cidrs:
                continue

            # Associate CIDRs with each non-masquerade, enabled router
            rtr_resp = self._session.get(
                f"{self._api_base}/networks/{net_id}/routers", timeout=15
            )
            rtr_resp.raise_for_status()

            for router in rtr_resp.json():
                if only_enabled and not router.get("enabled", True):
                    continue
                if router.get("masquerade", False):
                    log.debug("Skipping masquerade=true router in network %s", net_name)
                    continue
                peer_id = router.get("peer", "").strip()
                if not peer_id:
                    continue
                result.setdefault(peer_id, set()).update(cidrs)

        return result

    def get_router_metrics(self, only_enabled: bool = True) -> dict[str, int]:
        """Return the lowest NetBird routing metric for each peer.

        Iterates /api/networks → /api/networks/{id}/routers.  If a peer
        appears as a router in multiple networks, the minimum metric is
        returned.  Masquerade=true and disabled routers are excluded (same
        filters as :meth:`get_routes`).

        Returns:
            Mapping of peer_id -> metric (1–9999).  Peers absent from any
            network router list are not included; callers should default to
            9999 for missing entries.
        """
        networks_url = f"{self._api_base}/networks"
        resp = self._session.get(networks_url, timeout=15)
        resp.raise_for_status()
        networks = resp.json()

        result: dict[str, int] = {}

        for network in networks:
            net_id = network.get("id", "")

            rtr_resp = self._session.get(
                f"{self._api_base}/networks/{net_id}/routers", timeout=15
            )
            rtr_resp.raise_for_status()

            for router in rtr_resp.json():
                if only_enabled and not router.get("enabled", True):
                    continue
                if router.get("masquerade", False):
                    continue
                peer_id = router.get("peer", "").strip()
                if not peer_id:
                    continue
                metric = int(router.get("metric", 9999))
                if peer_id not in result or metric < result[peer_id]:
                    result[peer_id] = metric

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

    def get_overlay_network(self) -> str:
        """Return the NetBird overlay network CIDR.

        Tries ``GET /api/accounts`` → ``settings.network_range`` first (available in
        recent NetBird versions).  Falls back to inferring the /16 supernet from peer
        IPs, and finally to the standard CGNAT range ``100.64.0.0/10``.

        Returns:
            CIDR string, e.g. ``"100.64.0.0/16"`` or ``"100.91.0.0/16"``.
        """
        _fallback = "100.64.0.0/10"

        # 1. Try account settings (NetBird ≥ 0.37 exposes settings.network_range)
        try:
            url = f"{self._api_base}/accounts"
            resp = self._session.get(url, timeout=15)
            resp.raise_for_status()
            accounts = resp.json()
            if accounts:
                network_range = accounts[0].get("settings", {}).get("network_range")
                if network_range:
                    log.debug("Overlay network from account settings: %s", network_range)
                    return network_range
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not fetch account settings: %s", exc)

        # 2. Infer from peer IPs — compute the common /16 supernet
        try:
            url = f"{self._api_base}/peers"
            resp = self._session.get(url, timeout=15)
            resp.raise_for_status()
            peers = resp.json()
            overlay_ips = [
                ipaddress.IPv4Address(p["ip"])
                for p in peers
                if p.get("ip")
            ]
            if not overlay_ips:
                log.debug("No peer IPs available; using fallback overlay CIDR %s", _fallback)
                return _fallback
            supernets = {
                ipaddress.IPv4Network(f"{ip}/16", strict=False) for ip in overlay_ips
            }
            if len(supernets) == 1:
                cidr = str(supernets.pop())
                log.debug("Inferred overlay network from peer IPs: %s", cidr)
                return cidr
            log.debug(
                "Peer IPs span multiple /16 blocks (%s); using fallback %s",
                supernets,
                _fallback,
            )
            return _fallback
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not infer overlay network from API: %s — using %s", exc, _fallback)
            return _fallback
