"""Shape checks for short LLM-generated labels: refusals, prose and verdicts.

Several paths ask a tool-free background turn for a short, user-visible label
(the dashboard session title, the Slack/Telegram conversation name, the nav
panel link chips, the session-list summary) and store the reply. A tool-free
turn cannot follow a URL, so a pasted link makes the model narrate the denial
("I cannot access external URLs …") instead of labeling, and prompt wording
alone cannot guarantee the shape of a generation. Every label path therefore
validates the reply with the SAME two checks before storing it:

- :func:`is_verdict_reply` -- the reply is a taught control word (``SKIP``,
  ``KEEP``), alone or followed by a reason, so it means "no label";
- :func:`looks_like_prose` -- the reply is a sentence about the task rather
  than a name, so it is discarded and the caller keeps its fallback.

The disposition is the same everywhere: never persist the refusal, keep the
prior/fallback value. The checks were written for the dashboard title and
lived only there, so each new label path started from zero and stored the
sentence the guard exists to stop; this module is the single home so the paths
cannot drift again. The word and unspaced-character ceilings are parameters
because the prompts differ (a title is 3-6 words, a summary is up to 18).
"""

from __future__ import annotations

#: Ceiling on whitespace-separated words for a 3-6 word title. Anything
#: materially longer is the model answering instead of naming, so the ceiling
#: sits above any plausible real title and below a sentence.
TITLE_MAX_WORDS = 12

#: Codepoint ranges of scripts written WITHOUT spaces between words: kana, Han
#: (+ extension A and the compatibility block) and Thai. A label in one of them
#: is a single whitespace token, so the word ceiling can never fire for it --
#: it needs the character ceiling below instead. Hangul, Cyrillic, Devanagari,
#: Bengali and Latin are deliberately absent: those languages space their
#: words, so the word ceiling bounds a long sentence in them. A SHORT refusal
#: clears that ceiling, so it is caught by sentence shape instead -- see
#: ``ends_like_sentence`` and ``KO_SENTENCE_ENDINGS``.
_UNSPACED_SCRIPT_RANGES = (
    (0x0E00, 0x0E7F),  # Thai
    (0x3040, 0x30FF),  # Hiragana + Katakana
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
)

#: Ceiling on characters of unspaced script in a 3-6 word title. The title
#: prompt asks for ~4-14 characters in those languages, so this leaves headroom
#: for a long name while a refusal or an answer runs well past it. Counting
#: only the unspaced characters (not the whole string) keeps latin identifiers
#: free: "修复 PrivacyPanel 的动态键" spends 8 against the budget, not 24.
TITLE_MAX_UNSPACED_CHARS = 24

#: Sentence terminators that are NOT followed by a space in the scripts that use
#: them, so the ASCII rule's whitespace requirement would never fire on them.
_WIDE_TERMINATORS = "。！？"

#: Sentence terminators that ARE followed by a space, like the ASCII ones: the
#: Devanagari danda and double danda (Hindi, Bengali), the Arabic question mark,
#: the Urdu full stop, the Ethiopic full stop and question mark, the Armenian
#: full stop and the Myanmar section mark. Escapes keep the source ASCII.
_SPACED_TERMINATORS = ".!?\u0964\u0965\u061f\u06d4\u1362\u1367\u0589\u104b"

#: Every sentence terminator the end-of-reply rule reads, in any script: the
#: spaced ones above, the full-width/CJK ones and the halfwidth and full-width
#: full stops.
_SENTENCE_TERMINATORS = _SPACED_TERMINATORS + _WIDE_TERMINATORS + "\uff0e\uff61"

#: Commas in any script: ASCII, full-width, ideographic, Arabic, small and
#: halfwidth ideographic, Ethiopic.
_COMMAS = ",\uff0c\u3001\u060c\ufe50\uff64\u1363"

#: Closing quotes and brackets a model may wrap a sentence in. They are peeled
#: off before the last character is read, so a quoted sentence still ends in
#: its terminator.
_CLOSERS = "\"'\u201d\u2019\u300d\u300f\u300b\u00bb)\uff09"

#: A reply that ends in a terminator AND holds this many words reads as a
#: sentence. A 3-6 word title asked for "no punctuation" may still carry a
#: stray final period on a short name ("Fix login bug."), so three words stay
#: a name; four or more words closed by a terminator is a sentence.
SENTENCE_MIN_WORDS = 4

# Openers that mark the reply as prose about the model rather than a name. The
# observed failure was a pasted URL producing "I cannot access external URLs
# like Quip documents. Based solely on the message c…" as the session name.
PROSE_OPENERS = (
    "i cannot",
    "i can not",
    "i can't",
    "i cant",
    "i am unable",
    "i'm unable",
    "i am not able",
    "i'm not able",
    "i do not have",
    "i don't have",
    "i dont have",
    "i was unable",
    "i will not",
    "i won't",
    "i need ",
    "i would need",
    "unable to",
    "cannot access",
    "can't access",
    "cannot fetch",
    "can't fetch",
    "sorry",
    "apologies",
    "unfortunately",
    "as an ai",
    "based solely",
    "based on the",
    "it seems",
    "it looks like",
    "here is",
    "here's",
    "here are",
    "the conversation",
    "this conversation",
    "note:",
)

#: Korean refusal/prose shape. Hangul spaces its words, so the word ceiling
#: bounds a long Korean sentence -- but a refusal is SHORT (five words in the
#: observed case), and ``PROSE_OPENERS`` is English-only, so a short Korean
#: refusal clears every other check. Korean is SOV: the verb that marks a
#: sentence as a sentence comes LAST, so prefix openers cannot catch it --
#: match the sentence-final conjugation instead. The polite declarative
#: endings close the sentence forms a labeling model actually emits: "-nida"
#: (U+B2C8 U+B2E4, the hamnida/seumnida/imnida family) and the informal-polite
#: "-eoyo"/"-ayo"/"-haeyo" (U+C5B4/U+C544/U+D574 + U+C694). A noun-phrase
#: label carries none of them; a plain-form (banmal) refusal stays a documented
#: false negative. The one useful prefix is "joesong" (U+C8C4 U+C1A1, "sorry"),
#: the apology opener. Both signals trade deliberately toward rejection: a
#: sentence-form Korean label ("the login does not work") loses to the
#: fallback, which is the cheaper failure -- a fallback is still the user's own
#: words, a refusal stored as the label is the bug. Escapes keep the source
#: ASCII; the runtime values are the Hangul strings.
KO_PROSE_OPENERS = ("\uc8c4\uc1a1",)
KO_SENTENCE_ENDINGS = (
    "\ub2c8\ub2e4",
    "\uc5b4\uc694",
    "\uc544\uc694",
    "\ud574\uc694",
)


def unspaced_script_chars(s: str) -> int:
    """Count characters belonging to a script written without word spaces."""
    return sum(1 for ch in s if any(lo <= ord(ch) <= hi for lo, hi in _UNSPACED_SCRIPT_RANGES))


def ends_like_sentence(text: str) -> bool:
    """True when *text* ends in a sentence terminator of any script AND holds
    a comma of any script or at least ``SENTENCE_MIN_WORDS`` words.

    This is the language-neutral refusal shape. The label prompts ask for a
    name with no punctuation, and a name is a noun phrase, so it neither ends
    a sentence nor joins clauses. A refusal is a sentence in whatever language
    the model was told to write in ("No puedo acceder a ese enlace.",
    "मैं इस लिंक को नहीं खोल सकता।", "申し訳ありませんが、リンクを開けません。"):
    it closes with its script's terminator, and it is either a clause or
    several words long. Reading the shape instead of a phrase list covers every
    shipped language at once, with no per-locale vocabulary to keep up.

    A path that strips a trailing period before its other checks must call
    this on the reply line BEFORE that strip, or the terminator is gone.
    """
    stripped = text.strip().rstrip(_CLOSERS).rstrip()
    if not stripped or stripped[-1] not in _SENTENCE_TERMINATORS:
        return False
    body = stripped.rstrip(_SENTENCE_TERMINATORS)
    if any(char in _COMMAS for char in body):
        return True
    return len(body.split()) >= SENTENCE_MIN_WORDS


def looks_like_prose(
    text: str,
    *,
    max_words: int = TITLE_MAX_WORDS,
    max_unspaced_chars: int = TITLE_MAX_UNSPACED_CHARS,
    openers: tuple[str, ...] = PROSE_OPENERS,
    sentence_shape: bool = True,
) -> bool:
    """True when an LLM label reply is a sentence about the task, not a label.

    The labeling call is tool-free by contract (``run_bg_oneliner`` rejects
    every permission request), so a message containing a URL can make the model
    narrate the denial instead of labeling -- and that narration was being
    persisted as the label. Prompt wording alone cannot guarantee the shape of
    a generation, so the reply is also validated here and treated as SKIP when
    it fails, which routes to the caller's existing fallback.

    Five signals, each independently sufficient:

    - a refusal/narration opener (see ``PROSE_OPENERS``);
    - more words than the path's contract carries (``max_words``);
    - more unspaced-script characters than the contract carries
      (``max_unspaced_chars``). Chinese, Japanese and Thai put no spaces
      between words, so a whole sentence in them is ONE word by ``str.split``
      and slips past the word ceiling entirely;
    - sentence-terminating punctuation with text after it. A spaced terminator
      (ASCII, the danda, the Arabic question mark ...) must be followed by
      whitespace so "Node.js upgrade plan" and "Ship v1.2 to prod" stay valid;
      the full-width forms must not, because the scripts that use them do not
      space after punctuation. A reply that ENDS in a terminator of any script
      and holds a comma or ``SENTENCE_MIN_WORDS`` words is prose too (see
      :func:`ends_like_sentence`): the language-neutral refusal shape.
    - Korean sentence shape (see ``KO_SENTENCE_ENDINGS``). Hangul spaces its
      words, but a refusal is short enough to clear the word ceiling, and a
      prefix opener cannot catch an SOV language whose refusal verb comes last
      -- so the sentence-final polite conjugation is matched instead, a grammar
      fact rather than a phrase list.

    The two ceilings default to the 3-6 word title contract; a path whose
    prompt asks for a longer label (the 18-word session summary) passes its
    own so a legitimate long reply is not mistaken for prose. ``openers`` lets
    a path drop the entries that describe ITS legitimate output (a summary may
    well open with "the conversation"), and ``sentence_shape=False`` turns off
    the terminator and Korean-conjugation signals for a path whose label is a
    descriptive sentence by contract -- those two signals tell a name from a
    sentence, and cannot tell a summary from a refusal.

    Known false negative: a SHORT refusal in any language other than English
    or Korean that carries NO terminator ("无法访问该链接", "No puedo acceder a
    ese enlace") clears every signal and lands as the label. Without the
    terminator it has the same shape as a name, so only a per-locale phrase
    list could catch it, and keeping one per shipped locale is whack-a-mole.
    It fails to a wrong-but-short label, never to a paragraph.
    """
    stripped = text.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if lowered.startswith(openers):
        return True
    if stripped.startswith(KO_PROSE_OPENERS):
        return True
    if len(stripped.split()) > max_words:
        return True
    if unspaced_script_chars(stripped) > max_unspaced_chars:
        return True
    if not sentence_shape:
        return False
    if stripped.endswith(KO_SENTENCE_ENDINGS):
        return True
    if ends_like_sentence(stripped):
        return True
    for index, char in enumerate(stripped[:-1]):
        if char in _SPACED_TERMINATORS and stripped[index + 1].isspace():
            return True
        if char in _WIDE_TERMINATORS:
            return True
    return False


#: Characters that read as a verdict-reason separator right after a control
#: word ("SKIP: too vague", "SKIP (too vague)"). Deliberately NOT every
#: non-alphanumeric: "-" and "." separate only when spaced away from the next
#: word, so identifier labels the prompt tells the model to keep verbatim
#: ("KEEP-ALIVE header bug", "SKIP.md parser fix") survive, and "_" never
#: separates ("SKIP_TESTS env var flag").
_VERDICT_TRAILERS = ":,;!?("


def is_verdict_reply(text: str, control_words: tuple[str, ...]) -> bool:
    """True when *text* is a control word, alone or followed by a reason.

    The word is matched case-insensitively on every shape: the words are
    taught as literal ASCII, but a lowercased echo is still a verdict, with
    or without a reason attached ("skip", "Skip: greetings only", "keep - the
    title still fits"). What separates a verdict-plus-reason from a real
    label OPENING with the word is the separator: punctuation (or a spaced
    dash / spaced period) means verdict, while a plain following word or an
    identifier joiner means label ("SKIP and KEEP handling", "Keep alive
    timer bug", "SKIPPED frames in reveal", "KEEP-ALIVE header bug").
    """
    upper = text.upper()
    if upper in control_words:
        return True
    for word in control_words:
        if not upper.startswith(word):
            continue
        rest = text[len(word) :]
        head = rest.lstrip()
        spaced = len(head) != len(rest)
        if not head:
            return True
        char = head[0]
        if char in _VERDICT_TRAILERS:
            return True
        if char == "-" and (spaced or len(head) < 2 or head[1].isspace()):
            return True
        if char == "." and (len(head) < 2 or head[1].isspace()):
            return True
    return False
