META = {"name": "evil-match-class-private"}


async def workflow(ctx):
    # A class pattern keyword reaching a private ctx attribute.
    match ctx:
        case dict(_session_key=k):
            return k
    return None
