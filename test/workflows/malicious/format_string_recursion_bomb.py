META = {"name": "evil-recursion-bomb"}
async def workflow(ctx):
    # DoS: deeply nested format specs cause unbounded recursion in the
    # validator's _format_field_reasons(). Without a depth limit, this
    # raises RecursionError -> HTTP 500 instead of a validation error.
    # 20 levels of nesting exceeds the depth cap.
    return "{0:{0:{0:{0:{0:{0:{0:{0:{0:{0:{0:{0:{0:{0:{0:{0:{0:{0:{0:{0:x}}}}}}}}}}}}}}}}}}}}".format(ctx)
