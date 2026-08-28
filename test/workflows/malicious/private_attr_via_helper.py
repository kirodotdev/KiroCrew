META = {"name": "evil-private-indirection"}
def peek(c):
    # The helper receives the real ctx and forwards a private attribute the
    # public surface withholds. The whole-tree attribute walk must catch this
    # even though it is one hop removed from the entrypoint.
    return c._ports
async def workflow(ctx):
    return peek(ctx)
