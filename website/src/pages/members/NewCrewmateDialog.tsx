/**
 * "New crewmate" — the create dialog the Crewmates page opens from its header
 * "+" and its empty-state hero.
 *
 * A crewmate IS a crew record, so this posts to the same `POST /api/agents`
 * the crew manager's create form uses; the two stay one write path with two
 * front doors.
 *
 * One card, top to bottom: the look as its hero (a centred ghost tile that
 * re-rolls on a press), the name, the hint, Create, and a centred
 * "Advanced settings" toggle under it, so the primary action follows the name
 * directly. The way out is the header's X: the modal's own, or, in place, an
 * X beside the card's heading. Create builds the crewmate from the built-in `kirocrew` agent
 * unless told otherwise, and the template is not named until the user asks
 * for more. The "Advanced settings" toggle unfolds the rest of the form
 * inline, in the same card, directly under that line, and the unfolded
 * region ends with a second Create: what the crewmate is built from, what it
 * looks after in plain words,
 * and everything the crew manager's form also asks (workspace, model, routing
 * triggers, session colour), rendered by the SAME `Field` frame and field
 * components the editor mounts, so the two forms cannot drift. The disclosure
 * starts folded (`startExpanded` opens it, for the doors that ask for every
 * setting up front: the "+" menu's Advanced row and the crew manager), and
 * folding it hides those fields without clearing them: what was typed or
 * picked there is still sent.
 *
 * "Built from" lists the installed kiro agents (the templates a crew can
 * boot), never the configured default CREW: a crew named `default` is an
 * alias, and storing its name as `kiro_agent` would make the new crewmate run
 * a fallback instead of that crew's template. The built-in `kirocrew` agent
 * leads the list and is labelled as the default, and is offered even when the
 * installed read failed.
 *
 * "What it looks after" is stored as the crew record's `description`: the
 * one free-text field the record already carries for a human-readable
 * account of the crew, and the line the crewmate's first greeting is seeded
 * from (see MembersPage). Memory is provisioned by the server on create
 * (a private store per crewmate, never a choice here). The look is a
 * name-seeded ghost the user can re-roll; it is pinned on the record so
 * the face shown here is the face the crewmate keeps, and anything richer
 * (a picture, a pack) is edited on the detail page afterwards.
 *
 * `firstGreeting` asks the server to open the new crewmate's first chat with
 * the crewmate asking what it should do (`first_greeting` on the create; the
 * page's greeting request then starts that turn), and the card's hint says so.
 * The crew manager's door does not ask, opens no chat, and shows no hint.
 *
 * `initialDraft` pre-fills an opening with a proposed name and goal (a
 * Captain create link, a guide): the name lands in Name, the goal in "What it
 * looks after", and Advanced settings opens with it so the goal is in view.
 * The draft is a starting point, not a change: an opening left as it was
 * proposed leaves without asking, like a blank one. `guided` marks the door
 * the `crewmate.create` guide walks: its Create carries the guide's anchor,
 * and the create request carries the guide's headers so the gateway can
 * confirm the step from what it actually created.
 *
 * Kept mounted and driven by `open` (Modal's own contract): `Modal` renders
 * nothing while closed, and the form state below is reset on every open so a
 * dismissed draft does not reappear.
 */
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { ChevronDown, RefreshCw, X } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'

import Modal from '../../components/Modal'
import CrewAvatar, { seededTraits } from '../../components/CrewAvatar'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import { InstantTip, useInstantTip } from '../../components/InstantTip'
import { Btn, Input } from '../../components/ui'
import { api } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { useSidePanelLeaveGuard } from '../../components/SidePanelLayout'
// From the side-effect-free module, not `api/client`: test doubles of the
// client mock only `api`, and an `instanceof` against an undefined import
// throws instead of falling through to the generic message.
import { ApiError } from '../../api/apiError'
import { parseErrorCode } from '../../utils/errorReport'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import { captainIdentityRefusal, isOfferableTemplate } from '../../lib/assistantMember'
import { useGuideRequestHeaders, useGuideSaveLifecycle } from '../../guide/GuideContext'
import { GUIDE_ANCHORS } from '../../guide/guideActions'
import {
  Field,
  INHERIT_MODEL,
  ModelField,
  SessionColorField,
  TriggersField,
  WorkspaceField,
  WorkspaceModal,
} from '../KiroCrewAgentsPage'

/** The template a new crewmate is built from unless Advanced settings picks
 *  another: the built-in kiro agent every install ships, which leads the
 *  "Built from" list. The folded card never names it. Kept in step with
 *  `change_card_catalog.DEFAULT_CREWMATE_TEMPLATE`, which builds a crewmate
 *  Captain proposes. */
export const DEFAULT_CREWMATE_TEMPLATE = 'kirocrew'
/** The look seed while the name is still blank, so the preview is never empty. */
const BLANK_LOOK_SEED = 'crewmate'

/** The ghost a look seed draws: the name, plus a re-roll count once re-rolled. */
function lookSeed(name: string, roll: number): string {
  const base = name.trim() || BLANK_LOOK_SEED
  return roll > 0 ? `${base}#${roll}` : base
}

/** The id the Advanced settings row's `aria-controls` points at. */
const ADVANCED_ID = 'crewmate-create-advanced'

/**
 * How the Advanced fields unfold and fold. Opening is sequenced: the region
 * grows first and the fields fade in once there is room for them, so the
 * content below (the hint, the footer) never rides up over fields that are
 * still half visible. Folding overlaps the two: the fields start fading and
 * the height starts closing a moment later, eased out so the gap closes
 * fastest first. Waiting for the fade to finish would leave the card sitting
 * blank before it shrinks. The overlap is safe because the region clips
 * while it moves (`overflow` is `hidden` from the start of either animation
 * and `visible` once open), so a fading field is cut at the region's edge
 * rather than drawn over what follows, and a focus ring is not cut at rest.
 */
const ADVANCED_INITIAL = { height: 0, opacity: 0, overflow: 'hidden' } as const
const ADVANCED_OPEN = {
  height: 'auto',
  opacity: 1,
  transitionEnd: { overflow: 'visible' },
  transition: {
    height: { duration: 0.2, ease: 'easeOut' },
    opacity: { duration: 0.14, delay: 0.12, ease: 'easeOut' },
  },
} as const
const ADVANCED_FOLD = {
  height: 0,
  opacity: 0,
  overflow: 'hidden',
  transition: {
    opacity: { duration: 0.08, ease: 'easeIn' },
    height: { duration: 0.18, delay: 0.03, ease: 'easeOut' },
  },
} as const
/** Reduced motion: a cut both ways. */
const ADVANCED_CUT = { duration: 0 } as const

/**
 * The Advanced settings toggle: a quiet centred text button under Create,
 * the chevron before the label. The fields it unfolds open directly below
 * it, so the chevron turns from pointing down to pointing up while they are
 * open. Under reduced motion the chevron turns without animating.
 */
function AdvancedSettingsToggle({ open, onToggle, label }: {
  open: boolean
  onToggle: () => void
  label: string
}) {
  const reduceMotion = useReducedMotion()
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      aria-controls={ADVANCED_ID}
      className={SUBTLE_TEXT_BUTTON}
      data-testid="crewmate-create-advanced-toggle"
    >
      <motion.span
        className="flex shrink-0"
        aria-hidden="true"
        initial={false}
        animate={{ rotate: open ? 180 : 0 }}
        transition={reduceMotion ? { duration: 0 } : { duration: 0.18, ease: 'easeOut' }}
        data-testid="crewmate-create-advanced-chevron"
      >
        <ChevronDown size={15} />
      </motion.span>
      <span className="min-w-0">{label}</span>
    </button>
  )
}

/** The quiet text-button look of the Advanced settings toggle: 44px tall
 *  on a phone (the touch-target floor), compact from sm up. */
const SUBTLE_TEXT_BUTTON =
  'flex min-h-11 items-center gap-1 rounded-md border-none bg-transparent px-2 text-[13px] text-muted cursor-pointer hover:bg-bg-hover hover:text-text focus-ring disabled:cursor-default disabled:opacity-60 sm:min-h-8'

/** How far past the current roll a re-roll looks for a tile colour of its
 *  own before settling for the next roll. The tile palette is small, so a
 *  differing colour is found within a few draws; the cap only bounds the walk. */
const REROLL_SEARCH_LIMIT = 32

/**
 * The roll a re-roll press moves to: the first one after `roll` whose tile
 * colour differs from the one on screen, so every press is visibly a new
 * look and not just a new face on the same colour.
 */
function nextDistinctRoll(name: string, roll: number): number {
  const shown = seededTraits(lookSeed(name, roll)).tile
  for (let next = roll + 1; next <= roll + REROLL_SEARCH_LIMIT; next++) {
    if (seededTraits(lookSeed(name, next)).tile !== shown) return next
  }
  return roll + 1
}

/**
 * The look, as the card's hero: the crewmate's ghost on a 96px tile, and the
 * tile IS the re-roll button. The round re-roll badge on its corner is part of
 * the same button, so the tile and the badge are one tab stop and one name
 * rather than two controls doing the same thing; a press on either draws
 * another look. 96px clears the 44px touch-target floor on its own.
 *
 * The badge carries the shared `InstantTip` hint: pointer hover on the badge
 * shows it, and so does keyboard focus on the tile (only `:focus-visible`, so
 * a mouse press on the tile body leaves no bubble behind). The hint repeats
 * the button's `aria-label`, so it is not wired as its description as well,
 * which would have a screen reader say the same words twice. The touch-replay
 * notes sit on the whole tile, so a tap anywhere on it opens no bubble.
 */
function LookTile({ seed, onReroll, label, disabled }: {
  seed: string
  onReroll: () => void
  label: string
  disabled: boolean
}) {
  const { tip, tipHandlers, tipId } = useInstantTip()
  const { onPointerEnter, onPointerDown, onPointerUp, onMouseEnter, onMouseLeave, onFocus, onBlur } = tipHandlers
  return (
    <>
      <button
        type="button"
        onClick={onReroll}
        disabled={disabled}
        aria-label={label}
        onPointerEnter={onPointerEnter}
        onPointerDown={onPointerDown}
        onPointerUp={onPointerUp}
        onFocus={(e) => { if (e.currentTarget.matches(':focus-visible')) onFocus(e) }}
        onBlur={onBlur}
        className="group relative shrink-0 rounded-[24px] border-none bg-transparent p-0 cursor-pointer focus-ring disabled:cursor-default disabled:opacity-60"
        data-testid="crewmate-create-look"
      >
        <CrewAvatar seed={seed} avatar={{ kind: 'ghost', traits: seededTraits(seed) }} size={96} className="!rounded-[24px]" />
        <span
          aria-hidden="true"
          onMouseEnter={onMouseEnter}
          onMouseLeave={onMouseLeave}
          className="absolute -bottom-1.5 -right-1.5 flex size-7 items-center justify-center rounded-full border border-border-strong bg-bg text-text shadow-md transition-colors group-hover:bg-bg-hover group-focus-visible:ring-2 group-focus-visible:ring-[var(--ring)] group-focus-visible:ring-offset-1 group-focus-visible:ring-offset-[var(--bg)]"
          data-testid="crewmate-create-look-reroll"
        >
          <RefreshCw size={16} />
        </span>
      </button>
      <InstantTip tip={tip} tipId={tipId} className="w-max max-w-[calc(100vw-1rem)] whitespace-nowrap text-text">
        {label}
      </InstantTip>
    </>
  )
}

/**
 * The form's DOM id. Every control, both Create buttons included, lives
 * inside the form, so each Create is a submit button by nesting, and a form
 * with a submit button is what gives Enter in the Name field its implicit
 * submission (with two text fields and no submit button, Enter does nothing).
 */
const FORM_ID = 'crewmate-create-form'

/** Longest the create waits for the registry/config caches to re-read before
 *  handing over anyway (see `createMut.onSuccess`). */
export const CACHE_WARM_BOUND_MS = 2500
/** Longest a create with no server answer waits for the roster read that
 *  reconciles it (see `createMut.onError`). Past it the roster counts as
 *  unreadable — the same `null` an errored read yields — so the dialog says
 *  "unconfirmed" and unlocks instead of sitting on "Creating…". */
export const RECONCILE_BOUND_MS = 2500

/** What the page needs to open the new crewmate's chat and seed its greeting. */
export interface CreatedCrewmate {
  /** Exact crew name — MembersPage's `?member=` resolves by name. */
  name: string
  /** The "what it looks after" line as typed; '' when left blank. */
  job: string
}

/** The `POST /api/agents` body — the crew manager's create payload plus the
 *  record's `description`, which carries "what it looks after". `model` is
 *  sent only when pinned: the inherit spelling is the server's default. */
interface CreateBody {
  name: string
  kiro_agent: string
  workspace: string
  memory_store: string
  description: string
  triggers: string
  session_color: string
  model?: string
  avatar?: { kind: 'ghost'; traits: ReturnType<typeof seededTraits> }
  first_greeting?: true
}

/** A proposed name and goal an opening starts from. */
export interface CrewmateDraft {
  name: string
  goal: string
}

export default function NewCrewmateDialog({
  open, onClose, onCreated, existingNames, embedded = false, startExpanded = false, firstGreeting = false,
  initialDraft, guided = false, onDraftStateChange,
}: {
  open: boolean
  /** Pre-fills the opening (see the header comment). A new object re-fills an
   *  opening already on screen; the host replaces it only once leaving the
   *  current draft was agreed. */
  initialDraft?: CrewmateDraft
  /** This is the door the `crewmate.create` guide walks (see the header comment). */
  guided?: boolean
  /** Whether the user changed the opening (`edited`) and whether a create is
   *  in flight (`busy`), for a host that steps a pristine card aside. */
  onDraftStateChange?: (state: { edited: boolean; busy: boolean }) => void
  /** Every open starts with Advanced settings unfolded: the doors that ask
   *  for every setting up front. Folded otherwise. */
  startExpanded?: boolean
  /** Ask the server to open the new crewmate's first chat with its goal
   *  question (see the header comment). */
  firstGreeting?: boolean
  /**
   * Render the same complete form in place, inside the page region the host
   * gives it, instead of as a modal: no portal, no scrim, no focus trap and no
   * Escape/backdrop dismissal (the X beside the heading is the way out). State, validation, the
   * create write, reset-on-open and the leave guard are identical.
   */
  embedded?: boolean
  onClose: () => void
  /** Fired once the server has the record; the page takes it from there. */
  onCreated: (created: CreatedCrewmate) => void
  /**
   * The roster's names as the page last read them. A name already here is
   * refused before any request (the server would answer `agent_exists`), and
   * it is what makes the post-failure reconcile below sound: a row found
   * AFTER a dropped request proves this request committed only if the name
   * was absent BEFORE it.
   */
  existingNames: readonly string[]
}) {
  const { t } = useTranslation()
  const reduceMotion = useReducedMotion()

  const [name, setName] = useState('')
  // The look: how many times it was re-rolled (0 = the name's own ghost).
  const [lookRoll, setLookRoll] = useState(0)
  const [builtFrom, setBuiltFrom] = useState('')
  const [job, setJob] = useState('')
  const [advanced, setAdvanced] = useState(startExpanded)
  const [workspace, setWorkspace] = useState('default')
  const [pendingWorkspace, setPendingWorkspace] = useState<string | null>(null)
  const [model, setModel] = useState(INHERIT_MODEL)
  const [triggers, setTriggers] = useState('')
  const [sessionColor, setSessionColor] = useState('')
  // The nested New workspace dialog: whether it is open, and the GENERATION
  // of that opening. `WorkspaceForm`'s create is an awaited POST whose
  // continuation calls `onCreated` when the answer lands — after Radix has
  // already unmounted the form if the user pressed Cancel / X / Escape in the
  // meantime, or after this dialog was reset for a new crewmate. Without a
  // guard that late answer would still write the workspaces cache, park the
  // name in `pendingWorkspace` and switch a draft the user never asked to
  // change (a NEW draft, even, if the parent had been reopened). So every
  // opening gets a generation; the `onCreated` handed to that opening carries
  // it; and every close path — nested Cancel / X / Escape / backdrop, the
  // parent's reset-on-open, the parent closing, unmount — bumps the live
  // counter synchronously. A completion whose generation is no longer the
  // live one is dropped whole: no cache write, no pending pick, no draft
  // change. A completion from the still-open generation behaves as before.
  const [wsModal, setWsModal] = useState<{ open: boolean; gen: number }>({ open: false, gen: 0 })
  const wsModalOpen = wsModal.open
  const wsGen = useRef(0)
  const openWsModal = () => {
    wsGen.current += 1
    setWsModal({ open: true, gen: wsGen.current })
  }
  // Invalidates first, then closes: the bump is synchronous so a POST that
  // resolves in the same tick as the close still misses.
  const closeWsModal = useCallback(() => {
    wsGen.current += 1
    setWsModal((prev) => (prev.open ? { open: false, gen: prev.gen } : prev))
  }, [])
  // The nested New workspace form's unsaved input. Counted into this dialog's
  // navigation stake: a route change unmounts both, and the workspace draft is
  // as lost as the crewmate's. `WorkspaceModal` stays mounted across closes
  // (see its note on Radix layers), so this is read live, not on open.
  const [wsDirty, setWsDirty] = useState(false)
  const queryClient = useQueryClient()
  // Client-side validation ("name is required") is kept apart from a request
  // that FAILED: a blank name never left the browser, so it is not an error.
  const [hint, setHint] = useState('')
  // The server's own "already exists" (a 409 the roster did not predict):
  // said through the request-error notice like every server answer, AND
  // marked on the Name field, since the field is what the answer is about.
  const [nameRefused, setNameRefused] = useState(false)
  const [error, setError] = useState('')
  // The one failure that is NOT "nothing was created": the request got no
  // answer and the roster could not be read to reconcile it. It is rendered
  // through the same notice as every other failure, but under its own lead
  // and test id, because its next step differs — check the list, do not
  // simply resubmit — and a notice that reads like the retry-safe ones
  // invites the second POST that creates a namesake.
  const [unconfirmed, setUnconfirmed] = useState(false)

  // Every open starts blank, or from the proposal it was opened with: a
  // dismissed draft must not come back.
  useEffect(() => {
    if (!open) return
    const draftName = initialDraft?.name ?? ''
    const draftGoal = initialDraft?.goal ?? ''
    setLookRoll(0)
    setName(draftName); setBuiltFrom(''); setJob(draftGoal); setAdvanced(startExpanded || draftGoal.trim() !== '')
    setWorkspace('default'); setModel(INHERIT_MODEL); setTriggers(''); setSessionColor('')
    setHint(''); setError(''); setNameRefused(false); setUnconfirmed(false); setPendingWorkspace(null)
    // The nested workspace form too: a draft left in it belongs to the
    // dismissed open, and `atStake` must not count it against the next one.
    // Its generation was already retired when `open` dropped (the layout
    // effect below); this close is for the state, which the retire left alone.
    closeWsModal(); setWsDirty(false)
    // The draft is read by identity: a new proposal object re-fills an
    // opening already on screen, even one carrying the same text.
  }, [open, closeWsModal, startExpanded, initialDraft])
  // Closing the parent retires the nested generation too. The parent cannot
  // be dismissed while the nested dialog is open (see `dismissDisabled`), but
  // `open` can still drop with a create in flight — the page flips it from
  // outside, or unmounts the dialog on a route change the user confirmed —
  // and the reset above only runs on the NEXT open, which may never come.
  // A LAYOUT effect, not a passive one: a passive effect runs after paint,
  // and a POST that settles in the gap between the commit that closed the
  // dialog and that effect would still find the live generation and land on
  // a closed draft. The layout body runs synchronously inside the commit, so
  // by the time any continuation can run the generation is already retired.
  // Not a render-time ref write either: a render may be discarded or replayed
  // under concurrent rendering, and a bump that never committed would retire
  // a generation that is still open. The cleanup does the same on unmount,
  // and on every `open` transition (the retire is idempotent: `openWsModal`
  // is the only path that mints a generation, and it runs from a click).
  useLayoutEffect(() => {
    if (!open) closeWsModal()
    return () => { wsGen.current += 1 }
  }, [open, closeWsModal])

  // Option lists come from the same reads the crew editor uses, fetched only
  // while the dialog is open. Each falls back to its built-in default when
  // the read fails, and the failure is SAID: one notice, first failure wins,
  // so a shortened list never passes for the whole set of choices.
  // The same catalog the chat picker reads (`GET /api/agents/catalog`): its
  // template rows already exclude the runtime's background-only spec, fork
  // copies and masked names, so the dialog does not keep a second copy of
  // that rule. No session key: this page has no chat slot, so the catalog is
  // the global one — a crewmate is a global record and must not be built
  // from a template only one project checkout can resolve.
  // Re-read on every open. The app's queries never go stale on their own
  // (queryClient.ts: freshness is WebSocket-driven), but no server event
  // invalidates this key, so under the default a template installed or
  // removed mid-session would stay frozen in the list from the first open
  // until a reload. `staleTime: 0` makes each `enabled` flip (each open)
  // fetch again: the list a user sees is the catalog as of opening the dialog.
  const { data: catalog, error: installedError } = useQuery({
    queryKey: ['agents-catalog', 'global'],
    queryFn: () => api.agentCatalog(),
    enabled: open,
    staleTime: 0,
  })
  const { data: workspacesData, refetch: refetchWorkspaces, error: workspacesError } = useQuery({
    queryKey: ['workspaces'],
    queryFn: () => api.workspaces(),
    enabled: open,
  })
  const { data: availableModels, error: modelsError } = useAvailableModelsQuery({ enabled: open })
  const optionsError = installedError ?? workspacesError ?? modelsError

  // Installed kiro agents only (see the header comment). Private fork copies
  // (one crew's own definition) are not offered — a copy named after crew A
  // means nothing in crew B's list. The built-in agent leads, labelled as the
  // default; it is offered even when the installed read failed, because it
  // ships with every install.
  const installed = Array.isArray(catalog?.agents)
    ? catalog.agents
      .filter((row) => row.selection_kind === 'template' && Boolean(row.name))
      .map((row) => row.name)
      .filter((n: string) => n !== DEFAULT_CREWMATE_TEMPLATE && isOfferableTemplate(n))
    : []
  const builtFromOptions = [DEFAULT_CREWMATE_TEMPLATE, ...installed]
  const builtFromLabels = builtFromOptions.map((n) =>
    n === DEFAULT_CREWMATE_TEMPLATE ? t('pages.membersPage.built_from_default', { agent: n }) : n,
  )
  const builtFromValue = builtFrom || DEFAULT_CREWMATE_TEMPLATE
  const workspaceOptions = useMemo(
    () => workspacesData?.workspaces?.map((w: { name: string }) => w.name) || ['default'],
    [workspacesData],
  )
  // A workspace created from Advanced is picked only once its option is on
  // the list: Radix's hidden form <select> gathers its options a commit after
  // the items mount, so writing the value in the same commit as the new option
  // reads back as '' and clears the field. Reset-on-open clears the pending
  // pick, so a dismissed-and-reopened dialog never receives it.
  useEffect(() => {
    if (pendingWorkspace && workspaceOptions.includes(pendingWorkspace)) {
      setWorkspace(pendingWorkspace)
      setPendingWorkspace(null)
    }
  }, [pendingWorkspace, workspaceOptions])
  const modelOptions = [
    INHERIT_MODEL,
    ...(availableModels || []).map((m) => m.name).filter((n) => n && n !== INHERIT_MODEL),
  ]

  // Every editable value counts as a draft, not only the two text fields: a
  // template or an Advanced pick is as lost on an accidental dismissal as a
  // typed name, and the reset-on-open above means there is no way back.
  // Measured against what the opening started from: a proposed name and goal
  // left as proposed are still the proposal (it lives on where it came from),
  // not the user's work.
  const dirty = Boolean(
    name !== (initialDraft?.name ?? '') || job !== (initialDraft?.goal ?? '') || builtFrom
    || workspace !== 'default' || model !== INHERIT_MODEL || triggers || sessionColor
    || lookRoll > 0,
  )

  // The mutation callbacks below outlive the dialog. A route change the user
  // confirmed through `create_leave_busy` unmounts the page, but React Query
  // still runs `onSuccess` when the POST resolves, and `onCreated` →
  // `openMember` → `setSearchParams` would then `navigate` the page they
  // chose back to `/members?member=<new name>` — the opposite of what the
  // confirm's own text promised. `useMutation` cancels nothing on unmount,
  // so every post-await continuation checks this ref before touching the
  // page. (The ref, not `open`: the dialog stays mounted while closed.)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  /**
   * Re-read the roster behind EVERY door this dialog is opened from.
   *
   * Two readers, because the two doors do not share a query: the Crewmates page
   * renders `MEMBERS_ROSTER_QUERY_KEY` (`['kirocrew-agents', 'members-roster']`)
   * and the crew manager renders `['kirocrew-agents']` itself. An invalidation
   * matches when the FILTER key is a PREFIX of a query's key, and the roster leaf
   * is one segment LONGER than the registry key — so invalidating the leaf alone
   * never reaches the crew manager's list. Under the app's `staleTime: Infinity`
   * (queryClient.ts) that left the Crews-tab door holding its pre-request
   * snapshot: `existingNames` would keep refusing a name whose create never
   * landed, or miss one that did and let a resubmit leave as a second POST, which
   * is exactly the prediction the reconcile below is built on.
   *
   * `exact` on the registry key, so this stays the two queries that are stale
   * rather than every per-member projection nested under the prefix.
   *
   * `['kirocrewConfig']` goes with the registry key because the SAME write
   * (`POST /api/agents`) lands in both, and the crew manager's list is
   * config-derived (`KiroCrewAgentsPage`'s `['kirocrewConfig']` query). The
   * success path already invalidates both; this reconcile path — reached when a
   * create committed server-side but its response was lost (socket drop /
   * gateway restart mid-POST) or came back 409 `agent_exists` — must refresh the
   * same pair, or the committed crewmate is absent from the config-derived view
   * until a manual reload.
   */
  const refreshRosterReaders = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
    void queryClient.invalidateQueries({ queryKey: ['kirocrew-agents'], exact: true })
    void queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'] })
  }, [queryClient])
  const guideHeaders = useGuideRequestHeaders('crewmate.create')
  const guideSave = useGuideSaveLifecycle('crewmate.create')
  const createMut = useMutation({
    mutationFn: async (body: CreateBody) => {
      // A guided create (the human pressed Start on Captain's guide and is now
      // pressing Create) carries the guide headers on THIS request, so the
      // gateway confirms the step from what it actually created. A progress
      // report still in flight (the guide catching up with a fast click)
      // would leave the headers unread and the save uncredited.
      if (!guided) return { r: await api.createKirocrewAgent(body) as { error?: string; name?: string }, credited: true }
      await guideSave.sync()
      const headers = guideHeaders()
      const r = await (headers ? api.createKirocrewAgent(body, headers) : api.createKirocrewAgent(body)) as { error?: string; name?: string }
      return { r, credited: !!headers }
    },
    onSuccess: async ({ r, credited }, body) => {
      // A 2xx whose body still carries `error` is a refusal in the server's
      // words; like every other failure it is said in the product's.
      if (r?.error) { if (guided) guideSave.refused(); setError(t('pages.membersPage.create_failed')); return }
      // Saved, but without the guide's headers (the guide had not reached its
      // Create step): the guide is closed honestly rather than left asking for
      // the press the user already made.
      if (!credited) guideSave.savedUncredited()
      // What the crew manager's own create form does (`refetchAgents`): the
      // page re-reads the roster leaf itself, but the registry and config
      // caches are held at `staleTime: Infinity`, and `POST /api/agents`
      // pushes no refresh frame. Left as a pre-write snapshot, the header
      // pencil's deep link (`?crew=<name>`) finds no such agent in the crew
      // manager and silently drops the editor. `exact`: the roster leaf under
      // this prefix is re-read by the page in its own order (openCreated), and
      // a second concurrent read here would race that one. Awaited, with the
      // inactive queries refetched too (`refetchType: 'all'`): an invalidated
      // query still serves its old data until the refetch lands, and the crew
      // manager mounting in that window would read the pre-write list. But
      // BOUNDED: the crewmate exists server-side the moment the POST resolved,
      // and every dismissal path is refused while the mutation is pending, so
      // a cache warm-up that stalls (a 429 ladder, a half-open socket) must
      // not hold the dialog on "Creating…" with no exit. Past the bound the
      // refetches keep going in the background and the create proceeds.
      const warm = Promise.all([
        queryClient.invalidateQueries({ queryKey: ['kirocrew-agents'], exact: true, refetchType: 'all' }),
        queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'], refetchType: 'all' }),
        // The shared sessionless catalog key this dialog also reads at
        // `staleTime: 0` on open. Every OTHER reader of it holds it longer: the
        // command bar's crewmates view serves from it at `MATES_STALE_MS`, and
        // no server event invalidates it, so a crewmate created here would be
        // absent from that view for the stale window and from any later reader
        // until a reload. Marking it stale on create is the one freshness rule
        // every reader of this key inherits, in place of each inventing its own.
        // `refetchType: 'none'`: no reader is mounted on it at create time (the
        // dialog's own read is gated on `open`), so an eager refetch here would
        // be a catalog GET for a cache nobody is watching -- the next reader's
        // first fetch after this invalidation refetches because the key is
        // stale, which is the once-per-entry cost each view already pays.
        queryClient.invalidateQueries({ queryKey: ['agents-catalog', 'global'], refetchType: 'none' }),
      ])
      await Promise.race([warm, new Promise<void>((resolve) => setTimeout(resolve, CACHE_WARM_BOUND_MS))])
      if (!mounted.current) return
      // The server keys a free-form name by a derived id (the typed name
      // becomes its display_name), so the crewmate is opened by the id it
      // answered with, not the text the user typed.
      onCreated({ name: r?.name || body.name, job: body.description })
    },
    onError: async (e: Error, body) => {
      // A 4xx made nothing: the guide stops waiting on this save.
      if (guided && (captainIdentityRefusal(e) || (e instanceof ApiError && e.status >= 400 && e.status < 500))) guideSave.refused()
      if (e instanceof ApiError) {
        const code = parseErrorCode(e.body)
        const captainRefusal = captainIdentityRefusal(e)
        if (captainRefusal) {
          setError(captainRefusal)
          setNameRefused(true)
          return
        }
        if (e.status === 409 && code === 'agent_exists') {
          // The server has just proved the roster behind this dialog is
          // stale (the name got past `existingNames`): refresh it, so the row
          // shows and the next attempt is refused here, without a request.
          refreshRosterReaders()
          setError(t('pages.membersPage.create_name_taken', { name: body.name }))
          setNameRefused(true)
          return
        }
        // A name the server refuses on sight (it looks like a credential, or a
        // URL carrying one) is a request the server answered, so — like the 409
        // above — it is said in the ErrorNotice (the repo's one error surface),
        // with its cause, and the Name field is marked as the thing to change.
        // Collapsing it to "Nothing was created — try again" would invite the
        // same name forever with the cause hidden. The server deliberately does
        // not echo such a name, and neither does the notice.
        if (e.status === 400 && code === 'credential_shaped_name') {
          setError(t('pages.membersPage.create_name_credential_shaped'))
          setNameRefused(true)
          return
        }
        // A display name the server cannot store as text (`members.
        // validate_member_name`): hidden or non-canonical characters, a line
        // break or tab, over the length cap, or nothing but periods. Not a
        // grammar this dialog could pre-check — the same-looking name can be
        // fine or not depending on invisible bytes — so the server's verdict
        // is said as the Name field's, with the fix the user can make, and
        // the draft is kept. The server's sentence names the rule in its own
        // terms and may echo part of the name; neither is shown. The copy is
        // a MENU of alternatives ("try another version: … ; … ; or …"), one
        // per rule the server applies, not a checklist: a name that trips
        // one rule is fine on the others, and a checklist would read as
        // "periods are forbidden" to someone whose name merely has one. The
        // first item leads with "retype pasted text": a pasted name can look
        // perfectly ordinary and still carry a non-canonical spelling, and
        // retyping needs no knowledge of which byte was wrong.
        if (e.status === 400 && code === 'invalid_member_name') {
          setError(t('pages.membersPage.create_name_unusable'))
          setNameRefused(true)
          return
        }
        // Verbatim server text is reserved for the codes the dialog knows
        // (above); every other answer — a 5xx, a refused body — is said in the
        // product's words with its next step, never as the server's raw
        // sentence. The server answered, so nothing was created.
        setError(t('pages.membersPage.create_failed'))
        return
      }
      // No server answer at all (a dropped connection, a parse error): the
      // request may still have reached the server and been committed. The
      // dialog reads the roster to say something TRUE, but never claims the
      // create as its own: a row of this name proves only that the name now
      // exists — another tab could have created it in the same window — so
      // there is no request-correlated confirmation to open a chat and seed a
      // greeting on. The row is reported as what a retry would meet (taken),
      // the roster behind the dialog is refreshed so the row shows, and the
      // user picks it from the list; nothing is sent to it. The mutation
      // stays pending until this settles, so the form stays locked meanwhile
      // — which is why the read is BOUNDED like the cache warm-up above: while
      // pending every exit (X, Escape, backdrop, Cancel) is refused, and a
      // roster read that stalls must not hold the dialog on "Creating…" with
      // no way out. Past the bound the roster counts as unreadable.
      const present = await Promise.race([
        api.members()
          .then((r) => r.members.some((m) => m.name === body.name))
          // A rejected read and a body without a roster (the handler above
          // throwing) are both "unreadable": `.then(ok, fail)` would let the
          // handler's own throw escape past `fail` and leave nothing said.
          .catch((): null => null),
        new Promise<null>((resolve) => setTimeout(() => resolve(null), RECONCILE_BOUND_MS)),
      ])
      if (!mounted.current) return
      if (present) {
        refreshRosterReaders()
        setError(t('pages.membersPage.create_name_taken', { name: body.name }))
        setNameRefused(true)
        return
      }
      // `null`: the roster could not be read either, so whether the create
      // landed is unknown — "nothing was created" would be a guess.
      if (present === null) {
        // The roster behind the dialog is asked to re-read now, not on the
        // next attempt: if the create DID land, the row arrives in
        // `existingNames` and a resubmit of the same name is refused up front
        // as taken (no request), instead of reaching the server as a second
        // create that a stale roster could not predict.
        refreshRosterReaders()
        setUnconfirmed(true)
        // The attempted name leads the sentence: "whether the crewmate was
        // created" reads as the retry-safe "couldn't create the crewmate", and
        // the one thing that tells the two states apart on a glance is WHICH
        // crewmate is in doubt — the one just typed, which the list is then
        // checked for.
        setError(t('pages.membersPage.create_unconfirmed', { name: body.name }))
        return
      }
      setError(t('pages.membersPage.create_failed'))
    },
  })
  const busy = createMut.isPending
  // Typed New workspace fields are part of the draft too: a card whose own
  // fields are untouched but whose workspace form holds input is not pristine.
  useLayoutEffect(() => {
    onDraftStateChange?.({ edited: open && (dirty || (wsModalOpen && wsDirty)), busy })
  }, [onDraftStateChange, open, dirty, wsModalOpen, wsDirty, busy])

  // The modal's own guards (`guardAccidentalDismiss`, `dismissDisabled`) cover
  // Escape, the backdrop and the X. A client-side route change — the sidebar,
  // the command palette, the browser's Back — unmounts the whole page and this
  // dialog with it, and none of those see the modal. So the same stake is
  // published to the app shell: a typed draft asks before leaving, and a POST
  // in flight asks too, since leaving loses the answer (the crewmate may be
  // created, but its chat will not open here).
  //
  // ONE registration for both of this dialog's doors, through the side-panel
  // channel. Opened from a pane inside a SidePanelLayout (the crew manager's
  // Crews tab) it fills the pane slot the layout gates its tab switches on, and
  // rides the layout's own forward out to the app shell. Opened standalone (the
  // Crewmates page) there is no pane context, so `alsoGuardAppShell` makes the
  // hook register with the app shell itself. The hook publishes the stake and
  // registers the guard once through this one call — registering with the shell
  // HERE as well would put two entries resolving to this one predicate in the
  // shell's set inside a layout, and `ask()` would raise this confirm twice for
  // a single navigation — the second Cancel vetoing a leave already approved.
  // The shell keeps a SET of guards and stakes, so this form and the page's
  // other guards (an open Profile's drafts) each register their own.
  const atStake = open && (dirty || busy || (wsModalOpen && wsDirty))
  const mayLeave = () => {
    if (!atStake) return true
    return window.confirm(busy ? t('pages.membersPage.create_leave_busy') : embedded ? t('components.meetCrewmatesFlow.leave_draft') : t('pages.membersPage.create_leave_draft'))
  }
  useSidePanelLeaveGuard(mayLeave, atStake, true)
  // A reload or tab close loses the draft too.
  useEffect(() => {
    if (!atStake) return
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      // Legacy browsers only show the prompt when returnValue is set.
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [atStake])
  const titleId = useId()
  const title = t('pages.membersPage.add_member')

  const submit = () => {
    setError(''); setHint(''); setUnconfirmed(false)
    const n = name.trim()
    if (!n) { setHint(t('pages.membersPage.create_name_required')); return }
    // No grammar check on the name beyond the trim: a crewmate's name is
    // free-form display text (`members.validate_member_name` — spaces,
    // punctuation, any script, emoji), separate from the stable member id,
    // slug and slot key the server derives for it and from the strict
    // template identifier `Built from` carries. What the server refuses
    // (a credential-shaped name, hidden characters, an over-long name) it
    // says in its own answer, which `onError` renders; the roster lists
    // every name the server accepts, so nothing created here reads as gone.
    // The roster already has this name: the server would answer 409
    // `agent_exists`, so say that without a request — as a HINT under the
    // field like the other validation refusals (nothing failed; `error` and
    // its ErrorNotice are for requests that did). This is also the premise
    // of the reconcile in `onError`: every request that leaves here carries a
    // name the roster did NOT have.
    if (existingNames.includes(n)) { setHint(t('pages.membersPage.create_name_taken', { name: n })); return }
    // Every value is sent whether Advanced settings is unfolded or not:
    // folding hides those fields, it does not discard what was set in them,
    // and untouched they hold the defaults (the built-in template, the
    // default workspace, the inherited model).
    createMut.mutate({
      name: n,
      kiro_agent: builtFromValue,
      workspace,
      memory_store: 'default',
      description: job.trim(),
      triggers,
      session_color: sessionColor,
      ...(model !== INHERIT_MODEL ? { model } : {}),
      avatar: { kind: 'ghost' as const, traits: seededTraits(lookSeed(n, lookRoll)) },
      ...(firstGreeting ? { first_greeting: true as const } : {}),
    })
  }

  const formEl = (
    <form
      id={FORM_ID}
      className="flex flex-col"
      data-testid="crewmate-create-form"
      onSubmit={(e) => { e.preventDefault(); if (!busy) submit() }}
    >
      {/* One lock for the whole form while the POST is in flight: a
          disabled fieldset disables every control under it -- the look
          tile, both Create buttons and the editor's own fields, which take
          no `disabled` prop of their own -- so an edit cannot land after
          the body was sent and vanish when success closes the dialog. */}
      <fieldset
        disabled={busy || wsModalOpen}
        aria-busy={busy || undefined}
        className="contents min-w-0 m-0 p-0 border-0"
        data-testid="crewmate-create-fieldset"
      >
      {/* One column, top to bottom: the look, the name, the hint, Create,
          the secondary line, then the Advanced fields. Spacing is each
          element's own margin, so the space above the unfolding region is
          its own padding and moves with it. */}
      <div className="flex flex-col">
        {/* The look is the hero: the tile centred, a caption under it that
            says the tile is what re-rolls. No visible label over it. */}
        <div className="flex flex-col items-center gap-2" data-testid="crewmate-create-identity">
          <LookTile
            seed={lookSeed(name, lookRoll)}
            onReroll={() => setLookRoll((r) => nextDistinctRoll(name, r))}
            label={t('pages.membersPage.create_avatar_reroll')}
            disabled={busy}
          />
          <p className="m-0 text-center text-[12px] leading-relaxed text-muted" data-testid="crewmate-create-look-caption">
            {t('pages.membersPage.create_avatar_caption')}
          </p>
        </div>
        {/* Full width under the look. No visible label: the placeholder
            shows what goes here and `aria-label` names it. The row wrapper
            is what `Input`'s own `flex-1` grows along. */}
        <div className="mt-4 flex">
          <Input
            value={name}
            onChange={(e) => { setName(e.target.value); setHint(''); setError(''); setNameRefused(false); setUnconfirmed(false) }}
            aria-label={t('pages.membersPage.create_name')}
            aria-invalid={hint || nameRefused ? true : undefined}
            aria-describedby={hint ? 'crewmate-create-name-hint' : undefined}
            placeholder={t('pages.membersPage.create_name_placeholder')}
            // The variant carries an attribute selector, so it outranks the
            // base `border-border` whatever order the stylesheet emits them in.
            className="aria-invalid:border-danger"
            autoFocus
            disabled={busy}
          />
        </div>
        {/* A refusal, not a field hint: it reads in the error tone and the
            field's border goes with it, so a blank submit never looks like
            "the form before I typed anything". */}
        {hint && (
          <span id="crewmate-create-name-hint" role="alert" className="mt-1.5 text-[11.5px] leading-relaxed text-danger" data-testid="crewmate-create-name-hint">
            {hint}
          </span>
        )}
        {/* Only where it is true: the door that asks for a first greeting. */}
        {firstGreeting && (
          <p className="m-0 mt-3 text-[12px] leading-relaxed text-muted" data-testid="crewmate-create-quick-hint">
            {t(job.trim() ? 'pages.membersPage.create_quick_hint_with_job' : 'pages.membersPage.create_quick_hint')}
          </p>
        )}
        {/* No hand-off on either notice: both sit over this unsaved form --
            the name, job and every Advanced pick live only in local state --
            and the hand-off navigates to the chat, unmounting the dialog
            and the draft with it. Both sit right above Create, the press
            they answer. */}
        {/* A load that did not happen is a failure (errors-use-error-notice):
            the shared notice, inline, naming WHICH list fell back so the
            user knows what they are not being offered. The create still
            works with the defaults. */}
        {optionsError && !error && (
          <div className="mt-4">
            <ErrorNotice
              message={t('pages.membersPage.create_options_failed', {
                list: installedError
                  ? t('pages.membersPage.agent_template')
                  : workspacesError
                    ? t('pages.kiroCrewAgentsPage.workspace_2')
                    : t('pages.kiroCrewAgentsPage.model'),
              })}
              variant="inline"
              testId="crewmate-create-options-error"
            />
          </div>
        )}
        {/* An UNCONFIRMED create is not a failure like the others: its lead
            says so in bold and it carries its own test id, so it can never
            be read -- by a user or a test -- as one of the "nothing was
            created, try again" notices. */}
        {error && (
          <div className="mt-4">
            <ErrorNotice
              message={error}
              title={unconfirmed ? t('pages.membersPage.create_unconfirmed_title') : undefined}
              // With a title, the block notice would leave the message as a
              // bare text node beside the <strong> lead, so the exact message
              // is no longer addressable on its own (an exact text lookup sees
              // "Not confirmed Couldn't confirm..." as one element). `inline` is
              // a span's default display, so the wrap it buys changes nothing
              // on screen; it only gives the sentence its own element.
              messageClassName={unconfirmed ? 'inline' : undefined}
              testId={unconfirmed ? 'crewmate-create-unconfirmed' : 'crewmate-create-error'}
            />
          </div>
        )}
        {/* The primary action, right after the name: full width and 44px
            tall on a phone. It is the form's first submit button, so Enter
            in the Name field submits through it. */}
        <Btn primary type="submit" disabled={busy || wsModalOpen} className="mt-5 min-h-11 w-full justify-center sm:min-h-9" data-testid="crewmate-create-submit" data-guide-anchor={guided ? GUIDE_ANCHORS.crewmateCreate : undefined}>
          {busy ? t('pages.membersPage.create_submitting') : t('pages.membersPage.create_submit')}
        </Btn>
        {/* The secondary line, centred under Create: the Advanced settings
            toggle alone. The way out is the header's close button (the
            modal's own X, or the in-place card's X beside its heading). */}
        <div className="mt-2 flex items-center justify-center" data-testid="crewmate-create-secondary">
          <AdvancedSettingsToggle
            open={advanced}
            onToggle={() => setAdvanced((v) => !v)}
            label={t('pages.membersPage.create_advanced_settings')}
          />
        </div>
        {/* The fields grow out of the card directly under the toggle
            instead of appearing whole, so the reader sees where they came
            from (sequenced as `ADVANCED_OPEN` / `ADVANCED_FOLD` describe).
            Cut, not animated, under reduced motion. Folding unmounts the
            fields, never their values: those live in this dialog's state.
            The region ends with a second Create, so a user who scrolled
            down through the fields does not scroll back up to finish. It
            is a plain submit button of the same form: each press is one
            submit event through the one `onSubmit`, with the same lock. */}
        <AnimatePresence initial={false}>
          {advanced && (
            <motion.div
              key="advanced"
              id={ADVANCED_ID}
              initial={reduceMotion ? false : ADVANCED_INITIAL}
              animate={reduceMotion ? { ...ADVANCED_OPEN, transition: ADVANCED_CUT } : ADVANCED_OPEN}
              exit={reduceMotion ? { ...ADVANCED_FOLD, transition: ADVANCED_CUT } : ADVANCED_FOLD}
              data-testid="crewmate-create-advanced"
            >
              <div className="flex flex-col gap-5 pt-5">
                <Field label={t('pages.membersPage.agent_template')} hint={t('pages.membersPage.built_from_hint', { agent: DEFAULT_CREWMATE_TEMPLATE })}>
                  <SimpleSelect
                    options={builtFromOptions}
                    optionLabels={builtFromLabels}
                    value={builtFromValue}
                    onChange={setBuiltFrom}
                    disabled={busy}
                    aria-label={t('pages.membersPage.agent_template')}
                  />
                </Field>
                <Field
                  label={`${t('pages.membersPage.create_job')} · ${t('pages.membersPage.create_optional')}`}
                  hint={t('pages.membersPage.create_job_hint')}
                >
                  <Input
                    value={job}
                    onChange={(e) => setJob(e.target.value)}
                    aria-label={t('pages.membersPage.create_job')}
                    placeholder={t('pages.membersPage.create_job_placeholder')}
                    disabled={busy}
                  />
                </Field>
                <WorkspaceField
                  subject="member"
                  hint={t('pages.membersPage.create_workspace_hint')}
                  options={workspaceOptions}
                  value={workspace}
                  onChange={setWorkspace}
                  onNewWorkspace={openWsModal}
                />
                <ModelField
                  options={modelOptions}
                  value={model}
                  onChange={setModel}
                  hint={t('pages.kiroCrewAgentsPage.model_inherited_from_default')}
                />
                <TriggersField value={triggers} onChange={setTriggers} subject="member" />
                <SessionColorField value={sessionColor} onChange={setSessionColor} subject="member" />
                <Btn primary type="submit" disabled={busy || wsModalOpen} className="min-h-11 w-full justify-center sm:min-h-9" data-testid="crewmate-create-submit-end">
                  {busy ? t('pages.membersPage.create_submitting') : t('pages.membersPage.create_submit')}
                </Btn>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
      </fieldset>
    </form>
  )

  return (
    <>
      {embedded ? (
        /* In place: the SAME form under the same heading in a labelled page
           region -- no portal, no scrim, no focus trap, no Escape. The X
           beside the heading is the explicit way out, drawn like the
           modal's own header X, and locked under the same conditions the
           modal's X is (`dismissDisabled`). 44px on a phone, compact from
           sm up. */
        open && (
          <div
            role="region"
            aria-labelledby={titleId}
            className="flex w-full max-w-[560px] flex-col gap-5 self-start rounded-2xl border border-border bg-card p-5 sm:p-6"
            data-testid="crewmate-create-embedded"
          >
            <div className="flex items-center justify-between gap-3" data-testid="crewmate-create-header">
              <h2 id={titleId} className="m-0 min-w-0 truncate text-[15px] font-semibold text-text-strong">{title}</h2>
              <button
                type="button"
                onClick={onClose}
                disabled={busy || wsModalOpen}
                aria-label={t('pages.membersPage.create_cancel')}
                className="-mr-2 flex size-11 shrink-0 items-center justify-center rounded-md border-none bg-transparent text-muted cursor-pointer transition-colors hover:bg-bg-hover hover:text-text focus-ring disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-muted sm:size-8"
                data-testid="crewmate-create-close"
              >
                <X size={16} aria-hidden="true" />
              </button>
            </div>
            {formEl}
          </div>
        )
      ) : (
      <Modal
        open={open}
        onClose={onClose}
        title={title}
        maxWidth={480}
        guardAccidentalDismiss={dirty}
        dismissDisabled={busy || wsModalOpen}
        // The nested New workspace dialog is a Radix layer portaled to
        // document.body, outside this panel: while it is open, this panel is
        // inert and its Tab trap stands down with it, or every Tab in that
        // form is pulled back here (the shared trap's stacked-dialog contract).
        interactionDisabled={wsModalOpen}
      >
        {formEl}
      </Modal>
      )}
      <WorkspaceModal
        open={wsModalOpen}
        onDirtyChange={setWsDirty}
        workspaceOptions={workspaceOptions}
        // No network-deferred state write: the new name goes into the cached
        // list at once and `pendingWorkspace` picks it on the very next commit
        // (see the effect above), so nothing can land on a dialog that was
        // dismissed and reopened while a slow REFRESH was in flight. The
        // refetch only reconciles the list with the server. The CREATE itself
        // is the deferred step, and this handler is bound to the generation
        // of the opening it was rendered for: `WorkspaceForm` keeps the
        // handler from the render its submit started in, so a form that was
        // closed — or a parent that was reset or closed — before the POST
        // answered arrives here with a retired generation and is dropped.
        onCreated={(newName) => {
          if (wsModal.gen !== wsGen.current) return
          closeWsModal()
          queryClient.setQueryData(['workspaces'], (prev: { workspaces?: { name: string }[] } | undefined) => {
            const list = prev?.workspaces ?? [{ name: 'default' }]
            return list.some((w) => w.name === newName) ? prev : { ...prev, workspaces: [...list, { name: newName }] }
          })
          setPendingWorkspace(newName)
          void refetchWorkspaces()
        }}
        // Every nested close lands here — the form's Cancel, the X, Escape and
        // the backdrop (Radix `onOpenChange(false)`) — and retires the generation.
        onClose={closeWsModal}
      />
    </>
  )
}
