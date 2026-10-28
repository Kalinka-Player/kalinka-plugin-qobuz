"""Announces this player to the Qobuz app's device picker over mDNS.

Protocol details adapted from Pibuz (https://github.com/PhilipVinc/pibuz,
crates/pibuz/src/qconnect/pairing.rs), MIT License:
Copyright (c) 2024 blitzkriegfc, Copyright (c) 2026 Filippo Vicentini.
"""

import logging
import socket
import sys
from dataclasses import dataclass
from typing import Protocol

import ifaddr
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

logger = logging.getLogger(__name__.split(".")[-1])

SERVICE_TYPE = "_qobuz-connect._tcp.local."

# Container and VM bridges: advertising them hands the app an address it
# usually cannot reach.
_VIRTUAL_ADAPTERS = ("docker", "br-", "virbr", "veth")

_MAX_LABEL_BYTES = 63

# <linux/in.h>; Python does not export it.
_IP_MULTICAST_ALL = 49


# By name, the type cloud_session.DEVICE_TYPE sends as a number.
DEVICE_TYPE = "STREAMER"


class NoAddressesError(OSError):
    """No LAN IPv4 address to advertise."""


@dataclass(frozen=True)
class Advert:
    friendly_name: str
    device_uuid: str
    port: int
    sdk_version: str

    def properties(self) -> dict[str, str]:
        return {
            "path": "/streamcore",
            "type": DEVICE_TYPE,
            "sdk_version": self.sdk_version,
            "Name": self.friendly_name,
            "device_uuid": self.device_uuid,
        }


class Advertiser(Protocol):
    async def start(self, advert: Advert) -> None: ...

    async def stop(self) -> None: ...


@dataclass(frozen=True)
class Endpoint:
    """One LAN address, and the interface it is on."""

    interface: str
    address: str


def sanitize_instance_name(name: str, max_bytes: int = _MAX_LABEL_BYTES) -> str:
    """An mDNS-safe instance label; the display name travels in the TXT ``Name``."""
    out = []
    for char in name:
        if char.isalnum() or char == "_":
            out.append(char)
        elif not out or out[-1] != "-":
            out.append("-")
    label = "".join(out).strip("-") or "Kalinka"
    return label.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def lan_ipv4_endpoints() -> list[Endpoint]:
    endpoints = []
    for adapter in ifaddr.get_adapters():
        if str(adapter.name).startswith(_VIRTUAL_ADAPTERS):
            continue
        for ip in adapter.ips:
            if not ip.is_IPv4 or ip.ip.startswith(("127.", "169.254.")):
                continue
            if all(ip.ip != known.address for known in endpoints):
                endpoints.append(Endpoint(str(adapter.name), ip.ip))
    return endpoints


def instance_labels(friendly_name: str, endpoints: list[Endpoint]) -> list[str]:
    """One instance label per endpoint, unique on every network they share.

    A single endpoint keeps the plain name. Several get their interface as a
    suffix, and an interface with several addresses the address too: their
    responders are independent, and two answering for one name on one network
    would trip RFC 6762 conflict defence.
    """
    if len(endpoints) == 1:
        return [sanitize_instance_name(friendly_name)]
    per_interface: dict[str, int] = {}
    for endpoint in endpoints:
        per_interface[endpoint.interface] = per_interface.get(endpoint.interface, 0) + 1
    labels = []
    for endpoint in endpoints:
        suffix = sanitize_instance_name(endpoint.interface)
        if per_interface[endpoint.interface] > 1:
            suffix += "-" + socket.inet_aton(endpoint.address).hex()
        name = sanitize_instance_name(friendly_name, _MAX_LABEL_BYTES - len(suffix) - 1)
        labels.append(f"{name}-{suffix}")
    return labels


def hear_only_own_network(zeroconf: AsyncZeroconf) -> bool:
    """Keep a responder's listening socket to the network it joined.

    zeroconf listens on the mDNS port of every address and joins the group on
    one interface only, but Linux hands such a socket each network's queries,
    so the responder for one network would answer the others' with an address
    they cannot reach. False where that cannot be changed, which leaves it so.
    """
    if not sys.platform.startswith("linux"):
        return False
    engine = getattr(getattr(zeroconf, "zeroconf", None), "engine", None)
    listen = getattr(engine, "_listen_socket", None)
    if listen is None:
        return False
    try:
        listen.setsockopt(socket.IPPROTO_IP, _IP_MULTICAST_ALL, 0)
    except OSError:
        return False
    return True


class ZeroconfAdvertiser:
    """Registers ``_qobuz-connect._tcp`` on each LAN address while started.

    Each address gets its own responder, bound to it and announcing only it,
    so the app never learns an address from a network it cannot reach: a
    player on two networks is dropped from the device picker when the app
    tries the other one first.
    """

    def __init__(self):
        self._announced: list[tuple[AsyncZeroconf, ServiceInfo]] = []

    async def start(self, advert: Advert) -> None:
        """@throw NoAddressesError without a LAN address; the first zeroconf
        error when no address could be announced."""
        endpoints = lan_ipv4_endpoints()
        if not endpoints:
            raise NoAddressesError("no LAN IPv4 address")
        failures: list[tuple[Endpoint, Exception]] = []
        try:
            for endpoint, label in zip(endpoints, instance_labels(advert.friendly_name, endpoints)):
                info = ServiceInfo(
                    SERVICE_TYPE,
                    f"{label}.{SERVICE_TYPE}",
                    port=advert.port,
                    properties=advert.properties(),
                    # Named after the instance, not the machine, so it never
                    # contends with the system's own mDNS responder for the
                    # host's A record.
                    server=f"{label}.local.",
                    addresses=[socket.inet_aton(endpoint.address)],
                )
                zeroconf = AsyncZeroconf(
                    ip_version=IPVersion.V4Only, interfaces=[endpoint.address]
                )
                if len(endpoints) > 1 and not hear_only_own_network(zeroconf):
                    logger.info(
                        "Qobuz Connect on %s may also answer other networks' queries",
                        endpoint.interface,
                    )
                try:
                    await zeroconf.async_register_service(info, allow_name_change=True)
                except BaseException as exc:
                    await zeroconf.async_close()
                    if not isinstance(exc, Exception):
                        raise
                    failures.append((endpoint, exc))
                    continue
                self._announced.append((zeroconf, info))
        except BaseException:
            await self.stop()
            raise
        if not self._announced:
            raise failures[0][1]
        for endpoint, exc in failures:
            logger.warning(
                "Qobuz Connect not advertised on %s (%s): %s",
                endpoint.interface,
                endpoint.address,
                type(exc).__name__,
            )
        logger.info(
            "Advertising Qobuz Connect device %r on %s, port %d",
            advert.friendly_name,
            ", ".join(
                f"{info.parsed_addresses()[0]} as {info.name}" for _, info in self._announced
            ),
            advert.port,
        )

    async def stop(self) -> None:
        announced, self._announced = self._announced, []
        if not announced:
            return
        for zeroconf, info in announced:
            try:
                await zeroconf.async_unregister_service(info)
            except Exception as exc:
                logger.warning(
                    "Qobuz Connect: could not withdraw %s: %s", info.name, type(exc).__name__
                )
            finally:
                await zeroconf.async_close()
        logger.info("Stopped advertising Qobuz Connect")
