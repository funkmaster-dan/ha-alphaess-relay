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
    if "Pbat" in parsed:
        power = float(parsed["Pbat"])
        if math.isfinite(power):
            reading["battery_power_w"] = power
    return reading
