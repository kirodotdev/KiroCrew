"""Business adapters for the Jev decision seam.

Two adapters. ``skills.select`` picks the skill a message loads: an exact offered
key selects one, an explicit no-skill answer selects none, and a refusal keeps
trigger matching. ``message.steer`` decides whether a message sent into a RUNNING
turn steers it or queues for the next one, and a refusal takes the steer path the
composer has always defaulted to. Each adapter's refusal is the shipped behaviour,
never a third outcome.

The core package owns transport, sampling and diagnostic logging.
"""

# Skill identifiers must remain exact when passed to the loader. Drop a key
# exceeding this bound rather than truncating it into a different identifier.
MAX_KEY_CHARS = 120
