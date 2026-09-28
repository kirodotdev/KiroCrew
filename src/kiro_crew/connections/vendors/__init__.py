"""Container anchor for per-provider connector code (W01 · L01).

Each provider stream (W02..W14) owns its own ``vendors/<slug>/`` subpackage --
GitHub's code under ``vendors/github/``, Slack's under ``vendors/slack/``, and
so on. This ``__init__`` creates the container ONCE, on purpose, for two
reasons.

First, PACKAGING. ``setup.cfg`` builds with ``packages = find:``
(``find_packages``), which discovers only directories that CONTAIN an
``__init__.py`` and drops every subpackage under a directory that lacks one --
even a subpackage that has its own ``__init__.py``. So without this committed
file, ``vendors/`` and every ``vendors/<slug>/`` beneath it would be absent from
the wheel/sdist build artifact, and a normal (non-editable) install would ship
none of the provider code. (This is a build-artifact concern, not a source
concern: under a source/editable tree PEP 420 implicit namespace packages let
``vendors.<slug>`` import fine even with no ``__init__.py`` -- the file is what
puts it in the BUILT package, not what makes the import possible.)

Second, RACE AVOIDANCE. A single owner mints the anchor here so that several
provider streams landing in parallel do not each try to create ``vendors/`` and
collide. L01 creates the anchor and NOTHING under it -- no ``vendors/<slug>/``
subdirectory is this slice's to make.

The name is ``vendors`` and NOT ``providers`` deliberately:
``src/kiro_crew/providers/`` already exists and means LLM providers, so a
``connections/providers/`` here would be the same word for a different thing in
one package tree. ``vendors`` is the third-party-account sense, kept distinct.
"""
