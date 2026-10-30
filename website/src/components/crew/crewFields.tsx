/** The crew-binding form fields every crew editor shares: the field frame
 *  (`Field`), workspace (with the nested create-workspace dialog), memory
 *  store, model, reasoning effort, display name, routing keywords and session
 *  color. The crew editor sheet, the Crewmates page's New crewmate dialog and
 *  the crew profile settings mount them, each importing this module directly.
 *  It imports no page, so a consumer never closes a module cycle through it.
 *  `KiroCrewAgentsPage.tsx` re-exports every name for its existing importers. */
import { useState, useEffect } from 'react'
import { api } from '../../api/client'
import { FOLDER_COLOR_PALETTE } from '../folderColorCatalog'
import { Btn, SendBtn, Input } from '../ui'
import {
  Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '../ui/dialog'
import InfoTip from '../InfoTip'
import SimpleSelect from '../SimpleSelect'
import { type KiroCrewAgent } from '../AgentSelector'
import { EFFORT_LEVELS, effortLabel } from '../../lib/effort'
import { isAssistantMember } from '../../lib/assistantMember'
import { i18nT } from '../../i18n/t'
import { uiLocation } from '../../uiLocations/uiLocation'
import ErrorNotice from '../ErrorNotice'

// An example input value, independent of the display language.
const HEX_COLOR_EXAMPLE = '#4f8ef7'

/** Common shape returned by the agent/workspace mutation endpoints. */
export interface AgentMutationResult {
  error?: string
  name?: string
  memory_store?: string
}

/** The stored spelling for "no per-agent pin, inherit the next tier down". The
 *  select shows this as a real option; the backend normalizes it back to ''. */
export const INHERIT_MODEL = 'auto'

/** The create API's "a workspace of its own" value (`member_workspace.AUTO_WORKSPACE`):
 *  the member is bound to a fresh `member-<slug>` workspace made with its record.
 *  Create-time only; a stored record never carries it. */
export const OWN_WORKSPACE = ''

/** Which word the create form uses for the thing being made. */
/** Which word a create-form field uses for the thing being made. REQUIRED on
 *  every field the form composes — no default — so a future field cannot
 *  silently fall back to "agent" inside the member flow (the exact "renamed
 *  one field in" defect #9513's review caught). The editor passes 'agent'. */
type FormSubject = 'agent' | 'member'

/* ── Workspace Creation Dialog (a nested Radix layer inside the crew editor) ── */

/** The form itself, mounted only while the dialog is open.
 *
 *  Split out from `WorkspaceModal` on purpose: Radix unmounts `DialogContent`'s
 *  children on close, so keeping the state HERE resets a half-typed workspace
 *  name between openings for free. Hoisting it into the parent (which stays
 *  mounted so Radix can run its own close transition) would persist it. */
function WorkspaceForm({
  workspaceOptions,
  onCreated,
  onClose,
  onDirtyChange,
  onSaveChain,
}: {
  workspaceOptions: string[]
  onCreated: (name: string) => void
  onClose: () => void
  /** Whether the form holds unsaved input; the modal reads it to refuse
   *  Escape / backdrop dismissal while it does. */
  onDirtyChange?: (dirty: boolean) => void
  /** Register the in-flight create POST with the host so its navigation guard
   *  tracks it to SETTLEMENT, not to this form's lifetime: a boolean cleared on
   *  unmount would release the guard the instant the modal closes (Cancel)
   *  while the request still completes and creates the workspace. The host
   *  decrements only when the promise settles, so the guard holds across a
   *  cancel/close. */
  onSaveChain?: (p: Promise<unknown>) => void
}) {
  const [wsName, setWsName] = useState('')
  const [wsDir, setWsDir] = useState('workspace')
  const [dirTouched, setDirTouched] = useState(false)
  const [copyFrom, setCopyFrom] = useState('')
  const dirty = wsName !== '' || dirTouched || copyFrom !== ''
  // With a cleanup: Radix unmounts this form when the dialog closes, and a
  // draft that went with it must not leave the host reading `dirty` as true
  // (its Escape/backdrop guard and the crewmate dialog's navigation stake
  // both consume it).
  useEffect(() => { onDirtyChange?.(dirty); return () => onDirtyChange?.(false) }, [dirty, onDirtyChange])
  // Two states, because they are two different things: `wsHint` is client-side
  // validation (nothing failed), `wsError` is the outcome of a request that did.
  const [wsHint, setWsHint] = useState('')
  const [wsError, setWsError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  // Auto-fill directory from workspace name (unless user manually edited it)
  const handleNameChange = (v: string) => {
    setWsName(v)
    if (!dirTouched) {
      const slug = v.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
      setWsDir(slug ? `workspace-${slug}` : 'workspace')
    }
  }

  const submit = async () => {
    setWsHint('')
    setWsError('')
    const n = wsName.trim()
    if (!n) { setWsHint(i18nT('pages.kiroCrewAgentsPage.workspace_name_is_required')); return }
    setSubmitting(true)
    try {
      const body: Record<string, string> = { name: n, dir: wsDir }
      if (copyFrom) body.copy_from = copyFrom
      // Hand the create POST to the host BEFORE awaiting it, so its navigation
      // guard counts the write until it settles even if this form unmounts
      // (Cancel / backdrop) mid-request. The host swallows the rejection on its
      // tracking copy; the real outcome is still handled below.
      const createWrite = api.createWorkspace(body)
      onSaveChain?.(createWrite)
      const r: AgentMutationResult = await createWrite
      if (r.error) { setWsError(r.error); setSubmitting(false); return }
      onCreated(r.name || n)
    } catch (e) {
      setWsError(e instanceof Error ? e.message : i18nT('pages.kiroCrewAgentsPage.failed_to_create_workspace'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>{i18nT('pages.kiroCrewAgentsPage.create_workspace')}</DialogTitle>
      </DialogHeader>
      <DialogBody>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-1">
              {/* Native input associated via htmlFor+id; label-has-for's nesting requirement is a false positive. */}
              <label htmlFor="ws-name" className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('pages.kiroCrewAgentsPage.name')}</label>
              <InfoTip text={i18nT('pages.kiroCrewAgentsPage.a_unique_identifier_for_this_workspace_agents_re')} />
            </div>
            <Input id="ws-name" placeholder={i18nT('pages.kiroCrewAgentsPage.e_g_oncall')} value={wsName} onChange={e => handleNameChange(e.target.value)} autoFocus />
          </div>
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-1">
              {/* Native input associated via htmlFor+id; label-has-for's nesting requirement is a false positive. */}
              <label htmlFor="ws-dir" className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('pages.kiroCrewAgentsPage.directory')}</label>
              <InfoTip text={i18nT('pages.kiroCrewAgentsPage.subdirectory_inside_kiro_crew_where_this_workspa')} />
            </div>
            <Input id="ws-dir" placeholder={i18nT('pages.kiroCrewAgentsPage.workspace')} value={wsDir} onChange={e => { setDirTouched(true); setWsDir(e.target.value) }} />
          </div>
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-1">
              <span className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('pages.kiroCrewAgentsPage.copy_from_optional')}</span>
              <InfoTip text={i18nT('pages.kiroCrewAgentsPage.copy_the_contents_of_an_existing_workspace_into')} />
            </div>
            <SimpleSelect
              options={workspaceOptions}
              value={copyFrom}
              onChange={setCopyFrom}
              clearLabel={i18nT('pages.kiroCrewAgentsPage.none')}
              aria-label={i18nT('pages.kiroCrewAgentsPage.copy_from_workspace')}
            />
          </div>
          {/* Client-side validation hint (the name never left the browser) —
              not an error surface, so it stays plain text rather than ErrorNotice. */}
          {wsHint && <div className="text-danger text-[13px]">{wsHint}</div>}
          {/* No hand-off: the workspace name / directory / copy-from fields
              (wsName, wsDir, copyFrom) are unsaved — a failed create is exactly
              what did not store them. */}
          <ErrorNotice message={wsError} variant="inline" testId="workspace-create-error" />
        </div>
      </DialogBody>
      <DialogFooter>
        <Btn onClick={onClose}>{i18nT('pages.kiroCrewAgentsPage.cancel')}</Btn>
        <SendBtn onClick={submit} disabled={submitting}>{submitting ? i18nT('pages.kiroCrewAgentsPage.creating') : i18nT('pages.kiroCrewAgentsPage.create')}</SendBtn>
      </DialogFooter>
    </>
  )
}

export function WorkspaceModal({
  open,
  workspaceOptions,
  onCreated,
  onClose,
  onDirtyChange,
  onSaveChain,
}: {
  open: boolean
  workspaceOptions: string[]
  onCreated: (name: string) => void
  onClose: () => void
  /** The form's unsaved-input state, for a host that publishes its own
   *  navigation stake (the Crewmates dialog): a route change unmounts this
   *  modal with its host, and the host's guard must count this draft too. */
  onDirtyChange?: (dirty: boolean) => void
  /** Register the in-flight create POST with the host so a route-leave is
   *  refused until it SETTLES (tracked past this modal's unmount). */
  onSaveChain?: (p: Promise<unknown>) => void
}) {
  // Unsaved input in the form: Escape and a backdrop click are refused while
  // it is set (the same rule `components/Modal` applies as
  // `guardAccidentalDismiss`), so grazing the backdrop cannot discard a typed
  // name/directory/copy-from. The explicit Cancel and the X still close.
  const [dirty, setDirty] = useState(false)
  useEffect(() => { onDirtyChange?.(dirty) }, [dirty, onDirtyChange])
  /* Kept MOUNTED and driven by `open`, rather than conditionally rendered.
     Radix tracks dismissable layers in a global stack, and tearing this whole
     subtree out the instant it closes skipped the layer's own deregistration —
     the editor underneath was then left believing it was no longer the top
     layer, so Escape stopped closing it. Verified in a real browser
     (scripts/verify-crews-dialog-select.mjs), which is the only place the bug
     showed: happy-dom does not reproduce it.

     `z-[110]` because both layers are centered overlays and the editor's own
     content sits at z-[101]; at an equal z-index this would render behind its
     own opener. */
  return (
    <Dialog open={open} onOpenChange={next => { if (!next) onClose() }}>
      <DialogContent
        maxWidth={448}
        className="z-[110]"
        overlayClassName="z-[109]"
        aria-label={i18nT('pages.kiroCrewAgentsPage.create_workspace')}
        // preventDefault keeps Radix from dismissing AND marks the Escape as
        // handled, so the layer underneath (the crewmate dialog's own Escape
        // listener skips a defaultPrevented event) does not close either.
        onEscapeKeyDown={e => { if (dirty) e.preventDefault() }}
        onPointerDownOutside={e => { if (dirty) e.preventDefault() }}
      >
        <WorkspaceForm workspaceOptions={workspaceOptions} onCreated={onCreated} onClose={onClose} onDirtyChange={setDirty} onSaveChain={onSaveChain} />
      </DialogContent>
    </Dialog>
  )
}

/** One labelled control with an optional explainer: the frame around every
 *  field of the crew forms, so they share one frame. */
export function Field({ label, hint, info, children }: { label: string; hint?: string; info?: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="flex items-center gap-1.5 text-[11px] text-muted uppercase tracking-wider font-medium">
        {label}
        {info && <InfoTip text={info} />}
      </span>
      {children}
      {hint && <span className="text-[11.5px] leading-relaxed text-muted">{hint}</span>}
    </div>
  )
}

/** An EMPTY current value means "nothing selected" and must NOT be appended as an
 *  option: SimpleSelect treats an options list containing '' as making empty
 *  selectable, which suppresses the trigger placeholder and adds a blank row. */
function withCurrent(opts: string[], cur: string): string[] {
  return !cur || opts.includes(cur) ? opts : [...opts, cur]
}

/**
 * One component per binding, so the create dialog and the editor's panes render
 * the SAME control rather than two copies that drift. The create dialog
 * composes them in its Advanced settings; the editor mounts them individually,
 * one per rail pane.
 */
export function WorkspaceField({ options, value, onChange, onNewWorkspace, subject, hint, ownOption = false }: {
  options: string[]; value: string; onChange: (v: string) => void; onNewWorkspace: () => void; subject: FormSubject
  /** Replaces the subject's default hint. The defaults are EDIT-time copy
   *  ("new chats start fresh in the new folder"); a create form has no old
   *  chats to worry about, so it passes its own line. */
  hint?: string
  /** Offer `OWN_WORKSPACE` ("its own workspace") as the first choice. Create
   *  forms only: the API allocates on create and never on update, so the
   *  editor must not show a choice it cannot save. */
  ownOption?: boolean
}) {
  const choices = ownOption
    ? [OWN_WORKSPACE, ...withCurrent(options, value).filter(o => o !== OWN_WORKSPACE)]
    : withCurrent(options, value)
  return (
    <Field
      label={i18nT('pages.kiroCrewAgentsPage.workspace_2')}
      hint={hint ?? (subject === 'member' ? i18nT('pages.kiroCrewAgentsPage.workspace_hint_member') : i18nT('pages.kiroCrewAgentsPage.isolated_memory_and_files_for_this_crew'))}
      info={i18nT('pages.kiroCrewAgentsPage.bindings_preview_info')}
    >
      <SimpleSelect
        options={choices}
        optionLabels={ownOption ? choices.map(o => (o === OWN_WORKSPACE ? i18nT('pages.kiroCrewAgentsPage.own_workspace_option') : o)) : undefined}
        value={value}
        onChange={onChange}
        action={{ label: i18nT('pages.kiroCrewAgentsPage.new_workspace_action'), onSelect: onNewWorkspace }}
        aria-label={i18nT('pages.kiroCrewAgentsPage.workspace_2')}
      />
    </Field>
  )
}

export type MemberMemoryState = 'legacy' | 'private' | 'ownership_mismatch' | 'unavailable'

export function memberMemoryState(member: string, store: string, stores: Record<string, { memory_version?: number; owner_member?: string }> | undefined): MemberMemoryState {
  if (member === 'default') return store === 'default' ? 'legacy' : 'unavailable'
  if (!stores) return 'unavailable'
  const config = stores?.[store]
  if (config?.owner_member && config.owner_member !== member) return 'ownership_mismatch'
  if (config?.memory_version === 2 && config.owner_member === member) return 'private'
  if (Object.values(stores).some(value => value.owner_member === member)) return 'unavailable'
  if (store === 'default') return 'legacy'
  if (!config) return 'unavailable'
  const version = config.memory_version === undefined ? 1 : config.memory_version
  if (version === 1 && (config.owner_member === undefined || config.owner_member === '')) return 'legacy'
  return 'unavailable'
}

export function ModelField({ options, value, onChange, hint, servedAs, harness = '' }: {
  options: string[]; value: string; onChange: (v: string) => void; hint?: string
  /** The id the crew's harness serves a stored pin `options` lacks as, `''` for none
   *  (`resolved-model`'s `pin_served_as`). Omitted while no live session has said. */
  servedAs?: string
  /** That harness's name, for a pin it does not offer. */
  harness?: string
}) {
  // A stored pin spelled as another harness spells it is the row it runs as, not a second row for
  // the same model: two rows read as two models, and the readout below still names the stored id.
  const folded = servedAs !== undefined && servedAs !== value && options.includes(servedAs)
  const shown = folded ? options : withCurrent(options, value)
  // Otherwise the off-list pin says what this harness does with it.
  const offList = !folded && servedAs !== undefined && servedAs !== value && !options.includes(value)
  const label = (m: string) => {
    if (m === INHERIT_MODEL) return i18nT('pages.kiroCrewAgentsPage.inherited')
    if (m !== value || !offList) return m
    return servedAs
      ? i18nT('pages.kiroCrewAgentsPage.pin_runs_as', { model: m, served: servedAs })
      : i18nT('pages.kiroCrewAgentsPage.pin_not_offered', { model: m, harness })
  }
  return (
    <Field label={i18nT('pages.kiroCrewAgentsPage.model')} hint={hint}>
      <SimpleSelect
        options={shown}
        // The inherit option must NOT read as "auto": in the chat picker "auto"
        // promises task-based routing, whereas here it means "pin nothing,
        // inherit the next tier" — which can resolve to a concrete model. Label
        // it as the card does so the round trip stays honest.
        optionLabels={shown.map(label)}
        value={folded ? servedAs : value}
        onChange={onChange}
        aria-label={i18nT('pages.kiroCrewAgentsPage.edit_model')}
        {...uiLocation('agents.model')}
      />
    </Field>
  )
}

/** The crew's reasoning-effort pin. Rendered only when the model the crew will
 *  actually run on supports effort — the same gate the chat picker uses, so a
 *  crew on Haiku is not offered a control the backend would drop. */
export function EffortField({ value, onChange, levels }: {
  value: string; onChange: (v: string) => void
  /** The levels the crew's harness takes; the shared vocabulary when it reported none. */
  levels?: readonly string[]
}) {
  const options = levels ? ['', ...levels.filter(l => l && l !== 'default')] : [...EFFORT_LEVELS]
  return (
    <Field label={i18nT('pages.kiroCrewAgentsPage.reasoning_effort')} hint={i18nT('pages.kiroCrewAgentsPage.reasoning_effort_hint')}>
      <SimpleSelect
        options={options}
        // '' is the inherit sentinel, labelled as such rather than as a level:
        // it means "take the global default", which may itself be a level.
        optionLabels={options.map(l => (l === '' ? i18nT('pages.kiroCrewAgentsPage.inherited') : effortLabel(l)))}
        value={value}
        onChange={onChange}
        aria-label={i18nT('pages.kiroCrewAgentsPage.edit_reasoning_effort')}
      />
    </Field>
  )
}

/** The optional display label. Rendered by the create form and by the editor's
 *  routing pane, so it is a component rather than two copies. Presentation
 *  only: the crew's NAME stays the identity crons, spawn params and the CLI
 *  address, so relabelling never breaks a binding — which is exactly why the
 *  label is freely editable while the name is not. */
export function DisplayNameField({ value, onChange, fallback }: { value: string; onChange: (v: string) => void; fallback: string }) {
  return (
    <Field
      label={i18nT('pages.kiroCrewAgentsPage.display_name')}
      // Names the actual ID, so "the name" is never a reference to something
      // invisible from this pane (the UX lane's finding on this PR).
      hint={i18nT('pages.kiroCrewAgentsPage.shown_across_the_dashboard_instead_of_the_name_l', { name: fallback })}
    >
      <Input
        // The name it would fall back to, so the empty state reads as "shows
        // this" rather than as a missing value.
        placeholder={fallback}
        value={value}
        onChange={e => onChange(e.target.value)}
        aria-label={i18nT('pages.kiroCrewAgentsPage.display_name')}
        data-testid="display-name-input"
      />
    </Field>
  )
}

/** What an empty display name falls back to: the crew's key, except for the
 *  built-in Mate, whose empty label reads as its default name everywhere
 *  else in the dashboard -- so its editor must not offer the internal key. */
export function displayNameFallback(agent: KiroCrewAgent | null | undefined, key: string): string {
  return isAssistantMember(agent) ? i18nT('components.assistantWelcome.default_name') : key
}

/** The routing-keyword input. Rendered by the create form and by the editor's
 *  routing pane, so it is a component rather than two copies. */
export function TriggersField({ value, onChange, subject }: { value: string; onChange: (v: string) => void; subject: FormSubject }) {
  const hint = subject === 'member'
    ? i18nT('pages.kiroCrewAgentsPage.triggers_hint_member')
    : i18nT('pages.kiroCrewAgentsPage.triggers_hint')
  const info = subject === 'member'
    ? i18nT('pages.kiroCrewAgentsPage.triggers_info_member')
    : i18nT('pages.kiroCrewAgentsPage.triggers_info')
  return (
    <Field label={i18nT('pages.kiroCrewAgentsPage.triggers')} hint={hint} info={info}>
      <Input
        placeholder={i18nT('pages.kiroCrewAgentsPage.triggers_placeholder')}
        value={value}
        onChange={e => onChange(e.target.value)}
        aria-label={i18nT('pages.kiroCrewAgentsPage.triggers')}
      />
    </Field>
  )
}

/** Session color picker for agent configuration. Sets the default session
 *  tint color for new sessions created with this agent. */
export function SessionColorField({ value, onChange, subject }: { value: string; onChange: (v: string) => void; subject: FormSubject }) {
  const HEX_RE = /^#[0-9a-f]{6}$/i
  const [draft, setDraft] = useState(value || '')
  // Re-sync the draft when the committed value changes from outside (e.g. the
  // swatch, Clear, or opening the editor on a different crew).
  useEffect(() => { setDraft(value || '') }, [value])
  const commit = (raw: string) => {
    const v = raw.trim().toLowerCase()
    if (v === '') { onChange(''); setDraft('') }
    else if (HEX_RE.test(v)) { onChange(v); setDraft(v) }
    else { setDraft(value || '') } // invalid on blur → revert to committed
  }
  return (
    <Field label={i18nT('pages.kiroCrewAgentsPage.session_color')} hint={subject === 'member' ? i18nT('pages.kiroCrewAgentsPage.session_color_hint_member') : i18nT('pages.kiroCrewAgentsPage.session_color_hint')}>
      {/* Quick picks first, exact entry below — the order the session
       *  right-click menu uses, so the two surfaces read the same way.
       *
       *  These are FOLDER_COLOR_PALETTE, the repo's existing fixed-hex identity
       *  catalog, NOT the sidebar's generated palette. The sidebar's swatches
       *  are a `color_index` into a palette derived from the theme accent, so
       *  they re-derive when the theme changes; a crew's `session_color` is a
       *  stored hex, so a swatch here has to commit exactly the literal it
       *  shows and must not drift. That is the same job the folder catalog
       *  already does, and reusing it keeps one visual language across folders,
       *  tags and crews — as that file's own comment argues — instead of a
       *  second preset list that would silently diverge from it. Read-only:
       *  the catalog's KEEP IN SYNC contract with chat_folders.py governs
       *  changes to its entries, and consuming it adds no such coupling.
       *
       *  The active ring is matched by hex, so a custom colour outside the
       *  catalog correctly rings nothing.
       *
       *  No "no color" cell here: Clear already owns that, and two controls for
       *  one action is worse than one. */}
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        {FOLDER_COLOR_PALETTE.map(({ value: c, label }) => {
          const active = HEX_RE.test(value) && value.toLowerCase() === c
          return (
            <Btn
              type="button"
              key={c}
              aria-label={label()}
              aria-pressed={active}
              title={label()}
              // Btn, not a raw <button>, so the swatches inherit the standard
              // press and disabled treatment. `p-0` and the sizing below win
              // over Btn's own padding/radius/border because Btn twMerges
              // `className` last; the inline background beats its
              // `bg-transparent` (and its hover background) on specificity, so
              // the dot keeps its colour in every state.
              //
              // `border-text-strong`, not `border-accent`: the accent is itself a
              // purple in most themes, so an accent ring on the indigo and violet
              // entries reads as no ring at all. The near-white ring is what
              // SessionColorSwatches uses, and it separates from every hue here.
              className={`h-5 w-5 p-0 cursor-pointer rounded-full border-2 transition-transform hover:brightness-125 swatch-cue ${active ? 'border-text-strong scale-110' : 'border-border'}`}
              style={{ background: c }}
              onClick={() => onChange(c)}
            />
          )
        })}
      </div>
      <div className="flex items-center gap-2">
        <Input
          type="color"
          value={value || HEX_COLOR_EXAMPLE}
          onChange={e => onChange(e.target.value.toLowerCase())}
          className="h-8 w-8 flex-none cursor-pointer p-0.5"
          aria-label={i18nT('pages.kiroCrewAgentsPage.session_color')}
        />
        <Input
          placeholder={HEX_COLOR_EXAMPLE}
          value={draft}
          onChange={e => {
            const v = e.target.value.trim().toLowerCase()
            setDraft(v)
            // Live-commit only when the draft is a complete hex or cleared;
            // partial values stay local so typing is never swallowed.
            if (v === '' || HEX_RE.test(v)) onChange(v)
          }}
          onBlur={e => commit(e.target.value)}
          className="flex-1 font-mono text-[13px]"
          aria-label={i18nT('pages.kiroCrewAgentsPage.session_color_hex')}
        />
        {value && (
          <Btn
            type="button"
            onClick={() => onChange('')}
            className="text-[11px]"
            aria-label={i18nT('pages.kiroCrewAgentsPage.session_color_clear')}
          >
            {i18nT('pages.kiroCrewAgentsPage.session_color_clear')}
          </Btn>
        )}
      </div>
    </Field>
  )
}
