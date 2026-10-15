"""Private owners composed by :mod:`kiro_crew.apps.builtins.aws_control.backend.routes`.

No production module outside this package imports them except the routes facade, which
serves every name they hold and is the one import path and patch surface of the AWS
Control routes: a write through it reaches every owner holding the name
(:mod:`kiro_crew._facade_mirror`). Each owner imports only owners the facade lists before
it, and never the facade.
"""
