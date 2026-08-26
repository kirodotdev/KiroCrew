META = {"name": "evil-eval"}


async def workflow(ctx):
    # B1: eval/exec/compile are forbidden builtins
    return eval("__import__('os').system('id')")
