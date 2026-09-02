# Fixtures for semgrep/find-sentinel-truthiness.yaml, exercised by
# `semgrep --test` in the SAST job. `ruleid:` asserts the NEXT line MUST
# match; `ok:` asserts it must NOT. The negatives encode the precision
# contract: explicit -1 comparisons and index arithmetic stay clean.


def _boolean_context_misuses(text: str, needle: str) -> None:
    # ruleid: kirocrew.find-sentinel-in-boolean-context
    if text.find(needle):
        pass
    # ruleid: kirocrew.find-sentinel-in-boolean-context
    if text.rfind(needle):
        pass
    # ruleid: kirocrew.find-sentinel-in-boolean-context
    if not text.find(needle):
        pass
    # ruleid: kirocrew.find-sentinel-in-boolean-context
    while text.find(needle):
        break


def _short_circuit_misuses(text: str, limit: int) -> int:
    # ruleid: kirocrew.find-sentinel-in-boolean-context
    cut = text.rfind("\n") or limit
    # ruleid: kirocrew.find-sentinel-in-boolean-context
    other = text.find(":") and limit
    return cut + other


def _explicit_comparisons_are_fine(text: str, needle: str, limit: int) -> int:
    # ok: kirocrew.find-sentinel-in-boolean-context
    if text.find(needle) != -1:
        pass
    # ok: kirocrew.find-sentinel-in-boolean-context
    if text.rfind(needle) == -1:
        return limit
    # ok: kirocrew.find-sentinel-in-boolean-context
    pos = text.rfind("\n")
    if pos == -1:
        pos = limit
    # ok: kirocrew.find-sentinel-in-boolean-context
    if text.find(needle) >= 0:
        pass
    # ok: kirocrew.find-sentinel-in-boolean-context
    if needle in text:
        pass
    return pos
