from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from ipaddress import ip_address, ip_network

from app.api.errors import APIError
from app.config import Settings


@dataclass(frozen=True)
class ClientIdentity:
    bucket_hash: str
    key_version: int


def canonical_ip(value: str) -> str:
    if "%" in value:
        raise ValueError
    address = ip_address(value)
    if getattr(address, "ipv4_mapped", None) is not None:
        address = address.ipv4_mapped
    return str(address)


def identify(request, settings: Settings) -> ClientIdentity:
    try:
        peer = canonical_ip(request.client.host if request.client else "")
        address = peer
        if any(ip_address(peer) in ip_network(network) for network in settings.trusted_proxy_networks):
            values = request.headers.getlist(settings.client_ip_header)
            if len(values) != 1:
                raise ValueError
            address = canonical_ip(values[0].strip())
    except ValueError:
        # A malformed trusted header must not collapse all proxy clients into one bucket.
        raise APIError(400, "invalid_client_identity", "Client identity could not be verified.") from None
    digest = hmac.new(settings.rate_limit_hmac_secret.encode(), address.encode(), hashlib.sha256).hexdigest()
    return ClientIdentity(digest, settings.hmac_key_version)
