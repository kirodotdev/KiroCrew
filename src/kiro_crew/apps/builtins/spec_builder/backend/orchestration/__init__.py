"""Private owners behind Spec Builder's route facade.

Each module owns one lifecycle concern: the request prologue, directory
serialization, process-owned dispatch generations, autonomous execution state,
the decision outbox relay, and the mutating route families. ``handlers`` remains
the import surface for the route composition; no production module outside the
backend imports this package. Tests reach it through ``tests/routes_facade.py`` or
by importing an owner directly.
"""
