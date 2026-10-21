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


class AccessPolicy:
    """One trusted issuer, explicit subjects; a grant may name the author its data is stored under."""
    def __init__(self, issuer: str, grants: list[dict[str, str]]) -> None:
        if not issuer or not isinstance(grants, list):
            raise ValueError("policy requires issuer and a list of grants")
        # Author IDs name the issuer without a trailing slash, for every issuer. The OAuth
        # metadata may show "https://host/" (pydantic adds "/" to a bare host); stored
        # ownership (Canvas, Knowledge) uses this form, so it stays.
        self.issuer = issuer.rstrip("/")
        self.grants: dict[str, Caller] = {}
        authors: set[str] = set()
        for grant in grants:
            if not isinstance(grant, dict) or set(grant) - {"subject", "role", "author"}:
                raise ValueError("grant supports only subject, role and author")
            subject, role = grant.get("subject"), grant.get("role")
            if not isinstance(subject, str) or not subject or role not in {"reader", "writer"}:
                raise ValueError("grant needs a subject and reader/writer role")
            author = grant.get("author", json.dumps([self.issuer, subject], separators=(",", ":")))
            if not isinstance(author, str) or not author or subject in self.grants or author in authors:
                raise ValueError("subjects and author aliases must be nonempty and unique")
            authors.add(author)
            self.grants[subject] = Caller(self.issuer, subject, author, role)

    def resolve(self, subject: str) -> Caller | None:
        return self.grants.get(subject)


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
