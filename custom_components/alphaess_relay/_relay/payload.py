# Generated from tools/alpha_payload.py; edit the canonical source.
"""Decode captured AlphaESS payloads. No network or device operations.

Recovered from EMS3.6 V0.11.633, Thumb function 0x0800c5fc. Its caller at
0x0806cbba transforms the payload and then recalculates the outer wire CRC.
Each payload resets a 32-bit feedback state; feedback uses signed ciphertext
bytes. This is specific to the observed protocol, not a general firmware claim.
"""
import datetime
from dataclasses import dataclass
import json
import math


def encode_payload(plaintext):
    """Encode bytes offline; inverse of decrypt_payload, not a device operation."""
    state = 0xA621
    ciphertext = bytearray()
    for byte in plaintext:
        encoded = byte ^ ((state >> 8) & 0xFF)
        ciphertext.append(encoded)
        signed_byte = encoded if encoded < 128 else encoded - 256
        state = ((state + signed_byte) * 0x67A1 + 0x154F2) & 0xFFFFFFFF
    return bytes(ciphertext)


def decrypt_payload(ciphertext):
    state = 0xA621
    plaintext = bytearray()
    for byte in ciphertext:
        plaintext.append(byte ^ ((state >> 8) & 0xFF))
        signed_byte = byte if byte < 128 else byte - 256
        state = ((state + signed_byte) * 0x67A1 + 0x154F2) & 0xFFFFFFFF
    return bytes(plaintext)


@dataclass(frozen=True)
class DecodedPayload:
    format: str
    data: object
    layers: int


def decode_payload(payload, *, max_layers=2):
    """Recognize JSON/CSV, with a bounded number of feedback transforms.

    One captured live frame requires two layers; all other nonempty main
    protocol frames in the corpus require one. Plain JSON/CSV is recognized
    for legacy-port compatibility, which is not a verified local-server mode.
    This function does not remove bytes or repair malformed JSON.
    """
    if not 0 <= max_layers <= 2:
        raise ValueError("At most two observed encoding layers are supported")
    if not payload:
        return DecodedPayload("empty", None, 0)
    candidate = bytes(payload)
    for layers in range(max_layers + 1):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, (dict, list)):
                return DecodedPayload("json", parsed, layers)
        except (ValueError, UnicodeDecodeError):
            pass
        if candidate.startswith(b"Date,Time,"):
            try:
                return DecodedPayload("csv", candidate.decode("utf-8"), layers)
            except UnicodeDecodeError:
                pass
        candidate = decrypt_payload(candidate)
    raise ValueError("Payload is not supported JSON/CSV within two encoding layers")


def _power(parsed, key):
    """An absent or invalid optional measurement must not invent a zero."""
    value = parsed.get(key)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _power_total(values):
    if any(value is None for value in values):
        return None
    total = sum(values)
    return total if math.isfinite(total) else None


def telemetry_reading(parsed):
    """Return validated, minimal live-telemetry fields; omit other payload data."""
    if not isinstance(parsed, dict):
        raise ValueError("Telemetry must be a JSON object")
    serial = parsed.get("SN")
    device_time = parsed.get("Time")
    if not isinstance(serial, str) or not serial:
        raise ValueError("Missing telemetry serial number")
    if not isinstance(device_time, str):
        raise ValueError("Missing telemetry time")
    datetime.datetime.strptime(device_time, "%Y/%m/%d %H:%M:%S")
    soc = float(parsed["SOC"])
    if not math.isfinite(soc) or not 0 <= soc <= 100:
        raise ValueError("SoC is not a finite percentage from 0 to 100")
    reading = {"serial": serial, "device_time": device_time, "soc_pct": soc}
    battery = _power(parsed, "Pbat")
    # AlphaESS generation includes the external PV meter as well as all four
    # inverter PV strings. Grid is signed import (+) / export (-).
    generation = _power_total([_power(parsed, key) for key in
                               ("Ppv1", "Ppv2", "Ppv3", "Ppv4", "PmeterDC")])
    grid = _power_total([_power(parsed, f"PmeterL{phase}") for phase in (1, 2, 3)])
    # Derived from the same report, matching cloud ppv + pgrid + pbat. Native
    # Pbat is positive discharge / negative charge; do not substitute Preal.
    load = _power_total([generation, grid, battery])
    for key, value in (("battery_power_w", battery), ("generation_w", generation),
                       ("grid_io_w", grid), ("home_load_w", load)):
        if value is not None:
            reading[key] = value
    return reading
