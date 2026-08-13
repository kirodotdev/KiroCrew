META = {"name": "evil-format-traversal"}
async def workflow(ctx):
    # B2 variant: the dunder walk hides inside a str.format field, so it is not
    # an Attribute node — only .format triggers it. Must still be rejected.
    return "{0.__class__.__mro__}".format(ctx)
