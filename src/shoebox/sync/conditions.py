"""Sync gating: Wi-Fi / metered / charging.

Reads NetworkManager and UPower over D-Bus. Both probes are best-effort —
if the daemons aren't present we return permissive defaults.
"""

from __future__ import annotations

from dataclasses import dataclass

from gi.repository import Gio, GLib

# NetworkManager DeviceType enum (selected values)
NM_DEVICE_TYPE_WIFI = 2

# NetworkManager Metered enum: 1 = yes, 2 = guess-yes, 3 = no, 4 = guess-no, 0 = unknown
NM_METERED_VALUES_METERED = (1, 2)


@dataclass
class NetworkState:
    online: bool
    on_wifi: bool
    metered: bool

    @classmethod
    def unknown(cls) -> NetworkState:
        return cls(online=True, on_wifi=True, metered=False)


@dataclass
class PowerState:
    on_battery: bool

    @classmethod
    def unknown(cls) -> PowerState:
        return cls(on_battery=False)


def _proxy(name: str, path: str, iface: str) -> Gio.DBusProxy | None:
    try:
        return Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SYSTEM,
            Gio.DBusProxyFlags.NONE,
            None,
            name, path, iface, None,
        )
    except GLib.Error:
        return None


def query_network() -> NetworkState:
    nm = _proxy('org.freedesktop.NetworkManager',
                '/org/freedesktop/NetworkManager',
                'org.freedesktop.NetworkManager')
    if nm is None:
        return NetworkState.unknown()

    try:
        connectivity_v = nm.get_cached_property('Connectivity')
        connectivity = connectivity_v.get_uint32() if connectivity_v else 0
        online = connectivity >= 3  # 3=limited, 4=full

        primary_v = nm.get_cached_property('PrimaryConnection')
        primary_path = primary_v.get_string() if primary_v else '/'
        if not primary_path or primary_path == '/':
            return NetworkState(online=online, on_wifi=False, metered=False)

        conn = _proxy('org.freedesktop.NetworkManager', primary_path,
                      'org.freedesktop.NetworkManager.Connection.Active')
        if conn is None:
            return NetworkState(online=online, on_wifi=False, metered=False)

        type_v = conn.get_cached_property('Type')
        ctype = type_v.get_string() if type_v else ''
        on_wifi = ctype in ('802-11-wireless',)

        metered_v = nm.get_cached_property('Metered')
        metered_raw = metered_v.get_uint32() if metered_v else 0
        metered = metered_raw in NM_METERED_VALUES_METERED

        return NetworkState(online=online, on_wifi=on_wifi, metered=metered)
    except GLib.Error:
        return NetworkState.unknown()


def query_power() -> PowerState:
    up = _proxy('org.freedesktop.UPower',
                '/org/freedesktop/UPower',
                'org.freedesktop.UPower')
    if up is None:
        return PowerState.unknown()
    try:
        v = up.get_cached_property('OnBattery')
        return PowerState(on_battery=bool(v.get_boolean()) if v else False)
    except GLib.Error:
        return PowerState.unknown()


def should_sync(*, network_pref: str, charging_only: bool) -> tuple[bool, str]:
    """Return (allowed, reason).

    *network_pref* matches the gschema enum: 'any', 'wifi', or 'unmetered'.
    """
    net = query_network()
    if not net.online:
        return False, 'Offline'
    if network_pref == 'wifi' and not net.on_wifi:
        return False, 'Waiting for Wi-Fi'
    if network_pref == 'unmetered' and net.metered:
        return False, 'Connection is metered'

    if charging_only:
        power = query_power()
        if power.on_battery:
            return False, 'Waiting until charging'

    return True, 'Ready'
