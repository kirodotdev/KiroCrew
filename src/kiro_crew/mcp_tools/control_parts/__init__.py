"""Owner modules of :mod:`kiro_crew.mcp_tools.control`.

``control`` is the import path and the patch seam; the code lives here, one responsibility
per module, and new code for a responsibility goes to its module. The facade's mirror
(:mod:`kiro_crew._facade_mirror`) sends a write through ``control`` to every module below
that binds the name, so a patch of ``control.<name>`` reaches every reader. This package
re-exports nothing.
"""
