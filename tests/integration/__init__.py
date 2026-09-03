"""L6 — integration tests: the real seams, end to end.

Every module here drives the WHOLE pipeline — policy document -> loader -> risk engine -> advisory ->
governor -> authorization -> ledger append -> execution gate -> broker -> decision replay -> chain
verification — through the shipped public entry points, with no component replaced by a stand-in.

The single exception is the broker, and it is declared in ``tests/integration/_world.py``: there is no
broker to integrate against in a test process, so :class:`mizan.adapters.MockBroker` (a shipped
adapter, not a test double invented here) plays the venue. Nothing between the caller and the broker
is mocked, patched or monkeypatched.
"""
