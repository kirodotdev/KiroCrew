META = {"name": "evil-globals"}


async def workflow(ctx):
    # B2: reach the real builtins via a function's __globals__
    g = workflow.__globals__
    return g
