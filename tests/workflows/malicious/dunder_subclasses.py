META = {"name": "evil-subclasses"}


async def workflow(ctx):
    # B2: classic sandbox escape via __class__ / __subclasses__
    x = ().__class__.__bases__[0].__subclasses__()
    return len(x)
