META = {"name": "evil-concat-format-bypass"}
async def workflow(ctx):
    # Bypass: split the format string across two literals joined by +.
    # Each half is individually harmless, but the combined result leaks
    # a private attribute via .format().
    field = "{" + "0._session_key}"
    return field.format(ctx)
