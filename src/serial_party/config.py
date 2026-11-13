"""Validated host configuration shared by the broker tools and text client."""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re

DEFAULT_CONFIG = "/etc/serial-party/config.json"
ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
DEVICE_PATTERN = re.compile(r"/dev/[A-Za-z0-9_./:+@=-]+\Z")
BAUD_RATES = {50, 75, 110, 134, 150, 200, 300, 600, 1200, 1800, 2400,
              4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800,
              500000, 576000, 921600, 1000000, 1152000, 1500000, 2000000,
              2500000, 3000000, 3500000, 4000000}


class ConfigError(ValueError):
    """The supplied configuration cannot safely describe this installation."""


def _require(condition, message):
    if not condition:
        raise ConfigError(message)


def _identifier(value, field):
    _require(isinstance(value, str) and ID_PATTERN.fullmatch(value),
             f"{field} must start with a lowercase letter and contain only letters, digits, _ or - (32 characters maximum)")
    return value


def _port(value, field):
    _require(type(value) is int and 1024 <= value <= 65535,
             f"{field} must be an integer from 1024 to 65535")
    return value


def validate_config(data):
    """Return a new normalized mapping. This function never opens a device."""
    _require(isinstance(data, dict), "Configuration must be a JSON object")
    allowed = {"version", "broker_port", "broker_secondary_port", "listen_host", "writers", "boards"}
    _require(not set(data) - allowed, "Unknown top-level configuration fields")
    _require(type(data.get("version")) is int and data["version"] == 1,
             "Configuration version must be 1")
    bind = data.get("listen_host", "127.0.0.1")
    _require(isinstance(bind, str), "listen_host must be an IPv4 bind address")
    try:
        address = ipaddress.IPv4Address(bind)
    except (ipaddress.AddressValueError, TypeError) as error:
        raise ConfigError("listen_host must be an IPv4 bind address") from error
    _require(not address.is_multicast and str(address) != "255.255.255.255",
             "listen_host cannot be a multicast or broadcast address")
    result = {
        "version": 1,
        "broker_port": _port(data.get("broker_port", 7820), "broker_port"),
        "broker_secondary_port": _port(data.get("broker_secondary_port", 7821), "broker_secondary_port"),
        "listen_host": str(address),
    }
    writers = data.get("writers", ["operator", "hermes", "codex", "webcontrol"])
    _require(isinstance(writers, list) and 1 <= len(writers) <= 32, "writers must be a non-empty list (maximum 32)")
    writers = [_identifier(writer, "writer") for writer in writers]
    _require(len(set(writers)) == len(writers), "Writer labels must be unique")
    _require("monitor" not in writers, "monitor is reserved for read-only clients")
    _require("webcontrol" in writers, "writers must include webcontrol for the browser control terminal")
    result["writers"] = writers
    boards = data.get("boards")
    _require(isinstance(boards, list) and 1 <= len(boards) <= 16,
             "boards must contain from 1 to 16 consoles")
    used_ids, used_devices = set(), set()
    used_ports = {result["broker_port"], result["broker_secondary_port"]}
    _require(len(used_ports) == 2, "Broker ports must be different")
    result["boards"] = []
    for board in boards:
        _require(isinstance(board, dict), "Each board must be an object")
        _require(not set(board) - {"id", "name", "device", "baud", "monitor_port", "control_port"},
                 "Unknown board configuration fields")
        identifier = _identifier(board.get("id"), "board.id")
        _require(identifier not in used_ids, "Board IDs must be unique")
        used_ids.add(identifier)
        name = board.get("name", identifier)
        _require(isinstance(name, str) and bool(name.strip()) and len(name) <= 80
                 and name.isprintable(),
                 "Board name must be printable text of 1–80 characters")
        device = board.get("device")
        _require(isinstance(device, str) and DEVICE_PATTERN.fullmatch(device)
                 and ".." not in PurePosixPath(device).parts
                 and str(PurePosixPath(device)) == device,
                 "Board device must be a safe, normalized absolute /dev/ path")
        _require(device not in used_devices, "Device paths must be unique")
        used_devices.add(device)
        baud = board.get("baud", 115200)
        _require(type(baud) is int and baud in BAUD_RATES, "Unsupported baud rate")
        normalized = {"id": identifier, "name": name.strip(), "device": device, "baud": baud}
        for field in ("monitor_port", "control_port"):
            port = _port(board.get(field), f"{identifier}.{field}")
            _require(port not in used_ports, "All broker and terminal ports must be distinct")
            used_ports.add(port)
            normalized[field] = port
        result["boards"].append(normalized)
    return result


parse_config = validate_config


def load_config(path=None):
    """Read a JSON file; SERIAL_PARTY_CONFIG overrides the default path only."""
    selected = Path(path or os.environ.get("SERIAL_PARTY_CONFIG", DEFAULT_CONFIG))
    try:
        data = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConfigError(f"Cannot read a valid JSON configuration from {selected}") from error
    return validate_config(data)


def get_board(config, selector=None):
    """Select by exact ID or configured device path; no fuzzy device matching."""
    if selector is None:
        if len(config["boards"]) == 1:
            return config["boards"][0]
        raise ConfigError("Select a board explicitly when more than one is configured")
    matches = [board for board in config["boards"] if selector in (board["id"], board["device"])]
    if len(matches) != 1:
        raise ConfigError("No configured board matches that ID or device path")
    return matches[0]
