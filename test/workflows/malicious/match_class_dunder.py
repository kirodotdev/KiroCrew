META = {"name": "evil-match-class-dunder"}


async def workflow(ctx):
    # B2: a class pattern keyword is read with getattr at run time, but the
    # name lives in MatchClass.kwd_attrs, not in an Attribute node.
    match {}:
        case dict(__class__=c):
            return str(c)
    return None
