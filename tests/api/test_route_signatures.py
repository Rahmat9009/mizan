"""Regression pins for two bugs that a route-level test cannot see, and that ship silently.

**The ``Request`` annotation.** ``mizan.api`` uses ``from __future__ import annotations``, so every
route's annotations are strings that FastAPI resolves against the *module's* globals. When ``Request``
was imported inside ``create_app`` instead, that resolution failed, FastAPI reinterpreted
``request: Request`` as a required **query parameter**, and every route taking the request object
answered 422 before its first line ran. Nothing complains at import time; the app builds, the routes
register, and only a real request finds it. So the check lives here, at the signature level, where it
fails for a comprehensible reason.

**Route parity.** ``ROUTES`` is what the console and the SDK agree with. A route registered but not
listed (or listed but not registered) is a surface nobody audits.
"""

from __future__ import annotations

import pytest

from mizan.api import ROUTES, StaticTokenStore, create_app


@pytest.fixture
def app(pipelines):
    return create_app(lambda tenant_id: pipelines.get(tenant_id), tokens=StaticTokenStore())


def registered(app) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or ()
        if path is None or not path.startswith("/v1"):
            continue
        for method in methods:
            if method in {"GET", "POST"}:
                found.add((method, path))
    return found


def test_every_declared_route_is_registered_and_no_others_are(app):
    assert registered(app) == set(ROUTES)


def test_no_route_treats_the_request_object_as_a_query_parameter(app):
    """The bug this pins: ``Request`` resolvable only inside ``create_app``'s locals.

    FastAPI records what it decided each parameter was. If ``Request`` could not be resolved, the
    request object appears in ``dependant.query_params`` — and the route is unusable.
    """
    offenders: list[str] = []
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for parameter in dependant.query_params:
            if parameter.name in {"request", "principal"}:
                offenders.append(f"{getattr(route, 'path', '?')}: {parameter.name} became a query param")
    assert not offenders, offenders


def test_request_is_resolvable_from_module_scope():
    """The structural reason the above holds. Keep the import at module scope."""
    import mizan.api as api

    assert getattr(api, "Request", None) is not None
    assert getattr(api, "Depends", None) is not None
    assert api.FASTAPI_AVAILABLE is True


def test_every_v1_route_carries_a_security_dependency(app):
    """No route may be reachable without the credential dependency, health excepted (F-3).

    Health resolves its principal in the body instead, because it must answer an anonymous caller —
    which is exactly why it is the one route allowed to have no dependency here.
    """
    anonymous: list[str] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/v1") or path == "/v1/health":
            continue
        dependant = getattr(route, "dependant", None)
        assert dependant is not None, path
        if not dependant.dependencies:
            anonymous.append(path)
    assert not anonymous, anonymous


def test_the_app_exposes_no_route_outside_v1_or_the_documented_set(app):
    paths = {getattr(route, "path", "") for route in app.routes}
    surprises = {
        path
        for path in paths
        if path.startswith("/") and not path.startswith("/v1") and path not in {"/openapi.json"}
    }
    assert surprises <= {"/docs", "/docs/oauth2-redirect", "/redoc"}, surprises
