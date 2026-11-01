"""Grade a streamed answer prefix against its original notice predecessor."""

from collections.abc import Callable

from kiro_crew.messaging.display_safety import canonicalize_display
from kiro_crew.messaging.renderer import CREDENTIAL_REDACTION_TAGS, repaired_after_a_sent_tail

# Bounds retained answer content; whitespace alone cannot resolve a credential anchor.
NOTICE_SEAM_PREFIX_MAX = 4096


class NoticeAnswerPrefix:
    def __init__(self, predecessor: str, redactor: Callable[[str], str]) -> None:
        self._predecessor = predecessor
        self._redactor = redactor
        self._prefix = ""
        self._prefix_size = 0

    def feed(self, text: str) -> str:
        if not self._predecessor or not text:
            return text
        self._prefix_size = min(NOTICE_SEAM_PREFIX_MAX + 1, self._prefix_size + len(text))
        self._prefix += text
        if (
            self._prefix_size > NOTICE_SEAM_PREFIX_MAX
            and not canonicalize_display(self._prefix).strip()
        ):
            # Invisible formatting can separate a credential anchor from its token.
            # Keep the unresolved anchor without retaining the unbounded separator.
            self._predecessor = self._predecessor[-NOTICE_SEAM_PREFIX_MAX:]
            self._prefix = "\n"
            return text
        repaired = repaired_after_a_sent_tail(self._predecessor, self._prefix, self._redactor)
        if self._prefix_size > NOTICE_SEAM_PREFIX_MAX:
            self._predecessor = ""
            self._prefix = ""
        if repaired is None:
            return text
        # Earlier chunks may already be visible. Only release this chunk's suffix
        # that survived repair of the whole prefix; a chunk boundary is not proof
        # that the credential ending in the next chunk has finished.
        suffix = 0
        for original, replacement in zip(reversed(text), reversed(repaired)):
            if original != replacement:
                break
            suffix += 1
        if suffix == len(text):
            return text
        return CREDENTIAL_REDACTION_TAGS[0] + (text[-suffix:] if suffix else "")
