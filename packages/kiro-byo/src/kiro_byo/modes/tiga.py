"""Tiga Mode plug-in identity; all four Type strategies live in tiga.json.

No Python routing or model selection is embedded here. Mode Registry compiles
this Mode's data-only policies through the same shared BYO execution engine.
"""
from .contracts import BuiltinMode

MODE = BuiltinMode(
    id="tiga", policy_filename="tiga.json",
    type_ids=("sky", "multi", "power", "glitter"),
    default_type="multi", enabled=True,
)
