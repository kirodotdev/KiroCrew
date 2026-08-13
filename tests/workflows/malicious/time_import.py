META = {"name": "evil-determinism"}


async def workflow(ctx):
    # B3: time/random/uuid break determinism — import is rejected
    from time import time as now

    return now()
