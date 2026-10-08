import { useCallback, type MutableRefObject } from 'react'

import type { ComposerDraftStore } from '../../../chat-core/composer/draftStore'
import { makeRelative, type FileKind } from '../../../components/FilePickerMenu'
import {
  MENTION_LINE_SUFFIX, WRAPPER_CLOSER, addPendingFile, extendsConsumably, foldWinSep, isWindowsShapedPath,
  leadingMentionBoundary, mentionBoundary, mentionBoundaryFor, mentionTokenRegex, normalizeWindowsPath,
  parseDirTokens, spliceDirTokens,
} from '../../../utils/fileTokens'
import { findTokenRanges } from '../../../utils/pasteTokens'
import { i18nT } from '../../../i18n/t'
import { revealComposer } from '../composerFocus'
import type { ComposerStaging } from './composerStaging'
import type { MentionKeyMods } from '../../../components/chat-input/props'

/**
 * The composer's `@`-mentions of files and folders, and the file chips they
 * stage. A folder chip derives from its `@rel/` token alone; a file chip is
 * list-backed (`pendingFiles`) and kept in sync with the mention text through
 * the aliases a pick recorded, in both directions: hand-deleting the mention
 * unstages the chip, restoring it re-stages it, and the chip's remove strips
 * the mention. Insertion lands at the caret, never inside another token.
 */

/** Slot -> absolute path -> every `@rel` alias that file was inserted as. */
export type PickedFileTokens = MutableRefObject<Record<string, Record<string, string[]>>>

/**
 * The per-slot record of the exact `@rel` aliases each picked file was
 * inserted as: reads and writes over the persisted store the draft stores own.
 */
export function useFileMentionTokens(composerSlotRef: MutableRefObject<string | null>, pickedFileTokens: PickedFileTokens) {
  // useCallback (not a plain function) purely so a caller wrapped in its own
  // useCallback/useEffect gets a STABLE reference to depend on -- the body
  // only reads refs, which never change identity, so its deps are refs alone.
  const currentSlotTokens = useCallback(() => {
    const slot = composerSlotRef.current
    if (!slot) return null
    return pickedFileTokens.current[slot] ??= {}
  }, [composerSlotRef, pickedFileTokens])
  /** Append `token` as an alias for `absPath` in the CURRENT slot's token map,
   *  deduping exact repeats. No-ops outside a known slot. */
  const recordSlotToken = useCallback((absPath: string, token: string) => {
    const slotTokens = currentSlotTokens()
    if (!slotTokens) return
    const aliases = slotTokens[absPath] ?? []
    if (!aliases.includes(token)) slotTokens[absPath] = [...aliases, token]
  }, [currentSlotTokens])
  /** Merge a captured alias sub-map back into `slot`'s live token map,
   *  deduping exact repeats -- the one restore-side primitive every
   *  composer-recovery path shares (transport failure, queued cancel,
   *  create failure). See the seam table on `pickedFileTokens`. */
  const mergeSlotTokens = useCallback((slot: string, aliases: Record<string, string[]>) => {
    const live = pickedFileTokens.current[slot] ??= {}
    for (const [p, toks] of Object.entries(aliases)) {
      const cur = live[p] ?? []
      live[p] = [...cur, ...toks.filter(t => !cur.includes(t))]
    }
  }, [pickedFileTokens])
  return { pickedFileTokens, currentSlotTokens, recordSlotToken, mergeSlotTokens }
}

interface FileMentionActionsOptions {
  staging: ComposerStaging
  inputRef: MutableRefObject<string>
  setInput: ComposerDraftStore['set']
  /** The active slot's project directory, refreshed every render by ChatPage. */
  currentProjectRef: MutableRefObject<string | undefined>
  /** Live composer caret (ChatInput keeps it current) and the caret to restore after a splice. */
  voiceCaretRef: MutableRefObject<{ start: number; end: number } | null>
  voicePendingCaretRef: MutableRefObject<number | null>
  /** Set here to the reconciliation; the draft-commit sink calls it. */
  reconcileFileChipsRef: MutableRefObject<((text: string) => void) | null>
  /**
   * Politely announce an attachment change the reconciliation made on its own
   * (a hand-edited or pasted `@mention` un/restaging a chip), for screen-reader
   * users who get no focus change from it. Set by ChatPage to write the composer's
   * `aria-live` status region. Only the AUTOMATIC path calls this: the chip's own
   * ✕ and a picker pick already move focus, so `removeFileChip` deliberately does
   * not announce (that would double-announce — #14597).
   */
  announceAttachmentChange?: (message: string) => void
}

/**
 * Inserting, reconciling and removing file / folder mentions. Returns the
 * page's "Add to chat" entry point (tree context menu and tree drop), the
 * drop-offset clamp, and the three composer chip handlers.
 */
export function useFileMentionActions({
  staging,
  inputRef,
  setInput,
  currentProjectRef,
  voiceCaretRef,
  voicePendingCaretRef,
  reconcileFileChipsRef,
  announceAttachmentChange,
}: FileMentionActionsOptions) {
  const { pendingFiles, setPendingFiles, pendingFilesRef, pasteBlocksRef, currentSlotTokens, recordSlotToken } = staging
  // Mention checks in this block compare both separator forms only for a
  // Windows-shaped project, where the OS accepts them interchangeably. On
  // POSIX, `\` is a legal filename character: a nested file `src/main.ts`
  // and a literal filename `src\main.ts` in the project root are distinct.
  // The current-project check uses `isWindowsShapedPath` directly against the
  // drive-letter/UNC prefix, including Windows paths already written with `/`.
  // The shared `tokenRegex`'s trailing boundary requires whitespace or
  // end-of-string, so an entirely ordinary sentence -- "check @file.ts,
  // please" -- reads as "mention gone" the instant the comma is typed,
  // and the reconciliation effect below silently unstages a still-
  // intended attachment before the user finishes the sentence (fork GPT
  // review). Before this PR nothing acted on that gap: a file chip was
  // pure list state with no text-driven staleness check at all, so
  // `tokenRegex`'s pre-existing punctuation blind spot never had a
  // user-visible consequence. This reconciliation-only boundary also
  // accepts common trailing punctuation, scoped here rather than
  // widening the shared `tokenRegex` (used for insertion/splicing
  // elsewhere, where an inserted token is always followed by a real
  // space per the caret-insertion logic above). A bare punctuation
  // character is NOT enough on its own, though (fork GPT review): `.` is
  // a legal, common mid-filename character (`README.md`), so treating it
  // as a sufficient boundary by itself would match `@README` as a
  // PREFIX of the unrelated, longer `@README.md` mention. Each
  // punctuation option is therefore itself required to be followed by
  // whitespace or end-of-string. Shared with the remove-chip strip below
  // (round 16) so the two can never disagree about what counts as a
  // boundary.
  // A punctuation boundary can itself be the START of a DIFFERENT staged
  // file's own longer alias (fork GPT review, round 18): `report` and
  // `report,` can both be genuine, distinct filenames, so accepting a
  // bare trailing comma as `report`'s boundary matches it as a PREFIX of
  // `report,`'s own mention -- silently mis-attributing the LONGER file's
  // text to the shorter one on both sides of the reconciliation contract
  // (staleness AND remove-chip strip). Falls back to the strict
  // whitespace/end-only boundary whenever another currently-relevant
  // alias literally begins with this one, so a trailing punctuation
  // character that could belong to a real sibling file's name is never
  // treated as "just punctuation." `otherAliases` is optional: callers
  // with no cross-file context to check against (none currently) get the
  // permissive boundary, same as before this round.
  const relMentionedHere = useCallback((text: string, rel: string, otherAliases?: ReadonlySet<string>) => {
    // ONE regex builder (fork First Principles review): the shared
    // `mentionTokenRegex` assembles the identical pattern this file used to
    // build locally, and its sibling set drives the same `mentionBoundaryFor`
    // rule -- the recorded aliases carry a leading `@`, the shared builder
    // compares bare tokens, so the set is stripped once here.
    // On a Windows-shaped project separator identity is folded once
    // (`foldWinSep`): text, rel and sibling aliases are compared with `\`
    // read as `/`, so every spelling of the same file matches -- uniform or
    // mixed (`@src\foo/bar.ts`, fork GPT review) -- and a sibling alias
    // recorded in another spelling still triggers the prefix-sibling rule,
    // and revival's asymmetry guard with it.
    const fold = foldWinSep(isWindowsShapedPath(currentProjectRef.current || ''))
    const bare = otherAliases && new Set([...otherAliases].map(a => fold(a.startsWith('@') ? a.slice(1) : a)))
    return mentionTokenRegex(fold(rel), '', bare).test(fold(text))
  }, [currentProjectRef])

  // "Add to context" from the file-browser rail's row context menu: insert the
  // SAME `@`-mention the file picker does, so a right-click is just a second
  // entry point to the existing mention plumbing. A file gets an `@rel` token
  // plus a staged upload (chip + `[attached_file N]` on send); a folder gets a
  // bare `@rel/` reference (the token IS the reference — no upload). The caret
  // is unknown from the tree, so both append. Idempotent: re-adding a path
  // already referenced in the composer is a no-op.
  /** Move an insertion caret OUT of any token literal it would split.
   *  A caret strictly inside a token is a legal resting place (the click
   *  expander's preview inside a paste token; a click inside mention text),
   *  but splicing text there tears the literal apart. Every token kind, and
   *  how its span is found:
   *  - collapsed paste token `[ Paste #N · M lines ]` (contains spaces): its
   *    recorded range. Splitting it DESTROYS the block unrecoverably --
   *    pruneBlocks drops the record and the draft effect persists the pruned
   *    list (fork Opus review);
   *  - any `@`-reference -- a picked file mention, a typed or draft-restored
   *    one with no recorded alias, a folder token `@src/utils/`, optionally
   *    behind an opening wrapper: the whitespace-free run around the caret.
   *    Splitting it breaks the reference, so a file chip unstages or a
   *    folder silently drops out of the send (fork Opus review);
   *  - a recorded file alias, checked FIRST because it can contain spaces
   *    (`@My Report.pdf`, written unquoted by the picker): the run around a
   *    caret in `Report.pdf` does not start with `@`, so the run rule alone
   *    let the insert tear the mention and unstage its chip (fork GPT review).
   *    On a Windows-shaped project the alias is found with separators folded
   *    (`foldWinSep`), like the reconciliation that keeps the chip staged: a
   *    mention hand-edited to `@docs\My Report.pdf` is the same file, so the
   *    clamp must see it too (fork GPT review). The fold is 1:1, so indices
   *    found on the folded copy are the original's.
   *  Clamped to the nearer edge. A caret inside an ordinary word is left
   *  alone: inserting there is what the user pointed at. */
  const clampOutOfTokens = useCallback((text: string, at: number): number => {
    for (const r of findTokenRanges(text, pasteBlocksRef.current)) {
      if (at > r.start && at < r.end) return at - r.start <= r.end - at ? r.start : r.end
    }
    const fold = foldWinSep(isWindowsShapedPath(currentProjectRef.current || ''))
    const folded = fold(text)
    for (const alias of Object.values(currentSlotTokens() ?? {}).flat().map(fold)) {
      for (let s = folded.indexOf(alias); s !== -1; s = folded.indexOf(alias, s + 1)) {
        const e = s + alias.length
        if (at > s && at < e) return at - s <= e - at ? s : e
      }
    }
    let start = at
    let end = at
    while (start > 0 && !/\s/.test(text[start - 1])) start--
    while (end < text.length && !/\s/.test(text[end])) end++
    if (start < at && at < end && /^[($[{`"']?@/.test(text.slice(start, end))) {
      return at - start <= end - at ? start : end
    }
    return at
  }, [currentSlotTokens, pasteBlocksRef, currentProjectRef])

  const handleAddToContext = useCallback((absPath: string, kind: 'file' | 'dir', dropAt?: number | null) => {
    // `dropAt` is the text offset under a file-tree drop (useComposerTreeDrop);
    // the context menu passes none and the mention goes in at the caret.
    const insertAt = (): number | null => dropAt ?? voiceCaretRef.current?.start ?? null
    // `absPath` arrives from the tree with a forward-slash-normalized Windows
    // root; normalize the project root the same way (Windows-shaped roots
    // only — normalizeWindowsPath leaves POSIX paths, where `\` is a legal
    // name character, untouched) so makeRelative can relativize on native
    // Windows instead of keeping the absolute path.
    const rel = makeRelative(absPath, normalizeWindowsPath(currentProjectRef.current || ''))
    if (kind === 'dir') {
      // spliceDirTokens dedupes by exact string -- it only ever sees bare
      // RELATIVE tokens, with no platform context to prove a `\` is a
      // Windows separator rather than a literal POSIX filename character, so
      // it cannot safely widen the comparison itself. Widen HERE instead,
      // gated on the PROJECT being Windows-shaped (an absolute path DOES
      // carry a provable drive-letter/UNC prefix): only then can the Windows
      // @-picker's backslash-form dir token (`@src\utils\`) be recognized as
      // the SAME folder this handler's forward-slash `rel` (`src/utils/`)
      // refers to. On a POSIX project this widening never triggers, so two
      // genuinely different directories (`src/a\b/` vs `src/a/b/`) can never
      // be conflated.
      const relSlash = rel.endsWith('/') ? rel : `${rel}/`
      const project = currentProjectRef.current || ''
      // Checked directly against the drive-letter/UNC prefix, not via
      // `normalizeWindowsPath(project) !== project` (fork GPT review) --
      // that comparison misses a Windows-shaped project already spelled
      // with forward slashes (`C:/repo`).
      const projectIsWindowsShaped = isWindowsShapedPath(project)
      const dup = projectIsWindowsShaped && parseDirTokens(inputRef.current).some(
        t => t.rel.replace(/\\/g, '/') === relSlash,
      )
      if (!dup) {
        // Insert at the last known caret, same as a dir-token drop (the
        // resources controller's handleDrop)
        // -- both go through spliceDirTokens, so they share its caret contract.
        const dirCaret = insertAt()
        const spliced = spliceDirTokens(
          inputRef.current,
          dirCaret == null ? null : clampOutOfTokens(inputRef.current, Math.max(0, Math.min(dirCaret, inputRef.current.length))),
          [rel],
        )
        if (spliced.changed) {
          voicePendingCaretRef.current = spliced.caret
          setInput(spliced.value)
        }
      }
    } else {
      const token = `@${rel}`
      const project = currentProjectRef.current || ''
      const projectIsWindowsShaped = isWindowsShapedPath(project)
      const foldSep = foldWinSep(projectIsWindowsShaped)
      // Match EXACTLY this rel, never a shorter basename suffix. Separator
      // folding is valid only for a Windows-shaped project (`foldWinSep`: any
      // uniform or mixed spelling of the same file); on POSIX a
      // backslash is a legal filename character and can name a different file.
      // Checked with the SHARED permissive matcher (fork GPT review): the old
      // whitespace-only `tokenRegex` missed a punctuated existing mention
      // (`check @src/main.ts.`), so "Add to chat" inserted a duplicate token
      // beside the one already there. Checked against the live text (not
      // inside the updater) because token bookkeeping must follow the same
      // branch and record the spelling that is actually present.
      // The check passes the same live sibling aliases the reconciliation
      // passes (fork Opus review): with `report,` staged and mentioned, the
      // permissive boundary read `@report,` as `report`'s own mention, so
      // no token was inserted, while the reconciliation's strict boundary
      // for `report` found no match and unstaged the chip just added.
      const known = currentSlotTokens() ?? {}
      const siblings = new Set<string>()
      for (const otherPath of pendingFilesRef.current) {
        if (otherPath === absPath) continue
        known[otherPath]?.forEach(t => { if (relMentionedHere(inputRef.current, t.slice(1))) siblings.add(t) })
      }
      const bareSiblings = new Set([...siblings].map(t => foldSep(t.slice(1))))
      const alreadyMentioned = relMentionedHere(inputRef.current, rel, siblings)
      if (!alreadyMentioned) {
        // Insert at the last known caret rather than always appending, so
        // "please check @README.md for bugs" stays possible when the file is
        // added mid-sentence instead of the token always landing at the end.
        const prev = inputRef.current
        const caret = insertAt()
        let at = caret == null ? prev.length : Math.max(0, Math.min(caret, prev.length))
        // The caret can legally be parked INSIDE a token: vertical arrows
        // are only intercepted for prompt history at the text edges, the
        // select-snap deliberately leaves a caret inside a PASTE token (the
        // click expander's preview case), and a click can land inside a
        // mention. Splicing there tears the literal apart -- for a paste
        // token pruneBlocks then drops the block and the draft effect
        // persists the pruned list, destroying the pasted content
        // unrecoverably (fork Opus review). Clamp to the nearer edge of any
        // strictly containing range before slicing.
        at = clampOutOfTokens(prev, at)
        const before = prev.slice(0, at)
        const after = prev.slice(at)
        const lead = before && !/\s$/.test(before) ? ' ' : ''
        const trail = after && !/^\s/.test(after) ? ' ' : ''
        const run = `${lead}${token}${trail}${after ? '' : ' '}`
        voicePendingCaretRef.current = before.length + run.length
        setInput(before + run + after)
        recordSlotToken(absPath, token)
      } else {
        // Already mentioned, so no text was inserted -- but the file is
        // about to be staged below regardless, and the reconciliation effect
        // needs a recorded token to ever notice a later hand-edit on it (fork
        // GPT review: without this, a pick that lands on an existing mention
        // never gets bookkeeping, so deleting that mention later leaves an
        // orphaned chip -- the exact bug this PR fixes, through a side door).
        // Record the LITERAL form already in the text, not this handler's own
        // canonical `token` -- the existing mention could be a different
        // separator rendition (the Windows @-picker inserts backslash rels,
        // and a hand edit can mix both). It is found the same way
        // `alreadyMentioned` was, on the folded text, and read back from the
        // ORIGINAL at the match's indices (the fold is 1:1): the alias recorded
        // here is exactly the one whose presence justified the no-op insertion
        // branch, so this file never ends up with no recorded token (the
        // orphaned-chip gap, fork GPT review). Group 1 is the leading
        // boundary; `+ 1` skips the `@`.
        const m = mentionTokenRegex(foldSep(rel), '', bareSiblings).exec(foldSep(inputRef.current))
        const existing = m ? inputRef.current.slice(m.index + m[1].length + 1, m.index + m[1].length + 1 + rel.length) : null
        if (existing) recordSlotToken(absPath, `@${existing}`)
      }
      // addPendingFile dedupes by canonical Windows identity: the @-picker may
      // have already staged this file in native `C:\…` form, and an exact check
      // would send it twice under two attachment markers.
      setPendingFiles(prev => addPendingFile(prev, absPath))
    }
    revealComposer()
  }, [recordSlotToken, clampOutOfTokens, currentSlotTokens, relMentionedHere, setInput, voiceCaretRef, currentProjectRef, inputRef, voicePendingCaretRef, pendingFilesRef, setPendingFiles])

  // Recorded aliases of every known path other than `p` that are LIVE in
  // `input`, optionally limited to the paths in `onlyPaths`. Presence is
  // tested with the permissive boundary and no cross-alias context (no
  // recursion). The reconciliation below explains why each protecting set
  // has the scope it has.
  const liveOtherAliases = useCallback((
    input: string, known: Record<string, string[]>, p: string, onlyPaths?: ReadonlySet<string>,
  ) => {
    const others = new Set<string>()
    for (const [otherPath, otherTokens] of Object.entries(known)) {
      if (otherPath === p || (onlyPaths && !onlyPaths.has(otherPath))) continue
      otherTokens.forEach(t => { if (relMentionedHere(input, t.slice(1))) others.add(t) })
    }
    return others
  }, [relMentionedHere])

  // THE revival decision: would the reconciliation re-stage the unstaged file
  // `p` off the mentions in `input`, given the recorded `known` aliases and
  // the `staged` set? One function, so the chip remove handler asks exactly
  // the question the reconciliation will ask on the next commit (fork GPT
  // review): a leftover the reconciliation would never revive from, such as
  // an old project's alias, must not cost the chip its undo. The reasoning
  // for each condition is at its use in `reconcileFileChips`.
  const fileChipRevives = useCallback((
    input: string, p: string, known: Record<string, string[]>, staged: ReadonlySet<string>,
  ) => {
    const aliases = known[p]
    if (!aliases?.length) return false
    const sameSlash = (a: string, b: string) => a.replace(/\\/g, '/') === b.replace(/\\/g, '/')
    const currentRel = makeRelative(p, normalizeWindowsPath(currentProjectRef.current || ''))
    const otherAliases = liveOtherAliases(input, known, p)
    const stagedLive = liveOtherAliases(input, known, p, staged)
    return aliases.some(token => {
      const storedRel = token.slice(1)
      if (!sameSlash(storedRel, currentRel)) return false
      if ([...stagedLive].some(s => extendsConsumably(s, token))) return false
      return relMentionedHere(input, storedRel, otherAliases)
    })
  }, [liveOtherAliases, relMentionedHere, currentProjectRef])

  // pickedFileTokens reconciliation -- keeps a file chip in sync with its
  // `@rel` alias(es) in BOTH directions, the same way a folder chip
  // (`useStagedFolderRefs`) already is for free by being text-derived. A file chip is
  // list-backed (`pendingFiles`), not text-derived, so without this it never
  // notices a hand-edit:
  //  - A picker-picked/tree-added file none of whose recorded aliases appear
  //    in the text anymore has been hand-edited out (cut, or an over-eager
  //    selection-delete) -- drop its chip.
  //  - A file whose alias reappears (undo, or the text pasted back after a
  //    cut-to-reposition -- the very workflow the caret fix above exists for)
  //    restages its chip, rather than leaving the mention as plain text with
  //    the attachment silently gone.
  //
  // Each path can carry MULTIPLE recorded aliases (fork GPT review): a later
  // pick of the SAME file under a changed project adds a second `@rel` form
  // without replacing the first, since the two texts can coexist. So the
  // stale check requires NONE of the aliases to still be mentioned, and
  // revival accepts ANY of them.
  //
  // Every comparison trusts the STORED alias strings directly (not a
  // re-derived rel): `pickedFileTokens` is slot-scoped, so the entries read
  // here can only be ones THIS slot itself wrote, at THIS slot's own pick
  // time(s) -- unlike a re-derivation against the slot's CURRENT project,
  // which breaks the moment the slot's project changes after a pick (the
  // text was never rewritten, so the OLD rel is still what is sitting there;
  // re-deriving against the NEW project would falsely call it stale and drop
  // a still-referenced attachment).
  //
  // Entries are NOT deleted on unstage, nor on the chip's own ✕ -- keeping
  // the mapping alive is what makes revival possible, and it is how an undo
  // after ✕ brings the attachment back with its mention. The ✕ drops them
  // only when a mention it could not strip is left in the text.
  // Uploaded/dropped files carry no recorded aliases and are never touched by
  // either direction -- there is no text mention to lose or regain.
  //
  // Revival, unlike the stale check above, DOES cross-check each candidate
  // alias's rel against a fresh re-derivation under the CURRENT project (fork
  // GPT review): an entry that survived an earlier unstage can be for a path
  // whose alias no longer matches what that same rel would mean under the
  // project the slot has SINCE moved to. Reviving it purely because the text
  // happens to contain that rel again -- typed with the NEW project's own
  // file in mind -- would silently attach the OLD, unrelated absolute path
  // instead. An alias is skipped (not merely blocked) when the two ever
  // disagree, so a stale cross-project alias can never revive on a
  // coincidental rel match.
  //
  // Reload / slot-restore: `pickedFileTokens` is persisted beside `fileDrafts`
  // (`chatFileTokenDrafts`, same sessionStorage lifetime), so a chip restored
  // from a draft keeps the aliases it was recorded under and this effect
  // treats it exactly like a chip picked in this mount. Only a draft saved
  // before the aliases were persisted restores alias-less; such a chip sticks
  // on a hand-edit until the next pick records an alias for it.
  //
  // Runs from the composer-draft commit (`onComposerDraftCommit`, via
  // `reconcileFileChipsRef`) rather than an `[input]` effect: the text lives
  // in the draft store, so it fires once per committed text change without
  // re-rendering this page on a keystroke.
  const reconcileFileChips = useCallback((input: string) => {
    const known = currentSlotTokens()
    if (!known) return
    const staged = new Set(pendingFiles)
    // Deliberately NOT a general boundary-checked path-SUFFIX fallback (tried
    // and reverted -- fork GPT review, two rounds): recognizing "any suffix of
    // this path is mentioned" as proof of reference sounds safe for a single
    // file, but two DIFFERENT staged files can share a trailing path segment
    // (`/repo/src/main.ts` and `/repo/other/src/main.ts` both end in
    // `src/main.ts`). Deleting one file's own mention while the other's
    // longer, unrelated mention remains would then read as "still
    // referenced" and skip unstaging the WRONG file -- sent when the user
    // explicitly tried to remove it. Only the EXACT aliases this file was
    // actually recorded under (never a suffix borrowed from a sibling
    // file's mention) are trusted here.
    //
    // No shortened or re-derived spelling is accepted either: every
    // spelling this check ACCEPTS is one a pick RECORDED, which is the
    // symmetry that retires the recorded-vs-accepted divergence class
    // (staleness, revival and the remove strip all test the same set).
    // Text that stops matching every recorded alias -- hand-edited,
    // pasted over, or a restored draft -- is not a mention anymore, the
    // chip visibly unstages, and re-attaching is one pick away.
    // Every OTHER currently-STAGED path's aliases, so `relMentionedHere`
    // can refuse a punctuation-boundary match that is really the start of
    // a DIFFERENT file's own longer mention (fork GPT review, round 18).
    // Scoped to `staged`, not the full historical `known` map (fork GPT
    // review, round 20): entries are deliberately never deleted on
    // unstage (kept alive for revival), so `known` can carry an OLD
    // project's long-abandoned alias for a file that isn't attached to
    // anything anymore. Treating that stale history as "another real
    // file to protect" forced the strict boundary onto a CURRENTLY
    // staged file's own, entirely ordinary punctuated mention, and
    // wrongly unstaged the attachment the user is actually still typing
    // about.
    //
    // Only aliases LIVE in the current input protect (fork GPT review):
    // the `staged` snapshot is pre-effect, so a sibling whose own
    // mention was deleted in THIS SAME edit still sat here and forced
    // the strict boundary onto a survivor's ordinary punctuated
    // mention (`@report!` dying with `@report,`) -- dropping BOTH
    // attachments. A dead alias has no text occurrence left to
    // mis-attribute, so it protects nothing; the round-18 hazard
    // needs the longer mention actually present.
    const otherAliasesFor = (p: string) => liveOtherAliases(input, known, p, staged)
    const stale = pendingFiles.filter(p => {
      const aliases = known[p]
      if (aliases == null || aliases.length === 0) return false
      return !aliases.some(token => relMentionedHere(input, token.slice(1), otherAliasesFor(p)))
    })
    // Revival's protecting set is WIDER (fork GPT review): revival's
    // candidates are UNSTAGED entries, so a staged-scoped set is empty
    // exactly when it matters -- stage `report` and `report,`, delete both
    // mentions, paste `@report,` back: with no staged sibling to protect,
    // the shorter alias matched the longer file's own mention via the
    // punctuation boundary and BOTH revived, binding the text (and its
    // attachment) to the wrong file at send. Every KNOWN path's live
    // aliases protect here (invariant I3: the protecting population covers
    // the candidate population). The liveness requirement is unchanged, so
    // round 20's hazard stays closed: a dead historical alias has no text
    // occurrence to mis-attribute and still protects nothing. Residual,
    // preferred over the false positive it replaces: an OLD project's alias
    // literally live in the text refuses to revive a same-prefix file -- a
    // visible false negative, one pick away. (`fileChipRevives` builds this
    // set as `liveOtherAliases(input, known, p)`.)
    //
    // Revival asymmetry guard (fork GPT review), also in `fileChipRevives`:
    // a LONGER candidate whose
    // text occurrence merely extends a STAGED file's live alias with
    // boundary-consumable characters is not unambiguous evidence -- typing
    // a comma after a staged `@report` reads as that mention plus
    // punctuation, not as the deleted sibling `report,` re-typed. The
    // prefix-sibling `unsafe` test is one-directional by design (it forces
    // strict onto the SHORTER staged alias), so without this the longer
    // candidate always got the permissive boundary against a shorter
    // staged sibling and silently re-attached. Same predicate, opposite
    // direction; staged-scoped and liveness-gated like otherAliasesFor, so
    // the M2(b) both-unstaged revival (no staged sibling) is untouched and
    // the round-20 dead-history hazard stays closed. Accepted residual:
    // pasting `@report,` back while `report` is staged does not revive
    // `report,` -- a visible false negative, one pick away.
    const revived = Object.keys(known).filter(p =>
      !staged.has(p) && !stale.includes(p) && fileChipRevives(input, p, known, staged))
    if (!stale.length && !revived.length) return
    setPendingFiles(prev => {
      const next = prev.filter(p => !stale.includes(p))
      return revived.reduce((acc, p) => addPendingFile(acc, p), next)
    })
    // Announce the change the user did NOT initiate directly. Reconciliation
    // runs when an `@mention` is hand-edited or pasted, which moves no focus, so
    // a screen-reader user otherwise gets no sign a chip appeared or vanished and
    // may send with (or without) a file they did not mean to (#14597). The chip's
    // own ✕ and a picker pick move focus already, so they do not route here.
    // A chip shows its basename, so the announcement names the file the same way;
    // when one edit changes several at once, the names join into one message so
    // the single live region is not overwritten before it is read.
    //
    // ONE reconciliation == ONE announce call, even when the same edit both
    // restages and unstages chips (paste one `@mention` over another). The two
    // messages are joined into a single string so React does not batch two
    // `setAttachmentAnnouncement` calls into one render and drop the first --
    // which would otherwise leave a screen reader hearing only the removal --
    // and so the live region's nonce advances exactly once (two calls advanced
    // it by two, and `nonce % 2` could land back on its prior parity and never
    // fire). Restore is named before removal so the net new attachment is heard
    // first.
    if (announceAttachmentChange) {
      const basename = (p: string) => p.split(/[/\\]/).filter(Boolean).pop() || p
      const parts: string[] = []
      if (revived.length) {
        parts.push(i18nT('components.chatInput.attachment_restored', {
          file: revived.map(basename).join(', '),
        }))
      }
      if (stale.length) {
        parts.push(i18nT('components.chatInput.attachment_removed', {
          file: stale.map(basename).join(', '),
        }))
      }
      if (parts.length) announceAttachmentChange(parts.join('. '))
    }
  }, [currentSlotTokens, relMentionedHere, liveOtherAliases, fileChipRevives, pendingFiles, setPendingFiles, announceAttachmentChange])
  reconcileFileChipsRef.current = reconcileFileChips

  /** The file chip's remove (ChatInput `onRemoveFile`). */
  const removeFileChip = (p: string) => {
    setPendingFiles(prev => prev.filter(x => x !== p))
    // A picker-picked file also inserted `@rel` token(s) into the
    // composer, so its remove strips ALL of them too — the same
    // contract folder chips have, so the two chip kinds cannot
    // disagree about what "remove" means. The recorded aliases can
    // include more than one: a later pick under a changed project
    // adds a second `@rel` form for the same file without
    // replacing the first (fork GPT review) -- stripping only one
    // would leave the other sitting in the text with no chip
    // behind it. The aliases survive a reload (chatFileTokenDrafts),
    // but a draft saved before they were persisted re-stages the
    // file without any. Fall back to deriving the file's EXACT rel
    // under the current project -- the only form the picker ever
    // inserts -- and strip that if it is mentioned. Never a
    // suffix walk: a shortened
    // spelling matches no recorded alias, so it is ordinary
    // message text, not this chip's reference to delete.
    // Uploaded/dropped files lie outside the project or have no
    // token in the text, so the derivation finds nothing and
    // their remove stays state-only. On no match the text is
    // left alone -- visible and editable is the safe fallback.
    const slotTokens = currentSlotTokens()
    const projectIsWindowsShaped = isWindowsShapedPath(currentProjectRef.current || '')
    const derivedRel = makeRelative(p, normalizeWindowsPath(currentProjectRef.current || ''))
    const liveToken = derivedRel !== p && relMentionedHere(inputRef.current, derivedRel)
      ? `@${derivedRel}` : null
    const tokens = [...new Set([
      ...(slotTokens?.[p] ?? []),
      ...(liveToken ? [liveToken] : []),
    ])]
    // A later pick under a changed project can compute the SAME
    // rel for a DIFFERENT absolute path (fork GPT review) --
    // both files then share the identical literal alias string,
    // recorded independently under their own path. Stripping a
    // shared alias here would delete the ONLY text occurrence
    // both files' bookkeeping points at, and the reconciliation
    // effect would then read the OTHER, untouched file as stale
    // too and silently drop it. Any alias still claimed by
    // another currently-staged path is left in the text.
    const otherAliases = new Set<string>()
    for (const otherPath of pendingFilesRef.current) {
      if (otherPath === p) continue
      slotTokens?.[otherPath]?.forEach(t => otherAliases.add(t))
      // A RESTORED sibling has no recorded aliases at all
      // (pickedFileTokens is in-memory, rebuilt empty per mount,
      // while pendingFiles rehydrates from the persisted draft)
      // -- so it contributed nothing here, and removing a chip
      // whose rel the sibling's name extends consumably
      // (`report` beside `report,`) selected the PERMISSIVE
      // boundary and stripped the removed file's form out of
      // the SIBLING's mention, corrupting text the sibling
      // still points at (fork GPT review). Mirror the removed
      // side's own `liveToken` fallback: a sibling's
      // staying-staged claim is exactly its derived rel under
      // the current project, so that form always joins the
      // guard -- recorded aliases or not.
      const otherRel = makeRelative(otherPath, normalizeWindowsPath(currentProjectRef.current || ''))
      if (otherRel !== otherPath) otherAliases.add(`@${otherRel}`)
    }
    if (!tokens.length) {
      if (slotTokens) delete slotTokens[p]
      return
    }
    // On a Windows-shaped project a chip's OWN mention can have
    // been hand-edited to another separator spelling of the SAME
    // file after it was staged (rounds 9-12), uniform or mixed
    // (`@src\foo/bar.ts`). The reconciliation keeps the chip
    // staged through that edit, but the RECORDED alias still says
    // the old spelling, so stripping only that literal would leave
    // the edited mention behind as a stale, unattached `@rel`
    // (fork GPT review). Separator identity is therefore folded
    // ONCE (`foldWinSep`): the token, the sibling guard and the
    // text are compared with `\` read as `/`. The fold is a 1:1
    // character substitution, so each match found on the folded
    // copy is cut from the ORIGINAL at the same indices, and the
    // user's own spelling of everything else is untouched. The
    // sibling guard folds too (round 14): another staged file's
    // alias recorded in a different spelling is the same text
    // occurrence and must still protect it.
    const fold = foldWinSep(projectIsWindowsShaped)
    const foldedOthers = new Set([...otherAliases].map(fold))
    const cutFolded = (orig: string, re: RegExp): string => {
      // Keeps group 1 (the leading boundary) of each match, like
      // a `'$1'` replace, but locates matches on the folded copy.
      const folded = fold(orig)
      let out = ''
      let last = 0
      re.lastIndex = 0
      for (let m = re.exec(folded); m !== null; m = re.exec(folded)) {
        out += orig.slice(last, m.index + m[1].length)
        last = m.index + m[0].length
        if (m[0].length === 0) re.lastIndex++
      }
      return out + orig.slice(last)
    }
    const stripMentions = (source: string) => tokens.reduce((text, token) => {
      const candidate = fold(token)
      if (foldedOthers.has(candidate)) return text
      // Shares `mentionBoundary` (via `mentionBoundaryFor`, which also
      // takes the sibling aliases -- round 18) and
      // `leadingMentionBoundary` (round 19) with
      // `mentionRegex` (round 16) -- a mention followed
      // directly by punctuation (`@file.ts,`, no space) or
      // wrapped in parens (`(@file.ts)`) survives
      // reconciliation as still-mentioned (rounds 15/19), but
      // this replace used the OLD, stricter boundaries and
      // never matched it: the chip disappeared from the list
      // while its text reference was left behind and sent as
      // a stale, unattached `@rel`.
      const esc = candidate.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
      const boundarySrc = mentionBoundaryFor(candidate, foldedOthers)
      // A wrapping bracket pair is stripped WITH the mention,
      // not left behind (fork Opus review): matching only
      // `leadingMentionBoundary`'s optional OPENING bracket
      // and re-emitting it via `$1` left the never-consumed
      // CLOSING bracket stranded -- `(@file.ts)` -> `()`, a
      // stray, empty pair sent as real message text. Tried
      // for each bracket kind BEFORE the general strip below,
      // so a genuinely wrapping pair is removed as a unit;
      // an unpaired or mismatched bracket still falls through
      // to the general strip and is left in place, same as
      // any other punctuation this PR doesn't try to erase.
      let stripped = text
      // The optional `:line` suffix is consumed with the token,
      // bare and wrapped alike -- but ONLY under the permissive
      // boundary (fork Opus review): under `strictMentionBoundary`
      // a LONGER sibling alias extends this one with `:\d`, so a
      // trailing `:42` can be the sibling's own name and eating it
      // (even inside `(@a.ts:42)`) would strip the sibling's
      // wrapped mention and unstage its chip. One definition gates
      // every consumption site in this strip. A regex literal's
      // `.source`, not a string constant -- the same AST-shape
      // i18n exemption the shared boundary constants rely on.
      // ONE suffix grammar (fork First Principles review): the
      // shared MENTION_LINE_SUFFIX is the definition; this site
      // only wraps it optional and gates it on the permissive
      // boundary, same as replaceTokens' drop form. The shared
      // regex is anchored for its exec() consumers, so the `^`
      // is sliced off for mid-pattern embedding here.
      const lineSuffix = boundarySrc === mentionBoundary ? `(?:${MENTION_LINE_SUFFIX.source.slice(1)})?` : ''
      // ONE wrapper-pair table (fork First Principles review):
      // shared with replaceTokens' drop form in fileTokens.ts.
      for (const [open, close] of Object.entries(WRAPPER_CLOSER)) {
        const openEsc = open.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
        const closeEsc = close.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
        // `(@src/main.ts:42)` is one wrapped mention: stripping
        // only `(@src/main.ts)`'s bytes would leave `:42)`
        // stranded as message text (fork GPT review).
        stripped = cutFolded(stripped, new RegExp(`(^|\\s)${openEsc}${esc}${lineSuffix}${closeEsc}(?: |(?=${boundarySrc})|$)`, 'g'))
      }
      return cutFolded(stripped, new RegExp(`(${leadingMentionBoundary})${esc}${lineSuffix}(?: |(?=${boundarySrc})|$)`, 'g'))
    }, source)
    // Removal is undoable: the chip's aliases stay recorded, so an undo
    // that brings the mention back revives the chip through the
    // reconciliation effect above, and a redo that takes it out again
    // unstages it. The send-clear drops them. When a mention of this file
    // survives the strip (a copy shared with another staged file, or a
    // second copy the strip cannot reach) the aliases are dropped instead,
    // or that leftover would revive the chip this click just removed.
    // The survival check IS the reconciliation's revival decision
    // (`fileChipRevives`), asked of the stripped text with the
    // aliases this file would keep (`tokens`, including a live
    // derived spelling a restored draft never recorded) and this
    // file already out of the staged set. If the next commit would
    // revive the chip off a leftover, the aliases are dropped. If
    // it would not -- the leftover is a longer sibling's own
    // mention (fork Opus review) or an old project's alias the
    // revival never accepts (fork GPT review) -- they stay, and
    // undo can bring the chip back. A restored sibling protects
    // through the aliases persisted with the draft; only a
    // sibling from a draft saved before that protects nothing, so
    // its `@report,` reads as this file mentioned and the aliases
    // are dropped, as the reconciliation would otherwise revive it.
    const after = stripMentions(inputRef.current)
    const keptStaged = new Set(pendingFiles.filter(f => f !== p))
    if (slotTokens && fileChipRevives(after, p, { ...slotTokens, [p]: tokens }, keptStaged)) delete slotTokens[p]
    else if (liveToken) recordSlotToken(p, liveToken)
    setInput(prev => stripMentions(prev))
  }
  // Backspace/Delete on or next to a STAGED file mention removes the whole
  // `@mention` as one unit, so a hand-edit can never leave a half-reference
  // (`@src/main.t`) in the text whose chip the reconciliation then silently
  // unstages, sending the agent a file name with no attachment (#14675). The
  // chip follows via the reconciliation effect, which drops a chip whose
  // aliases no longer appear. Returns the atomic-delete result (new text and
  // caret) when the keystroke lands on a staged mention, or null to let the
  // key fall through to the default one-character edit -- a plain word, a
  // typed `@token` with no staged chip behind it, or any key but a modifier-
  // free Backspace/Delete on a collapsed caret. Only the recorded aliases of
  // currently-staged files are treated atomically: a leftover alias of a
  // removed chip is ordinary text the user is editing by hand.
  const mentionKeyEdit = (
    text: string, selStart: number, selEnd: number, key: string, mods: MentionKeyMods,
  ): { value: string; caret: number } | null => {
    if (key !== 'Backspace' && key !== 'Delete') return null
    // Shift+Delete over a selection is the native Cut chord on Windows/Linux:
    // handing it to the atomic delete would preventDefault the browser's cut,
    // so the selection vanishes and nothing reaches the clipboard — the user
    // loses the text. Let it fall through so the native cut runs (cut is the
    // deferred value-change seam, #18263) (crew-pr-reviewer / Opus).
    if (key === 'Delete' && mods.shift && selStart !== selEnd) return null
    // Shift alone is a plain delete (Shift+Backspace == Backspace); a word-
    // delete chord (Ctrl/Alt, not Cmd) deletes the adjacent mention whole, the
    // way the paste-token atom does, so `@src/main.ts` never degrades to a
    // half-reference `@src/main.` the chip then unstages (crew-pr-reviewer).
    // Cmd (meta) line-delete is left to the browser — a mention sits mid-line
    // and the native line delete is the user's intent, not a chip edit.
    const wordDelete = (mods.alt || mods.ctrl) && !mods.meta
    if (mods.meta) return null
    if (!wordDelete && (mods.alt || mods.ctrl)) return null
    const hasSelection = selStart !== selEnd
    const slotTokens = currentSlotTokens()
    if (!slotTokens) return null
    const staged = new Set(pendingFilesRef.current)
    const fold = foldWinSep(isWindowsShapedPath(currentProjectRef.current || ''))
    const folded = fold(text)
    const caret = selStart
    // Every alias of every currently-staged file, each with the sibling set
    // the shared matcher needs so one mention is never read as the prefix of
    // a longer sibling's (`report` vs `report,`). Separators are folded on a
    // Windows-shaped project so a respelled mention is still found; the fold
    // is 1:1, so indices on the folded copy are the original's.
    const aliases: string[] = []
    for (const p of staged) for (const t of slotTokens[p] ?? []) aliases.push(fold(t.slice(1)))
    // Longest alias first: when one staged mention is a prefix of another
    // (`@report` vs `@report final.pdf`), the complete mention must win the
    // scan, else the short alias matches inside the long one and leaves a
    // half-reference whose chip then unstages (GPT 6.1 review).
    aliases.sort((a, b) => b.length - a.length)
    const siblingSet = new Set(aliases)
    // The whole-mention spans [at, end) this keystroke should remove, each as
    // original-string indices (the fold is 1:1). A collapsed caret keeps at
    // most one; a selection keeps every mention the range overlaps.
    const hits: { at: number; end: number }[] = []
    for (const bare of aliases) {
      const others = new Set([...siblingSet].filter(a => a !== bare))
      const re = mentionTokenRegex(bare, 'g', others)
      for (let m = re.exec(folded); m !== null; m = re.exec(folded)) {
        if (m[0].length === 0) { re.lastIndex++; continue }
        const at = m.index + m[1].length // the `@`
        const end = m.index + m[0].length // just past the token (boundary is a lookahead)
        let take: boolean
        if (hasSelection) {
          // A selection that overlaps any part of the mention takes it whole —
          // a partial cover would otherwise slice it to a half-reference.
          take = selStart < end && selEnd > at
        } else if (wordDelete) {
          // Word-delete chord (Ctrl/Alt+Backspace / +Delete): use the SAME
          // predicate as a plain delete — the caret inside the mention or just
          // at its near edge — so the two paths agree (a plain Backspace inside
          // `@src/main.ts` is already atomic, so the word-delete must be too),
          // AND treat the whitespace between the caret and the mention end as
          // adjacent: a word delete deletes back/forward across spaces to the
          // token, so Ctrl/Alt+Backspace with the caret after the trailing
          // space of `@src/main.ts ` must take the whole mention, not leave the
          // half-reference `@src/main.` the chip then unstages (crew-pr-reviewer).
          const onlySpaceBetween = (a: number, b: number) => {
            for (let i = a; i < b; i++) if (folded[i] !== ' ' && folded[i] !== '\t') return false
            return true
          }
          take = key === 'Backspace'
            ? (caret > at && caret <= end) || (caret > end && onlySpaceBetween(end, caret))
            : (caret >= at && caret < end) || (caret < at && onlySpaceBetween(caret, at))
        } else {
          // Plain (or Shift-held) Backspace fires when the caret is just past
          // the mention or inside it; Delete when it is just before or inside.
          // Strictly inside is covered by both so a mid-mention edit is atomic.
          take = key === 'Backspace' ? caret > at && caret <= end : caret >= at && caret < end
        }
        if (take && !hits.some(h => h.at === at && h.end === end)) hits.push({ at, end })
      }
    }
    if (hits.length === 0) return null
    hits.sort((a, b) => a.at - b.at)
    if (hasSelection) {
      // Remove the selection together with the whole of every mention it
      // overlaps, so a partial-cover selection never strands a fragment.
      // Span the FURTHEST end (and nearest start) across every hit, not the
      // last one by start: when a prefix sibling shares a start (`@report` in
      // `@report final.pdf`) the shorter span can sort last on `at`, and
      // taking its end would truncate the longer mention and orphan
      // `final.pdf` (GPT 6.1 review).
      const s = Math.min(selStart, ...hits.map(h => h.at))
      const e = Math.max(selEnd, ...hits.map(h => h.end))
      return { value: text.slice(0, s) + text.slice(e), caret: s }
    }
    const { at, end } = hits[0]
    // Word-delete across whitespace: when a Ctrl/Alt+Backspace fires with the
    // caret after the mention's trailing space(s) (or +Delete before its
    // leading space(s)), the native word delete would consume that whitespace
    // too, so remove the mention together with the spaces between it and the
    // caret. The adjacency was already proven whitespace-only above.
    if (wordDelete) {
      if (key === 'Backspace' && caret > end) {
        return { value: text.slice(0, at) + text.slice(caret), caret: at }
      }
      if (key === 'Delete' && caret < at) {
        return { value: text.slice(0, caret) + text.slice(end), caret }
      }
    }
    // Consume one adjacent space so the surrounding text does not collapse
    // to a double space (`a @f b` -> `a  b`); drop the leading one when
    // there is no trailing space to take instead.
    let s = at
    let e = end
    if (e < text.length && text[e] === ' ') e++
    else if (s > 0 && text[s - 1] === ' ') s--
    return { value: text.slice(0, s) + text.slice(e), caret: s }
  }
  /** The folder chip's remove (ChatInput `onRemoveDir`). */
  const removeDirChip = (rel: string) => {
    // The chip derives from the `@rel/` token, so removing the
    // reference IS removing the token. Boundary-checked so
    // "@src/pages/" never eats a longer "@src/pages/sub/" token.
    const esc = `@${rel}`.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    setInput(prev => prev.replace(new RegExp(`(^|\\s)${esc}(?: |(?=\\s)|$)`, 'g'), '$1'))
  }
  // A folder pick is complete once ChatInput inserts its `@rel/`
  // token — the chip derives from the text, so there is no state
  // to stage here. Files stay list-backed (uploads have no token)
  // and additionally record their inserted token for remove.
  const selectPickedFile = (path: string, kind?: FileKind, token?: string) => {
    if (kind === 'dir') return
    // Stage under the canonical (forward-slash Windows) identity —
    // the same form the tree context menu stages — so the SAME file
    // picked through both entry points dedupes instead of sending
    // twice. Token bookkeeping keys on the staged form so remove
    // finds it.
    const canon = normalizeWindowsPath(path)
    if (token) recordSlotToken(canon, token)
    setPendingFiles(prev => addPendingFile(prev, canon))
  }
  return { clampOutOfTokens, handleAddToContext, removeFileChip, removeDirChip, selectPickedFile, mentionKeyEdit }
}
