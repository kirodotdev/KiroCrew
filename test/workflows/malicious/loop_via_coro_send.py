META = {"name": "evil-loop-via-coro-send"}


async def workflow(ctx):
    # A workflow script runs inside the gateway's own event loop. It cannot
    # ``await`` and then inspect, but it can step a coroutine by hand: driving a
    # ctx coroutine with ``.send(None)`` hands back the pending Future it is
    # suspended on, and ``.get_loop()`` on that Future reaches the gateway's real
    # loop — a handle no ctx surface is meant to expose. Both the driver and the
    # loop accessor must be rejected by name.
    coro = ctx.agent("x")
    pending = coro.send(None)
    return pending.get_loop()
