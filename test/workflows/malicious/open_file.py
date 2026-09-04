META = {"name": "evil-open"}


async def workflow(ctx):
    # B7: no filesystem egress — open() is a forbidden builtin
    with open("/home/user/.config/.env") as fh:
        return fh.read()
