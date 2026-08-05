// Canonical [OPTION(S):] follow-up-pill marker regex — the single source of truth
// for the frontend, mirroring the backend's ReDoS-hardened OPTIONS_RE_LINE
// (src/kiro_crew/constants.py). Import this instead of hand-rolling a copy so the
// grammar can't drift between the dashboard's several parsers.
//
// The tempered body `(?:[^[\n]|\[(?!OPTIONS?:))*` matches any run of characters that
// does NOT begin a fresh `[OPTION(S):` marker, which gives three properties:
//   1. a label may itself contain `]` — the block ends at the LAST `]` that ends the
//      line, not the first `]` (so "[OPTIONS: a] | b]]" → ["a]", "b]"]);
//   2. two same-line markers can't merge into one garbage label;
//   3. it fails in O(1) per `[OPTIONS:` prefix instead of rescanning the line, so
//      untrusted model output with thousands of `[OPTIONS:` prefixes can't drive
//      quadratic (ReDoS-class) backtracking in the synchronous render path.
// The marker must END ITS LINE (`\][ \t]*$` with the `m` flag) — a trailing note,
// question, or diff on later lines is left intact. `i` = case-insensitive OPTION(S);
// `g` = take the LAST marker / strip all. Group 1 = optional "S"; group 2 = labels.
//
// The optional `(?:\([^\s()]*\))?` after the `]` tolerates a stray markdown-link
// close that models sometimes append, e.g. `[OPTIONS: A | B](OPTIONS)`. Without it
// that suffix (a) breaks the end anchor so the marker leaks unparsed and (b) forms a
// valid `[label](url)` link, so the dashboard renders the whole thing as a purple
// link instead of buttons. The `(` must abut the `]` (no gap), so real trailing
// prose or a spaced `] (note)` still fails the anchor and is preserved. The group is
// OUTSIDE the label capture, so choices are unaffected — and because the regex is
// used with `replace`, the stray `(...)` is stripped from the displayed text too.
// The inner class is `[^\s()]` (not `[^)\n]`) so it shares no character with the
// trailing `[ \t]*` — that keeps the group unambiguous and ReDoS-safe (mirrors the
// backend OPTIONS_RE_LINE). The real tic contains no whitespace, so nothing is lost.
//
// Only use with String#matchAll and String#replace (which don't carry the global
// regex `lastIndex` hazard); do NOT call `.exec`/`.test` on this shared const.
export const OPTION_MARKER_RE = /\[OPTION(S)?:((?:[^[\n]|\[(?!OPTIONS?:))*)\](?:\([^\s()]*\))?[ \t]*$/gim
