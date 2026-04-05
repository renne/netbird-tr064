"""TR-064 backend — vendor-agnostic implementation using raw HTTP/SOAP.

TR-064 is a Broadband Forum standard implemented by Fritz!Box, Teltonika,
and many other home/SOHO routers. This backend uses only the standard
LANIPRoute:1 and WANIPConnection:1 services defined in the TR-064 spec.

No vendor-specific libraries are used; only `requests` and the Python
standard library's `xml.etree.ElementTree`.
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


def _param(name: str, value: str) -> str:
    return f"      <{name}>{value}</{name}>\n"


def _cidr_to_mask(cidr: str) -> tuple[str, str]:
    """Convert CIDR notation to (dest_ip, dotted-decimal mask)."""
    net = ipaddress.IPv4Network(cidr, strict=False)
    return str(net.network_address), str(net.netmask)


class TR064Backend(RouterBackend):
    """TR-064 compliant router backend.

    Supports any router implementing LANIPRoute:1 service, including
    Fritz!Box models, Teltonika RUT series, and others.
    """

    def __init__(self, config: dict) -> None:
        self._base_url = config["url"].rstrip("/")
        self._username = config.get("username", "")
        self._password = config.get("password", "")
        self._gateway_ip: Optional[str] = config.get("gateway_ip") or None

        self._session = requests.Session()
        self._session.auth = HTTPDigestAuth(self._username, self._password)

        # Discovered from tr64desc.xml
        self._lan_control_url: Optional[str] = None
        self._lan_service_type: Optional[str] = None
        self._wan_control_url: Optional[str] = None
        self._wan_service_type: Optional[str] = None

        self._discover()

    # ------------------------------------------------------------------ #
    # Discovery                                                            #
    # ------------------------------------------------------------------ #

    def _discover(self) -> None:
        """Fetch tr64desc.xml and locate LANIPRoute:1 and WANIPConnection:1."""
        desc_url = f"{self._base_url}/tr64desc.xml"
        resp = self._session.get(desc_url, timeout=10)
        resp.raise_for_status()

        root = ET.fromstring(resp.text)
        ns = {"tr": "urn:dslforum-org:device-1-0"}

        for service in root.iter("service"):
            stype = service.findtext("serviceType", default="")
            control_path = service.findtext("controlURL", default="")
            if not stype or not control_path:
                continue

            control_url = f"{self._base_url}{control_path}"

            if "LANIPRoute:1" in stype:
                self._lan_service_type = stype
                self._lan_control_url = control_url
                log.debug("Found LANIPRoute:1 at %s", control_url)

            if "WANIPConnection:1" in stype or "WANPPPConnection:1" in stype:
                if self._wan_control_url is None:
                    self._wan_service_type = stype
                    self._wan_control_url = control_url
                    log.debug("Found WAN connection service at %s", control_url)

        if self._lan_control_url is None:
            raise RuntimeError(
                f"LANIPRoute:1 service not found in TR-064 description at {desc_url}"
            )

    # ------------------------------------------------------------------ #
    # SOAP helper                                                          #
    # ------------------------------------------------------------------ #

    def _soap(self, control_url: str, service_type: str, action: str,
              params: dict | None = None) -> ET.Element:
        """Execute a TR-064 SOAP action and return the response Body element."""
        param_str = ""
        if params:
            for k, v in params.items():
                param_str += _param(k, v)

        body = SOAP_TEMPLATE.format(
            soap_ns=SOAP_NS,
            soap_enc=SOAP_ENC,
            action=action,
            service_type=service_type,
            params=param_str,
        )

        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{service_type}#{action}"',
        }

        resp = self._session.post(control_url, data=body.encode(), headers=headers,
                                  timeout=10)

        # Fall back to Basic Auth if the server did not negotiate Digest
        if resp.status_code == 401:
            www_auth = resp.headers.get("WWW-Authenticate", "")
            if "Digest" not in www_auth:
                log.debug("Server requires Basic Auth, retrying")
                self._session.auth = (self._username, self._password)
                resp = self._session.post(control_url, data=body.encode(),
                                          headers=headers, timeout=10)

        resp.raise_for_status()

        root = ET.fromstring(resp.text)
        body_el = root.find(f"{{{SOAP_NS}}}Body")
        if body_el is None:
            raise RuntimeError(f"Malformed SOAP response from {control_url}")
        return body_el

    def _lan_action(self, action: str, params: dict | None = None) -> ET.Element:
        return self._soap(self._lan_control_url, self._lan_service_type,
                          action, params)

    # ------------------------------------------------------------------ #
    # RouterBackend interface                                              #
    # ------------------------------------------------------------------ #

    def get_routes(self) -> set[tuple[str, str]]:
        body = self._lan_action("GetLANIPRouteNumberOfEntries")
        count_el = body.find(".//{*}NewNumberOfEntries")
        if count_el is None or not count_el.text:
            return set()
        count = int(count_el.text)

        routes: set[tuple[str, str]] = set()
        for i in range(count):
            entry_body = self._lan_action("GetGenericLANIPRouteEntry",
                                          {"NewIndex": str(i)})
            dest = entry_body.findtext(".//{*}NewDestIPAddress", default="").strip()
            mask = entry_body.findtext(".//{*}NewDestSubnetMask", default="").strip()
            if dest and mask:
                routes.add((dest, mask))
        return routes

    def add_route(self, dest: str, mask: str, gateway: str) -> None:
        self._lan_action("AddLANIPRoute", {
            "NewDestIPAddress": dest,
            "NewDestSubnetMask": mask,
            "NewGatewayIPAddress": gateway,
            "NewRouteType": "1",
            "NewRouteMetric": "0",
        })
        log.info("Added route %s/%s via %s", dest, mask, gateway)

    def delete_route(self, dest: str, mask: str) -> None:
        self._lan_action("DeleteLANIPRoute", {
            "NewDestIPAddress": dest,
            "NewDestSubnetMask": mask,
        })
        log.info("Deleted route %s/%s", dest, mask)

    def get_default_gateway(self) -> str:
        if self._gateway_ip:
            return self._gateway_ip

        # Try to read the gateway from WAN connection service first
        if self._wan_control_url:
            try:
                body = self._soap(self._wan_control_url, self._wan_service_type,
                                  "GetConnectionTypeInfo")
                gw = body.findtext(".//{*}NewDefaultGateway", default="").strip()
                if gw:
                    return gw
            except Exception:
                pass

        # Fallback: scan LAN routes for a default route (dest 0.0.0.0/0.0.0.0)
        routes = self.get_routes()
        for dest, mask in routes:
            if dest == "0.0.0.0" and mask == "0.0.0.0":
                # Would need gateway column — not always available; best effort
                log.warning("Cannot auto-detect gateway from routing table without "
                            "gateway column; set gateway_ip in config")
                break

        raise RuntimeError(
            "Cannot determine default gateway. Set 'gateway_ip' explicitly in config."
        )
