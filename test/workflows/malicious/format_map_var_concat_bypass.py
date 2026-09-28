META = {"name": "evil-format-map-bypass"}


async def workflow(ctx):
    # Bypass: format_map takes the same runtime template by a different name,
    # so closing only .format would leave this open.
    left = "{c."
    right = "_session_key}"
    return (left + right).format_map({"c": ctx})
