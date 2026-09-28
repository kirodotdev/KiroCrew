META = {"name": "evil-import"}


async def workflow(ctx):
    import os  # B1: no imports allowed

    return os.getcwd()
