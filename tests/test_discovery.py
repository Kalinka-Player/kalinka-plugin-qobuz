"""What the Qobuz app sees in its device picker, without touching the network."""

import socket
from types import SimpleNamespace

import pytest

from kalinka_plugin_qobuz.connect import discovery
from kalinka_plugin_qobuz.connect.discovery import (
    SERVICE_TYPE,
    Advert,
    Endpoint,
    NoAddressesError,
    ZeroconfAdvertiser,
    instance_labels,
    lan_ipv4_endpoints,
    sanitize_instance_name,
)


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


def _adapter(name, *ips):
    return SimpleNamespace(
        name=name,
        ips=[SimpleNamespace(ip=ip, is_IPv4=isinstance(ip, str)) for ip in ips],
    )


class _FakeZeroconf:
    instances = []
    # Address -> the error registering on it raises.
    failing = {}

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        _FakeZeroconf.instances.append(self)

    async def async_register_service(self, info, **kwargs):
        self.calls.append(("register", info, kwargs))
        failure = _FakeZeroconf.failing.get(self.kwargs["interfaces"][0])
        if failure:
            raise failure

    async def async_unregister_service(self, info):
        self.calls.append(("unregister", info))

    async def async_close(self):
        self.calls.append(("close",))


@pytest.fixture
def lan(monkeypatch):
    adapters = [
        _adapter("lo", "127.0.0.1"),
        _adapter("eth0", "192.168.1.20", ("fe80::1", 0, 2)),
        _adapter("docker0", "172.17.0.1"),
        _adapter("wlan0", "169.254.3.4", "10.0.0.5"),
    ]
    monkeypatch.setattr(discovery.ifaddr, "get_adapters", lambda: adapters)
    _FakeZeroconf.instances = []
    _FakeZeroconf.failing = {}
    monkeypatch.setattr(discovery, "AsyncZeroconf", _FakeZeroconf)
    return adapters


ADVERT = Advert(
    friendly_name="Kalinka (living room)",
    device_uuid="5b0a3c52-3a2e-4b8e-9a51-6a4d2f0e7d11",
    port=8183,
    sdk_version="kalinka-qobuz-4.0.0",
)


def test_only_reachable_lan_ipv4_addresses_are_advertised(lan):
    assert lan_ipv4_endpoints() == [Endpoint("eth0", "192.168.1.20"), Endpoint("wlan0", "10.0.0.5")]


@pytest.mark.parametrize(
    "name, instance",
    [
        ("Kalinka (living room)", "Kalinka-living-room"),
        ("  ", "Kalinka"),
        ("Küche_1", "Küche_1"),
        ("x" * 80, "x" * 63),
    ],
)
def test_instance_names_are_mdns_safe(name, instance):
    assert sanitize_instance_name(name) == instance


def test_one_endpoint_keeps_the_plain_name():
    assert instance_labels("Kalinka (living room)", [Endpoint("wlan0", "10.0.0.5")]) == [
        "Kalinka-living-room"
    ]


def test_several_endpoints_are_told_apart_by_interface_and_then_address():
    endpoints = [
        Endpoint("eth0", "192.168.1.20"),
        Endpoint("wlan0", "10.0.0.5"),
        Endpoint("wlan0", "10.0.0.6"),
    ]

    assert instance_labels("Kalinka (living room)", endpoints) == [
        "Kalinka-living-room-eth0",
        "Kalinka-living-room-wlan0-0a000005",
        "Kalinka-living-room-wlan0-0a000006",
    ]


def test_a_long_name_keeps_its_interface_suffix():
    labels = instance_labels("x" * 80, [Endpoint("eth0", "1.2.3.4"), Endpoint("wlan0", "5.6.7.8")])

    assert labels == ["x" * 58 + "-eth0", "x" * 57 + "-wlan0"]
    assert all(len(label.encode()) <= 63 for label in labels)


@pytest.mark.asyncio
async def test_the_service_carries_the_properties_the_app_reads(lan):
    advertiser = ZeroconfAdvertiser()

    await advertiser.start(ADVERT)

    zeroconf = _FakeZeroconf.instances[0]
    kind, info, options = zeroconf.calls[0]
    assert kind == "register" and options == {"allow_name_change": True}
    assert info.type == SERVICE_TYPE
    assert info.name == f"Kalinka-living-room-eth0.{SERVICE_TYPE}"
    assert info.server == "Kalinka-living-room-eth0.local."
    assert info.port == 8183
    # zeroconf hands properties back as given (str) or as encoded (bytes),
    # depending on its version.
    assert {_text(key): _text(value) for key, value in info.properties.items()} == {
        "path": "/streamcore",
        "type": "STREAMER",
        "sdk_version": "kalinka-qobuz-4.0.0",
        "Name": "Kalinka (living room)",
        "device_uuid": ADVERT.device_uuid,
    }


@pytest.mark.asyncio
async def test_each_network_hears_only_its_own_address(lan):
    """An app on one network never gets an address it cannot reach from there."""
    await ZeroconfAdvertiser().start(ADVERT)

    announced = [
        (zeroconf.kwargs["interfaces"], zeroconf.calls[0][1]) for zeroconf in _FakeZeroconf.instances
    ]
    assert [(bound, info.addresses) for bound, info in announced] == [
        (["192.168.1.20"], [socket.inet_aton("192.168.1.20")]),
        (["10.0.0.5"], [socket.inet_aton("10.0.0.5")]),
    ]
    assert len({info.name for _, info in announced}) == 2


class _Socket:
    def __init__(self):
        self.options = []

    def setsockopt(self, level, option, value):
        self.options.append((level, option, value))


def test_a_responder_is_kept_to_the_network_it_joined(monkeypatch):
    monkeypatch.setattr(discovery.sys, "platform", "linux")
    listen = _Socket()
    zeroconf = SimpleNamespace(zeroconf=SimpleNamespace(engine=SimpleNamespace(_listen_socket=listen)))

    assert discovery.hear_only_own_network(zeroconf) is True
    assert listen.options == [(socket.IPPROTO_IP, 49, 0)]


def test_a_zeroconf_without_that_socket_is_left_as_it_is(monkeypatch):
    monkeypatch.setattr(discovery.sys, "platform", "linux")

    assert discovery.hear_only_own_network(SimpleNamespace()) is False


def test_only_linux_is_told(monkeypatch):
    monkeypatch.setattr(discovery.sys, "platform", "darwin")
    listen = _Socket()
    zeroconf = SimpleNamespace(zeroconf=SimpleNamespace(engine=SimpleNamespace(_listen_socket=listen)))

    assert discovery.hear_only_own_network(zeroconf) is False
    assert listen.options == []


@pytest.mark.asyncio
async def test_stop_withdraws_the_service_then_closes(lan):
    advertiser = ZeroconfAdvertiser()
    await advertiser.start(ADVERT)

    await advertiser.stop()
    await advertiser.stop()

    for zeroconf in _FakeZeroconf.instances:
        assert [call[0] for call in zeroconf.calls] == ["register", "unregister", "close"]


@pytest.mark.asyncio
async def test_without_an_address_nothing_is_started(monkeypatch, lan):
    monkeypatch.setattr(discovery.ifaddr, "get_adapters", lambda: [_adapter("lo", "127.0.0.1")])

    with pytest.raises(NoAddressesError):
        await ZeroconfAdvertiser().start(ADVERT)
    assert _FakeZeroconf.instances == []


@pytest.mark.asyncio
async def test_a_failed_registration_releases_zeroconf(lan):
    _FakeZeroconf.failing = {
        "192.168.1.20": OSError("no multicast"),
        "10.0.0.5": OSError("no multicast"),
    }

    with pytest.raises(OSError):
        await ZeroconfAdvertiser().start(ADVERT)

    for zeroconf in _FakeZeroconf.instances:
        assert [call[0] for call in zeroconf.calls] == ["register", "close"]


@pytest.mark.asyncio
async def test_one_network_failing_leaves_the_others_advertised(lan, caplog):
    _FakeZeroconf.failing = {"192.168.1.20": OSError("no multicast")}
    advertiser = ZeroconfAdvertiser()

    await advertiser.start(ADVERT)

    failed, working = _FakeZeroconf.instances
    assert [call[0] for call in failed.calls] == ["register", "close"]
    assert [call[0] for call in working.calls] == ["register"]
    assert "not advertised on eth0" in caplog.text

    await advertiser.stop()
    assert [call[0] for call in working.calls] == ["register", "unregister", "close"]
