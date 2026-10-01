import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChevronsDownUp, ChevronsUpDown } from 'lucide-react'
import { COMPOSER_EXPAND_EVENT } from '../../pages/chat/composerFocus'
import { safeSetItem } from '../../utils/safeStorage'
import { i18nT } from '../../i18n/t'
import type { ComposerControl } from '../composerControl'

/* The user-driven collapse: "put the message box away while I read". The
   composer unmounts, a bar stands where it was, and the preference persists. */
/**
 * Whether the composer is collapsed for reading (`'1'`). Persisted for the same
 * reason the drag height is: someone reading long output wants the room to stay
 * reclaimed across a reload, not to re-collapse every mount.
 *
 * Persisting a composer view preference is only safe when the way back is
 * obvious, which is the trap `manualHeight` records ("one stray tap and the box
 * was that size for good, across reloads"). The way back here is a full-width
 * labelled bar standing exactly where the composer was, so it cannot be missed
 * and it is reachable by keyboard.
 */
const COMPOSER_COLLAPSED_LS_KEY = 'mc-composer-collapsed'

export function useComposerCollapse({ collapsible, composerControl, value }: {
  collapsible: boolean
  composerControl: () => ComposerControl | null
  value: string
}) {
  /**
   * Reading-space collapse. The composer is UNMOUNTED, not hidden, and a bar in
   * the composer's own wrapper stands in its place.
   *
   * Both of those are inherited rather than invented: the collapse reuses the
   * `AnimatePresence` gate the approval ghost bar already drives (see the
   * "Unified input container" comment in ChatInput.tsx), so the shown state stays
   * `initial === animate` — re-entry needs no animation and cannot be stranded
   * invisible — and unmounting is what keeps a collapsed composer from being a
   * persistently focusable invisible element.
   *
   * Collapsing cannot lose a half-typed message, and not because this component
   * is careful: the text is not ours to lose. `value` is a prop, and the host
   * owns it (ChatPage keeps it in `input`, seeded from and written back to its
   * per-slot `drafts` through `saveDrafts`), as it does the paste blocks, staged
   * files and session refs. The bar below still SAYS a draft is waiting rather
   * than leaving the user to trust that.
   *
   * Spelled like `voiceModePref`: a lazy localStorage read, a `safeSetItem`
   * write.
   */
  const [composerCollapsed, setComposerCollapsed] = useState(
    // Gated on the opt-in, not just read: without this a surface that has no
    // collapse entry point (the side chat, a split pane) still reads the key the
    // MAIN composer wrote and comes up collapsed, which is how "hiding is not
    // collapsing" gets shipped by accident. `collapsible` is host-supplied and
    // constant for a mount, so a lazy initializer is the whole story.
    () => collapsible && localStorage.getItem(COMPOSER_COLLAPSED_LS_KEY) === '1',
  )
  const collapsedBarRef = useRef<HTMLButtonElement | null>(null)
  /** Latest-value mirror for the window listener below, which is bound once. */
  const composerCollapsedRef = useRef(composerCollapsed)
  composerCollapsedRef.current = composerCollapsed
  /**
   * Two directions rather than one toggle, because each has a different place to
   * put the caret.
   *
   * Both controls unmount THEMSELVES on click: the menu row goes with the
   * composer, and the bar goes when the composer comes back. So neither can rely
   * on focus staying where it was -- with nothing done, focus falls to `body` and
   * a keyboard user re-Tabs from the top of the page on every collapse and every
   * restore. Focus therefore follows the gesture to whichever control now stands
   * in the same place: the bar on collapse, the textarea on restore.
   *
   * Next frame, not synchronously: the target does not exist until React has
   * committed the new state. Same reason `focusComposer` defers.
   */
  const collapseComposer = useCallback(() => {
    setComposerCollapsed(true)
    safeSetItem(COMPOSER_COLLAPSED_LS_KEY, '1')
    requestAnimationFrame(() => collapsedBarRef.current?.focus())
  }, [])
  const expandComposer = useCallback(() => {
    setComposerCollapsed(false)
    safeSetItem(COMPOSER_COLLAPSED_LS_KEY, '0')
    // Engine-neutral: the live composer may be the textarea or the opt-in
    // Lexical editor, and a collapsed composer re-mounts whichever it was.
    requestAnimationFrame(() => composerControl()?.focus())
  }, [composerControl])
  /**
   * Typing intent is an implicit expand.
   *
   * Every programmatic route to the composer resolves through the textarea
   * (`queryComposer` finds `textarea[data-composer-input]`; the `/` shortcut and
   * the autoFocusKey effect call `inputRef.current?.focus()`), and a collapsed
   * composer has no textarea -- so without this, `/`, quote-to-compose, a widget
   * send and post-create focus all silently do nothing, and a pre-fill lands in a
   * draft the user cannot see. Review named this correctly against the ghost
   * precedent this collapse otherwise inherits: the ghost is transient and the app
   * decides it, so a seconds-long no-op window is tolerable; this state is
   * indefinite and survives a reload, which would turn the same window into a
   * standing dead end for every "I want to type" gesture.
   *
   * Expanding on the intent is safe in a way hiding it would not be: the bar
   * already proves re-entry restores the draft intact, so the user loses nothing
   * by the box coming back uninvited -- they asked for it.
   */
  useEffect(() => {
    // Only the collapsible composer listens. A non-opted composer can never BE
    // collapsed, so its listener could only ever decline -- but declining is not
    // free: `preventDefault` on this event is what tells the caller a retry is
    // worth scheduling, and the event is a window broadcast every listener sees.
    // Not registering keeps the answer unambiguous with N composers on screen.
    //
    // Honest limit: this guard is currently REDUNDANT and a mutation removing it
    // survives the suite. With the state initializer above also gated, a non-opted
    // composer's `composerCollapsedRef` is always false, so the listener would
    // decline anyway and the two paths are indistinguishable from outside -- there
    // is no test that can tell them apart, so none is claimed. It is kept because
    // the two guards protect different things: that one stops a non-opted composer
    // from INHERITING the shared preference, this one stops it from answering for
    // the whole window if some future path sets the state another way. Deleting it
    // would make that future change silently wrong instead of merely wrong.
    if (!collapsible) return
    const onExpandRequest = (e: Event) => {
      // Read through a ref, and decide OUTSIDE the state updater: `preventDefault`
      // is a side effect, and a reducer that fires it would run it twice under
      // StrictMode's double-invoke and once for a no-op update.
      if (!composerCollapsedRef.current) return
      // Answering is what licenses the caller's one retry -- see
      // requestComposerExpand. Only a composer that was really collapsed answers,
      // so a lookup that missed for any other reason schedules nothing.
      e.preventDefault()
      setComposerCollapsed(false)
      safeSetItem(COMPOSER_COLLAPSED_LS_KEY, '0')
      // Deliberately no focus here: the caller does that, and only it knows
      // whether to focus or merely scroll into view -- `revealComposer` scrolls on
      // touch precisely to keep the soft keyboard off the content being read.
    }
    window.addEventListener(COMPOSER_EXPAND_EVENT, onExpandRequest)
    return () => window.removeEventListener(COMPOSER_EXPAND_EVENT, onExpandRequest)
  }, [collapsible])
  /**
   * One line of the waiting draft, shown on the collapsed bar.
   *
   * It is the user's OWN text rather than a status phrase, which is why the bar
   * can report a kept draft without adding a translated string: the sentence
   * they typed is already in their language. It also says more than a label
   * would — "Draft kept" tells you something is there, the first line tells you
   * WHICH message, which is the question someone returning to a collapsed
   * composer actually has.
   */
  const collapsedDraftLine = useMemo(() => {
    const line = value.split('\n').find(l => l.trim().length > 0)?.trim() ?? ''
    return line.length > 120 ? `${line.slice(0, 120)}…` : line
  }, [value])

  return { composerCollapsed, collapsedBarRef, collapseComposer, expandComposer, collapsedDraftLine }
}

/**
 * The collapse entry point, defined once and rendered by whichever menu the
 * layout has.
 *
 * There are two hosts because there are two layouts, and the split is forced:
 * on a pointer device the "+" opens a drop-up and this is a row in it, but on
 * touch `directFilePicker` turns that "+" into a bare file-input `<label>` and
 * no menu mounts at all -- so the same row hangs off the touch overflow
 * instead. Review found this the hard way: moving the control off the capped
 * action row into the "+" menu fixed a blocking rule and simultaneously made
 * the action unreachable at 390px, which `narrow-viewport-required` names in
 * as many words ("if a control is the only host of an action, removing it on a
 * phone removes the action").
 *
 * ONE definition rather than a copy per host, so the label, the description,
 * the icon and the close-then-collapse ordering cannot drift between layouts.
 * Closing both menus is unconditional and harmless: only one of them is ever
 * open, and each host unmounts with the composer anyway.
 */
export function collapseMenuRowElement(onCollapse: () => void) {
  return (
    <button
      type="button"
      data-testid="composer-collapse-row"
      onClick={onCollapse}
      title={i18nT('components.chatInput.collapse_composer')}
      className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover transition-colors cursor-pointer text-left"
    >
      <ChevronsDownUp size={14} className="w-4 shrink-0 text-muted lucide-inline" />
      <div className="min-w-0">
        <div className="text-[12px] font-medium text-text">{i18nT('components.chatInput.collapse_composer')}</div>
        <div className="text-[11px] text-muted leading-snug">{i18nT('components.chatInput.collapse_composer_desc')}</div>
      </div>
    </button>
  )
}

/**
 * The way back. It stands exactly where the composer was and is the only
 * thing this feature adds to the collapsed layout, because a collapse with
 * no discoverable restore is a trap rather than a preference — and this
 * preference persists across reloads, so the trap would too.
 *
 * A full-width button rather than a small icon: the whole bar is the
 * target, so the gesture back is as cheap as the gesture in, and it cannot
 * be missed by someone who does not remember collapsing anything.
 *
 * The button's accessible name must stay the ACTION, never the user's own
 * draft text. Two independent things hold that and either alone is
 * sufficient, which is measured rather than assumed: an explicit name
 * (`aria-label`, with `title` as an equivalent fallback) wins over element
 * contents, and `aria-hidden` on the draft line empties the contents so
 * the fallback has nothing to pick up. Dropping one keeps the name
 * correct; dropping BOTH makes the draft the label. Keep both — sighted
 * users get the draft, screen-reader users get the button's job, and
 * neither gets a sentence that is both.
 */
export function CollapsedComposerBar({ collapsedBarRef, expandComposer, collapsedDraftLine }: {
  collapsedBarRef: React.RefObject<HTMLButtonElement>
  expandComposer: () => void
  collapsedDraftLine: string
}) {
  return (
    <button
      type="button"
      ref={collapsedBarRef}
      data-testid="composer-collapsed-bar"
      onClick={expandComposer}
      aria-expanded={false}
      aria-label={i18nT('components.chatInput.expand_composer')}
      title={i18nT('components.chatInput.expand_composer')}
      className="w-full flex items-center gap-2 px-3.5 py-2 rounded-2xl border-none bg-transparent text-muted hover:text-text transition-colors cursor-pointer text-left"
    >
      <ChevronsUpDown size={16} className="shrink-0" />
      {/* The verb is ALWAYS visible, and the draft joins it when there is one.
          Review's blind reader named this control correctly but rated it "a
          guess, but a confident one" when the bar carried the draft alone: the
          action then lived only in `title`/`aria-label`, so a sighted reader
          had chevrons and grey text to infer from. Naming the action outright
          costs nothing and removes the inference.

          The draft still earns its place next to it -- it answers WHICH
          message is waiting, which is the question someone returning to a
          collapsed composer actually has, and it is the user's own words so it
          needs no translation.

          Both spans are aria-hidden: the button's explicit aria-label already
          names it, and exposing this as content would only duplicate it. */}
      <span aria-hidden="true" className="shrink-0 text-[13px] font-body">
        {i18nT('components.chatInput.expand_composer')}
      </span>
      {collapsedDraftLine && (
        <span aria-hidden="true" className="min-w-0 flex-1 truncate text-[13px] font-body text-muted">
          {collapsedDraftLine}
        </span>
      )}
    </button>
  )
}
