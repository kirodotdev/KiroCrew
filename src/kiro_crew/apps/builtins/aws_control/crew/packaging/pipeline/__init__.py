"""The crew bundle builder's owners, behind the one import path ``packaging.build``.

Private: import the builder as ``packaging.build`` (or its in-package spelling), never an
owner directly, and patch it there. The owners, lowest layer first, each importing only the
ones above it in this list:

``contract``     bundle, plan and report versions, file names, the read ceiling, ``ExportRefused``
``scan``         credential scanning and redacted findings
``sensitive``    credential name, location and standalone path fences
``pinned``       no-follow reads, redirect detection, the reparse-safe walk, directory pins
``destination``  the ``--out`` UNC screen, the parent check, the no-follow writer
``hashing``      the skill content pin, the staged-copy pin, the bundle digest
``crew``         crew-name checks, crew resolution, the agent-spec read
``candidates``   skill and MCP candidate enumeration
``plan``         the curation plan: template, read, merge, verification, decision set
``prompt``       the ``file://`` persona read
``spec``         ``agent.json`` / ``mcp.json`` normalisation
``layout``       the staged leaf writes and the selected-skill copy
``staging``      the run marker, the ownership proof, the private-aside disposal
``report``       the report schema, its ownership check and its publication
``transaction``  ``build_bundle``: stage, promote, roll back
``cli``          the ``plan`` and ``build`` verbs and ``main``

This file imports nothing, so importing the package runs no owner.
"""
