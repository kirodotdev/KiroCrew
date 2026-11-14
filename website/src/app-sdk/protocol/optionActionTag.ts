// The close-session chip's control tag: `<!-- option-action:close=<label> -->`.
//
// One family of the control-tag grammar (`keep-visible`, `deliver:`, `plan_task_id:`;
// the twin is `keepVisibleMarker.ts`, the backend `constants.py`). It sits on its own
// line in the trailing block of control tags at the very end of a reply, after the
// `[OPTIONS: ...]` line when there is one. The head is case-insensitive. The label has no
// `>` and no newline, and the body after `option-action:` is at most 256 characters.
//
// Read line by line from the end: skip blank lines and other control-tag lines, and take
// the first `option-action` line. What counts as a tag line is `isStringTagLine` from
// `keepVisibleMarker.ts`, the mirror of the backend's `_is_string_tag_line`, so the chip,
// copy and search, and the backend strip agree on every line. A tail inside an unterminated
// or unplaceable code fence offers nothing: `inOpenFence` decides.

/** The actions the tag can offer. Only `close` exists; the label is inert prose. */
export type OfferedActionKind = 'close'
export interface OfferedAction {
  action: OfferedActionKind
  label: string
}

import { hasKeepVisibleMarker, inOpenFence, isStringTagLine } from './keepVisibleMarker'

const HEAD = 'option-action:'
const ACTION_KINDS: ReadonlySet<string> = new Set<OfferedActionKind>(['close'])

/** The text after `option-action:` on a tag line `isStringTagLine` accepted, or null when
 *  the line is another family. */
function optionActionBody(line: string): string | null {
  const inner = line.trim().slice(4, -3).trimStart()
  return inner.slice(0, HEAD.length).toLowerCase() === HEAD ? inner.slice(HEAD.length) : null
}

/** The character offset where line `k` of `lines` starts. */
function lineOffset(lines: string[], k: number): number {
  let at = 0
  for (let i = 0; i < k; i++) at += (lines[i] ?? '').length + 1
  return at
}

/** The `option-action` tag body in the reply's trailing control-tag block, with its line
 *  index, or null. */
function findTag(lines: string[]): { body: string; at: number } | null {
  for (let k = lines.length - 1; k >= 0; k--) {
    const line = lines[k] ?? ''
    if (line.trim() === '' || hasKeepVisibleMarker(line)) continue
    if (!isStringTagLine(line)) return null
    const body = optionActionBody(line)
    if (body !== null) return { body, at: k }
  }
  return null
}

/** The action the reply's `option-action` tag offers, or null. An unknown action, a missing
 *  `=`, or an empty label offers nothing, so a typo never becomes a chip. */
export function optionActionOf(content: string): OfferedAction | null {
  const lines = content.split('\n')
  const tag = findTag(lines)
  if (tag === null || inOpenFence(content, lineOffset(lines, tag.at))) return null
  const eq = tag.body.indexOf('=')
  if (eq < 0) return null
  const action = tag.body.slice(0, eq).trim().toLowerCase()
  const label = tag.body.slice(eq + 1).trim()
  if (!label || !ACTION_KINDS.has(action)) return null
  return { action: action as OfferedActionKind, label }
}

/** `content` without its live `option-action` tag line, for copy and search. The tag is an
 *  HTML comment the renderer never shows, so text read from it would be a phantom match. */
export function stripOptionActionTag(content: string): string {
  const lines = content.split('\n')
  const tag = findTag(lines)
  if (tag === null || inOpenFence(content, lineOffset(lines, tag.at))) return content
  lines.splice(tag.at, 1)
  return lines.join('\n').trimEnd()
}
