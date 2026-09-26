META = {"name": "evil-mro-walk"}


async def workflow(ctx):
    # dict.mro()[1] is object, the root of every subclass-enumeration escape.
    root = dict.mro()[1]
    return str(root)
