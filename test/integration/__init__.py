"""The integration layer -- a package so its conftest imports as
``integration.conftest`` and never shadows ``test/conftest.py``, which the unit
files import by the bare name ``conftest``."""
