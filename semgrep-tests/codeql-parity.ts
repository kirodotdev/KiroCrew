// Fixtures for semgrep/codeql-parity.yaml, exercised by `semgrep --test` in
// the SAST job (same pinned engine as the scan). Each `ruleid:` annotation
// asserts the NEXT line MUST match that rule; each `ok:` asserts it must NOT.
// The negatives are the load-bearing half: every one below encodes an idiom a
// precision defect was found in while the rules were written (see the file
// header of semgrep/codeql-parity.yaml and the review thread on PR #4838).
//
// Test mode ignores the rules' `paths:` filters, so this file does not need
// to live under website/src/. The normal scan never reads this directory
// (.semgrepignore), so the deliberately vulnerable code here cannot trip the
// SAST job itself.

declare const s: string;
declare const v: string;
declare const remoteUrl: string;
declare const keyName: string;
const SEP = ",";

// ─── kirocrew.identity-replacement ──────────────────────────────────────

export function identityReplacement(): void {
  // ruleid: kirocrew.identity-replacement
  s.replace("<", "<");
  // ruleid: kirocrew.identity-replacement
  s.replaceAll(SEP, SEP);
  // A replace with a genuinely different replacement is the normal case.
  // ok: kirocrew.identity-replacement
  s.replace("<", "&lt;");
  // ok: kirocrew.identity-replacement
  s.replaceAll("&", "&amp;");
}

// ─── kirocrew.incomplete-multi-char-sanitization ────────────────────────

export function incompleteSanitization(): void {
  // String first-args are never global in JS: only the first occurrence
  // is removed, so a nested payload survives one pass.
  // ruleid: kirocrew.incomplete-multi-char-sanitization
  s.replace("<script>", "");
  // A `^` just inside `[` is class negation, not an anchor — testing for
  // `[\^$]` anywhere in the source wrongly exempted this genuinely
  // incomplete strip (the character-class-anchor defect).
  // ruleid: kirocrew.incomplete-multi-char-sanitization
  s.replace(/<script[^>]*>/, "");
  // With /m, `^` matches at every line boundary, so an anchored non-global
  // replace still strips only the first line's tag (the /m-flag defect).
  // ruleid: kirocrew.incomplete-multi-char-sanitization
  s.replace(/^<script>/m, "");
  // Ordinary text munging without a dangerous sequence is not flagged.
  // ok: kirocrew.incomplete-multi-char-sanitization
  s.replace("foo", "");
  // A real start anchor makes the strip complete in one pass.
  // ok: kirocrew.incomplete-multi-char-sanitization
  s.replace(/^<!DOCTYPE[^>]*>/, "");
  // A real end anchor: everything from the match to the end goes in one pass.
  // ok: kirocrew.incomplete-multi-char-sanitization
  s.replace(/<mcwidget[\s\S]*$/, "");
  // A global regex removes every occurrence.
  // ok: kirocrew.incomplete-multi-char-sanitization
  s.replace(/<script>/g, "");
}

// ─── kirocrew.redos-nested-quantifier ───────────────────────────────────

export function redosNestedQuantifier(): void {
  // ruleid: kirocrew.redos-nested-quantifier
  const re1 = /(a+)+/;
  // The JS flag set is closed by the language spec; `d` (hasIndices) and
  // `v` (unicodeSets) were the two a narrower class let through.
  // ruleid: kirocrew.redos-nested-quantifier
  const re2 = /(a+)+/d;
  // ruleid: kirocrew.redos-nested-quantifier
  const re3 = /(a+)+/v;
  // ruleid: kirocrew.redos-nested-quantifier
  const re4 = /([\s\S]*)*/gimsuy;
  // Raw text that merely MENTIONS a nested quantifier is not a regex
  // literal. As a bare pattern-regex this rule scanned file text and fired
  // on a comment like this one: (a+)+ — binding a metavariable first
  // restricts it to real expression syntax.
  // ok: kirocrew.redos-nested-quantifier
  const s1 = "a nested quantifier such as (a+)+ backtracks exponentially";
  // ok: kirocrew.redos-nested-quantifier
  const t1 = `template text mentioning ([0-9]+)* is not a regex literal`;
  // The unrolled-loop idiom X+(?:sepX+)+ is linear: the separator keeps the
  // inner and outer quantifiers from matching the same characters.
  // ok: kirocrew.redos-nested-quantifier
  const safe = /\w+(?:,\w+)+/;
  void re1; void re2; void re3; void re4; void s1; void t1; void safe;
}

// ─── kirocrew.clear-text-web-storage ────────────────────────────────────

export function clearTextWebStorage(): void {
  // ruleid: kirocrew.clear-text-web-storage
  localStorage.setItem("authToken", v);
  // ruleid: kirocrew.clear-text-web-storage
  sessionStorage.setItem("session_key", v);
  // ruleid: kirocrew.clear-text-web-storage
  window.localStorage.setItem("api_key", v);
  // ruleid: kirocrew.clear-text-web-storage
  window.sessionStorage.setItem("privateKey", v);
  // A plausible non-secret UI key: no credential token as a substring.
  // ok: kirocrew.clear-text-web-storage
  localStorage.setItem("sessionKeyOrder", v);
  // ok: kirocrew.clear-text-web-storage
  localStorage.setItem("theme", v);
  // A non-literal key is out of scope for a syntactic rule.
  // ok: kirocrew.clear-text-web-storage
  localStorage.setItem(keyName, v);
}
