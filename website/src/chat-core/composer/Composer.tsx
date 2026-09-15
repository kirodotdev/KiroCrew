/**
 * `Composer` — the composable chat composer root (chat-core RFC §3, P3).
 *
 * `ChatInput` grew into a 4,900-line component with 110 props, and every new
 * capability meant one more row of prop plumbing at every host. That is exactly
 * how the split panes and the Crew Members DM ended up without a microphone
 * (#9775): the capability existed, the host simply had not wired 23 props.
 *
 * The replacement shape is a root plus atoms. The root holds ONE small context
 * a host can fill in a few lines — which slot this is, the editor's text, what
 * the surface is allowed to do — and each atom owns its own state and hooks
 * and reads the context instead of a prop chain. Surfaces compose what they
 * need: the main chat mounts everything, a pane will mount everything but the
 * queue split (#8852) once the Send atom exists, a side chat or embed picks its
 * subset. No atom imports the Redux store; everything it needs arrives through
 * this context or a hook (#8651, store-free seam).
 *
 * Strangler order. `ChatInput` keeps its textarea, send button, attachments,
 * pickers and follow-ups for now and becomes, step by step, a preset
 * composition of atoms; each slice moves one capability. This slice (P3-b)
 * moves VOICE: the root mounts the dictation atom, `ChatInput` reads the
 * resulting state from the context, and the 23 `voice*`/`onVoice*` props are
 * gone. Planned atoms, in order: Paste (P3-c), Mention (P3-d),
 * Slash + Skills (P3-e), Editor + Send + Attach + FollowUps (P3-f, at which
 * point `ChatInput` is a preset and is deleted).
 */
import { createContext, forwardRef, useCallback, useContext, useImperativeHandle, useLayoutEffect, useMemo, useRef, useSyncExternalStore, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import VoiceDisabledModal from '../../components/VoiceDisabledModal'
import { settingsPath } from '../../components/settingsPath'
import { useComposerVoice, composerVoiceInputProps, type ComposerVoice, type ComposerVoiceHost } from './useComposerVoice'

/** Host-supplied dictation behaviour the Voice atom cannot know on its own.
 *  Everything is optional; a per-slot composer (a pane) passes nothing. */
export type ComposerVoiceOptions = Pick<ComposerVoiceHost, 'isComposerFor' | 'deliverOffScreen' | 'onAutoSubmit' | 'pushToTalk'> & {
  /** Where the "voice needs setting up" modal sends the user. Defaults to Settings → Voice. */
  settingsRoute?: string
  /** Host-owned caret refs, when the host also reads them (ChatPage's resources
   *  controller does). Omit and the atom creates its own. */
  caretRef?: React.MutableRefObject<{ start: number; end: number } | null>
  pendingCaretRef?: React.MutableRefObject<number | null>
}

export interface ComposerProps {
  /** Slot whose composer this is. */
  slotKey: string | null
  /** The editor text. Controlled by the host until the Editor atom lands (P3-f). */
  value: string
  onChange: (value: string) => void
  voice?: ComposerVoiceOptions
  children?: ReactNode
}

/*
 * No `capabilities` record yet — deliberately. The atom shape (RFC §3 layer 4)
 * has one, so a surface can say what it does not offer, but every surface that
 * mounts this root today wants everything the root has (voice), and a surface
 * that wants nothing simply does not mount it. The record arrives with the first
 * writer: the ChatEmbed `embedded` preset, when its capability props move onto
 * the root in P3-f. Shipping a flag with no writer was reviewed as dead surface
 * twice on #9787; this note is what stops a third.
 */

function shallowEqual(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  const ka = Object.keys(a); const kb = Object.keys(b)
  if (ka.length !== kb.length) return false
  for (const k of ka) if (!Object.is(a[k], b[k])) return false
  return true
}

/** One-value external store: the Voice atom writes, `ChatInput` and the host
 *  handle read. `useSyncExternalStore` keeps the read tear-free and lets a
 *  parent (`ChatInput`) observe a child's (the atom's) state without lifting the
 *  hook out of the atom. */
interface Slot<T> {
  get: () => T
  /** `silent` swaps the value without notifying — only for a write made DURING
   *  render, where subscribers rendering later in the same pass read `get()`
   *  themselves and a notification would be an update-during-render. */
  set: (next: T, silent?: boolean) => void
  /** Wake every subscriber; one whose snapshot is already current is a no-op. */
  notify: () => void
  subscribe: (fn: () => void) => () => void
}
function createSlot<T>(initial: T): Slot<T> {
  let value = initial
  const subs = new Set<() => void>()
  const notify = () => { for (const fn of subs) fn() }
  return {
    get: () => value,
    set: (next, silent = false) => { if (next === value) return; value = next; if (!silent) notify() },
    notify,
    subscribe: (fn) => { subs.add(fn); return () => { subs.delete(fn) } },
  }
}

/** The `ChatInput` voice prop bundle, as the atom computes it. */
export type ComposerVoiceInputProps = ReturnType<typeof composerVoiceInputProps>

/** What the Voice atom publishes: its controls for the host's send path, and
 *  the values `ChatInput`'s mic button / dictation panel / hold gesture read. */
export interface ComposerVoiceSlice {
  controls: ComposerVoice | null
  inputProps: Partial<ComposerVoiceInputProps>
}

export interface ComposerContextValue {
  slotKey: string | null
  editor: {
    onChange: (value: string) => void
    /** Live mirror of the host's `value`, fresh every render. The text itself is
     *  deliberately NOT in the context: it changes on every keystroke, and a
     *  context that changes that often re-renders every atom for nothing. Atoms
     *  read through the ref; `ChatInput` still takes `value` as a prop until the
     *  Editor atom owns it (P3-f). */
    inputRef: React.MutableRefObject<string>
  }
  voiceOptions: ComposerVoiceOptions
  voiceSlot: Slot<ComposerVoiceSlice | null>
  /** Set only by `ComposerVoiceSliceOverride`: no atom must run under it. */
  testOverride?: true
}

const ComposerContext = createContext<ComposerContextValue | null>(null)

const NULL_SLOT = createSlot<ComposerVoiceSlice | null>(null)

/** The Voice atom's live state, or null when no atom is mounted (no root).
 *  This is how `ChatInput` learns about dictation now — one hook instead of 23
 *  props. */
export function useComposerVoiceSlice(): ComposerVoiceSlice | null {
  const ctx = useContext(ComposerContext)
  const slot = ctx?.voiceSlot ?? NULL_SLOT
  return useSyncExternalStore(slot.subscribe, slot.get, slot.get)
}

/** Imperative surface for the host's own send path. */
export interface ComposerHandle {
  /** The Voice atom's controls, or null when voice is not mounted. */
  voice: () => ComposerVoice | null
}

const ComposerRoot = forwardRef<ComposerHandle, ComposerProps>(function Composer(
  { slotKey, value, onChange, voice, children }, ref,
) {
  const inputRef = useRef(value); inputRef.current = value
  const voiceSlot = useRef(createSlot<ComposerVoiceSlice | null>(null)).current
  const voiceOptions = voice ?? EMPTY_VOICE_OPTIONS
  useImperativeHandle(ref, () => ({ voice: () => voiceSlot.get()?.controls ?? null }), [voiceSlot])
  const ctx = useMemo<ComposerContextValue>(() => ({
    slotKey,
    editor: { onChange, inputRef },
    voiceOptions, voiceSlot,
  }), [slotKey, onChange, voiceOptions, voiceSlot])
  // The atoms the root mounts live HERE, as siblings of the children, never
  // inside a subscriber. `ChatInput` subscribes to the voice slot; if the
  // atom were its child, every publish would re-render `ChatInput`, which would
  // re-render the atom, which would publish again (React #185 — seen in CI on
  // the first draft, where a test engine returning fresh callbacks each render
  // defeated the shallow-equal guard). As a sibling the atom re-renders only on
  // its own state and on the host's renders, and a publish cannot reach back up.
  return (
    <ComposerContext.Provider value={ctx}>
      <VoiceAtom />
      {children}
    </ComposerContext.Provider>
  )
})
const EMPTY_VOICE_OPTIONS: ComposerVoiceOptions = {}

// ---------------------------------------------------------------------------
// The dictation atom (root-mounted, module-private).
// ---------------------------------------------------------------------------

/**
 * Owns dictation for the composer it is mounted in: the STT config read, the
 * shared `useVoiceInput` engine, caret-anchored splicing, the disarm flags, the
 * one-mic-across-composers mutex, and the "voice needs setting up" modal. It
 * publishes its state into the root's voice slot; `ChatInput` reads it there
 * for the mic button, the dictation panel and the touch hold gesture (those
 * controls move into this atom together with the Editor in P3-f — they are
 * welded to the textarea today).
 *
 * Mounted by the `Composer` root, as a sibling of the host's children (see the
 * root for why not inside `ChatInput`). With no root there is no atom at all,
 * so a standalone `ChatInput` (tests, embeds without a root) simply has no mic.
 */
function VoiceAtom() {
  const ctx = useContext(ComposerContext)
  // `voiceSlot.subscribe` doubles as the "am I the real root?" test: the test
  // seam below hands `ChatInput` a fixed bundle and must not also run an engine.
  if (!ctx || ctx.voiceSlot === NULL_SLOT || ctx.testOverride) return null
  return <VoiceAtomInner ctx={ctx} />
}

function VoiceAtomInner({ ctx }: { ctx: ComposerContextValue }) {
  const { slotKey, editor, voiceOptions, voiceSlot } = ctx
  const navigate = useNavigate()
  const cv = useComposerVoice({
    sessionId: slotKey,
    inputRef: editor.inputRef,
    setInput: editor.onChange,
    isComposerFor: voiceOptions.isComposerFor,
    deliverOffScreen: voiceOptions.deliverOffScreen,
    onAutoSubmit: voiceOptions.onAutoSubmit,
    pushToTalk: voiceOptions.pushToTalk,
    caretRef: voiceOptions.caretRef,
    pendingCaretRef: voiceOptions.pendingCaretRef,
  })
  // Layout effect, not effect: a `useSyncExternalStore` update inside the
  // layout phase re-renders subscribers before paint, so `ChatInput` never
  // shows a frame without its mic.
  //
  // Publish only when a VALUE changed. This atom is rendered by `ChatInput`,
  // which is also its subscriber: an unconditional publish of a fresh object on
  // every render is a render loop (React #185). The bundle's members are
  // primitives, refs and memoized callbacks, so a shallow compare is exact —
  // it changes when dictation state changes and is stable across a re-render
  // that changed nothing. `controls` is refreshed in place: the host handle
  // reads it lazily and nothing renders from it.
  useLayoutEffect(() => {
    const next = composerVoiceInputProps(cv)
    const prev = voiceSlot.get()
    if (prev && shallowEqual(prev.inputProps, next)) { prev.controls = cv; return }
    voiceSlot.set({ controls: cv, inputProps: next })
  })
  useLayoutEffect(() => () => { voiceSlot.set(null) }, [voiceSlot])
  const { setup } = cv
  const close = useCallback(() => setup.setOpen(false), [setup])
  const openSettings = useCallback(() => {
    setup.setOpen(false)
    navigate(voiceOptions.settingsRoute ?? settingsPath({ tab: 'voice' }))
  }, [setup, navigate, voiceOptions.settingsRoute])
  return (
    <VoiceDisabledModal
      open={setup.open}
      reason={setup.reason}
      provider={setup.provider}
      onClose={close}
      onOpenSettings={openSettings}
    />
  )
}

/**
 * TEST SEAM. Mounts `ChatInput` under a root whose voice slice is a fixed bundle
 * of the props the mic button, dictation panel and hold gesture read, with the
 * Voice atom itself switched off so it cannot overwrite the bundle. This is how
 * the ChatInput dictation tests drive "recording", "transcribing", an error, a
 * device label — the states used to arrive as 23 props and now arrive here.
 */
export function ComposerVoiceSliceOverride({ inputProps, children }: { inputProps: Partial<ComposerVoiceInputProps>; children?: ReactNode }) {
  const inputRef = useRef('')
  const slotRef = useRef(createSlot<ComposerVoiceSlice | null>({ controls: null, inputProps }))
  // A fresh bundle on rerender replaces the published one, so a test can flip
  // `voiceRecording` and watch the composer react. Written DURING this render,
  // silently: `ChatInput` renders after us in the same pass and reads the slot
  // itself, so its `value` prop and the new voice state land in ONE render — the
  // way the old props did. Publishing from an effect instead gave the composer
  // one render with the new draft but the old voice state, and a hold-to-talk
  // test that flips both at once watched the hold bar unmount in that gap.
  if (slotRef.current.get()?.inputProps !== inputProps) slotRef.current.set({ controls: null, inputProps }, true)
  // And wake subscribers after commit: a memoized `ChatInput` whose own props did
  // not change bails out of the render above and never re-reads the slot. A
  // subscriber that did render is already current and re-renders nothing.
  useLayoutEffect(() => { slotRef.current.notify() }, [inputProps])
  const ctx = useMemo<ComposerContextValue>(() => ({
    slotKey: null,
    editor: { onChange: () => {}, inputRef },
    voiceOptions: EMPTY_VOICE_OPTIONS, voiceSlot: slotRef.current,
    testOverride: true,
  }), [])
  return <ComposerContext.Provider value={ctx}>{children}</ComposerContext.Provider>
}

// One composition mechanism: the root mounts the atoms its `capabilities`
// allow. There is deliberately no `Composer.Voice` export — a host mounting an
// atom by hand next to the root's own would run two engines into one slot.
// Named export only: nothing imports a default, and the module-private context
// is reached through `useComposerVoiceSlice` (the one consumer that exists).
// An atom outside this module will get a context hook the round it needs one.
export const Composer = ComposerRoot
