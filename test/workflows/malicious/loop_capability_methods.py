META = {"name": "evil-loop-capability-methods"}


async def workflow(ctx):
    # Event-loop methods that run work outside the script sandbox in the
    # gateway's own identity. Referencing them by name must be rejected as
    # defence in depth, independent of whether a loop handle was reached.
    handle = ctx.args
    return (handle.subprocess_shell, handle.subprocess_exec, handle.run_in_executor)
