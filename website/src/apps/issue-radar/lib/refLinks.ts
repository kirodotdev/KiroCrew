// Cross-reference links inside an issue/PR body or comment.
//
// A GitHub markdown body routinely links to OTHER issues and PRs. Every such
// link used to leave the app (target=_blank → browser), which breaks the triage
// loop: you lose the list, the filters, and the pane you were reading. When the
// link points at the repo Issue Radar already has connected, the app can render
// the target itself in a bottom sheet instead (see components/RefSheet.tsx).
//
// This module is the pure half of that: URL → { kind, number }, plus the
// placeholder list rows the sheet passes to the detail panes while their real
// detail streams in. Kept dependency-free (no React, no DOM) so it is directly
// unit-testable.
import { maskInlineCode } from '../../../hooks/useBlockAssembler'
import type { Issue, PullRequest } from '../api'

/** Which detail pane renders a reference target. */
export type RefKind = 'issue' | 'pull'

/** A resolved same-repo reference — what the sheet needs to open a target. */
export interface RepoRef {
  kind: RefKind
  number: number
}

/** Hosts whose `/owner/repo/...` paths are GitHub. Issue Radar identifies a repo
 * by owner/repo only (no host field — it follows `gh`'s default host), so an
 * Enterprise host is deliberately NOT matched: `acme.ghe.com/o/r#5` is a
 * different repo from `github.com/o/r#5` and must keep opening externally. */
const GITHUB_HOSTS = new Set(['github.com', 'www.github.com'])

/** Path segment → pane. GitHub's canonical PR path is `/pull/<n>`; `/pulls/<n>`
 * is accepted because it redirects there and appears in hand-written links. */
const KIND_BY_SEGMENT: Record<string, RefKind> = {
  issues: 'issue',
  pull: 'pull',
  pulls: 'pull',
}

/**
 * Resolve `href` to a reference INTO `owner/repo`, or null when it points
 * anywhere else (another repo, another host, a discussion, a non-numeric path).
 *
 * Only absolute http(s) GitHub URLs match. A relative href is deliberately not
 * resolved against any base: in a GitHub body it resolves against the *repo*
 * page, which this module has no way to distinguish from a dashboard-relative
 * link, and guessing wrong would hijack an unrelated click.
 *
 * Owner/repo are compared case-insensitively (GitHub treats them that way, so
 * `/KiroDotDev/KiroCrew/issues/5` is the same target as the lowercase form).
 * Trailing segments (`/files`, `/commits`), query strings and `#issuecomment-…`
 * fragments are ignored — they all address the same issue or PR.
 */
export function parseRepoRef(
  href: string | null | undefined,
  owner: string,
  repo: string,
): RepoRef | null {
  if (!href || !owner || !repo) return null

  let url: URL
  try {
    url = new URL(href)
  } catch {
    return null  // relative or malformed — not a resolvable cross-reference
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') return null
  if (!GITHUB_HOSTS.has(url.hostname.toLowerCase())) return null

  const segments = url.pathname.split('/').filter(Boolean)
  if (segments.length < 4) return null
  const [hrefOwner, hrefRepo, kindSegment, numberSegment] = segments
  if (hrefOwner.toLowerCase() !== owner.toLowerCase()) return null
  if (hrefRepo.toLowerCase() !== repo.toLowerCase()) return null

  const kind = KIND_BY_SEGMENT[kindSegment]
  if (!kind) return null
  if (!/^[0-9]+$/.test(numberSegment)) return null
  const number = Number(numberSegment)
  if (!Number.isSafeInteger(number) || number <= 0) return null

  return { kind, number }
}

/** The canonical GitHub URL for a reference — used for the sheet's "open on
 * GitHub" escape hatch and as the placeholder row's `url` until the real detail
 * (which carries GitHub's own url) lands. */
export function refUrl(owner: string, repo: string, ref: RepoRef): string {
  const segment = ref.kind === 'pull' ? 'pull' : 'issues'
  return `https://github.com/${owner}/${repo}/${segment}/${ref.number}`
}

/** Stable identity for a stack entry (also the React key). */
export function refKey(ref: RepoRef): string {
  return `${ref.kind}-${ref.number}`
}

/** Regions of a markdown source that must never be rewritten: whole markdown
 * link/image constructs (`[label](target)`) — rewriting inside a link label would
 * nest one link inside another — plus autolinks and raw HTML tags. Fenced code and
 * inline code are handled separately (see `maskFences` / `maskInlineCode`). */
const MASKED_REGIONS = [
  /!?\[[^\]\n]*\]\([^)\n]*\)/g,           // inline link / image
  /!?\[[^\]\n]*\]\[[^\]\n]*\]/g,          // reference-style link / image
  /^[ \t]{0,3}\[[^\]\n]+\]:[^\n]*/gm,     // link reference DEFINITION line
  /<[^<>\n]+>/g,                          // autolink, raw HTML tag
]

/** Blank out every line inside a ``` / ~~~ fence (fence lines included), keeping
 * line lengths so match indices stay valid against the original.
 *
 * The opener's LENGTH is tracked, not just its character: per CommonMark a fence
 * is closed only by a run of the same character that is at least as long, so a
 * four-backtick fence may contain a three-backtick line (the usual way to show a
 * fenced example). Closing on the shorter run would unmask the rest of the block
 * and let its content be rewritten. An UNCLOSED fence masks to end of source,
 * which is what a truncated body wants. */
function maskFences(source: string): string {
  const lines = source.split('\n')
  let fenceChar: string | null = null
  let fenceLen = 0
  for (let i = 0; i < lines.length; i++) {
    const marker = /^[ \t]*(`{3,}|~{3,})/.exec(lines[i])
    const blank = ' '.repeat(lines[i].length)
    if (fenceChar === null) {
      if (marker) {
        fenceChar = marker[1][0]
        fenceLen = marker[1].length
        lines[i] = blank
      }
    } else {
      lines[i] = blank
      if (marker && marker[1][0] === fenceChar && marker[1].length >= fenceLen) {
        fenceChar = null
        fenceLen = 0
      }
    }
  }
  return lines.join('\n')
}

/** Replace every masked region with spaces, preserving length so match indices
 * from the masked copy stay valid against the original.
 *
 * Inline code goes through the shared `maskInlineCode`, which is CommonMark-correct
 * about delimiter LENGTH: a run of N backticks is closed only by a run of exactly
 * N, so ``` ``a ` #5`` ``` is masked as one span rather than ending at the inner
 * backtick and leaving `#5` exposed. */
function maskMarkdown(source: string): string {
  let masked = maskFences(source)
  masked = masked.split('\n').map((line) => maskInlineCode(line)).join('\n')
  for (const re of MASKED_REGIONS) {
    masked = masked.replace(re, (m) => ' '.repeat(m.length))
  }
  return masked
}

/** A shorthand issue reference: `#123`.
 *
 * Rejected when PRECEDED by a character that makes it something else — a word
 * character or `/` (a URL fragment such as `…/issues/12#issuecomment-9`, or a
 * cross-repo `owner/repo#5`), `&` (`&#123;`, an HTML entity), `[` or `(`
 * (markdown link syntax the mask may not have caught), or another `#`.
 *
 * Rejected when FOLLOWED by a word character, so a hex colour (`#1a2b3c`) is not
 * read as `#1`. An all-digit run IS taken as a reference — GitHub does the same,
 * and a repo with six-figure issue numbers is ordinary, so length cannot decide.
 */
const SHORTHAND_RE = /(^|[^\w/&[(#])#(\d{1,7})(?!\w)/g

/**
 * Rewrite bare `#123` references into real markdown links to this repo, so they
 * render as links — and pick up the in-app reference affordance — exactly like a
 * pasted full URL. GitHub renders the same shorthand on its own web UI; the raw
 * markdown the API returns carries only the literal text.
 *
 * The target is always the `/issues/<n>` form, because the shorthand does not say
 * which it is: the reference UI resolves issue-vs-PR from the ref summary, and
 * GitHub itself redirects `/issues/<n>` to `/pull/<n>` for a PR, so the link is
 * still correct if it is ever followed externally.
 *
 * Code, autolinks, raw HTML and existing markdown links are masked out first, so
 * nothing inside them is rewritten.
 */
export function linkifyIssueRefs(source: string, owner: string, repo: string): string {
  if (!source || !owner || !repo) return source
  if (!source.includes('#')) return source
  const masked = maskMarkdown(source)

  const edits: Array<{ start: number; end: number; text: string }> = []
  SHORTHAND_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = SHORTHAND_RE.exec(masked)) !== null) {
    const lead = m[1] ?? ''
    const start = m.index + lead.length
    const end = m.index + m[0].length
    const number = Number(m[2])
    if (!Number.isSafeInteger(number) || number <= 0) continue
    edits.push({ start, end, text: `[#${number}](${refUrl(owner, repo, { kind: 'issue', number })})` })
  }
  if (edits.length === 0) return source

  let out = ''
  let pos = 0
  for (const e of edits) {
    out += source.slice(pos, e.start) + e.text
    pos = e.end
  }
  return out + source.slice(pos)
}

/** A placeholder LIST row for an issue the list may not hold (a closed issue, or
 * one outside the current filters). The detail panes render `detail?.x ?? row.x`
 * throughout, so every field here is only the pre-fetch first paint. The title is
 * deliberately EMPTY: the panes render a skeleton wherever a field is missing, so
 * a placeholder must not fabricate one (a literal `#<n>` title would read as real
 * content for the length of the fetch). */
export function placeholderIssue(
  owner: string, repo: string, number: number, state?: string,
): Issue {
  return {
    number,
    title: '',
    url: refUrl(owner, repo, { kind: 'issue', number }),
    labels: [],
    comments: 0,
    updated_at: '',
    // Left UNDEFINED unless the caller knows it. Defaulting to 'open' would make
    // the pane offer "Close as completed" on an already-closed issue and let that
    // write clobber its state_reason; the panes gate their write actions on having
    // an authoritative state (see awaitingFirstPaint).
    state,
  }
}

/** The PR twin of `placeholderIssue`. `state` is the reference summary's when the
 * caller has it; it drives only the first-paint pill and the poll rate, and the
 * real detail corrects both on arrival. The PR pane has no state-write action, so
 * unlike the issue side there is nothing here to clobber. */
export function placeholderPull(
  owner: string, repo: string, number: number, state?: string,
): PullRequest {
  return {
    number,
    title: '',
    url: refUrl(owner, repo, { kind: 'pull', number }),
    state: state ?? 'open',
    draft: false,
    labels: [],
    updated_at: '',
  }
}
