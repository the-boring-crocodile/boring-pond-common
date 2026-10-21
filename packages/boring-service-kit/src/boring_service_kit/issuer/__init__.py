"""A server's own OAuth authorization server (boring-service-kit 0.5.0).

- `store`: the SQLite store (grants in `records`, clients with origin and state in `clients`).
- `clients`: the client policy (pre-registration, metadata documents, optional
  dynamic registration) and the admin's client list.
- `provider`: `SQLiteIssuer`, the SDK's authorization server provider.
- `web`: SDK settings, the approval page, the advertised metadata.
- `cli`: `boring-service-oauth`, the admin's tool for a service's own store.

The Crocodile builds on the same classes; a service gets all of it from the
`issuer` section of its configuration (`boring_service_kit.http`).
"""
from .clients import DEFAULT_METADATA_HOSTS, ClientPolicy, IssuerClient
from .provider import ConsentDetails, Denied, SQLiteIssuer, Target, canonical_resource

__all__ = ["DEFAULT_METADATA_HOSTS", "ClientPolicy", "ConsentDetails", "Denied", "IssuerClient",
           "SQLiteIssuer", "Target", "canonical_resource"]
