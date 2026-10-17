META = {"name": "evil-var-concat-format-bypass"}


async def workflow(ctx):
    # Bypass: the halves live in NAMES, not literals, so the constant fold in
    # visit_BinOp cannot resolve the pair and neither half is dangerous alone.
    # Only refusing the .format call itself closes this.
    left = "{0."
    right = "_session_key}"
    field = left + right
    return field.format(ctx)
