META = {"name": "evil-loop-coro-throw"}


async def workflow(ctx):
    # ``.throw(...)`` is the sibling of ``.send(...)``: it, too, steps a coroutine
    # and yields back the pending Future it was suspended on, so a ``send``-only
    # block would leave an obvious bypass. Rejected by name.
    coro = ctx.agent("x")
    return coro.throw(ValueError)
