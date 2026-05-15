"""Sync conditions — `should_sync` decision logic.

The actual D-Bus probes (`query_network`, `query_power`) talk to
NetworkManager + UPower so we can't unit-test those without
mocking the whole bus. The *decision* on top of them is pure
Python — monkeypatch the probes, exercise each branch of the
network-pref / charging-only matrix.

This is the matrix that gates background sync. A regression here
means the user's "Wi-Fi only" or "while charging" pref silently
stops working, which they only notice when their data cap takes
a hit or their phone runs flat overnight.
"""

import pytest

from shoebox.sync import conditions
from shoebox.sync.conditions import NetworkState, PowerState, should_sync


def _stub_states(monkeypatch, *, net: NetworkState, power: PowerState):
    monkeypatch.setattr(conditions, "query_network", lambda: net)
    monkeypatch.setattr(conditions, "query_power", lambda: power)


# --- offline always blocks ----------------------------------------------

def test_offline_always_blocks(monkeypatch):
    _stub_states(
        monkeypatch,
        net=NetworkState(online=False, on_wifi=False, metered=False),
        power=PowerState(on_battery=False),
    )
    allowed, reason = should_sync(network_pref="any", charging_only=False)
    assert allowed is False
    assert "Offline" in reason


# --- network preference matrix ------------------------------------------

def test_any_network_allows_when_online(monkeypatch):
    _stub_states(
        monkeypatch,
        net=NetworkState(online=True, on_wifi=False, metered=True),
        power=PowerState(on_battery=False),
    )
    allowed, _ = should_sync(network_pref="any", charging_only=False)
    assert allowed is True


def test_wifi_only_blocks_on_cellular(monkeypatch):
    _stub_states(
        monkeypatch,
        net=NetworkState(online=True, on_wifi=False, metered=True),
        power=PowerState(on_battery=False),
    )
    allowed, reason = should_sync(network_pref="wifi", charging_only=False)
    assert allowed is False
    assert "Wi-Fi" in reason


def test_wifi_only_allows_on_wifi(monkeypatch):
    _stub_states(
        monkeypatch,
        net=NetworkState(online=True, on_wifi=True, metered=False),
        power=PowerState(on_battery=False),
    )
    allowed, _ = should_sync(network_pref="wifi", charging_only=False)
    assert allowed is True


def test_unmetered_blocks_on_metered_wifi(monkeypatch):
    """A metered Wi-Fi (hotspot tethering, cellular bridge) still
    counts the bytes — `unmetered` must refuse it."""
    _stub_states(
        monkeypatch,
        net=NetworkState(online=True, on_wifi=True, metered=True),
        power=PowerState(on_battery=False),
    )
    allowed, reason = should_sync(network_pref="unmetered", charging_only=False)
    assert allowed is False
    assert "metered" in reason.lower()


def test_unmetered_allows_on_unmetered(monkeypatch):
    _stub_states(
        monkeypatch,
        net=NetworkState(online=True, on_wifi=True, metered=False),
        power=PowerState(on_battery=False),
    )
    allowed, _ = should_sync(network_pref="unmetered", charging_only=False)
    assert allowed is True


# --- charging-only -----------------------------------------------------

def test_charging_only_blocks_on_battery(monkeypatch):
    _stub_states(
        monkeypatch,
        net=NetworkState(online=True, on_wifi=True, metered=False),
        power=PowerState(on_battery=True),
    )
    allowed, reason = should_sync(network_pref="any", charging_only=True)
    assert allowed is False
    assert "charging" in reason.lower()


def test_charging_only_allows_when_plugged_in(monkeypatch):
    _stub_states(
        monkeypatch,
        net=NetworkState(online=True, on_wifi=True, metered=False),
        power=PowerState(on_battery=False),
    )
    allowed, _ = should_sync(network_pref="any", charging_only=True)
    assert allowed is True


def test_charging_only_false_ignores_battery_state(monkeypatch):
    _stub_states(
        monkeypatch,
        net=NetworkState(online=True, on_wifi=True, metered=False),
        power=PowerState(on_battery=True),
    )
    allowed, _ = should_sync(network_pref="any", charging_only=False)
    assert allowed is True


# --- network + power compounded ---------------------------------------

def test_wifi_only_plus_charging_only_both_required(monkeypatch):
    """The most-restrictive user setting — both predicates must
    pass. Common phone default: "Wi-Fi + charging" = overnight
    upload."""
    _stub_states(
        monkeypatch,
        net=NetworkState(online=True, on_wifi=False, metered=False),
        power=PowerState(on_battery=False),
    )
    allowed, _ = should_sync(network_pref="wifi", charging_only=True)
    assert allowed is False  # offline on cellular

    _stub_states(
        monkeypatch,
        net=NetworkState(online=True, on_wifi=True, metered=False),
        power=PowerState(on_battery=True),
    )
    allowed, _ = should_sync(network_pref="wifi", charging_only=True)
    assert allowed is False  # wifi but discharging

    _stub_states(
        monkeypatch,
        net=NetworkState(online=True, on_wifi=True, metered=False),
        power=PowerState(on_battery=False),
    )
    allowed, _ = should_sync(network_pref="wifi", charging_only=True)
    assert allowed is True   # both satisfied
