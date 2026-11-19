import React from 'react'
import { ChevronRight } from 'lucide-react'
import InfoTip from './InfoTip'
import SearchableSelect, { type SearchableSelectOption } from './SearchableSelect'
import SimpleSelect from './SimpleSelect'
import MultiSelect, { type MultiSelectOption } from './MultiSelect'
import { Input, Toggle } from './ui'

import { i18nT } from '../i18n/t'
import { RadioChipGroup, radioChip } from './RadioChipGroup'
/* ── Settings-specific UI primitives ──
 *
 * These match the pencil design system components:
 *   - SettingsToggle   → flat row: label+description left, toggle right
 *   - SettingsSelect   → one row: label left, dropdown right
 *   - SettingsCombobox → one row: label left, searchable dropdown right
 *   - SettingsInput    → stacked (text, typed in order) or one row (number)
 *   - SettingsSection  → group heading + description above one group box
 *
 * Layout rule: website/docs/page-layout.md, "Settings groups".
 */

/* ── Toggle ── */

interface SettingsToggleProps {
  label: string
  /** Status beside the label, kept separate from its searchable and accessible name. */
  labelBadge?: React.ReactNode
  // ReactNode (not just string) so callers can pass rich copy — e.g. the
  // Telegram forum toggle describes setup with inline <span className="font-mono">
  // fragments. The render path already wraps it in a <div>, so any node is safe.
  description?: React.ReactNode
  /** A small status badge drawn before `description` on the same line (e.g. a
   *  "Restart" badge on a switch that applies at the next gateway start). A
   *  separate prop so `description` stays the literal the settings extractor reads. */
  descriptionBadge?: React.ReactNode
  checked: boolean
  onChange: (value: boolean) => void
  disabled?: boolean
  /** Opt-in: the toggle is saving its own change right now. The switch keeps
   *  keyboard focus (not taken out of the tab order like a plain `disabled`,
   *  which drops focus to the page) and ignores presses while the save runs,
   *  announced as `aria-disabled` + `aria-busy`. The row click is ignored too.
   *  Default unset keeps today's behaviour, so no other caller changes. */
  busy?: boolean
  /** Backend config key this toggle writes (e.g. 'telemetry.beacon_enabled'). Used by the settings registry and SettingRef linking. */
  configKey?: string
  /** Stable highlight anchor (`data-setting-id`) for a row with no gateway
   *  `configKey`, as on `SettingsField`. Lets a deep link find a late row. */
  settingId?: string
  /** id of an element describing a CONSEQUENCE of flipping this toggle, rendered
   *  outside the row (so it is not dimmed with a disabled row). Threaded to the
   *  switch's `aria-describedby` so assistive tech announces it before the user
   *  acts, instead of leaving a side effect discoverable only by exploring. */
  describedBy?: string
  /**
   * An info tip beside the label, for prose that explains what the control IS.
   *
   * The counterpart to `description`, which keeps its text permanently on the row.
   * Use `description` only when the sentence is needed to MAKE the choice (a
   * consequence, a cost, where data goes, a status); anything a reader would want
   * once and never again belongs here, because a row that always shows two lines
   * of prose spends attention whether or not it is being read. The tip shows on
   * hover and focus, pins on click or tap, and is the control's accessible
   * description (`InfoTip`). One catalog string per tip.
   */
  hint?: string
  /**
   * `false` drops the `data-setting-*` highlight anchors, for a copy of a
   * Settings row drawn somewhere else (the What's-new modal reuses About's
   * update switches), so a deep link can only land on the panel's row.
   */
  anchor?: boolean
}

/** A link or a button inside a row's description stays above the label's
 *  stretched click layer, so it is pressed itself instead of the switch. */
const PRESSABLE_ABOVE_ROW_CLS = '[&_a]:relative [&_button]:relative'

export function SettingsToggle({ label, labelBadge, description, descriptionBadge, hint, checked, onChange, disabled, busy, configKey, settingId, describedBy, anchor = true }: SettingsToggleProps) {
  const inert = disabled || busy
  // The row's own text first, then the caller's note about a consequence: the
  // order they are read on screen. A screen reader hears both, with the name and
  // the on/off state, at the one stop the row has.
  const switchId = React.useId()
  const descriptionId = React.useId()
  const hasDescription = Boolean(descriptionBadge || description)
  const switchDescribedBy = [hasDescription ? descriptionId : null, describedBy].filter(Boolean).join(' ') || undefined
  return (
    // The row itself is not a control and handles nothing. A row that was also
    // a button gave every setting two Tab stops, and the first one read out as a
    // button that never said on or off. The switch is the one control; the name
    // beside it is its <label>, stretched over the row (`after:inset-0`) so a
    // click anywhere on the row still presses the switch, the way a click on any
    // label does. The tip, the switch and anything a description can press sit
    // above that layer.
    <div {...(anchor ? { 'data-setting-label': label, ...(configKey ? { 'data-setting-key': configKey } : {}), ...(settingId ? { 'data-setting-id': settingId } : {}) } : {})} className={`relative flex items-center justify-between py-1.5 group ${inert ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}`}>
      <div className="flex-1 min-w-0 mr-4">
        <div className="flex items-center gap-1.5">
          <label htmlFor={switchId} className="min-w-0 text-[13px] font-semibold text-text group-hover:text-text-strong transition-colors after:absolute after:inset-0 after:content-['']">{label}</label>
          {labelBadge && <span className="shrink-0">{labelBadge}</span>}
          {hint && <span data-settings-hint className="relative"><InfoTip text={hint} placement="top" /></span>}
        </div>
        {descriptionBadge ? (
          // Badge and description each in their own block: two separate UI
          // units side by side, not one sentence built from two catalog keys.
          <div id={descriptionId} className={`flex flex-wrap items-center gap-1.5 text-[12px] text-muted mt-0.5 ${PRESSABLE_ABOVE_ROW_CLS}`}>
            <div className="shrink-0">{descriptionBadge}</div>
            {description && <div>{description}</div>}
          </div>
        ) : description && <div id={descriptionId} className={`text-[12px] text-muted mt-0.5 ${PRESSABLE_ABOVE_ROW_CLS}`}>{description}</div>}
      </div>
      <Toggle id={switchId} checked={checked} onChange={onChange} disabled={disabled} busy={busy} label={label} describedBy={switchDescribedBy} />
    </div>
  )
}


/* ── Select ── */

/** Shared field wrapper: label + optional hint + optional description.
 *
 * `controlId` is the id of the labelable control rendered inside `children`.
 * When provided, the caption renders as `<label htmlFor>` so the visible text
 * becomes the control's programmatic name (unlocking screen-reader
 * announcement and `getByLabelText` in tests). Without it the caption stays a
 * `<span>` — a `<label>` with a dangling `htmlFor`, or one wrapping a group of
 * buttons (SettingsStepper / SettingsButtonGroup), would be wrong. Optional so
 * the wrappers without a single labelable control keep compiling unchanged. */
/** How a field row lays out its caption and control.
 *  - `stacked`: caption above, control below at full width. For text a person
 *    types in order (a form), where a half-width box and its error would cramp.
 *  - `inline-fixed` / `inline-auto`: caption left, control right, one row per
 *    setting, inside a Settings group box only. The control sizes to its own
 *    content, so a long selected label shows whole; `fixed` adds a 12rem floor so
 *    short dropdowns still read as one column, `auto` lets a segmented control or
 *    stepper keep its own width. The row wraps on the space it actually has (the
 *    box, not the viewport): a control too wide to sit beside its caption drops
 *    under it at full width instead of being clipped. Outside a box both render
 *    as `stacked`. */
export type SettingsFieldLayout = 'stacked' | 'inline-fixed' | 'inline-auto'

/** One content width for every Settings group, so a caption on the left and
 *  its control on the right stay close enough to read as one row on a wide
 *  window, and every group's right edge lines up with the next. */
export const SETTINGS_CONTENT_MAX_W = 'max-w-[720px]'

export function SettingsField({ label, description, hint, configKey, settingId, controlId, layout = 'stacked', children }: { label: string; description?: string; hint?: string; configKey?: string; settingId?: string; controlId?: string; layout?: SettingsFieldLayout; children: React.ReactNode }) {
  const caption = (
    <div className="flex items-center gap-1.5">
      {controlId
        ? <label htmlFor={controlId} className="text-[13px] font-semibold text-text">{label}</label>
        : <span className="text-[13px] font-semibold text-text">{label}</span>}
      {hint && <InfoTip text={hint} placement="top" />}
    </div>
  )
  const anchors = { 'data-setting-label': label, ...(configKey ? { 'data-setting-key': configKey } : {}), ...(settingId ? { 'data-setting-id': settingId } : {}) }
  if (layout === 'stacked') {
    return (
      <div {...anchors} className="flex flex-col gap-1.5 py-1.5">
        {caption}
        {description && <div className="text-[12px] text-muted">{description}</div>}
        {children}
      </div>
    )
  }
  // Inline only inside a group box (`in-[[data-settings-box]]:`), which covers
  // both SettingsCard's box and a grouped Card's. There the row wraps on its own
  // width: the caption takes nearly all spare space (grow 999, 12rem basis) and
  // the control keeps its content width (grow 1 adds a fraction of a pixel), so
  // the two share a line when they fit. When they do not, the control wraps under
  // the caption, where the grow fills the line, and `max-w-full` keeps it inside
  // the box's overflow-hidden frame. No viewport breakpoint: a narrow Settings
  // pane in a wide window gets the wrapped form too.
  return (
    <div {...anchors} className="flex flex-col gap-1.5 py-1.5 in-[[data-settings-box]]:flex-row in-[[data-settings-box]]:flex-wrap in-[[data-settings-box]]:items-center in-[[data-settings-box]]:gap-x-6">
      <div className="min-w-0 in-[[data-settings-box]]:grow-[999] in-[[data-settings-box]]:basis-48">
        {caption}
        {description && <div className="text-[12px] text-muted mt-0.5">{description}</div>}
      </div>
      <div className={layout === 'inline-fixed'
        ? 'min-w-0 max-w-full in-[[data-settings-box]]:shrink-0 in-[[data-settings-box]]:grow in-[[data-settings-box]]:min-w-[min(12rem,100%)]'
        : 'min-w-0 max-w-full in-[[data-settings-box]]:shrink-0 in-[[data-settings-box]]:grow'}>
        {children}
      </div>
    </div>
  )
}

interface SettingsSelectProps {
  label: string
  description?: string
  hint?: string
  value: string
  options: string[]
  /** Optional display labels for each option (same order as options). Falls back to the option value. */
  optionLabels?: string[]
  onChange: (value: string) => void
  /** Optional action at top of dropdown (e.g. "+ New workspace…") */
  action?: { label: string; onSelect: () => void }
  disabled?: boolean
  /** Opt-in: this select is saving its own change right now. It keeps keyboard
   *  focus instead of going native `disabled` (which would drop focus to the
   *  page) and refuses picks while the save runs. See `SimpleSelect`'s `busy`.
   *  Default unset keeps today's behaviour, so no other caller changes. */
  busy?: boolean
  /** Schema-backed config key this select writes. */
  configKey?: string
  /** Explicit UI row identity for deep links, independent of the config schema. */
  settingId?: string
}

export function SettingsSelect({ label, description, hint, value, options, optionLabels, onChange, action, disabled, busy, configKey, settingId }: SettingsSelectProps) {
  // Per-instance id pairing the caption's htmlFor with the select trigger, so
  // the visible caption is the control's programmatic label. The aria-label
  // below stays as a fallback: it wins the accessible-name computation and
  // carries the same string, so nothing double-announces.
  const controlId = React.useId()
  // The trigger can still cut a label that is wider than the whole box, so it
  // carries the full text as its hover title.
  const selectedLabel = optionLabels?.[options.indexOf(value)] ?? (value || '—')
  return (
    <SettingsField label={label} description={description} hint={hint} configKey={configKey} settingId={settingId} controlId={controlId} layout="inline-fixed">
      <SimpleSelect
        id={controlId}
        options={options}
        optionLabels={optionLabels}
        value={value}
        onChange={onChange}
        action={action}
        disabled={disabled}
        busy={busy}
        aria-label={label}
        title={selectedLabel}
        triggerFallback={selectedLabel}
      />
    </SettingsField>
  )
}

/* ── Combobox ── */

interface SettingsComboboxProps {
  label: string
  description?: string
  hint?: string
  value: string
  options: SearchableSelectOption[]
  onChange: (value: string) => void
  /** Trigger text when `value` matches no option — e.g. a typed-in value. */
  triggerFallback?: string
  searchPlaceholder?: string
  /** Offer the typed text as a committable value, shaped here. See `SearchableSelect`. */
  customValueOption?: (typed: string) => Omit<SearchableSelectOption, 'value'>
  /** Action row inside the list, e.g. an opt-in permission prompt. */
  action?: { label: string; onSelect: () => void }
  /** One-line outcome of the last action run, rendered beside it in the popup. */
  actionStatus?: string
  /** Backend config key this combobox writes. */
  configKey?: string
}

/**
 * Searchable dropdown row — `SettingsSelect`'s sibling for a list too long to
 * scan, or one that carries a per-option sublabel. Reach for `SettingsSelect` at
 * a dozen-ish fixed options and this past that.
 */
export function SettingsCombobox({ label, description, hint, value, options, onChange, triggerFallback, searchPlaceholder, customValueOption, action, actionStatus, configKey }: SettingsComboboxProps) {
  const controlId = React.useId()
  // SearchableSelect takes no `title`, so the full trigger text (built the way
  // its trigger builds it) hangs on a wrapper: hovering the trigger shows it.
  const selected = options.find(o => o.value === value)
  const triggerText = selected
    ? (selected.sublabel ? `${selected.label} (${selected.sublabel})` : selected.label)
    : (triggerFallback ?? value ?? '—')
  return (
    <SettingsField label={label} description={description} hint={hint} configKey={configKey} controlId={controlId} layout="inline-fixed">
      <div title={triggerText}>
        <SearchableSelect
          id={controlId}
          options={options}
          value={value}
          onChange={onChange}
          triggerFallback={triggerFallback}
          searchPlaceholder={searchPlaceholder}
          customValueOption={customValueOption}
          action={action}
          actionStatus={actionStatus}
          aria-label={label}
        />
      </div>
    </SettingsField>
  )
}

interface SettingsMultiSelectProps {
  label: string
  description?: string
  hint?: string
  options: MultiSelectOption[]
  selected: ReadonlySet<string>
  onToggle: (value: string, selected: boolean) => void
  bulkActions?: ReadonlyArray<{ label: string; onSelect: () => void }>
  summary: string
  searchPlaceholder?: string
  disabled?: boolean
  configKey?: string
  settingId?: string
  onOpenChange?: (open: boolean) => void
  notice?: string
}

export function SettingsMultiSelect({ label, description, hint, options, selected, onToggle, bulkActions, summary, searchPlaceholder, disabled, configKey, settingId, onOpenChange, notice }: SettingsMultiSelectProps) {
  const controlId = React.useId()
  return (
    <SettingsField label={label} description={description} hint={hint} configKey={configKey} settingId={settingId} controlId={controlId} layout="inline-fixed">
      {/* MultiSelect takes no `title`; the wrapper carries the summary its
          trigger may cut. */}
      <div title={summary}>
        <MultiSelect
          id={controlId}
          label={label}
          options={options}
          selected={selected}
          onToggle={onToggle}
          bulkActions={bulkActions}
          summary={summary}
          searchPlaceholder={searchPlaceholder}
          disabled={disabled}
          onOpenChange={onOpenChange}
          notice={notice}
        />
      </div>
    </SettingsField>
  )
}

/* ── Input ── */

interface SettingsInputProps {
  label: string
  description?: string
  hint?: string
  value: string
  onChange: (value: string) => void
  onBlur?: React.FocusEventHandler<HTMLInputElement | HTMLTextAreaElement>
  /** Key handler on the control itself. Needed by panels that commit on blur and
   *  have no Save button (WeChat), where Enter must commit the value the way it
   *  would in a form — a `<div>` wrapper cannot carry that without becoming an
   *  interactive static element. */
  onKeyDown?: React.KeyboardEventHandler<HTMLInputElement | HTMLTextAreaElement>
  /** Composition/focus pass-throughs so callers can spread `ime.bindComposition()`
   *  from `useImeGuard` onto the control; see the WeChat folder-name field. */
  onFocus?: React.FocusEventHandler<HTMLInputElement | HTMLTextAreaElement>
  onCompositionStart?: React.CompositionEventHandler<HTMLInputElement | HTMLTextAreaElement>
  onCompositionEnd?: React.CompositionEventHandler<HTMLInputElement | HTMLTextAreaElement>
  placeholder?: string
  type?: 'text' | 'number' | 'time'
  min?: number
  max?: number
  step?: number
  disabled?: boolean
  multiline?: boolean
  'aria-label'?: string
  /** Backend config key this input writes. */
  configKey?: string
  /** Id of the element explaining why the current value was refused. When set,
   *  the control is marked invalid and described by that element. */
  invalidMessageId?: string
  /** Rendered under the control inside the row, e.g. the refusal notice
   *  `invalidMessageId` names. */
  after?: React.ReactNode
}

export function SettingsInput({ label, description, hint, value, onChange, onBlur, onKeyDown, onFocus, onCompositionStart, onCompositionEnd, placeholder, type = 'text', min, max, step, disabled, multiline, 'aria-label': ariaLabel, configKey, invalidMessageId, after }: SettingsInputProps) {
  // Per-instance id pairing the caption's htmlFor with the control. This is
  // what gives the single-line branch an accessible name by DEFAULT: it used
  // to render aria-label={ariaLabel} with ariaLabel undefined unless a caller
  // duplicated the caption, leaving the input nameless to screen readers.
  // An explicit aria-label still wins the name computation, so deliberate
  // overrides keep working.
  const controlId = React.useId()
  const errorProps = invalidMessageId ? { 'aria-invalid': true, 'aria-describedby': invalidMessageId } : {}
  return (
    <SettingsField label={label} description={description} hint={hint} configKey={configKey} controlId={controlId} layout={!multiline && type === 'number' ? 'inline-fixed' : 'stacked'}>
      {multiline ? (
        <textarea
          id={controlId}
          value={value}
          onChange={e => onChange(e.target.value)}
          onBlur={onBlur}
          onKeyDown={onKeyDown}
          onFocus={onFocus}
          onCompositionStart={onCompositionStart}
          onCompositionEnd={onCompositionEnd}
          placeholder={placeholder}
          disabled={disabled}
          rows={3}
          aria-label={ariaLabel ?? label}
          {...errorProps}
          className="w-full rounded border border-border bg-bg px-2 py-1 text-sm text-text focus-visible:border-accent focus:outline-hidden resize-y flex-none"
        />
      ) : (
        <Input
          id={controlId}
          type={type}
          value={value}
          onChange={e => onChange(e.target.value)}
          onBlur={onBlur}
          onKeyDown={onKeyDown}
          onFocus={onFocus}
          onCompositionStart={onCompositionStart}
          onCompositionEnd={onCompositionEnd}
          placeholder={placeholder}
          min={min}
          max={max}
          step={step}
          disabled={disabled}
          aria-label={ariaLabel}
          {...errorProps}
          className={invalidMessageId ? 'flex-none border-danger' : 'flex-none'}
        />
      )}
      {after}
    </SettingsField>
  )
}

/* ── Section header (sits outside the Card) ── */

interface SettingsSectionProps {
  /** Section heading. Required: every SettingsSection in the app is titled, so
   *  the header always renders (no title-less caller to guard for). */
  title: string
  /**
   * Optional node rendered inline after the title — a platform/status tag such
   * as the Computer Use panel's "macOS only" badge. Kept as a sibling of the
   * title text (not concatenated into it) so a `getByText(title)` query still
   * matches the header exactly.
   */
  badge?: React.ReactNode
  /**
   * An info tip beside the heading, for a section's introduction: what the
   * section is, said once for a reader who wants it. The same split as a row's
   * `hint`; a sentence the reader needs to make a choice stays visible instead.
   */
  hint?: string
  /** One line under the heading, at the heading's indent. For a sentence the
   *  reader needs before the rows; anything else belongs in `hint`. */
  description?: React.ReactNode
  /**
   * Render the header as a disclosure that hides its own rows until clicked.
   *
   * For a group whose rows are real and adjustable but which almost nobody needs
   * to see: a heading alone still spends the reader's attention on every row
   * under it, and a panel where a dozen rows carry equal weight gives no hint
   * which ones matter. Collapsed by default when set, because a disclosure that
   * starts open is just a heading.
   */
  collapsible?: boolean
  children?: React.ReactNode
}

/* ── A settings deep link that is still looking for its row ──
 *
 * The command palette and every `SettingRef` chip navigate to
 * `/settings/<tab>?highlight=<id>`, and `useSettingHighlight` then resolves that
 * id by QUERYING THE DOM for the row. A collapsed group renders no rows at all,
 * so a setting inside one is not merely hidden from the reader -- it is absent
 * from the document the deep link searches, and the link arrives on the tab
 * having revealed nothing and rung nothing.
 *
 * What is published is the SELECTOR the probe is looking for, not merely the fact
 * that it is looking. A bare "a link is pending" flag is not enough to decide
 * which group should answer it: every collapsible group on the page would see the
 * same flag and open, which trades "the target stays hidden" for "unrelated groups
 * open and stay open". The Voice tab is where that shows, because it nests one
 * collapsible group inside another -- "Start dictation with a key" is the last
 * child of "Fine-tuning" -- so a link to a Fine-tuning row opened both.
 *
 * Carried as a signal rather than read from the router HERE, because
 * `SettingsSection` is also rendered outside a router (Mochi's Electron renderer
 * under `apps/mochi`), where a `useSearchParams` in a shared primitive would
 * throw. `useSettingHighlight` owns the router and publishes.
 */
let deepLinkTarget: string | null = null
const deepLinkListeners = new Set<() => void>()

/**
 * Publish the CSS selector a settings deep link is probing for, or null when none
 * is outstanding. For containment only: the probe's own resolution is finer (it
 * disambiguates a repeated label by `occurrence`), and a group answers the coarser
 * question "is the row it wants inside me".
 */
export function setSettingsDeepLinkTarget(next: string | null): void {
  if (deepLinkTarget === next) return
  deepLinkTarget = next
  for (const cb of deepLinkListeners) cb()
}

function subscribeDeepLinkTarget(cb: () => void): () => void {
  deepLinkListeners.add(cb)
  return () => { deepLinkListeners.delete(cb) }
}
const readDeepLinkTarget = () => deepLinkTarget
const readDeepLinkOnServer = (): string | null => null

/** The selector a settings deep link is still probing for, or null. For a
 * panel that keeps rows mounted but hidden (a tab or disclosure of its own),
 * so it can reveal the one holding the target before the probe rings it. */
export function useSettingsDeepLinkTarget(): string | null {
  return React.useSyncExternalStore(subscribeDeepLinkTarget, readDeepLinkTarget, readDeepLinkOnServer)
}

export function SettingsSection({ title, badge, hint, description, collapsible, children }: SettingsSectionProps) {
  const [open, setOpen] = React.useState(false)
  const bodyId = React.useId()
  const bodyRef = React.useRef<HTMLDivElement>(null)
  const target = React.useSyncExternalStore(
    subscribeDeepLinkTarget, readDeepLinkTarget, readDeepLinkOnServer,
  )
  /* Which target this group has already answered for, so the two passes below
   * settle exactly once instead of oscillating. */
  const answered = React.useRef<string | null>(null)
  /* Whether THIS effect is the thing that opened the group. Only a group the probe
   * revealed may be closed again by the probe; one that was already open on arrival
   * -- the user opened it -- is never ours to close. */
  const revealed = React.useRef(false)
  /* REVEAL, then KEEP only if it was ours. A closed group cannot be asked what it
   * contains -- that is the whole defect -- so it opens far enough to be searched
   * and closes again in the same commit when the row is not inside it. Both passes
   * are layout effects, so the speculative open is never painted.
   *
   * Nesting resolves itself: a child group answers before its parent, and the
   * parent's query spans its whole subtree, so a target inside the nested group
   * keeps BOTH open while a target beside it keeps only the parent.
   *
   * A group the USER opened is left alone: when it is already `open` on arrival the
   * effect stamps `answered` and returns without ever touching `open` or the
   * `revealed` flag, so nothing here closes something a reader chose to see. Latched
   * for the owner, because the probe strips its parameter the moment it has rung
   * the row and a group that merely mirrored the signal would close on that tick.
   *
   * The signal's withdrawal clears `answered`, so the SAME deep link used a second
   * time reveals the group again instead of returning early on a stale stamp. */
  React.useLayoutEffect(() => {
    if (!collapsible) return
    /* Signal withdrawn. Clear BOTH latches: `answered` so the same link reveals the
     * group again on its next use, and `revealed` so a group the probe once opened
     * is no longer flagged probe-owned once the link that opened it is gone. Leaving
     * `revealed` latched would let a LATER deep link to a row elsewhere collapse a
     * group the reader had meanwhile re-opened by hand -- the exact case the
     * `revealed` guard exists to prevent. */
    if (!target) { answered.current = null; revealed.current = false; return }
    if (answered.current === target) return
    if (!open) { setOpen(true); revealed.current = true; return }
    answered.current = target
    /* Already open before the probe touched it: the user opened it. Stamp and leave
     * `open` and `revealed` alone -- a group the reader chose to see is never ours
     * to close, even when the target it wants is somewhere else. */
    if (!revealed.current) return
    /* Decided one microtask after the reveal, not inside it. A NESTED group opens
     * in a later commit than its parent -- the parent has to render its body before
     * the child exists to open at all -- so a parent that answered within this
     * commit would search a subtree whose child group has not rendered yet, find
     * nothing, and close over the very row it holds. React flushes the whole
     * cascade of layout-effect state updates synchronously in one task, so a
     * microtask queued here runs after every group has revealed and still before
     * the browser paints: nothing speculative is ever on screen. */
    let cancelled = false
    queueMicrotask(() => {
      if (cancelled) return
      if (!bodyRef.current?.querySelector(target)) { setOpen(false); revealed.current = false }
    })
    return () => { cancelled = true }
  }, [collapsible, target, open])
  return (
    <>
      {/* `mt-6` separates one section from the previous section's controls, so it
        * is load-bearing between sections — but the FIRST section on a tab has
        * nothing above it except the pane, which already owns the gap under the
        * narrow tab strip (SidePanelLayout's `pt-3`) and under the desktop header
        * (`pb-3`). `first:mt-0` drops it in exactly that case: the fragment adds
        * no DOM node, so every section's header is a sibling in one parent and
        * only the leading one matches. When a tab renders something of its own
        * above the first section, the header is no longer first and keeps the
        * margin — which is what it should do, because now something IS above it. */}
      <div className={`flex items-center gap-2 mt-6 mb-2 px-4 first:mt-0 ${SETTINGS_CONTENT_MAX_W}`}>
        {collapsible ? (
          /* The whole header is the control, not a chevron beside it: a 14px
             target next to a clickable-looking title is the classic near-miss.
             `<h4>` stays the heading so the document outline is unchanged and a
             `getByText(title)` query still matches. */
          <button
            type="button"
            className="flex items-center gap-1.5 bg-transparent border-none p-0 cursor-pointer text-left group"
            onClick={() => setOpen(o => !o)}
            aria-expanded={open}
            aria-controls={bodyId}
          >
            <ChevronRight
              size={14}
              className={`text-muted transition-transform group-hover:text-text ${open ? 'rotate-90' : ''}`}
            />
            <h4 className="text-base font-semibold text-text-strong">{title}</h4>
          </button>
        ) : (
          <h4 className="text-base font-semibold text-text-strong">{title}</h4>
        )}
        {hint && <InfoTip text={hint} placement="top" />}
        {badge}
      </div>
      {description && <div className={`-mt-1 mb-2 px-4 text-[12px] text-muted ${SETTINGS_CONTENT_MAX_W}`}>{description}</div>}
      {/* Unmounted rather than hidden when closed. A collapsed group exists to
          stop costing the reader attention, and an `aria-hidden` subtree still
          costs a screen-reader user their place in the tab order. */}
      {/* The body. Loose text a panel puts between the heading and its box (a
          note, an intro line) takes the heading's indent, so it lines up with
          the heading and the row text without each panel padding it. A child
          that is or holds a box keeps the full width. */}
      {collapsible
        ? open && <div id={bodyId} ref={bodyRef} className={`${SETTINGS_CONTENT_MAX_W} [&>:not([data-settings-card]):not(:has([data-settings-box]))]:px-4`}>{children}</div>
        : <div className={`${SETTINGS_CONTENT_MAX_W} [&>:not([data-settings-card]):not(:has([data-settings-box]))]:px-4`}>{children}</div>}
    </>
  )
}

/** The heading of one Settings group, and the line under it. Every group
 *  heading on a Settings page draws through this, so all of them sit at the
 *  same indent as the row text in the box below. `SettingsSection` uses it; a
 *  panel that lays its own group out uses it directly instead of a bare h4. */
export function SettingsGroupHeader({ title, description, size = 'md' }: { title: React.ReactNode; description?: React.ReactNode; size?: 'md' | 'sm' }) {
  return (
    <div className={`px-4 mb-2 ${SETTINGS_CONTENT_MAX_W}`}>
      {size === 'md'
        ? <h4 className="text-base font-semibold text-text-strong">{title}</h4>
        : <h5 className="text-sm font-semibold text-text-strong">{title}</h5>}
      {description && <div className="text-[12px] text-muted mt-0.5">{description}</div>}
    </div>
  )
}

/* ── Settings Card (borderless group of controls with vertical gap) ── */

/**
 * Delay step between successive settings cards' entrance animations, in ms.
 * Matches the stat-tile stagger ladder on the Overview page (`delay={i * 60}`
 * in `pages/OverviewPage.tsx`) so every Settings section rises with the same
 * rhythm as Overview.
 */
export const SETTINGS_CARD_STAGGER_MS = 60

/**
 * The group box every `SettingsCard` draws: one rounded, bordered card whose
 * DIRECT children are its rows, each padded, with a hairline between rows. A row
 * that renders nothing leaves no DOM node, so it draws no padding and no
 * hairline. A card nested inside a row loses its own bottom gap, so a card-shaped
 * row (an edition row, the KAS card) sits flush like the others.
 * `data-settings-box` lets a caller test for an empty box with `:empty`.
 *
 * The hairline selects "every row but the first" as `:not(:first-child)`, never
 * as `* + *`: a sibling combinator between two bare `*` gives the browser no
 * class to key on, so any node inserted anywhere restyles the whole subtree of
 * the elements after it (frontend-conventions.md, Styling). `:where()` keeps the
 * specificity of a single class.
 */
const SETTINGS_BOX_CLASS =
  'rounded-xl overflow-hidden border border-[var(--border)] bg-[var(--card)] empty:hidden '
  + '[&>*]:px-4 [&>*]:py-3 [&>:where(:not(:first-child))]:border-t [&>:where(:not(:first-child))]:border-[var(--border)] [&_[data-settings-card]]:mb-0'

export function SettingsCard({ index, children }: {
  /**
   * Ordinal of this card within its panel (0-based). Maps onto the shared
   * entrance-stagger ladder: the card's `animate-rise` entrance is delayed by
   * `index * SETTINGS_CARD_STAGGER_MS`. Omit (or pass 0) for the first card —
   * it rises immediately, exactly as before this prop existed. Purely
   * presentational; gaps in the sequence (from conditionally hidden cards)
   * are harmless. Under `prefers-reduced-motion` the delay is zeroed by the
   * `.animate-rise` rule in `index.css` (the global reduced-motion rule only
   * zeroes duration, and `backwards` fill would otherwise hold the card
   * invisible for its whole delay).
   */
  index?: number
  children: React.ReactNode
}) {
  return (
    // One group box under the section heading, rows split by hairlines
    // (page-layout.md, "Settings groups"). `data-settings-card` is a stable,
    // nonvisual crop anchor for the capture scripts.
    <div
      data-settings-card=""
      className={`mb-2 animate-rise ${SETTINGS_CONTENT_MAX_W}`}
      style={index ? { animationDelay: `${index * SETTINGS_CARD_STAGGER_MS}ms` } : undefined}
    >
      <div data-settings-box="" className={SETTINGS_BOX_CLASS}>
        {children}
      </div>
    </div>
  )
}

/* ── Stepper (numeric value with −/+ buttons) ── */

interface SettingsStepperProps {
  label: string
  description?: string
  hint?: string
  /**
   * The displayed value. `string` is allowed for an ALREADY-FORMATTED, localised
   * value — a duration rendered as "0.5 seconds", for instance, where the unit
   * word is part of the catalog string and cannot be split off into `suffix`
   * (locales place and inflect it differently). The stepper only interpolates
   * this, so the numeric state stays with the caller either way.
   */
  value: number | string
  onIncrement: () => void
  onDecrement: () => void
  onReset?: () => void
  /**
   * When given, the readout is a number input: the typed integer is passed
   * here on Enter or blur, and the box then shows `value` again, so the
   * caller's own clamp decides what is displayed. Non-numeric input, and
   * Escape, revert to `value`; so does empty input, unless `emptyValue` is
   * set. `min` / `max` are the input's native range.
   */
  onSet?: (value: number) => void
  /** With `onSet`: the value an emptied box commits, instead of reverting. */
  emptyValue?: number
  /** With `onSet`: what an empty box shows, for a `value` of `''`. */
  placeholder?: string
  min?: number
  max?: number
  suffix?: string
  disabled?: boolean
  /**
   * Disables the − button alone, for a value at the floor of its range: a
   * button that looks clickable and does nothing reads as broken. The + button
   * and the readout follow `disabled` only.
   */
  decrementDisabled?: boolean
  /**
   * The values the readout must fit, so its width, and the + button beside it,
   * stay put as the value changes: a localized "Default" and the largest
   * number, say. Each is rendered in the readout as an invisible, `aria-hidden`
   * copy stacked in one grid cell with the shown value, so the box takes the
   * width of the widest of them in whatever locale and font the reader has. A
   * fixed `ch` floor fits one language's word and still shifts for a longer
   * translation. The typed-input readout (`onSet`) sizes its input the same
   * way when this is given, and to a fixed width otherwise.
   */
  reserveWidthFor?: Array<string | number>
  /** Backend config key this stepper writes. */
  configKey?: string
}

/** Shared box of the centre readout, whether it is a reset button or plain text. */
const STEPPER_READOUT_CLASS = 'min-w-[56px] h-8 rounded-md border border-border bg-bg-elevated text-text-strong text-sm font-bold flex items-center justify-center px-2 transition-all'

/**
 * The readout's text. With `reserve`, the shown value and an invisible,
 * `aria-hidden` copy of each reserved value stack in one grid cell, so the cell
 * is as wide as the widest of them and the + button beside the box stays put as
 * the value changes; the copies take width and nothing else. Without `reserve`
 * the text renders bare, so a stepper that does not pass it keeps its DOM.
 */
function StepperReadoutText({ value, suffix, reserve }: { value: number | string; suffix: string; reserve?: Array<string | number> }) {
  if (!reserve?.length) return <>{value}{suffix}</>
  return (
    <span className="grid text-center">
      {reserve.map((reserved, i) => (
        <span key={i} className="col-start-1 row-start-1 invisible" aria-hidden="true">{reserved}{suffix}</span>
      ))}
      <span className="col-start-1 row-start-1">{value}{suffix}</span>
    </span>
  )
}

function StepperValueInput({ label, value, onSet, emptyValue, placeholder, min, max, suffix, disabled, reserve }: { label: string; value: number | string; onSet: (value: number) => void; emptyValue?: number; placeholder?: string; min?: number; max?: number; suffix: string; disabled?: boolean; reserve?: Array<string | number> }) {
  const [draft, setDraft] = React.useState(String(value))
  // A number input reports a half-typed value such as "-" as "", so `badInput`
  // is what tells it apart from a box the user emptied.
  const [badInput, setBadInput] = React.useState(false)
  // A new `value` from the caller (a step, or the result of a typed value)
  // replaces whatever is in the box.
  const [shown, setShown] = React.useState(value)
  if (shown !== value) {
    setShown(value)
    setDraft(String(value))
  }
  const commit = () => {
    const n = badInput ? NaN : draft.trim() === '' ? (emptyValue ?? NaN) : Math.round(Number(draft))
    setDraft(String(value))
    setBadInput(false)
    if (Number.isFinite(n) && n !== value) onSet(n)
  }
  const sized = !!reserve?.length
  const input = (
    <input
      type="number"
      inputMode="numeric"
      min={min}
      max={max}
      step={1}
      disabled={disabled}
      aria-label={label}
      placeholder={placeholder}
      value={draft}
      onChange={e => {
        setDraft(e.target.value)
        setBadInput(e.target.validity.badInput)
      }}
      onBlur={commit}
      onKeyDown={e => {
        if (e.key === 'Enter') commit()
        else if (e.key === 'Escape') {
          setDraft(String(value))
          setBadInput(false)
        }
      }}
      className={`${sized ? 'col-start-1 row-start-1 w-full min-w-0 text-center' : 'w-[3.5ch] text-right'} bg-transparent text-text-strong text-sm font-bold placeholder:text-text-strong [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none disabled:cursor-not-allowed`}
    />
  )
  return (
    <span className={`${STEPPER_READOUT_CLASS} gap-0.5 cursor-text hover:border-border-strong focus-within:border-accent${disabled ? ' opacity-40' : ''}`}>
      {sized ? (
        <span className="grid">
          {reserve!.map((reserved, i) => (
            <span key={i} className="col-start-1 row-start-1 invisible" aria-hidden="true">{reserved}</span>
          ))}
          {input}
        </span>
      ) : input}
      {suffix && <span aria-hidden="true">{suffix}</span>}
    </span>
  )
}

export function SettingsStepper({ label, description, hint, value, onIncrement, onDecrement, onReset, onSet, emptyValue, placeholder, min, max, suffix = '', disabled, decrementDisabled = false, reserveWidthFor, configKey }: SettingsStepperProps) {
  return (
    <SettingsField label={label} description={description} hint={hint} configKey={configKey} layout="inline-auto">
      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={disabled || decrementDisabled}
          className="w-8 h-8 rounded-md border border-border bg-bg-elevated text-text text-sm font-bold cursor-pointer hover:border-border-strong hover:bg-bg-hover transition-all disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center"
          onClick={onDecrement}
          aria-label={i18nT('components.settings.decrease')}
        >−</button>
        {onSet ? (
          <StepperValueInput label={label} value={value} onSet={onSet} emptyValue={emptyValue} placeholder={placeholder} min={min} max={max} suffix={suffix} disabled={disabled} reserve={reserveWidthFor} />
        ) : onReset ? (
          <button
            type="button"
            disabled={disabled}
            className={`${STEPPER_READOUT_CLASS} cursor-pointer hover:border-accent hover:text-accent disabled:opacity-40 disabled:cursor-not-allowed`}
            onClick={onReset}
            title={i18nT('components.settings.click_to_reset')}
          ><StepperReadoutText value={value} suffix={suffix} reserve={reserveWidthFor} /></button>
        ) : (
          // A readout with nothing to click is text, not a disabled button: a
          // `<button disabled>` draws dimmed with a not-allowed cursor and reads
          // to assistive tech as an action that is refused, when nothing was
          // ever on offer. It only dims with the rest of the control.
          <span className={`${STEPPER_READOUT_CLASS} cursor-default${disabled ? ' opacity-40' : ''}`}><StepperReadoutText value={value} suffix={suffix} reserve={reserveWidthFor} /></span>
        )}
        <button
          type="button"
          disabled={disabled}
          className="w-8 h-8 rounded-md border border-border bg-bg-elevated text-text text-sm font-bold cursor-pointer hover:border-border-strong hover:bg-bg-hover transition-all disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center"
          onClick={onIncrement}
          aria-label={i18nT('components.settings.increase')}
        >+</button>
      </div>
    </SettingsField>
  )
}

/* ── Button Group (mutually exclusive options) ── */

interface SettingsButtonGroupProps {
  label: string
  description?: string
  hint?: string
  value: string
  /** `disabled` on an OPTION keeps the choice visible but unselectable — for a
   *  value this build knows about but cannot serve. Renders the full vocabulary
   *  rather than hiding it, so the control does not silently change shape
   *  between builds and the reader can see what exists.
   *
   *  `describedById` is the id of the element stating WHY, wired through as
   *  `aria-describedby`. Dimming carries "unavailable" visually and through the
   *  native `disabled` state, but the REASON is usually rendered outside this
   *  component, where proximity alone associates them — which is no association
   *  at all for a screen reader. Optional, so a group whose options are all
   *  selectable stays unchanged. */
  options: {
    value: string
    label: string
    icon?: React.ReactNode
    disabled?: boolean
    describedById?: string
  }[]
  onChange: (value: string) => void
  disabled?: boolean
  /** Backend config key this button group writes. */
  configKey?: string
}

export function SettingsButtonGroup({ label, description, hint, value, options, onChange, disabled, configKey }: SettingsButtonGroupProps) {
  return (
    <SettingsField label={label} description={description} hint={hint} configKey={configKey} layout="inline-auto">
      {/* Segmented control: a RECESSED track (`bg-accent`) holding a RAISED
          selected thumb (`bg-elevated` + border + shadow).

          The track must not be `bg-elevated`: in every light theme
          `--bg-elevated` and `--card` are both #ffffff (index.css), so a
          `bg-elevated` track is invisible against the card it sits on. Only
          the selected pill rendered, reading as one stray grey box rather
          than as a three-way choice. `bg-accent` is a step DARKER than the
          card in light themes and darker than `bg-elevated` in dark ones, so
          the track is visible in both directions.

          Selection is conveyed by elevation + weight, not by hue alone, so it
          survives a theme whose accent is low-contrast — and the radio group's
          `aria-checked` carries it to screen readers, which no amount of
          styling does. */}
      <RadioChipGroup ariaLabel={label} className="inline-flex flex-wrap items-center gap-0.5 p-[3px] rounded-lg border border-border bg-bg-accent w-fit max-w-full">
        {options.map(o => (
          <button
            key={o.value}
            type="button"
            disabled={disabled || o.disabled}
            {...radioChip(value === o.value)}
            aria-describedby={o.describedById}
            className={`flex items-center gap-1.5 px-3 py-[5px] rounded-md text-[13px] cursor-pointer border transition-colors ${
              value === o.value
                ? 'bg-bg-elevated text-text-strong border-border-strong shadow-sm font-semibold'
                : 'bg-transparent text-muted border-transparent font-medium hover:text-text-strong'
            } disabled:opacity-40 disabled:cursor-not-allowed`}
            onClick={() => !disabled && onChange(o.value)}
          >
            {o.icon}
            {o.label}
          </button>
        ))}
      </RadioChipGroup>
    </SettingsField>
  )
}
