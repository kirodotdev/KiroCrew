"""Private owners behind Spec Builder's route facade.

Each module owns one lifecycle concern: the request prologue, directory
serialization, process-owned dispatch generations, autonomous execution state,
the decision outbox relay, and the mutating route families. ``handlers`` remains
the import surface for the route composition; nothing outside the backend
imports this package directly.
"""
