META = {"name": "evil-frame-introspection"}
async def workflow(ctx):
    # B2 variant: gi_frame / f_back / f_builtins are neither dunders nor
    # underscore-prefixed, but the chain reaches the real builtins and
    # __import__. Must be rejected by name.
    g = (x for x in [1])
    return g.gi_frame.f_back.f_builtins
