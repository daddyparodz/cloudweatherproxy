"""Explicit physical identity, independent of authentication and cloud forwarding."""
from hashlib import sha256
from ipaddress import ip_address
import json


def numeric_address(value: str) -> str:
    """Validate a numeric address; never resolve hostnames."""
    return str(ip_address(value))


def request_address(request, trusted_proxies: set[str]) -> str | None:
    """Use the transport peer, not HA's potentially rewritten request.remote.

    Only one numeric XFF hop is supported. Reject other forwarding headers and
    untrusted/malformed/ambiguous claims for identity (not for forwarding).
    """
    peer = request.transport.get_extra_info("peername") if request.transport else None
    if not peer:
        return None
    try:
        address = numeric_address(peer[0])
        if "X-Real-IP" in request.headers or "Forwarded" in request.headers:
            return None
        forwarded = request.headers.getall("X-Forwarded-For", [])
        if forwarded:
            if address not in trusted_proxies or len(forwarded) != 1:
                return None
            return numeric_address(forwarded[0].strip())
        if address in trusted_proxies:
            return None
        return address
    except ValueError:
        return None


def station_identity(station, binding: str | None, legacy: bool) -> str | None:
    """Associate protocols only for one explicitly bound physical address."""
    address = station.station_client_ip
    if not address:
        return None
    if binding and address == binding:
        if legacy:
            return ""
        parts = ["physical", binding]
    else:
        if not station.station_id.strip():
            return None
        parts = [station.vendor.value, station.station_id, address]
    return "station_" + sha256(json.dumps(parts).encode()).hexdigest()[:32]
