"""The Share My Crew deploy machinery: driver, templates, bundle curation, image.

What lives here, and why each part is shaped the way it is:

``scripts/``    ``smc-deploy.sh``, the 13-gate deploy driver, plus its gate suite
                and two seam guards under ``scripts/tests/``. It ships as a shell
                script -- the same form as
                ``kiro_crew/deploy/skills/artifact-deploy/scripts/*.sh`` -- because
                rewriting 2,300 lines in python would discard 147 gate tests and a
                driver proven against a real account.
``templates/``  the two CloudFormation templates the driver deploys.
``packaging/``  bundle curation: what of the owner's crew travels into an image,
                and the deny-by-default guards on what must not.
``runtime/``    the deployed container's own source and its Dockerfiles. NOT a
                python package: it is a docker build context, and the gateway
                must never import it (pinned by
                ``test_spawn_audit.py::test_container_image_assets_are_not_imported``).
``*.md``        the two contracts that still describe live invariants -- what each
                memory mode claims, and the crew-in-image seam.

This ``__init__.py`` is load-bearing in one non-obvious way. It makes
``packaging/tests/`` a fully-qualified subpackage, so pytest resolves those tests
without putting this directory on ``sys.path``. Without it, pytest prepends this
directory instead, and ``packaging`` here then SHADOWS the PyPA ``packaging``
distribution for every other test in the same worker -- a name nothing in this
repository imports today, which is exactly the kind of landmine that goes off in
an unrelated change months later.

The driver still invokes the curator as ``python -m packaging.build`` with this
directory as cwd, which is its contract in PACKAGING-CONTRACT.md. That runs in a
CHILD process, so the shadow it relies on is scoped to that child and cannot
reach the gateway.
"""
