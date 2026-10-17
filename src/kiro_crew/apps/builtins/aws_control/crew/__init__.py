"""Bundle curation for Share My Crew: what of an owner's crew travels into an image.

``packaging/`` holds all of it -- the curator, its deny-by-default guards on what
must not travel, and its tests. Nothing else lives here yet. The deploy driver, the
CloudFormation templates and the container's own build context arrive with the two
pieces that follow this one, each with the tests that pin it.

This ``__init__.py`` is load-bearing in one non-obvious way. It makes
``packaging/tests/`` a fully-qualified subpackage, so pytest resolves those tests
without putting this directory on ``sys.path``. Without it, pytest prepends this
directory instead, and ``packaging`` here then SHADOWS the PyPA ``packaging``
distribution for every other test in the same worker -- a name nothing in this
repository imports today, which is exactly the kind of landmine that goes off in an
unrelated change months later.

The curator is invoked as ``python -m packaging.build`` with this directory as cwd.
That runs in a CHILD process, so the shadow it relies on is scoped to that child and
cannot reach the gateway. No in-repo caller invokes it yet; the driver that will is
part of a later piece.
"""
