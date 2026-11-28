"""Built-in Dyna: three full Type policies on the shared, governed runtime.

The tuple is UI/catalogue order, not default-selection precedence.
Flash remains the declared default Type.
"""
from .contracts import BuiltinMode

MODE = BuiltinMode(
    id="dyna", policy_filename="dyna.json",
    type_ids=("miracle", "flash", "strong"),
    default_type="flash", enabled=True,
)
