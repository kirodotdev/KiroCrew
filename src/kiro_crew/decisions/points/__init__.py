"""Business adapters for the Jev decision seam.

``skills.select`` is the sole adapter. An exact offered key selects a skill,
an explicit no-skill answer selects none, and a refusal keeps trigger matching.
The core package owns transport, sampling and diagnostic logging.
"""

# Skill identifiers must remain exact when passed to the loader. Drop a key
# exceeding this bound rather than truncating it into a different identifier.
MAX_KEY_CHARS = 120
