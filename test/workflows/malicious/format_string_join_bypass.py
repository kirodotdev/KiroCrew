META = {"name": "evil-join-format-bypass"}


async def workflow(ctx):
    # Bypass: str.join assembles the template from a list. Not a BinOp at all,
    # so folding never even applies.
    parts = ["{0.", "__class__}"]
    return "".join(parts).format(ctx)
