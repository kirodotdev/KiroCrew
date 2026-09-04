import { useState, useMemo, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Compass, Link2, RefreshCw } from 'lucide-react'
import { api } from '../../api/client'
import { store } from '../../store'
import { Card, Btn, SearchInput, EmptyState } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import Modal from '../../components/Modal'
import SearchableSelect from '../../components/SearchableSelect'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import ListDetailBack from '../../components/ListDetailBack'
import { useListDetailView } from '../../hooks/useListDetailView'
import type { SteeringFile, SteeringList } from '../../types'

import { parseErrorCode } from '../../utils/errorReport'

import { i18nT } from '../../i18n/t'
/**
 * Catalog KEY for each steering scope's chip label.
 *
 * Keys, not strings: this is module scope, evaluated once at import, so an
 * `i18nT()` call here would freeze the boot language. `sourceLabel()` does the
 * lookup during render, and a flat `Record` of full literal keys indexed inline at
 * the `i18nT()` call is the form `scripts/check-i18n-keys.mjs` can resolve.
 */
const SOURCE_LABEL_KEY: Record<string, string> = {
  user: 'pages.overview.steeringTab.global',
  workspace: 'pages.overview.steeringTab.workspace',
}

/**
 * Catalog KEY for the workspace scope row, per project state.
 *
 * Three whole labels rather than a base label plus an appended parenthetical:
 * the suffix used to be raw English concatenated onto a translated string, so
 * every non-English catalog rendered a half-translated row, and a translator
 * given only "(no project set)" cannot see what it qualifies. Full sentences
 * also let a language put the qualifier where its grammar wants it.
 */
const WORKSPACE_SCOPE_LABEL_KEY: Record<'set' | 'none' | 'ambiguous', string> = {
  set: 'pages.overview.steeringTab.workspace_this_project_only',
  none: 'pages.overview.steeringTab.workspace_scope_no_project',
  ambiguous: 'pages.overview.steeringTab.workspace_scope_project_conflict',
}

/** Catalog KEY for the sentence under the Scope select that says what to DO
 *  about an unavailable workspace scope. `set` has its own line naming the
 *  directory the file will land in, so the dialog always states its target. */
const SCOPE_HINT_KEY: Record<'set' | 'none' | 'ambiguous', string> = {
  set: 'pages.overview.steeringTab.scope_hint_writes_to',
  none: 'pages.overview.steeringTab.scope_hint_no_project',
  ambiguous: 'pages.overview.steeringTab.scope_hint_project_conflict',
}

/** Localised scope chip text, falling back to the raw source token so a scope the
 *  backend adds still renders.
 *
 *  `hasOwnProperty`, not `in`: the source comes from /api/steering, so a value of
 *  `toString` would otherwise resolve to an inherited Object.prototype member and
 *  hand a function to i18next. */
function sourceLabel(source: string): string {
  return Object.prototype.hasOwnProperty.call(SOURCE_LABEL_KEY, source)
    ? i18nT(SOURCE_LABEL_KEY[source])
    : source
}

/** The document body, with its front matter removed for DISPLAY only.
 *
 *  The renderer draws an opening `---` as a horizontal rule and the declarations
 *  under it as a heading, so a document that declares a mode was shown its own
 *  YAML as if it were prose — above the title, in the largest type on the pane.
 *  The mode is already stated by the chip and, for `auto`, by the warning; a
 *  second raw copy is noise.
 *
 *  Display only: `detail.content` is what the editor loads and saves back, so
 *  stripping there would delete the declaration on the next save. The regex is
 *  deliberately anchored and non-greedy — it matches a block at the very start
 *  and stops at the first closing fence, so a `---` used as a rule later in the
 *  document is untouched. */
export function steeringBody(content: string): string {
  return content.replace(/^---\r?\n[\s\S]*?\r?\n---[^\n]*\r?\n?/, '')
}

/** Did the author spell a mode that is not one? The chip and the detail-pane
 *  banner both key on this, so they cannot disagree about what counts. */
export function unrecognizedMode(file: SteeringFile): boolean {
  const mode = typeof file.inclusion === 'string' ? file.inclusion : ''
  const declared = typeof file.inclusion_declared === 'string' ? file.inclusion_declared : ''
  return declared !== '' && declared.toLowerCase() !== mode.toLowerCase()
}

/** Chip naming a document's declared `inclusion` mode.
 *
 *  The mode name itself is NOT translated. `fileMatch` is a literal the author
 *  types into their own front matter, so a localized chip would name a mode that
 *  does not appear in the file — the same reason a token the user must type is
 *  never a catalog value. Only the surrounding explanation is copy.
 *
 *  Rendered only when the mode DEPARTS from the default, or when the declared
 *  spelling is not a mode at all. Every document without front matter resolves to
 *  `always`, so badging that would put a chip on nearly every row and bury the
 *  ones worth noticing.
 *
 *  An unrecognized spelling shows verbatim in a warning chip rather than being
 *  silently normalized: the author's own typo is the only thing that explains why
 *  a document they declared `manual` is loading everywhere. */
function InclusionChip({ file, showDefault = false }: { file: SteeringFile; showDefault?: boolean }) {
  // Read both fields defensively rather than trusting the declared type. This
  // renders inside the list pane, so a missing or non-string field would take
  // the whole pane down with it — and the chip is the least important thing on
  // the row. An absent field simply means no chip.
  const mode = typeof file.inclusion === 'string' ? file.inclusion : ''
  const declared = typeof file.inclusion_declared === 'string' ? file.inclusion_declared : ''
  const unrecognized = unrecognizedMode(file)
  // The list hides a default-mode chip (it would sit on nearly every row);
  // the detail header shows it, because there the mode is the document's own
  // property and there is room to state it.
  if (!showDefault && !unrecognized && (mode === '' || mode === 'always')) return null
  if (mode === '' && !unrecognized) return null
  let title: string
  if (unrecognized) {
    title = i18nT('pages.overview.steeringTab.inclusion_unrecognized', { declared, fallback: mode })
  } else if (mode === 'fileMatch' && file.file_match_pattern) {
    title = i18nT('pages.overview.steeringTab.inclusion_file_match_pattern', { pattern: file.file_match_pattern })
  } else {
    title = i18nT('pages.overview.steeringTab.inclusion_mode', { mode })
  }
  return (
    <span
      title={title}
      aria-label={title}
      className={`text-[10px] px-1.5 py-[1px] rounded-full font-mono shrink-0 border max-w-[9rem] truncate ${
        unrecognized
          ? 'bg-warn-subtle text-warn border-warn'
          : 'bg-bg-elevated text-muted border-border'
      }`}
    >
      {unrecognized ? declared : mode}
    </span>
  )
}

/** Icon-only marker on a linked row.
 *
 *  A linked document loads into every session like any other steering file, but
 *  the write path refuses its key (`resolve_steering_file` never follows a leaf
 *  link for write). The full explanation — including the target to edit
 *  instead — is a visible banner in the detail pane; this marker only flags the
 *  row. Icon-only deliberately: the 240px list column makes the name span yield
 *  width to any chip text, truncating the very filename the row exists to show.
 *  No aria-label either — the row's own "Select {path}" label overrides
 *  children for AT, so a string here never reaches it; the reason lives in the
 *  banner, which is real text. */
function LinkedChip({ file }: { file: SteeringFile }) {
  if (file.linked !== true) return null
  return (
    <span
      data-testid="steering-linked-chip"
      title={i18nT('pages.overview.steeringTab.linked_read_only', { target: file.target })}
      className="text-muted shrink-0 inline-flex items-center"
    >
      <Link2 className="lucide-inline" aria-hidden />
    </span>
  )
}

/** The four modes Kiro's steering format defines, in the order its docs list
 *  them. Code constants, never catalog values: each one is a literal the author
 *  types into their own front matter, so a translated option would name a mode
 *  that does not exist in the file. */
const INCLUSION_MODES = ['always', 'fileMatch', 'manual', 'auto'] as const

/** Segmented control over the four modes, plus the pattern field `fileMatch`
 *  needs. Rendered inside the editor, so the mode is changed where the document
 *  is changed rather than by hand-typing YAML above the text. */
/** Trim a pattern and drop one symmetric wrapping quote pair.
 *
 *  Kiro's own documentation spells the field `fileMatchPattern: "src/**\/*.ts"`,
 *  so the quoted form is exactly what a user copies. Sent verbatim it is refused
 *  by the writer (a value carrying a double quote has no spelling both readers
 *  agree on) — a paste from the vendor's docs failing on its own punctuation. No
 *  glob is delimited by quotes, so removing the wrapper cannot lose meaning.
 */
export function normalizePattern(raw: string): string {
  const value = raw.trim()
  const quoted = value.length >= 2
    && (value[0] === '"' || value[0] === "'")
    && value[value.length - 1] === value[0]
  return quoted ? value.slice(1, -1).trim() : value
}

/** Codes whose natural home is under the File pattern input, not the tab-level
 *  banner: each is a refusal about the value the user just typed there. Kept as
 *  a set so the banner and the field agree on the split instead of drifting into
 *  showing the same sentence twice, or neither.
 */
const FIELD_SCOPED_ERROR_CODES = new Set([
  'steering_field_unrepresentable',
  'steering_field_too_long',
])

/** Translated copy for a declaration refusal, or `null` to fall back.
 *
 *  The backend owns these strings and has no catalog path, so it answers with a
 *  machine-readable `code` and the dashboard supplies the wording. Falling back
 *  to `mutError.message` puts an untranslated English diagnostic in front of a
 *  reader of any of the twelve languages, so every code reachable through this
 *  tab is mapped here; `steering_field_type` is not, because no control in it
 *  can send a non-string.
 */
function declarationErrorText(code: string | undefined): string | null {
  switch (code) {
    case 'steering_file_match_needs_pattern':
      return i18nT('pages.overview.steeringTab.pattern_required', { mode: 'fileMatch' })
    case 'steering_field_unrepresentable':
      return i18nT('pages.overview.steeringTab.pattern_unrepresentable')
    case 'steering_field_too_long':
      return i18nT('pages.overview.steeringTab.declaration_too_long')
    case 'steering_unknown_inclusion':
      return i18nT('pages.overview.steeringTab.inclusion_unknown_rejected')
    default:
      return null
  }
}

function DeclarationEditor({
  mode, pattern, onMode, onPattern, fieldError,
}: {
  mode: string
  pattern: string
  onMode: (next: string) => void
  onPattern: (next: string) => void
  /** A refusal the SERVER raised about this field, shown where it was typed. */
  fieldError?: string | null
}) {
  return (
    <div className="mb-3 rounded-md border border-border bg-bg-elevated p-3">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[11px] font-bold uppercase tracking-wide text-muted">
          {i18nT('pages.overview.steeringTab.inclusion')}
        </span>
        <div role="radiogroup" aria-label={i18nT('pages.overview.steeringTab.inclusion')} className="flex flex-wrap gap-1">
          {INCLUSION_MODES.map(option => (
            <button
              key={option}
              type="button"
              role="radio"
              aria-checked={mode === option}
              onClick={() => onMode(option)}
              className={`text-[12px] font-mono px-2 py-[3px] rounded-md border transition-colors focus-ring ${
                mode === option
                  ? 'bg-accent-subtle border-accent text-text-strong'
                  : 'bg-bg border-border text-muted hover:bg-bg-hover'
              }`}
            >
              {option}
            </button>
          ))}
        </div>
      </div>
      {mode === 'fileMatch' && (
        <>
          <label htmlFor="steering-file-pattern" className="mt-2.5 flex items-center gap-2 flex-wrap">
            <span className="text-[11px] font-bold uppercase tracking-wide text-muted">
              {i18nT('pages.overview.steeringTab.file_pattern')}
            </span>
            <input
              id="steering-file-pattern"
              value={pattern}
              onChange={e => onPattern(e.target.value)}
              aria-label={i18nT('pages.overview.steeringTab.file_pattern')}
              aria-describedby={normalizePattern(pattern) ? undefined : 'steering-file-pattern-required'}
              className="flex-1 min-w-[180px] bg-bg border border-border rounded-md px-2 py-1 text-[12px] font-mono text-text outline-none focus-ring"
            />
          </label>
          {/* Stated where and when it applies. Save is disabled the moment
            * `fileMatch` is picked, and the server-side refusal that carries this
            * same sentence is unreachable BECAUSE of that disable — so without
            * this line the button is simply dead with no reason given. */}
          {!normalizePattern(pattern) && (
            <p id="steering-file-pattern-required" className="mt-1.5 text-[12px] text-warn">
              {i18nT('pages.overview.steeringTab.pattern_required', { mode: 'fileMatch' })}
            </p>
          )}
          {/* A refusal about THIS value belongs under it. The tab-level banner
            * sits above the search box, off-screen once the editor is scrolled
            * to, so a rejected pattern otherwise reads as a save that silently
            * did nothing. */}
          {fieldError && (
            <p className="mt-1.5 text-[12px] text-danger">{fieldError}</p>
          )}
        </>
      )}
    </div>
  )
}

/** Seed body for a new steering file. A function, not a module-level const: the
 *  const would bake in the boot language. Resolved when the create form opens (and
 *  when it resets after a successful create), which is the only time it is used —
 *  re-resolving later would mean overwriting whatever the operator has typed. */
function newTemplate(): string {
  // The trailing newline is file format, not copy, so it lives here rather than
  // in the catalog value — a catalog string with edge whitespace is what
  // `qa.test.ts`'s `edge-whitespace` check exists to stop, and translators
  // routinely drop or double a trailing blank line.
  // Concatenation, not a template literal: the strict rule matches the static
  // `\n` chunk inside a template literal and `[added-lines]` is zero-tolerance,
  // so `+ '\n'` expresses the same thing in a shape the rule does not flag.
  return i18nT('pages.overview.steeringTab.title_describe_the_convention_the_agent_should_a') + '\n'
}

/** Did this write fail because the project moved out from under the listing?
 *
 *  Keyed on the machine-readable identity — HTTP 409 plus the server's `code` —
 *  never on the human sentence. Matching prose would let a copy edit silently
 *  disable the recovery path the 409 exists to trigger, which is the whole reason
 *  the response carries a `code` at all.
 *
 *  Reads `status`/`body` structurally rather than testing `instanceof ApiError`:
 *  the class identity does not survive a module mock that omits the export, and a
 *  predicate that THROWS during render is a worse failure than one that misses a
 *  refresh. The fields are the contract; the class is just who usually carries it. */
function isProjectConflict(err: unknown): boolean {
  const status = (err as { status?: unknown } | null)?.status
  if (status !== 409) return false
  const body = (err as { body?: unknown }).body
  // The CODE is the identity, and it is required — 409 alone is not enough on
  // these routes, because create already answers 409 for a name collision
  // ("'x.md' already exists") with no code. Treating a bare 409 as a project
  // conflict showed "the active project changed" over a collision and re-listed
  // for nothing. Anything without this code keeps the server's own message.
  return parseErrorCode(typeof body === 'string' ? body : undefined) === 'steering_project_changed'
}

/** Textarea styling matches SkillForm's raw-markdown editor. */
const EDITOR_CLASS =
  'w-full h-full min-h-[320px] bg-bg-elevated border border-border rounded-md p-3 text-text font-mono text-[13px] outline-none resize-none focus-ring'

/**
 * The list-detail shell's height.
 *
 * `svh` (the viewport with browser chrome SHOWING) rather than `vh`: `vh`
 * resolves against the large viewport, so on a phone the pane runs under the
 * address bar and its bottom edge — which while narrow holds the only visible
 * pane — is unreachable. `svh` also does not re-resolve as the URL bar
 * animates, unlike `dvh`. Identical to `vh` on a desktop, where there is no
 * dynamic chrome. The `vh` declaration stays as the fallback for browsers
 * without `svh`, matching the shell's own `supports-[height:100dvh]` pattern.
 */
const PANE_SHELL_CLASS = 'flex gap-3 -mx-2 md:mx-0 h-[calc(100vh-260px)] supports-[height:100svh]:h-[calc(100svh-260px)] min-h-[420px]'

export default function SteeringTab() {
  const queryClient = useQueryClient()
  const [filter, setFilter] = useState('')
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  /** The project key in force when the draft was loaded.
   *
   *  Save must be judged against the project the CONTENT came from, not
   *  whichever project the listing has since refreshed to: a background
   *  refetch re-syncs `projectKey`, so sending the live value would let a
   *  draft typed against project A satisfy the server's precondition for B
   *  and overwrite B's same-named file. Sending the captured value makes
   *  that case fail the precondition (409) with the draft still on screen —
   *  which is why the editor is not simply torn down on a project change:
   *  discarding what the user typed is its own kind of loss. */
  const [draftProjectKey, setDraftProjectKey] = useState<string | undefined>(undefined)
  // The declaration being edited, captured when the draft loads. Held apart
  // from `draft` because the server owns the front-matter rewrite: sending the
  // mode as its own field is what keeps the editor from having to splice YAML
  // into the text it is showing.
  const [draftMode, setDraftMode] = useState('')
  const [draftPattern, setDraftPattern] = useState('')
  // What the declaration was when the draft loaded. Save sends only the fields
  // whose control actually moved: the server applies a declaration ON TOP of
  // the submitted text, so unconditionally sending the seeded values would
  // silently revert front matter the user had just edited by hand in the
  // textarea — the mode control winning an argument the user never started.
  const [baseMode, setBaseMode] = useState('')
  const [basePattern, setBasePattern] = useState('')
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [newSource, setNewSource] = useState<'user' | 'workspace'>('workspace')
  const [newBody, setNewBody] = useState(newTemplate)

  /**
   * The chat slot whose project `workspace/` keys resolve against.
   *
   * This tab lives on a settings page with no chat of its own, so "this project"
   * can only mean the project of the chat the operator was last in. Sending it
   * lets the server answer for THAT slot; with no key it can only fall back to
   * "the one project every slot shares" and fails closed once two chats sit on
   * different projects — which is what made workspace scope unreachable.
   *
   * Read once at mount, not subscribed: leaving the page unmounts the tab, so a
   * later chat switch is picked up on return, while a value that cannot change
   * mid-mount keeps it stable as a query key.
   */
  const [slotKey] = useState<string | undefined>(() => {
    const slot = store.getState().chat.activeSlot
    return slot ? `dashboard:${slot}` : undefined
  })

  const { data, isLoading, isFetching, refetch } = useQuery<SteeringList>({
    queryKey: ['steering', slotKey ?? null],
    queryFn: () => api.steeringFiles(slotKey),
  })
  const files = useMemo(() => data?.files ?? [], [data])
  const roots = useMemo(() => data?.roots ?? [], [data])
  /** Parent of the global steering root, for the "Writes to …" hint.
   *
   *  Taken from the listing's own `roots` rather than assumed, and the trailing
   *  `/.kiro/steering` is stripped because the catalog string supplies that
   *  suffix itself. The `~` fallback only applies before the first response. */
  const globalHintPath = useMemo(() => {
    const userRoot = roots.find(r => r.source === 'user')?.path
    return (userRoot ?? '~/.kiro/steering').replace(/\/\.kiro\/steering$/, '')
  }, [roots])
  const project = data?.project ?? ''
  /** `project` alone cannot distinguish "none set" from "chats disagree"; fall
   *  back to inferring it so an older backend still renders a sane label. */
  // `?? 'none'` is the pre-load default, not a compatibility shim: the field is
  // required on the wire and `data` is simply absent until the first response.
  const projectState: 'set' | 'none' | 'ambiguous' = data?.project_state ?? 'none'
  const hasProject = projectState === 'set'
  /** Fingerprint of the project THIS listing resolved to. Every workspace write
   *  echoes it so the server refuses (409) rather than acting on a different
   *  project when the chat slot has been re-pointed since the list was drawn. */
  const projectKey = data?.project_key

  const { data: detail } = useQuery({
    // projectKey is part of the identity, not decoration: `workspace/api.md`
    // names a DIFFERENT file in a different project, so a key without it serves
    // project A's cached body as project B's file.
    queryKey: ['steering-file', selectedKey, slotKey ?? null, projectKey ?? null],
    queryFn: () => api.steeringFile(selectedKey!, slotKey),
    enabled: !!selectedKey,
  })

  const createFile = useMutation({
    mutationFn: (body: { name: string; content: string; source: string }) =>
      api.createSteering(body.name, body.content, body.source, slotKey, projectKey),
    onSuccess: (res: { key?: string }) => {
      setCreating(false)
      setNewName('')
      setNewBody(newTemplate())
      if (res?.key) {
        setSelectedKey(res.key)
        // Drop any cached detail for this key: a file deleted and recreated
        // under the same name would otherwise populate the editor from the
        // OLD file's retained cache entry (gcTime keeps it, and it is served
        // stale on re-select), and saving that would overwrite the new file.
        queryClient.removeQueries({ queryKey: ['steering-file', res.key] })
      }
      queryClient.invalidateQueries({ queryKey: ['steering'] })
    },
  })

  const updateFile = useMutation({
    mutationFn: ({ key, content, declaration }: {
      key: string
      content: string
      declaration?: { inclusion?: string; file_match_pattern?: string }
    }) =>
      api.updateSteering(key, content, slotKey, draftProjectKey ?? projectKey, declaration),
    onSuccess: (
      res: {
        content?: string
        inclusion?: string
        inclusion_declared?: string
        file_match_pattern?: string
      } | undefined,
      vars,
    ) => {
      setEditing(false)
      // Seed the cache from the WRITE's own response before invalidating. A
      // refetch can fail or lag, and a second Edit would then re-seed from the
      // pre-save body and mode and write them back over what was just saved.
      // The server echoes the stored text for exactly this.
      if (typeof res?.content === 'string') {
        queryClient.setQueryData(
          ['steering-file', vars.key, slotKey, draftProjectKey ?? projectKey],
          (old: unknown) => (old && typeof old === 'object' ? { ...old, content: res.content } : old),
        )
      }
      // Same reasoning for the LIST row: Edit re-seeds its draft mode from
      // `selected.inclusion`, which lives in THIS cache, not the detail one
      // above. A list refetch that fails or lags left this row's declaration
      // stale, so reopening Edit before it resolved showed the mode the user
      // just replaced — as if the save had not happened.
      if (res && (res.inclusion !== undefined || res.file_match_pattern !== undefined)) {
        queryClient.setQueryData(['steering', slotKey ?? null], (old: unknown) => {
          if (!old || typeof old !== 'object' || !Array.isArray((old as SteeringList).files)) {
            return old
          }
          const list = old as SteeringList
          return {
            ...list,
            files: list.files.map(f =>
              f.key === vars.key
                ? {
                    ...f,
                    inclusion: res.inclusion ?? f.inclusion,
                    inclusion_declared: res.inclusion_declared ?? f.inclusion_declared,
                    file_match_pattern: res.file_match_pattern ?? f.file_match_pattern,
                  }
                : f,
            ),
          }
        })
      }
      queryClient.invalidateQueries({ queryKey: ['steering'] })
      queryClient.invalidateQueries({ queryKey: ['steering-file'] })
    },
  })

  const deleteFile = useMutation({
    mutationFn: (key: string) => api.deleteSteering(key, slotKey, projectKey),
    onSuccess: (_res, key) => {
      setSelectedKey(null)
      setEditing(false)
      // Remove, not invalidate: the file is gone, so its cached detail must
      // not survive to seed a later file created under the same key.
      queryClient.removeQueries({ queryKey: ['steering-file', key] })
      queryClient.invalidateQueries({ queryKey: ['steering'] })
    },
  })

  const mutError = (createFile.error || updateFile.error || deleteFile.error) as Error | null
  const mutErrorCode = parseErrorCode((mutError as { body?: unknown } | null)?.body as string | undefined)
  // Split once, here: a field-scoped refusal renders under the input it is
  // about and is withheld from the tab-level banner, so the two never say the
  // same sentence twice — and never both stay silent.
  const fieldErrorText = mutErrorCode && FIELD_SCOPED_ERROR_CODES.has(mutErrorCode)
    ? declarationErrorText(mutErrorCode)
    : null
  // Suppress the banner only while that copy is actually ON SCREEN. The
  // pattern input renders solely while editing AND on `fileMatch`, so keying
  // the banner off the error CODE alone hid both of them the moment the user
  // switched mode or left the editor — the save stayed refused with nothing
  // on screen saying so.
  const fieldErrorShown = Boolean(fieldErrorText) && editing && draftMode === 'fileMatch'

  // A 409 means the project moved under this listing, so the rows on screen are
  // the stale input that produced it — refetch instead of leaving the user to
  // guess that "refresh" means the button in the corner.
  //
  // EXCEPT while editing: the refetch lists the NEW project, where this file may
  // not exist at all, and the selection effect then drops the row the editor is
  // attached to — hiding the draft the 409 exists to preserve. A conflict on a
  // create or a delete has no draft to lose, so those still refresh.
  const conflictError = isProjectConflict(mutError)
  const savingConflict = conflictError && !!updateFile.error && editing
  useEffect(() => {
    if (!conflictError) return
    if (savingConflict) return
    queryClient.invalidateQueries({ queryKey: ['steering'] })
  }, [conflictError, savingConflict, queryClient])

  const filtered = useMemo(() => {
    const q = filter.toLowerCase()
    if (!q) return files
    return files.filter(f => (f.key + ' ' + (f.description || '')).toLowerCase().includes(q))
  }, [files, filter])

  const selected = useMemo(() => files.find(f => f.key === selectedKey) ?? null, [files, selectedKey])

  /** Linked entries are read-only: the backend never resolves their key for
   *  write, so offering Edit/Save or Delete would only manufacture a 404.
   *  `=== false` fails open for a cached listing predating the field. */
  const selectedReadOnly = selected?.editable === false

  // Narrow viewport shows one pane at a time; a desktop shows both.
  const { isMobile, showList, showDetail, openDetail, closeDetail } = useListDetailView()

  // Keep a valid selection; suspended while editing so an unsaved draft is
  // never discarded by a background refetch reordering the list.
  useEffect(() => {
    if (editing) return
    if (filtered.length === 0) { if (selectedKey !== null) setSelectedKey(null); return }
    if (!selectedKey || !filtered.some(f => f.key === selectedKey)) setSelectedKey(filtered[0].key)
  }, [filtered, selectedKey, editing])

  // Default the create dialog to the scope that exists.
  useEffect(() => { setNewSource(hasProject ? 'workspace' : 'user') }, [hasProject])

  /** Open or close the create dialog, dropping any previous attempt's error.
   *
   *  react-query holds mutation state until the next `mutate()`/`reset()`, so a
   *  failed create followed by Cancel and reopen rendered a stale banner over a
   *  form the user had not submitted. Reset here rather than in an effect: an
   *  effect runs a render late, which still paints the stale banner for a frame.
   */
  const setCreateDialog = (open: boolean) => {
    createFile.reset()
    setCreating(open)
  }

  /** The declaration fields the user actually changed, or `undefined`.
   *
   *  `undefined` means "leave the front matter alone", which is what lets a
   *  hand-edit in the textarea survive: the server only rewrites the keys it is
   *  given. A switch AWAY from `fileMatch` deliberately does not clear the
   *  pattern — the endpoint edits a declaration, and a glob the author typed is
   *  theirs to keep. */
  const changedDeclaration = () => {
    const out: { inclusion?: string; file_match_pattern?: string } = {}
    if (draftMode !== baseMode) out.inclusion = draftMode
    if (normalizePattern(draftPattern) !== normalizePattern(basePattern)) {
      out.file_match_pattern = normalizePattern(draftPattern)
    }
    return Object.keys(out).length ? out : undefined
  }

  const select = (f: SteeringFile) => { setSelectedKey(f.key); setEditing(false); openDetail() }

  const renderRow = (f: SteeringFile) => {
    const isSel = f.key === selectedKey
    return (
      <div
        key={f.key}
        role="button"
        tabIndex={0}
        aria-current={isSel ? 'true' : undefined}
        aria-label={i18nT('pages.overview.steeringTab.select', { path: f.rel })}
        onClick={() => select(f)}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(f) } }}
        className={`flex flex-col gap-0.5 px-3 py-2.5 rounded-md cursor-pointer mb-1 transition-colors ${
          isSel ? 'list-selected bg-accent-subtle' : 'bg-bg-elevated hover:bg-bg-hover'
        }`}
      >
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="text-[13px] font-semibold text-text truncate flex-1">{f.rel}</span>
          <LinkedChip file={f} />
          <InclusionChip file={f} />
          <span className="text-[10px] px-1.5 py-[1px] rounded-full bg-bg-elevated text-muted border border-border font-bold shrink-0">
            {sourceLabel(f.source)}
          </span>
        </div>
        {f.description && <div className="text-[11px] text-muted truncate">{f.description}</div>}
      </div>
    )
  }

  const rootHint = roots.map(r => r.path).join('  ·  ')

  return (<>
    <Modal
      open={creating}
      onClose={() => setCreateDialog(false)}
      title={i18nT('pages.overview.steeringTab.new_steering_file')}
      maxWidth={640}
      footer={<>
        <Btn onClick={() => setCreateDialog(false)}>{i18nT('pages.overview.steeringTab.cancel')}</Btn>
        <Btn
          primary
          disabled={!newName.trim() || !newBody.trim() || createFile.isPending}
          onClick={() => createFile.mutate({ name: newName.trim(), content: newBody, source: newSource })}
        >{i18nT('pages.overview.steeringTab.create')}</Btn>
      </>}
    >
      <div className="flex flex-col gap-3">
        {/* A failed create leaves this modal OPEN, so the page-level error banner
          * behind it is invisible — the refusal has to render here or the Create
          * button just appears inert. */}
        {createFile.error && (
          <div
            role="alert"
            className="px-3 py-2 rounded-md bg-danger/10 border border-danger/20 text-[13px] text-danger"
          >
            {/* Same code-keyed substitution as the page banner. Without it this
              * surface — the only verb with its own alert — printed the server's
              * English diagnostic, which `steering.py` explicitly documents as a
              * fallback for clients that cannot localize. A create conflict has
              * already triggered the re-list, so it takes that copy. */}
            {isProjectConflict(createFile.error)
              ? i18nT('pages.overview.steeringTab.conflict_list_refreshed')
              : (createFile.error as Error).message}
          </div>
        )}
        <label className="flex flex-col gap-1" htmlFor="steering-new-name">
          <span className="text-[13px] text-muted">{i18nT('pages.overview.steeringTab.file_name')}</span>
          <input
            id="steering-new-name"
            aria-label={i18nT('pages.overview.steeringTab.steering_file_name')}
            className="w-full bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-[13px] outline-none focus-ring"
            placeholder={i18nT('pages.overview.steeringTab.api_standards_md')}
            value={newName}
            onChange={e => setNewName(e.target.value)}
          />
        </label>
        {/* The control IS nested here — label-has-for only recognises native
            form elements, so its nesting check false-positives on the trigger
            <button> SearchableSelect renders. Same disable as SchedulePage's
            render-timezone label, for the same component family. */}
        <label className="flex flex-col gap-1" htmlFor="steering-new-scope">
          <span className="text-[13px] text-muted">{i18nT('pages.overview.steeringTab.scope')}</span>
          {/* SearchableSelect, not SimpleSelect: the workspace row is a REAL option
              that is conditionally unselectable, and only per-option `disabled`
              keeps it visible — so "workspace scope exists, you just have no
              project set" survives instead of the row vanishing. The label keeps
              both channels: `id`/htmlFor so clicking "Scope" focuses the trigger,
              and an explicit aria-label so the name does not depend on how a
              <label> is resolved for the <button> the trigger renders as. */}
          <SearchableSelect
            id="steering-new-scope"
            aria-label={i18nT('pages.overview.steeringTab.scope')}
            options={[
              {
                value: 'workspace',
                label: i18nT(WORKSPACE_SCOPE_LABEL_KEY[projectState]),
                disabled: !hasProject,
              },
              { value: 'user', label: i18nT('pages.overview.steeringTab.global_every_project') },
            ]}
            value={newSource}
            onChange={v => setNewSource(v as 'user' | 'workspace')}
          />
          {/* The dead end this closes: the row said "(no project set)" and
            * stopped there, naming a scope with no way to reach it. The hint
            * names the control that binds a project, or — when open chats
            * disagree — says that is the problem, since the two look identical
            * from here. */}
          {/* Two independent facts, so two lines rather than one overloaded slot:
            * where the file lands for the scope currently SELECTED, and — when
            * workspace scope cannot be chosen — what to do about that. Keying the
            * destination on project state alone claimed the project directory
            * after the user switched to Global; dropping the guidance to fix that
            * removed the affordance this dialog was missing. Both are needed. */}
          <span className="flex flex-col gap-0.5 text-[11px] text-muted" data-testid="steering-scope-hint">
            <span>
              {i18nT('pages.overview.steeringTab.scope_hint_writes_to', {
                path: newSource === 'user' ? globalHintPath : project,
              })}
            </span>
            {projectState !== 'set' && (
              <span>{i18nT(SCOPE_HINT_KEY[projectState], { path: project })}</span>
            )}
          </span>
        </label>
        <label className="flex flex-col gap-1" htmlFor="steering-new-body">
          <span className="text-[13px] text-muted">{i18nT('pages.overview.steeringTab.content')}</span>
          <textarea
            id="steering-new-body"
            aria-label={i18nT('pages.overview.steeringTab.steering_file_content')}
            className={EDITOR_CLASS}
            rows={14}
            value={newBody}
            onChange={e => setNewBody(e.target.value)}
          />
        </label>
      </div>
    </Modal>

    {/* No top margin — see the same note in SkillsTab: the hosting pane owns the
      * gap under the tab strip, and a margin here would stack on it. */}    <h4 className="text-sm font-semibold text-text-strong mb-2 flex items-center gap-2">
      {i18nT('pages.overview.steeringTab.steering_count', { count: files.length })}
      <InfoTip text={i18nT('pages.overview.steeringTab.always_on_markdown_conventions_injected_into_eve')} />
      <span className="ml-auto">
        <Btn primary onClick={() => setCreateDialog(true)}>{i18nT('pages.overview.steeringTab.new_steering_file_2')}</Btn>
      </span>
    </h4>
    <Card>
      <div className="flex items-center gap-2 mb-3">
        <div className="relative max-w-[480px] flex-1">
          <SearchInput placeholder={i18nT('pages.overview.steeringTab.filter_steering_files')} value={filter} onChange={e => setFilter(e.target.value)} />
          {filter && (
            <button
              className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-text transition-colors cursor-pointer"
              onClick={() => setFilter('')}
              aria-label={i18nT('pages.overview.steeringTab.clear_search')}
            >{"\u00d7"}</button>
          )}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Btn onClick={() => refetch()} disabled={isFetching} aria-label={i18nT('pages.overview.steeringTab.refresh_steering_files')}>
            <RefreshCw size={14} className={isFetching ? 'animate-spin' : ''} />
          </Btn>
        </div>
      </div>

      {mutError && !fieldErrorShown && (
        <div className="mb-3 px-3 py-2 rounded-md bg-danger/10 border border-danger/20 text-[13px] text-danger">
          {conflictError
            ? i18nT(savingConflict
              ? 'pages.overview.steeringTab.conflict_while_editing'
              : 'pages.overview.steeringTab.conflict_list_refreshed')
            : declarationErrorText(mutErrorCode) ?? mutError.message}
        </div>
      )}

      {isLoading ? (
        <div className={PANE_SHELL_CLASS}>
          <div className="w-[240px] shrink-0 space-y-1">{Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="h-[52px] rounded-md animate-pulse" style={{ background: 'var(--border)', opacity: 0.5, animationDelay: `${i * 80}ms` }} />
          ))}</div>
          <div className="flex-1 rounded-md animate-pulse" style={{ background: 'var(--border)', opacity: 0.3 }} />
        </div>
      ) : files.length === 0 ? (
        <EmptyState
          icon={<Compass className="lucide-inline" />}
          title={i18nT('pages.overview.steeringTab.no_steering_files_yet')}
          subtitle={i18nT('pages.overview.steeringTab.steering_files_looked_in', { path: rootHint || '~/.kiro/steering' })}
        />
      ) : (
        <div className={PANE_SHELL_CLASS}>
          {showList && <div className={`${isMobile ? 'w-full' : 'w-[240px]'} shrink-0 overflow-y-auto scrollbar-overlay border border-border rounded-md p-2`} role="listbox" aria-label={i18nT('pages.overview.steeringTab.steering_files')}>
            {filtered.map(renderRow)}
            {filtered.length === 0 && <div className="text-muted/70 text-[12px] italic px-2 py-2">{i18nT('pages.overview.steeringTab.no_files_match_query', { query: filter })}</div>}
          </div>}

          {showDetail && <div className="flex-1 min-w-0 flex flex-col border border-border rounded-md bg-card overflow-hidden">
            {!selected ? (
              <div className="flex items-center justify-center h-full text-muted text-[13px]">{i18nT('pages.overview.steeringTab.select_a_steering_file_to_view_it')}</div>
            ) : (
              <div className="flex flex-col h-full min-h-0">
                {/* Wraps rather than shrinks: at 390px the title, scope chip and
                    the Edit/Delete pair do not fit on one line, and squeezing
                    them onto one overlapped the two buttons. */}
                {/* Own row: this header's action slot already holds either
                    Cancel+Save or Edit+Delete, so Back would be a third. */}
                {isMobile && (
                  <div className="px-4 pt-2.5 shrink-0">
                    <ListDetailBack label={i18nT('pages.overview.steeringTab.steering_files')} onBack={closeDetail} />
                  </div>
                )}
                <div className="flex items-center justify-between gap-2 flex-wrap px-4 py-2.5 border-b border-border shrink-0">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-sm font-bold text-text-strong truncate">{selected.rel}</span>
                    <span className="text-[11px] px-1.5 py-[1px] rounded-full bg-bg-elevated text-muted border border-border font-bold shrink-0">
                      {sourceLabel(selected.source)}
                    </span>
                    {/* No LinkedChip here: the read-only banner directly below
                      * states the full sentence, and a header chip is what
                      * pushed Edit/Delete onto a second line. */}
                    <InclusionChip file={selected} showDefault />
                    {/* The absolute path is reference detail, not identity: it
                        never fits beside the name on a phone and would push the
                        actions off the row. */}
                    {!isMobile && <span className="text-[11px] text-muted font-mono truncate">{selected.path}</span>}
                  </div>
                  <div className="flex gap-2 shrink-0">
                    {editing ? (<>
                      <Btn onClick={() => setEditing(false)}>{i18nT('pages.overview.steeringTab.cancel')}</Btn>
                      <Btn primary disabled={!draft.trim() || updateFile.isPending || (draftMode === 'fileMatch' && !normalizePattern(draftPattern))} onClick={() => updateFile.mutate({ key: selected.key, content: draft, declaration: changedDeclaration() })}>{i18nT('pages.overview.steeringTab.save')}</Btn>
                    </>) : (<>
                      {/* Disabled rather than hidden for a linked entry, so the
                        * controls' absence does not read as a rendering bug; the
                        * title says why, naming the target file to edit instead. */}
                      <Btn
                        disabled={detail === undefined || selectedReadOnly}
                        title={selectedReadOnly
                          ? i18nT('pages.overview.steeringTab.linked_read_only', { target: selected.target })
                          : undefined}
                        onClick={() => { setDraft(detail?.content ?? ''); setDraftProjectKey(projectKey); setDraftMode(unrecognizedMode(selected) ? '' : (selected.inclusion || 'always')); setDraftPattern(selected.file_match_pattern || ''); /* An unrecognised declaration resolves to `always`, so seeding either
   side from it is wrong, and the two failures are opposite. Seeding the
   BASE from it makes picking `always` a no-op, leaving the warning chip
   in place — a silent retry loop on the very failure this tab ends.
   Seeding the DRAFT from it is worse: draft and base then differ before
   the user touches anything, so a body-only save silently REWRITES the
   author's declaration to `always`. Both start unset: nothing is sent
   until a mode is actually picked, and any pick is a real change. */
                        setBaseMode(unrecognizedMode(selected) ? '' : (selected.inclusion || 'always')); setBasePattern(selected.file_match_pattern || ''); setEditing(true) }}>{i18nT('pages.overview.steeringTab.edit')}</Btn>
                      <Btn
                        danger
                        disabled={selectedReadOnly}
                        title={selectedReadOnly
                          ? i18nT('pages.overview.steeringTab.linked_read_only', { target: selected.target })
                          : undefined}
                        onClick={() => { if (confirm(i18nT('pages.overview.steeringTab.delete_confirm', { path: selected.rel }))) deleteFile.mutate(selected.key) }}>{i18nT('pages.overview.steeringTab.delete')}</Btn>
                    </>)}
                  </div>
                </div>
                <div className="flex-1 min-h-0 overflow-y-auto p-4 flex flex-col">
                  {selectedReadOnly && (
                    /* Visible, not tooltip-only: Chrome and Safari suppress
                       mouse events on a disabled button, so the Edit/Delete
                       titles never surface there, and touch has no hover at
                       all — the same reasoning as the unrecognized-mode
                       banner below. This line is also the only place the
                       resolved target is actually rendered. */
                    <div className="mb-3 rounded-md border border-border bg-bg-elevated p-3">
                      <p className="text-[12px] leading-relaxed text-muted">
                        <Link2 className="lucide-inline" aria-hidden />{' '}
                        {i18nT('pages.overview.steeringTab.linked_read_only', { target: selected.target })}
                      </p>
                    </div>
                  )}
                  {unrecognizedMode(selected) && (
                    /* A typo'd mode silently resolves to `always`, and the
                       only sign of it was the chip's tooltip — unreachable on
                       touch and to anyone who deep-links here. This banner
                       states a fact about the FILE, not a prediction about
                       what the harness will do with it.

                       Rendered while EDITING too, and above the control: the editor
                       seeds an unrecognised declaration as UNSELECTED, so hiding this
                       on Edit leaves an empty radiogroup and no cue — at the exact
                       moment the user came to fix the typo it names. */
                    <div className="mb-3 rounded-md border border-warn bg-warn-subtle p-3">
                      <p className="text-[12px] leading-relaxed text-warn">
                        {i18nT('pages.overview.steeringTab.inclusion_unrecognized', {
                          declared: selected.inclusion_declared,
                          fallback: selected.inclusion,
                        })}
                      </p>
                    </div>
                  )}
                  {editing && (
                    <DeclarationEditor
                      mode={draftMode}
                      pattern={draftPattern}
                      onMode={setDraftMode}
                      onPattern={setDraftPattern}
                      fieldError={fieldErrorShown ? fieldErrorText : null}
                    />
                  )}
                  <div className="flex-1 min-h-0">
                    {editing
                      ? <textarea className={EDITOR_CLASS} aria-label={i18nT('pages.overview.steeringTab.edit_2', { path: selected.rel })} value={draft} onChange={e => setDraft(e.target.value)} />
                      : detail === undefined
                        ? <div className="text-muted text-[13px]">{i18nT('pages.overview.steeringTab.loading')}</div>
                        : <div className="text-sm leading-relaxed"><MarkdownRenderer content={steeringBody(detail.content)} /></div>}
                  </div>
                </div>
              </div>
            )}
          </div>}
        </div>
      )}
    </Card>
  </>)
}
