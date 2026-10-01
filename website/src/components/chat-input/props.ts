import type { SessionRef } from '../../utils/sessionRefs'
import type { ResizeInfo } from '../../utils/resizeImage'
import type { SendMode } from '../../pages/chat/ChatSettings'
import type { AutomationRecord } from '../../monitoring/automation'
import type { PasteBlock } from '../../utils/pasteTokens'
import type { FileKind } from '../FilePickerMenu'
import type { PromptHistoryItem } from '../composerPromptHistory'

/* The composer's public prop contract. `components/ChatInput.tsx` takes it
   and re-exports `ComposerBusyMode`; the owners under this directory take the
   props they need as their own parameters, a few by indexed access
   (`ChatInputProps['onFileSelect']`). */
/** Busy-composer send affordance — see `ChatInputProps.busyMode`. */
export type ComposerBusyMode = 'split' | 'steer-only'

export interface ChatInputProps {
  /** The editor text. Omit it under a `<Composer draft>` root, which hands the
   *  text over through its store so the host does not re-render per keystroke. */
  value?: string
  onChange: (v: string) => void
  onSend: () => void
  /** Rendered inside the composer's own width wrapper, directly above the
   * bordered input box. Children here share the EXACT box geometry of the
   * composer (same padding container, same resolved max-width), so band
   * surfaces like the feature tip can never drift out of alignment the way
   * parallel sibling containers with percentage widths do. */
  aboveComposer?: React.ReactNode
  /** When true (composer is busy — a running turn, or background sub-agents
   * still running for the slot), show the split Steer/Queue send button.
   * Steer's meaning follows the state: mid-turn it injects into the live turn;
   * with only sub-agents running it starts a turn now instead of parking the
   * message behind them. If the slot's backend is not steer-capable (e.g.
   * claude), the POST safely falls through to the queue server-side.
   * Plumbing a per-slot capability flag is a follow-up. */
  canSteer?: boolean
  /** Act on the composer NOW rather than queueing: a mid-turn steer into the
   * running turn, or a fresh turn when only sub-agents are running. Reads the
   * composer text and pending files itself (ChatPage) and clears them
   * atomically — ChatInput must NOT clear the value around this call.
   *
   * `auto` asks the GATEWAY to choose between steering and queueing for this one
   * message (`steer: "auto"`, `decisions/points/message_steer.py`). It rides this
   * callback rather than a second one because it is the same send down the same
   * route: only the flag differs, and a host that ignores the argument keeps
   * today's behaviour, which is the steer this callback has always meant. */
  onSteer?: (opts?: { auto?: boolean }) => void
  /** Whether the host may offer `Auto (Jev)` in the split button's mode picker:
   * the gateway reports the Decisions seam as permitted by governance AND
   * consented to. Defaults to false, so a surface that never asks cannot offer a
   * mode the gateway would refuse to act on. */
  jevAutoAvailable?: boolean
  /** How the BUSY composer offers its send. `'split'` (default): the
   * Steer/Queue split button with its per-slot mode picker — the main chat
   * and split-view panes. `'steer-only'`: the surface has no queue concept —
   * while busy the plain send button stays in place and Enter/click steers
   * into the running turn (or starts one when only sub-agents run). A
   * conversation with ONE named peer (a member DM thread) uses it: talking to
   * a person has no "wait until they finish, then they'll listen" step, so
   * offering one would present a console control inside a chat. Needs
   * `canSteer` + `onSteer` exactly like the split button; without a steer
   * path the busy send still falls back to the queue button. */
  busyMode?: ComposerBusyMode
  disabled?: boolean
  placeholder?: string
  prefillHint?: boolean
  onDismissHint?: () => void
  /** macOS-only screenshot */
  onScreenshot?: () => void
  /** Browser-native file upload (cross-platform) */
  onUploadFiles?: (files: File[]) => void
  /** Whether file actions are in progress */
  uploading?: boolean
  /** Abort the upload in flight; turns the upload spinner into a cancel control */
  onCancelUpload?: () => void
  /** Pending file paths (images + non-images) for preview strip */
  pendingFiles?: string[]
  /** Pending folder references for the preview strip: RELATIVE paths with trailing slash, derived from `@rel/` composer tokens (a path reference handed to the agent, not an upload) */
  pendingDirs?: string[]
  /** Resize details keyed by pending-file path; renders a badge on the chip */
  resizedInfo?: Record<string, ResizeInfo>
  /** Remove a pending file by path */
  onRemoveFile?: (path: string) => void
  /** Remove a pending folder reference by its relative path (strips its composer token) */
  onRemoveDir?: (path: string) => void
  /** Session references staged by dragging a session onto the chat pane.
   *  Rendered as chips above the textarea, the same treatment as attachments.
   *  Serialized as links (never transcripts) when the message is sent. */
  pendingSessions?: SessionRef[]
  /** Unstage a session reference by its session key */
  onRemoveSessionRef?: (key: string) => void
  /** Show macOS-only buttons (screenshot) */
  isMac?: boolean
  /** Drag-and-drop handler for the entire input bar */
  onDrop?: (e: React.DragEvent) => void
  /** Drag-over event handler */
  onDragOver?: (e: React.DragEvent) => void
  /** Drag-leave event handler */
  onDragLeave?: (e: React.DragEvent) => void
  /*
   * Voice. There are no voice props any more (chat-core P3-b): dictation is the
   * Voice atom the `<Composer>` root mounts beside this input, read by
   * `components/ChatInput.tsx` through `useComposerVoiceSlice()`. A host gets a microphone by wrapping this in a
   * `<Composer>` root; a host that wants none does not mount the root.
   */
  /** Chat-level controls in input bar */
  agentName?: string
  /**
   * Display label for the agent chip when it must differ from the raw alias.
   * The chip shows this; everything else keyed on the agent (the skills query,
   * the `agent` prop, the switch title) keeps using `agentName`, the real
   * alias. It carries the inherited-default marker (`kirocrew · default`) so an
   * agent-less slot that resolves to the current default is distinguishable
   * from one explicitly pinned to that same alias (#8770). Falls back to
   * `agentName` when unset. */
  agentLabel?: string
  /**
   * True when the agent chip shows an INHERITED default (agent-less slot
   * resolving to the current default), not an explicit pin. Drives an
   * explanatory tooltip on the chip -- reachable on hover (`title`) and on
   * keyboard focus / screen readers (`aria-label`) -- because the ` . default`
   * marker alone reads as opaque (#8770 UX review). Only the inherited case
   * gets it; a pinned chip has nothing to explain. */
  agentIsInheritedDefault?: boolean
  agentSource?: string
  modelName?: string
  /**
   * True when `modelName` is the model an INHERITING slot actually runs on (the
   * backend's served default), not a pin. The chip then carries the same
   * ` · default` marker and explanatory tooltip the agent chip uses for its
   * inherited case, so a served model does not read as something the user
   * chose. A pinned chip has nothing to explain. Yields to
   * `modelIsJevRouted` below, which describes the same unpinned slot more
   * specifically. */
  modelIsInheritedDefault?: boolean
  /**
   * True when `modelName` was picked FOR the user -- an Auto router's choice, or
   * a withheld pin's fallback -- and is not the Settings default. The chip then
   * carries an ` · auto` marker, so it never reads as a pin or as the default.
   * Yields to `modelIsJevRouted` and `modelIsInheritedDefault`. */
  modelIsAutoChosen?: boolean
  /**
   * True when THIS turn's model is Jev's to pick: the slot names no model
   * (`auto`, or the empty string a freshly dispatched slot carries) and the Jev
   * preview is on, so `model.route` puts the turn in a tier and runs it on that
   * tier's model.
   *
   * The chip then names the POLICY (`Auto (Jev)`) in place of `modelName`, rather
   * than an id with a marker beside it. A routed session's model changes from turn
   * to turn, so naming one makes a chip that reads like a pin and is stale by the
   * next reply; the model a given turn actually ran on is on that turn's routing
   * receipt, which is per-turn and cannot go stale. It is also the exact label the
   * picker highlights for this slot (`jevRouteShownModel`), so the chip and the
   * open menu say the same word for the same choice.
   *
   * Hosts compute it from the SAME `jevRouteOffered()` the picker's row is drawn
   * from (`lib/jevRoute.ts`) against the slot's raw `model`, which is what the
   * routing gate reads. One condition, so the chip cannot say Auto for a turn that
   * routed, nor Auto (Jev) for one that did not.
   *
   * Wins over `modelIsInheritedDefault`: both describe a slot that pinned nothing,
   * and this one names WHO picks instead, which is the more specific fact and the
   * one that costs money. */
  modelIsJevRouted?: boolean
  /**
   * Picker openers (agent, model, project, and `onSessionControlClick` below).
   * Each hands the host the chip's click-time rect AND the chip element itself:
   * the host owns the picker's portal and must keep it glued to the chip while
   * it is open (the composer moves under an open menu when the mobile keyboard
   * closes, the composer grows, or a container scrolls), which needs a live
   * element to re-read, not a one-time snapshot (#10616). Hosts feed both into
   * `useAnchoredTriggerRect`.
   */
  onAgentClick?: (rect: DOMRect, trigger?: HTMLElement) => void
  /** `composerHadFocus` is whether the message editor held focus when the chip
   *  was pressed, read before the press moved focus onto the chip. The picker
   *  uses it to hand focus back to the editor after a pick, and only then: a
   *  user who was not typing does not get the composer focused under them. */
  onModelClick?: (rect: DOMRect, trigger?: HTMLElement, composerHadFocus?: boolean) => void
  onProjectClick?: (rect: DOMRect, trigger?: HTMLElement) => void
  /** App-contributed session controls (contributes.sessionControls in app.json). */
  sessionControls?: {
    key: string
    label: string
    icon?: string
    /** True while this control's popover is open. */
    active?: boolean
    /**
     * App-reported per-session state. `ok` tints the chip with --ok so a
     * configured control is visible without opening it; `warn` uses --warn.
     * Absent for apps that declare no status route — the original appearance.
     */
    state?: 'ok' | 'warn' | 'none'
    /** Replaces the tooltip when the app explains its state. */
    statusTooltip?: string
  }[]
  onSessionControlClick?: (key: string, rect: DOMRect, trigger?: HTMLElement) => void
  contextPct?: number
  contextUsedTokens?: number
  contextWindowTokens?: number
  showContextPct?: boolean
  /** Show used/window token counts in the inline context readout. */
  showContextTokens?: boolean
  isRunning?: boolean
  onStop?: () => void
  /**
   * True when an EMPTY composer can hand the thread back to the agent, so the
   * dead send button becomes a Continue control instead. Offered on any idle
   * slot with a conversation — a force-quit leaves no trace of the turn it
   * killed, so restricting this to visibly-broken transcripts would miss exactly
   * the case that needs it most.
   */
  continuable?: boolean
  /**
   * True when the transcript SHOWS the last turn ending badly (unanswered user
   * row, or a trailing error). Picks between "the last turn was interrupted" and
   * the neutral "keep going" wording, so the button never asserts a breakage it
   * cannot see. NOT copy-only any more: `ChatPage` composes this into the
   * `continuable` it passes, so on the dashboard it also decides whether the
   * control appears at all. A caller may still pass `continuable` alone — the
   * component keeps working, it just gets the neutral wording.
   */
  continueIsRecovery?: boolean
  onContinue?: () => void
  /** True while a continue request is in flight. */
  continuing?: boolean
  isQueued?: boolean
  stopState?: 'idle' | 'soft_pending' | 'killing'
  /** An automatic context compaction is running on this session. With no turn
   *  running and nothing typed, the send slot shows a non-destructive
   *  "compacting" state instead of Send: a Stop here would cancel the
   *  compaction, not a turn, and the backend declines it. */
  compacting?: boolean
  /** A cooperative Stop was declined moments ago because the session was
   *  compacting; the next press is the force stop and the armed Stop says so. */
  stopDeclined?: boolean
  approvalMode?: string
  reasoningEffort?: string
  /** True when `reasoningEffort` is the configured default rather than a
   *  per-slot pick. Only the chip's hover / accessible name says so: outside
   *  the picker the two states otherwise read identically. */
  effortIsDefault?: boolean
  /** True when the session's model takes a reasoning-effort level. The chip
   *  then names the level in force beside the model; the slider that CHANGES
   *  it lives inside the model picker the chip opens -- model + effort are one
   *  control, never a second composer button (docs/decisions/2026-06-14). */
  hasEffort?: boolean
  providerId?: string
  /** Invoked when an @-mention picks a file or directory. `kind` defaults to
   *  'file'. `token` is the exact composer text the pick inserted (e.g.
   *  "@src/pages/"), computed against the picker's search root — the staging
   *  side records it so a later chip-remove can strip precisely this token. */
  onFileSelect?: (path: string, kind?: FileKind, token?: string) => void
  /** A Files-panel tree row dropped on the composer: the host's "Add to
   *  chat" handler (absolute path, entry kind), which inserts and stages the
   *  same mention the row's context menu does. `at` is the text offset under
   *  the drop point, or null to use the caret. Absent: tree rows are not
   *  accepted. */
  onTreeEntryDrop?: (absPath: string, kind: FileKind, at?: number | null) => void
  /** The host's clamp for a drop offset (out of mentions and pasted chips),
   *  so the drop caret previews where `onTreeEntryDrop` will insert. */
  clampDropOffset?: (text: string, at: number) => number
  onFileOpen?: (path: string) => void
  project?: string
  /** Checked-out branch of the active project (or short SHA when detached). */
  projectBranch?: string
  /** True when the project's HEAD is detached, so the label is a commit. */
  projectDetached?: boolean
  memoryMode?: string
  /** User-sent messages for ↑/↓ history navigation (oldest → newest). */
  sentMessages?: PromptHistoryItem[]
  /** ⌘↑ (macOS) / Ctrl+↑ elsewhere — edit the last user message.
   *  Claimed by the composer, not the global shortcut handler; fires only from
   *  an EMPTY composer so it never shadows multi-line caret movement or the
   *  ↑/↓ history recall. */
  onEditLastRequest?: () => void
  /** Authoritative automation record for this slot (if any). */
  onAutomationClick?: (open: boolean) => void
  automation?: AutomationRecord | null
  automationOpen?: boolean
  onAutomationChange?: (automation: AutomationRecord | null) => void
  automationCreationReady?: boolean
  automationSnapshotFailed?: boolean
  /** Session routing mode; crew/member cannot host direct monitor turns. */
  sessionMode?: string
  /** Send-key mode. Omitted means the user's stored Settings -> Chat ->
   *  Composer preference; pass it only to override that (e.g. mobile). */
  sendOnEnter?: SendMode
  /** Follow-up options from assistant message */
  followUpOptions?: string[]
  /** Options the user has picked (visual highlight in FollowUpBar) */
  followUpPicked?: Set<string>
  /** Select a follow-up option — handler toggles text in input (see ChatPage wiring).
   *  Third arg is `followUpSourceKey` as it was when the chip was CLICKED (the
   *  chip debounces, and the row can advance inside that window); `undefined`
   *  when no `followUpSourceKey` is supplied. */
  onFollowUpSelect?: (option: string, event: React.MouseEvent, sourceKeyAtClick?: string | null) => void
  /** Immediate send (double-click / Send-now). Second arg is the click-time
   *  row identity FollowUpBar already snapshots for onSelect. */
  onFollowUpSend?: (text?: string, sourceKeyAtClick?: string | null) => void
  /** Quick Send enabled — clicking sends immediately */
  quickSend?: boolean
  /** Layout mode for the follow-up bar: 'multiline' (default) or 'scroll' (original single-line). */
  followUpLayout?: 'multiline' | 'scroll'
  /** Identity of the transcript row the follow-up options were derived from.
   *  Forwarded to FollowUpBar so a chip click carries the row it acted on. */
  followUpSourceKey?: string | null
  /** Labels whose follow-up dispatch is outstanding. Only a host that actually
   *  dispatches a chip passes this. */
  followUpPendingOptions?: ReadonlySet<string> | null
  /** Labels whose click the dispatch would refuse; this is what dims. */
  followUpRefusedOptions?: ReadonlySet<string> | null
  /** Detail of the last failed chip dispatch, or null when none failed. */
  followUpError?: string | null
  /** Collapsed paste blocks backing `⌜🗒 Pasted …⌟` tokens in `value`. */
  pasteBlocks?: PasteBlock[]
  /** Replace the current list of paste blocks (add/remove). */
  onPasteBlocksChange?: (next: PasteBlock[]) => void
  /** Leave a long paste as full editable text instead of collapsing it into a
   *  `[ Paste #N · M lines ]` chip. Defaults false — the chip is the established
   *  behaviour, and it is what keeps a very large paste off the main thread.
   *  Cmd/Ctrl+Shift+V still forces one raw paste when this is off. */
  showFullPastes?: boolean
  /** Opt into the first Lexical composer migration slice. Defaults off so the
   *  established textarea path remains the production fallback until parity is complete. */
  lexicalComposer?: boolean
  /** Optional knowledge chip rendered above the input */
  knowledgeChip?: React.ReactNode
  /** When this key changes, focus the textarea (e.g. on chat session switch). */
  /** Focus-on-switch key. Any new consumer of this prop must honor the
   *  composerFocus one-shot (consumeComposerRelease) or macOS keyboard
   *  switches will autofocus through the release — see composerFocus.ts. */
  autoFocusKey?: string | null
  /**
   * Accessible name for the textarea. Defaults to the main chat's "Message
   * input". A host mounting a SECOND composer on the same screen (the side
   * panel) must pass a distinct name, or a screen-reader user tabbing between
   * the two hears the same announcement for both.
   */
  inputAriaLabel?: string
  /**
   * The typed '/' command and '$' skill triggers, their pickers, and their
   * rows in the plus menu. Defaults on. A host whose sends bypass command
   * handling (the side panel's isolated Q&A turns treat text literally)
   * turns this off so the menus cannot offer commands that would be sent as
   * plain text.
   */
  typedCommandMenus?: boolean
  /**
   * The slot's approval chrome (tool-approval bar, spawn-approval banner).
   * Defaults on. These are store-driven for the composer's slot, so a second
   * composer on the SAME slot (the side panel) must opt out or the main
   * turn's approvals render twice on one screen.
   */
  slotApprovalChrome?: boolean
  /**
   * The prompt-optimizer button. Defaults on. Its slot-mismatch completion
   * path routes a late result through `onOptimizeResult`; a host whose
   * displayed slot can change mid-optimize and that supplies no such route
   * (the side panel) must opt out, or an optimize finished after a session
   * switch silently discards the draft it produced.
   */
  promptOptimizer?: boolean
  /**
   * The user-driven collapse: the "put the message box away while I read" entry
   * point, the bar that replaces it, and the persisted preference.
   *
   * Defaults OFF, which is the opposite of its siblings above, and the default
   * is the feature's central invariant rather than caution. The preference is
   * one `localStorage` key and the expand request is one window-level event, so
   * both address "the composer" in the singular -- correct only while exactly
   * one composer can be collapsed. `composerFocus.ts`'s own header records that
   * the split view breaks the one-composer assumption (each `ChatPane` mounts
   * its own), so a default-on flag would mean: collapse the main composer, and
   * every pane and side-chat composer mounted afterwards reads the same key and
   * comes up collapsed; then one typing intent anywhere broadcasts the expand
   * and every listener answers it, silently undoing a preference the user set
   * per pane. Review found that chain. Opting IN keeps the singular true by
   * construction -- ChatPage's single main composer is the only caller -- so the
   * shared key and the broadcast are correct rather than lucky.
   *
   * Making the split view collapsible therefore is NOT a matter of passing this
   * flag: it needs the key scoped per surface and the event targeted at the
   * pane the intent resolved to. Left as a follow-up, deliberately.
   */
  collapsible?: boolean
  /** Gateway WebSocket connection state. When false, send is blocked and a
   *  warning banner appears above the input. Defaults to true so callers that
   *  don't track connectivity (e.g. tests, embedded previews) keep working. */
  connected?: boolean
  /** Deliver an optimize result to the session that initiated it when that
   *  session is no longer the one displayed in this ChatInput (the user
   *  navigated away mid-optimize). The parent routes `optimized` into
   *  `slotId`'s draft so the result is never written to the wrong session and
   *  never silently lost. When the originating session is still on screen,
   *  ChatInput writes the result itself (undoable) and does NOT call this. */
  onOptimizeResult?: (slotId: string | null, optimized: string) => void
}
