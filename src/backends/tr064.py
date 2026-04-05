"""TR-064 backend — uses Layer3Forwarding:1 service (AVM Fritz!Box and compatible routers).

No vendor-specific libraries are used; only `requests` and the Python
standard library's `xml.etree.ElementTree`.

Correct parameters confirmed from avmtools (Gincules/avmtools) and AVM SCPD:
  NewType      = "Host"
  NewInterface = "LanHostConfigManagement1"
"""

import ipaddress
import logging
import xml.etree.ElementTree as ET
from typing import Optional

import requests
from requests.auth import HTTPDigestAuth

from .base import RouterBackend

log = logging.getLogger(__name__)

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP_ENC = "http://schemas.xmlsoap.org/soap/encoding/"

SOAP_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{soap_ns}" s:encodingStyle="{soap_enc}">
  <s:Body>
    <u:{action} xmlns:u="{service_type}">
{params}    </u:{action}>
  </s:Body>
</s:Envelope>"""

SERVICE_TYPE = "urn:dslforum-org:service:Layer3Forwarding:1"
ROUTE_TYPE = "Host"
ROUTE_INTERFACE = "LanHostConfigManagement1"


def _param(name: str, value: str) -> str:
    return f"      <{name}>{value}</{name}>\n"


def _cidr_to_mask(cidr: str) -> tuple[str, str]:
    """Convert CIDR notation to (dest_ip, dotted-decimal mask)."""
    net = ipaddress.IPv4Network(cidr, strict=False)
    return str(net.network_address), str(net.netmask)


class TR064Backend(RouterBackend):
    """TR-064 Layer3Forwarding:1 backend for Fritz!Box routers."""

    def __init__(self, config: dict) -> None:
        self._base_url = config["url"].rstrip("/")
        self._username = config.get("username", "")
        self._password = config.get("password", "")
        self._gateway_ip: Optional[str] = config.get("gateway_ip") or None

        self._session = requests.Session()
        self._session.auth = HTTPDigestAuth(self._username, self._password)

        self._control_url: Optional[str] = None
        self._discover()

    # ------------------------------------------------------------------ #
    # Discovery                                                            #
    # ------------------------------------------------------------------ #

    def _discover(self) -> None:
        """Fetch tr64desc.xml and locate Layer3Forwarding:1 control URL."""
        desc_url = f"{self._base_url}/tr64desc.xml"
        resp = self._session.get(desc_url, timeout=10)
        resp.raise_for_status()

        root = ET.fromstring(resp.text)

        for service in root.iter("service"):
            stype = service.findtext("serviceType", default="")
            control_path = service.findtext("controlURL", default="")
            if "Layer3Forwarding:1" in stype and control_path:
                self._control_url = f"{self._base_url}{control_path}"
                log.debug("Found Layer3Forwarding:1 at %s", self._control_url)
                return

        raise RuntimeError(
            f"Layer3Forwarding:1 service not found in TR-064 description at {desc_url}"
        )

    # ------------------------------------------------------------------ #
    # SOAP helper                                                          #
    # ------------------------------------------------------------------ #

    def _soap(self, action: str, params: dict | None = None) -> ET.Element:
        """Execute a Layer3Forwarding:1 SOAP action and return the response Body."""
        param_str = ""
        if params:
            for k, v in params.items():
                param_str += _param(k, str(v))

        body = SOAP_TEMPLATE.format(
            soap_ns=SOAP_NS,
            soap_enc=SOAP_ENC,
            action=action,
            service_type=SERVICE_TYPE,
            params=param_str,
        )

        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "soapaction": f'"{SERVICE_TYPE}#{action}"',
        }

        resp = self._session.post(
            self._control_url, data=body.encode(), headers=headers, timeout=10
        )

        if resp.status_code == 401:
            www_auth = resp.headers.get("WWW-Authenticate", "")
            if "Digest" not in www_auth:
                log.debug("Server requires Basic Auth, retrying")
                self._session.auth = (self._username, self._password)
                resp = self._session.post(
                    self._control_url, data=body.encode(), headers=headers, timeout=10
                )

        resp.raise_for_status()

        root = ET.fromstring(resp.text)
        body_el = root.find(f"{{{SOAP_NS}}}Body")
        if body_el is None:
            raise RuntimeError(
                f"Malformed SOAP response for action {action}"
            )
        return body_el

    # ------------------------------------------------------------------ #
    # RouterBackend interface                                              #
    # ------------------------------------------------------------------ #

    def get_routes(self) -> set[tuple[str, str]]:
        body = self._soap("GetForwardNumberOfEntries")
        count_el = body.find(".//{*}NewForwardNumberOfEntries")
        if count_el is None or not count_el.text:
            return set()
        count = int(count_el.text)

        routes: set[tuple[str, str]] = set()
        for i in range(count):
            entry = self._soap("GetGenericForwardingEntry",
                               {"NewForwardingIndex": str(i)})
            dest = entry.findtext(".//{*}NewDestIPAddress", default="").strip()
            mask = entry.findtext(".//{*}NewDestSubnetMask", default="").strip()
            if dest and mask:
                routes.add((dest, mask))
        return routes

    def get_route_gateway(self, dest: str, mask: str) -> Optional[str]:
        """Return the gateway IP for a specific route, or None if not found."""
        body = self._soap("GetForwardNumberOfEntries")
        count_el = body.find(".//{*}NewForwardNumberOfEntries")
        if count_el is None or not count_el.text:
            return None
        count = int(count_el.text)

        for i in range(count):
            entry = self._soap("GetGenericForwardingEntry",
                               {"NewForwardingIndex": str(i)})
            e_dest = entry.findtext(".//{*}NewDestIPAddress", default="").strip()
            e_mask = entry.findtext(".//{*}NewDestSubnetMask", default="").strip()
            if e_dest == dest and e_mask == mask:
                return entry.findtext(".//{*}NewGatewayIPAddress", default="").strip()
        return None

    def add_route(self, dest: str, mask: str, gateway: str) -> None:
        self._soap("AddForwardingEntry", {
            "NewType": ROUTE_TYPE,
            "NewDestIPAddress": dest,
            "NewDestSubnetMask": mask,
            "NewSourceIPAddress": "0.0.0.0",
            "NewSourceSubnetMask": "0.0.0.0",
            "NewGatewayIPAddress": gateway,
            "NewInterface": ROUTE_INTERFACE,
            "NewForwardingMetric": "0",
        })
        log.info("Added route %s/%s via %s", dest, mask, gateway)

    def delete_route(self, dest: str, mask: str) -> None:
        self._soap("DeleteForwardingEntry", {
            "NewDestIPAddress": dest,
            "NewDestSubnetMask": mask,
            "NewSourceIPAddress": "0.0.0.0",
            "NewSourceSubnetMask": "0.0.0.0",
        })
        log.info("Deleted route %s/%s", dest, mask)

    def get_default_gateway(self) -> str:
        if self._gateway_ip:
            return self._gateway_ip
        raise RuntimeError(
            "gateway_ip must be set in config — it should be the LAN IP of the "
            "routing peer through which NetBird overlay routes are reachable."
        )
