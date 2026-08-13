/**
 * useComposerDraft — the composer's DRAFT behaviour, owned in one place.
 *
 * A chat surface is three things: a transcript, a protocol, and a composer. The
 * first two already live here (`ChatMessageList` + the row registry, and
 * `protocol/`). This is the third — not the composer's markup, which every
 * surface is entitled to draw differently, but the behaviour of the text while
 * the user is still writing it:
 *
 *   - what a follow-up choice does to the draft (and how it is read back off it)
 *   - where text goes when the server hands it back (a cancelled queue entry, a
 *     rejected submit) and the user has already started typing something else
 *   - when Enter means "send" and when it means "my IME is committing a candidate"
 *   - how tall the box may grow before it scrolls
 *   - how large a submit may be before it is refused client-side
 *
 * These had drifted into three implementations with DIFFERENT semantics — most
 * visibly the picked-option state, which the side panel derives from the draft
 * text while the main composer keeps a separate `Set` beside it. Two answers to
 * "is this option selected" is one too many: the draft is what gets submitted, so
 * the draft is the only honest source, and an option the user has since woven
 * into their own sentence correctly stops being a removable block.
 *
 * Deliberately Redux-free, API-free and copy-free, like the rest of this
 * directory: it takes state and returns behaviour, so a surface backed by a
 * Redux slot, by React Query, or by an embedding app's own store can all use it.
 * Two host helpers are REUSED rather than reimplemented — `useImeGuard` for the
 * composition Enter and `chatDrafts.mergeIntoDraft` for the append — because a
 * private copy of either would be one more spelling of the thing this module
 * exists to collapse.
 *
 * The draft may be UNCONTROLLED (the hook holds it; pass `initialDraft` at most)
 * or CONTROLLED (`draft` + `onDraftChange`, for a surface that already persists
 * the text elsewhere, as the main composer does per slot). Both are supported
 * from the start so the remaining consumers do not have to change this signature.
 */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type FocusEvent,
  type KeyboardEvent,
  type MutableRefObject,
  type SetStateAction,
} from 'react'
import { useImeGuard } from '../hooks/useImeGuard'
import { mergeIntoDraft as appendToDraft } from '../utils/chatDrafts'

/** Max auto-grow height (px) before the box scrolls instead of growing. */
const DEFAULT_MAX_HEIGHT = 240

/**
 * `useLayoutEffect` warns when it runs on a server render, and this hook is part of
 * a published surface an app may render outside the dashboard. The measurement is a
 * DOM read either way, so on a server there is nothing to do and `useEffect` is the
 * correct no-op.
 */
const useIsomorphicLayoutEffect = typeof window === 'undefined' ? useEffect : useLayoutEffect

/**
 * Options forming the `, `-joined tail of `text`, in the order they appear.
 *
 * Longest match wins so an option that contains another plus the separator (`foo, bar`
 * next to `bar`) peels as itself. An option already peeled is not peeled twice: the tail
 * belongs to the picks, and an earlier occurrence is the user's own text.
 */
export function pickedFromDraft(text: string, options: readonly string[]): string[] {
  const picked: string[] = []
  let rest = text
  for (;;) {
    const hit = options
      .filter(o => o && !picked.includes(o) && (rest === o || rest.endsWith(`, ${o}`)))
      .sort((a, b) => b.length - a.length)[0]
    if (!hit) break
    picked.unshift(hit)
    rest = rest === hit ? '' : rest.slice(0, rest.length - hit.length - 2)
  }
  return picked
}

/**
 * UTF-8 size of `text` in bytes.
 *
 * Byte length, not `.length`: the limit exists because the server measures the
 * request body, and a draft of CJK or emoji is two to four times its code-unit
 * count. Sizing by characters would let a submit the server refuses through, and
 * the user would see a failure instead of the composer's own refusal.
 */
export function draftByteSize(text: string): number {
  return new TextEncoder().encode(text).length
}

export interface ComposerDraftOptions {
  /**
   * The choices offered alongside the last answer — normally straight from
   * `deriveFollowUpOptions`. Picks are read back off the draft against THIS list,
   * so an option that is no longer offered stops being a removable block.
   */
  followUpOptions?: readonly string[]
  /**
   * Size above which `exceedsByteLimit` reports true. Omitted or 0 = no limit.
   * The hook never blocks a send itself — the surface owns its own refusal and
   * its own wording for it.
   */
  maxBytes?: number
  /**
   * Cap for the auto-grown textarea, in px. Defaults to 240. Auto-grow only runs
   * for an element attached to `textareaRef`; a surface that sizes its own box
   * (or has no box to size) simply does not attach it.
   */
  maxHeight?: number
  /** UNCONTROLLED only: seed the draft. Read on first render, ignored after. */
  initialDraft?: string
  /**
   * CONTROLLED: the surface owns the text. When supplied, this value is what the
   * hook reads and every write is reported through `onDraftChange` instead of
   * being stored here.
   */
  draft?: string
  /** Required with `draft`. Receives the resolved next value, never an updater. */
  onDraftChange?: (next: string) => void
}

export interface ComposerDraft {
  draft: string
  /** Accepts a value or an updater, like `useState`'s setter. */
  setDraft: Dispatch<SetStateAction<string>>
  /** Attach to the textarea so it grows with content up to `maxHeight`. */
  textareaRef: MutableRefObject<HTMLTextAreaElement | null>
  /**
   * Spread onto the input so the IME guard can see composition start/end, and so
   * an abandoned composition recovers on blur. A composition abandoned WITHOUT a
   * `compositionend` (focus moves away mid-composition, an OS-level IME cancel)
   * leaves the guard latched; the `onBlur` half clears it, so Enter works again
   * when focus returns. A surface with its own blur behaviour composes the two —
   * spreading this AFTER an own `onBlur` would silently drop that handler.
   */
  composition: {
    onCompositionStart: () => void
    onCompositionEnd: () => void
    onBlur: <T extends HTMLElement>(e: FocusEvent<T>) => void
  }
  /**
   * Whether this key event is an IME committing a candidate rather than a real
   * keypress. Exposed for a surface whose key handler is too rich to delegate to
   * `submitOnEnter` — it still must not read a composition Enter as a submit.
   */
  isComposing: <T extends HTMLElement>(e: KeyboardEvent<T>) => boolean
  /** Append text to the draft, keeping whatever the user has already typed. */
  mergeIntoDraft: (incoming: string) => void
  /** Options currently forming the picked tail of the draft. */
  picked: ReadonlySet<string>
  /** Add or remove an option from the picked tail of the draft. */
  toggleOption: (option: string) => void
  /** `text` is above `maxBytes`. Always false when no limit was given. */
  exceedsByteLimit: (text: string) => boolean
  /**
   * Enter submits, Shift+Enter inserts a newline, and an Enter that is committing
   * an IME composition does neither. Pass the surface's own submit — this hook
   * never sends anything itself. Generic over the element so an `<input>`-based
   * composer can use it too.
   */
  submitOnEnter: <T extends HTMLElement>(e: KeyboardEvent<T>, submit: () => void) => void
}

export function useComposerDraft(opts: ComposerDraftOptions = {}): ComposerDraft {
  const {
    followUpOptions = [],
    maxBytes = 0,
    maxHeight = DEFAULT_MAX_HEIGHT,
    initialDraft = '',
    draft: controlledDraft,
    onDraftChange,
  } = opts
  const [internalDraft, setInternalDraft] = useState(initialDraft)
  const controlled = controlledDraft !== undefined
  const draft = controlled ? controlledDraft : internalDraft
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const ime = useImeGuard()

  // Read through refs so the setter identity is STABLE across renders. A setter that
  // changed every render would re-run any effect that lists it, and the side panel's
  // seed listener is registered in exactly such an effect.
  const draftRef = useRef(draft)
  draftRef.current = draft
  const controlledRef = useRef(controlled)
  controlledRef.current = controlled
  const onDraftChangeRef = useRef(onDraftChange)
  onDraftChangeRef.current = onDraftChange

  const setDraft = useCallback<Dispatch<SetStateAction<string>>>(next => {
    if (!controlledRef.current) {
      // Passed through rather than resolved here, so React's own update queueing
      // still applies and two updaters in one tick compose instead of colliding.
      setInternalDraft(next)
      return
    }
    const value = typeof next === 'function' ? next(draftRef.current) : next
    onDraftChangeRef.current?.(value)
  }, [])

  // Auto-grow so a multi-line paste or a seeded quote is fully visible instead of
  // being clipped to the element's `rows` default, then scroll past `maxHeight`.
  //
  // Measured with `overflow: hidden` and the previous scroll position restored:
  // the measurement resets height, and a surface whose transcript sits in the same
  // flex column would otherwise see that intermediate and reflow visibly. The
  // element's own CSS floor (e.g. a `min-h-*` class) still decides the empty size,
  // so a surface keeps its own resting geometry.
  useIsomorphicLayoutEffect(() => {
    const el = textareaRef.current
    if (!el) return
    const prevOverflow = el.style.overflow
    const prevScrollTop = el.scrollTop
    el.style.overflow = 'hidden'
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`
    el.style.overflow = prevOverflow
    el.scrollTop = prevScrollTop
  }, [draft, maxHeight])

  /**
   * Append, never substitute: both texts are typed work, and text the server has
   * handed back has no other home (its card or its request is already gone), so it
   * cannot be the one dropped. The host's own helper does the appending — the
   * reducer that stashes a released question already uses it, so a private copy
   * here would let the two halves of one release drift apart.
   */
  const mergeIntoDraft = useCallback((incoming: string) => {
    setDraft(prev => appendToDraft(prev, incoming))
  }, [setDraft])

  // What the last toggle wrote, the base it was built from, and the picks it wrote.
  // The picks are stored rather than re-read because the text does not determine them
  // uniquely: with overlapping options the re-derived set can differ from the real one,
  // and a base inferred from that wrong set eats the user's own prose.
  const lastJoinRef = useRef<{ produced: string; base: string; picks: readonly string[] } | null>(null)

  const picked = useMemo<ReadonlySet<string>>(() => {
    // While the draft is still exactly what a toggle wrote, the memo KNOWS what was
    // picked and text re-derivation must not overrule it. Re-deriving is ambiguous
    // whenever one option is another plus the separator ("Run tests" vs
    // "Check CI, Run tests"): the scan prefers the longest match, so picking the
    // short one would highlight the long one the user never chose.
    const memo = lastJoinRef.current
    if (memo && memo.produced === draft) return new Set(memo.picks)
    return new Set(pickedFromDraft(draft, followUpOptions))
  }, [draft, followUpOptions])

  /**
   * Picking edits the DRAFT rather than sending: the text in the composer is what gets
   * submitted, so a choice stays amendable.
   *
   * The memo is authoritative while the draft still equals what it produced; once the user
   * has typed, the picks are re-derived from the text and an option they have woven into
   * their own sentence correctly stops being a removable block.
   *
   * Computed OUTSIDE the state updater, against `draftRef`. A state updater must be pure:
   * StrictMode invokes it twice, and the memo write below is exactly the kind of side
   * effect that makes the second pass see state the first pass already changed.
   */
  const toggleOption = useCallback((option: string) => {
    const prev = draftRef.current
    const memo = lastJoinRef.current
    const remembered = memo !== null && memo.produced === prev
    const current = remembered ? [...memo.picks] : pickedFromDraft(prev, followUpOptions)
    let base: string
    if (remembered) {
      base = memo.base
    } else {
      const block = current.join(', ')
      if (block) {
        // `pickedFromDraft` only reports a tail, so the block is at the end by construction.
        base = prev.slice(0, prev.length - block.length)
        if (base.endsWith(', ')) base = base.slice(0, -2)
      } else {
        base = prev
      }
    }
    const next = current.includes(option)
      ? current.filter(o => o !== option)
      : [...current, option]
    const newBlock = next.join(', ')
    if (!newBlock) {
      lastJoinRef.current = null
      setDraft(base)
      return
    }
    const tail = base.trimEnd()
    // A draft mid-sentence may already end with the separator; a second one would be
    // submitted verbatim. Appending just the space still lands on the `, ` shape the
    // block is read back by.
    const produced = !tail
      ? newBlock
      : tail.endsWith(',') ? `${tail} ${newBlock}` : `${tail}, ${newBlock}`
    lastJoinRef.current = { produced, base, picks: next }
    setDraft(produced)
  }, [followUpOptions, setDraft])

  const exceedsByteLimit = useCallback(
    (text: string) => maxBytes > 0 && draftByteSize(text) > maxBytes,
    [maxBytes],
  )

  const isComposing = useCallback(
    <T extends HTMLElement>(e: KeyboardEvent<T>) => ime.isComposing(e),
    [ime],
  )

  const submitOnEnter = useCallback(<T extends HTMLElement>(
    e: KeyboardEvent<T>,
    submit: () => void,
  ) => {
    // Escape while a composition is latched means the user cancelled the IME, and
    // some IME/browser pairs deliver no `compositionend` for that cancel. Clear the
    // latch and fall through untouched — no submit, no preventDefault — so the
    // surface's own Escape behaviour (closing a panel, dismissing a picker) still
    // runs.
    if (e.key === 'Escape') {
      ime.reset()
      return
    }
    if (e.key !== 'Enter' || e.shiftKey) return
    // An IME sends a final Enter to COMMIT the candidate the user just chose. Reading
    // that as a submit sends a half-written question and is unrecoverable — the text is
    // already gone from the box. The guard is the host's, layered over the native flag,
    // because the native flag alone is false on that Enter in some browsers.
    if (ime.isComposing(e)) return
    e.preventDefault()
    submit()
  }, [ime])

  return {
    draft,
    setDraft,
    textareaRef,
    composition: {
      ...ime.composition,
      // Focus leaving the box mid-composition means no `compositionend` is coming.
      // Clear the latch, or the guard eats every Enter for the component's life.
      onBlur: () => { ime.reset() },
    },
    isComposing,
    mergeIntoDraft,
    picked,
    toggleOption,
    exceedsByteLimit,
    submitOnEnter,
  }
}
