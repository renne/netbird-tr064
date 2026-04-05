from abc import ABC, abstractmethod


class RouterBackend(ABC):
    """Abstract base class for router backends.

    Each backend manages static LAN IP routes on a single router.
    Only routes injected by this service (identified by matching gateway IP)
    are ever modified or deleted -- foreign routes are never touched.
    """

    @abstractmethod
    def get_routes(self) -> set[tuple[str, str, str]]:
        """Return the set of currently configured static routes.

        Returns:
            Set of (dest_ip, subnet_mask, gateway_ip) tuples,
            e.g. {("10.0.0.0", "255.255.255.0", "10.0.0.29")}
        """

    @abstractmethod
    def add_route(self, dest: str, mask: str, gateway: str) -> None:
        """Add a static LAN IP route.

        Args:
            dest:    Destination IP address (e.g. "10.0.0.0")
            mask:    Subnet mask in dotted-decimal form (e.g. "255.255.255.0")
            gateway: Gateway IP address
        """

    @abstractmethod
    def delete_route(self, dest: str, mask: str) -> None:
        """Remove a static LAN IP route.

        Args:
            dest: Destination IP address
            mask: Subnet mask in dotted-decimal form
        """

    @abstractmethod
    def get_default_gateway(self) -> str:
        """Return the default gateway IP used by this router for LAN-side routing.

        Used to determine ownership of routes when gateway_ip is not specified
        in config.
        """
