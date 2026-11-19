"""Verified caller identities and explicit per-instance permissions."""
from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from typing import Any, Callable
from mcp.server.auth.middleware.auth_context import get_access_token
from .outcomes import Refused


@dataclass(frozen=True)
class Caller:
    issuer: str
    subject: str
    author: str
    role: str


def verified_caller(issuer: str, subject: str, role: Any) -> Caller:
    """Canonical attribution; each authentication mode owns the supplied role."""
    if (not isinstance(issuer, str) or not issuer or not isinstance(subject, str) or not subject
            or not isinstance(role, str) or role not in {"reader", "writer"}):
        raise ValueError("a verified caller needs issuer, subject and a reader/writer role")
    return Caller(issuer, subject, json.dumps([issuer, subject], separators=(",", ":")), role)


def calling_caller() -> Caller:
    access = get_access_token()
    claims = access.claims if access is not None else None
    data = claims.get("boring_caller") if isinstance(claims, dict) else None
    if not isinstance(data, dict):
        raise Refused("forbidden", "this call has no verified service identity")
    return Caller(**data)


def calling_author() -> str:
    return calling_caller().author


def require(permission: str) -> None:
    if permission not in {"reader", "writer"}:
        raise ValueError("permission must be reader or writer")
    caller = calling_caller()
    if permission == "writer" and caller.role != "writer":
        raise Refused("forbidden", "this caller has reader access; writer access is required")


def permitted(fn: Callable[..., Any], permission: str) -> Callable[..., Any]:
    """Authorize before domain work. Tool annotations are not access policy."""
    if permission not in {"reader", "writer"}:
        raise ValueError("permission must be reader or writer")
    @functools.wraps(fn)
    def call(*args: Any, **kwargs: Any) -> Any:
        require(permission)
        return fn(*args, **kwargs)
    return call
