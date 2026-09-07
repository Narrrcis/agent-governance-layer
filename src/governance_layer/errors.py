"""Exception types with explicit fail-closed semantics.

The governance layer distinguishes two kinds of failure, because they must be
handled in opposite ways.

A *governance* failure means the request itself is not authorized: the
permission is malformed, expired, or issued to a different agent. Absorbing one
of these into a fallback would turn the layer fail-open, so they always
propagate to the caller.

An *adapter* failure means a downstream component the layer merely delegates to
did not work. Only this kind may be degraded into the scalar fallback path, and
only after the request has already passed authorization.
"""

from __future__ import annotations


class GovernanceError(Exception):
    """Base class for every failure raised by this package."""


class PermissionInvalidError(GovernanceError, ValueError):
    """The permission cannot authorize this order.

    Subclasses ``ValueError`` so that existing callers that catch ``ValueError``
    keep working. Never caught by the hybrid fallback.
    """


class PermissionExpiredError(PermissionInvalidError):
    """The evaluation time falls outside the permission's validity window."""


class GovernanceAdapterError(GovernanceError):
    """A downstream adapter failed while executing an authorized decision.

    This is the only exception the hybrid path degrades into the scalar
    fallback. Adapters must wrap their own failures in this type; anything else
    propagates and fails the order closed.
    """
