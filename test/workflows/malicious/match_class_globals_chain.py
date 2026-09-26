META = {"name": "evil-match-class-globals-chain"}


async def workflow(ctx):
    # B2: walk object -> subclasses -> __init__ -> __globals__ using only
    # class-pattern keywords, with no dunder Attribute or Name node anywhere.
    root = dict.mro()[1]
    match root:
        case root(__subclasses__=subs):
            for k in subs():
                match k:
                    case root(__init__=init):
                        match init:
                            case root(__globals__=g):
                                return len(g)
    return None
