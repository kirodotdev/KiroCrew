/**
 * Settings -> Security -> Denied commands: the built-in rules grouped by
 * category, the user's own deny patterns and their add input, and the
 * policy-pinned and floor-locked tooltip keys the governance rows reuse. One of
 * the sections `SecurityPanel.tsx` composes.
 */
import { useEffect, useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Lock, AlertTriangle, ChevronRight, ChevronDown, Plus, Trash2 } from 'lucide-react'
import { useImeGuard } from '../../hooks/useImeGuard'
import { Badge, Btn, Input, Toggle, Checkbox } from '../../components/ui'
import { SettingsSection, SettingsCard } from '../../components/settings'
import Modal from '../../components/Modal'
import InfoTip from '../../components/InfoTip'
import {
  api, type DeniedCommandsData, type DeniedCommandRule, type DeniedUserRule,
} from '../../api/client'
import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
import { trustFailureMessage } from './SecurityPanelTrust'

/** Tooltip on every control an enterprise policy pins. A catalog KEY, resolved at each
 *  of its three render sites for the reason given on `FEATURE_LABEL_KEY`
 *  (SecurityPanelLayers.tsx): at module scope `i18nT()` would resolve once at boot. */
export const PINNED_TOOLTIP_KEY = 'pages.settings.securityPanel.pinned_by_policy'
const FLOOR_TOOLTIP_KEY = 'pages.settings.securityPanel.enforced_by_floor'

/** A rule is locked (forced-on, non-toggleable) when governance pins it or an
 *  always-on floor enforces it. `lock_reason` picks the tooltip wording. */
function isRuleLocked(rule: DeniedCommandRule): boolean {
  return rule.pinned || rule.lock_reason === 'floor'
}

/* ── Denied Commands ── */

/** Human-readable category header, e.g. "aws-destructive" → "Aws Destructive". */
function categoryLabel(category: string): string {
  return category
    .split('-')
    .map(w => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(' ')
}

/** A single built-in denied-command rule row (Card A). */
function BuiltinDenyRow({ rule, dimmed, onToggle, error, errorHandoff }: { rule: DeniedCommandRule; dimmed: boolean; onToggle: (next: boolean) => void; error?: string; errorHandoff?: boolean }) {
  const [open, setOpen] = useState(false)
  const Chevron = open ? ChevronDown : ChevronRight
  return (
    <div className="py-2">
      <div className="flex items-center gap-2.5">
        <button
          type="button"
          className="shrink-0 text-muted hover:text-text transition-colors bg-transparent border-none cursor-pointer p-0"
          onClick={() => setOpen(o => !o)}
          aria-label={open ? i18nT('pages.settings.securityPanel.hide_pattern') : i18nT('pages.settings.securityPanel.show_pattern')}
          aria-expanded={open}
        >
          <Chevron size={14} />
        </button>
        <span className="flex-1 min-w-0 text-[13px] text-text">{rule.description}</span>
        {rule.source === 'edition' && (
          /* The seam's whole point is that a contributed rule is DISCOVERABLE
             rather than an invisible refusal, so the row has to say where it came
             from — otherwise an operator cannot tell it from a shipped rule and
             cannot tell whom to ask about it.
             The badge is a plain LABEL, not a control: the row already spends its
             two-action budget (`max-two-buttons-per-row`) on the expand chevron
             and the toggle, and an InfoTip trigger here would be a third button.
             The explanation lives in the expand panel below instead, which makes
             it readable rather than hover-only. */
          <span className="shrink-0 rounded bg-bg-elevated border border-border px-1.5 py-0.5 text-[11px] font-mono text-muted">
            {i18nT('pages.settings.securityPanel.edition_rule_badge')}
          </span>
        )}
        {isRuleLocked(rule) ? (
          <span className="flex items-center gap-1.5 shrink-0">
            <Lock size={13} className="text-muted" />
            {/* Two literal i18nT call sites (not one with a computed key) so the
                static key checker keeps verifying both catalog keys. */}
            <InfoTip text={rule.lock_reason === 'floor' ? i18nT(FLOOR_TOOLTIP_KEY) : i18nT(PINNED_TOOLTIP_KEY)} />
            <Toggle checked disabled onChange={() => { /* locked — forced on */ }} label={rule.description} />
          </span>
        ) : (
          <span className={`shrink-0 ${dimmed ? 'opacity-50' : ''}`}>
            <Toggle checked={rule.enabled} onChange={onToggle} label={rule.description} />
          </span>
        )}
      </div>
      {/* The rejected write for THIS row, beside the switch that snapped back —
          a banner at the top of a long rule list is off-screen from the toggle
          that was pressed. `errorHandoff` is decided by the section, which knows
          whether Card B's add-pattern draft (panel-shell state the navigation
          would discard) is empty. No hand-off otherwise: the deny-pattern draft. */}
      <ErrorNotice variant="inline" className="mt-1 ml-6" message={error} askAgent={!!errorHandoff} testId={error ? `denied-rule-error-${rule.id}` : undefined} />
      {open && (
        <>
          {rule.source === 'edition' && (
            <p className="mt-1.5 ml-6 text-[12px] text-muted">
              {i18nT('pages.settings.securityPanel.edition_rule_tooltip')}
            </p>
          )}
          <pre className="mt-1.5 ml-6 overflow-x-auto rounded-md bg-bg-elevated border border-border px-2.5 py-1.5 text-[12px] font-mono text-muted whitespace-pre-wrap break-all">{rule.pattern}</pre>
        </>
      )}
    </div>
  )
}

/** A collapsible category group (Card A) — folds its rules under a header that
 *  shows the category name, an enabled/total count, and a pinned-lock hint.
 *  Collapsed by default to keep the 137-rule panel scannable.
 *
 *  `rules` is what renders; `allRules` is the category as SHIPPED and is what the
 *  count badge, the pinned-lock hint and the all-off warning are computed from.
 *  They differ only while a search filter is active, and the distinction is
 *  load-bearing: reporting "2/2" for two search hits inside a 21-rule category
 *  would tell the reader the gate is 19 rules smaller than it is. */
function CategoryGroup({
  category,
  rules,
  allRules,
  open,
  onToggleOpen,
  disableAll,
  onRuleToggle,
  collapsible = true,
  rowError,
  errorHandoff,
}: {
  category: string
  rules: DeniedCommandRule[]
  allRules?: DeniedCommandRule[]
  open: boolean
  onToggleOpen: () => void
  disableAll: boolean
  onRuleToggle: (rule: DeniedCommandRule, next: boolean) => void
  /** False while a search filter is active: matches are force-open, so a
   *  chevron would be a control that visibly does nothing. Render a plain
   *  header instead of an inert button. */
  collapsible?: boolean
  /** The one rejected rule write, if any, keyed to the row it belongs to. */
  rowError?: { id: string; message: string } | null
  errorHandoff?: boolean
}) {
  const Chevron = open ? ChevronDown : ChevronRight
  const counted = allRules ?? rules
  const enabled = counted.filter(r => r.enabled).length
  const locked = counted.some(isRuleLocked)
  // "off" when every non-locked rule in the group is disabled.
  const allOff = enabled === 0
  return (
    <div className="border-t border-border first:border-t-0">
      {collapsible ? (
        <button
          type="button"
          className="w-full flex items-center gap-2 py-2.5 bg-transparent border-none cursor-pointer text-left group"
          onClick={onToggleOpen}
          aria-expanded={open}
          aria-label={open
            ? i18nT('pages.settings.securityPanel.collapse_category_rules', { category: categoryLabel(category) })
            : i18nT('pages.settings.securityPanel.expand_category_rules', { category: categoryLabel(category) })}
        >
          <Chevron size={14} className="shrink-0 text-muted group-hover:text-text transition-colors" />
          <span className="text-[11px] font-semibold uppercase tracking-[.04em] text-muted group-hover:text-text transition-colors">
            {categoryLabel(category)}
          </span>
          {locked && <Lock size={12} className="shrink-0 text-muted" />}
          <span className="flex-1" />
          {allOff && !locked && (
            <span className="text-[11px] text-warn">{i18nT('pages.settings.securityPanel.off')}</span>
          )}
          <Badge variant="muted" className="tabular-nums">{enabled}/{counted.length}</Badge>
        </button>
      ) : (
        <div className="w-full flex items-center gap-2 py-2.5 pl-[22px]">
          <span className="text-[11px] font-semibold uppercase tracking-[.04em] text-muted">
            {categoryLabel(category)}
          </span>
          {locked && <Lock size={12} className="shrink-0 text-muted" />}
          <span className="flex-1" />
          {allOff && !locked && (
            <span className="text-[11px] text-warn">{i18nT('pages.settings.securityPanel.off')}</span>
          )}
          <Badge variant="muted" className="tabular-nums">{enabled}/{counted.length}</Badge>
        </div>
      )}
      {open && (
        <div className="divide-y divide-border pb-1.5 pl-6">
          {rules.map(rule => (
            <BuiltinDenyRow
              key={rule.id}
              rule={rule}
              dimmed={disableAll && !isRuleLocked(rule)}
              onToggle={next => onRuleToggle(rule, next)}
              error={rowError?.id === rule.id ? rowError.message : undefined}
              errorHandoff={errorHandoff}
            />
          ))}
        </div>
      )}
    </div>
  )
}

/** A single user-authored denied-command row (Card B). */
function CustomDenyRow({ rule, onToggle, onDelete, error, errorHandoff }: { rule: DeniedUserRule; onToggle: (next: boolean) => void; onDelete: () => void; error?: string; errorHandoff?: boolean }) {
  return (
    <div className="py-2">
      <div className="flex items-center gap-2.5">
        <div className="flex-1 min-w-0">
          <code className="block overflow-x-auto text-[12px] font-mono text-text whitespace-pre-wrap break-all">{rule.pattern}</code>
          {/* The note is what the agent is shown when this rule fires, so surface it
              next to the pattern it explains rather than hiding it behind an edit
              affordance that does not exist (rules are create-only). */}
          {rule.note ? <p className="mt-0.5 text-[11px] text-muted whitespace-pre-wrap break-words">{rule.note}</p> : null}
        </div>
        <Toggle checked={rule.enabled} onChange={onToggle} label={rule.pattern} />
        <button
          type="button"
          className="shrink-0 text-muted hover:text-danger transition-colors bg-transparent border-none cursor-pointer p-1"
          onClick={onDelete}
          aria-label={i18nT('pages.settings.securityPanel.delete_pattern', { name: rule.pattern })}
        >
          <Trash2 size={14} />
        </button>
      </div>
      {/* The rejected toggle/delete for THIS row, beside the controls that
          caused it. `errorHandoff` is the section's call on whether the
          add-pattern draft below is empty (No hand-off otherwise: that draft). */}
      <ErrorNotice variant="inline" className="mt-1" message={error} askAgent={!!errorHandoff} testId={error ? `denied-rule-error-${rule.id}` : undefined} />
    </div>
  )
}

/** Add-a-custom-pattern input with client-side RegExp validation (Card B).
 *
 *  `value` and `note` are CONTROLLED from the panel shell rather than held here,
 *  because the rules section unmounts when the reader picks another rail section
 *  — local state would silently discard a half-typed deny pattern or note.
 *  `error` stays local: it is derived from the value and costs nothing to
 *  recompute. */
function AddDenyInput({ value, onChange, note, onNoteChange, onAdd, busy, submitError }: { value: string; onChange: (next: string) => void; note: string; onNoteChange: (next: string) => void; onAdd: (pattern: string, note: string) => void; busy: boolean; submitError: string }) {
  const ime = useImeGuard()
  const [error, setError] = useState('')

  const submit = () => {
    const pattern = value.trim()
    if (!pattern) return
    try {
      new RegExp(pattern)
    } catch (e) {
      setError(e instanceof Error ? e.message : i18nT('pages.settings.securityPanel.invalid_regular_expression'))
      return
    }
    setError('')
    // Deliberately does NOT clear the drafts. The server can still reject this
    // (a note carrying the refusal prefix is a 400), and clearing here would
    // discard text the operator has to retype. The parent clears on SUCCESS.
    onAdd(pattern, note.trim())
  }

  return (
    <div className="pt-1.5">
      <div className="flex items-center gap-2">
        <Input
          value={value}
          onChange={e => { onChange(e.target.value); if (error) setError('') }}
          {...ime.bindEnter({ onEnter: submit })}
          placeholder={i18nT('pages.settings.securityPanel.add_a_custom_deny_pattern_regex_e_g_rm_rf_tmp_mi')}
          aria-label={i18nT('pages.settings.securityPanel.custom_deny_pattern')}
        />
        <Btn primary onClick={submit} disabled={busy || !value.trim()}>
          <Plus size={14} />
          {i18nT('pages.settings.securityPanel.add')}
        </Btn>
      </div>
      {/* Optional. Whatever is typed here is what the agent reads instead of the
          raw regex when the rule fires, so it is remediation ("use --maxdepth"),
          not a label. Capped server-side at 200 chars; mirrored here so the
          field stops accepting input rather than 400-ing on submit. */}
      <div className="mt-1.5">
        <Input
          value={note}
          onChange={e => onNoteChange(e.target.value)}
          {...ime.bindEnter({ onEnter: submit })}
          placeholder={i18nT('pages.settings.securityPanel.optional_why_shown_to_the_agent_when_this_rule_fir')}
          aria-label={i18nT('pages.settings.securityPanel.custom_deny_note')}
          maxLength={200}
        />
      </div>
      {/* Two surfaces, because they have two sources. `error` is invalid-regex
          feedback on the input the user is still typing — a form hint, not a
          failure to diagnose, so it stays plain text. `submitError` carries a
          SERVER rejection (e.g. a note carrying the refusal prefix): without it
          the 400 is silent, the list does not change, and Add looks like it did
          nothing. Local hint wins — it is about what is on screen. */}
      {error ? (
        <p className="text-[12px] text-danger mt-1.5 mb-0" data-testid="deny-pattern-hint">{error}</p>
      ) : (
        /* No hand-off: the pattern (`value`) and note (`note`) the user typed are
           kept on a rejected add so they can be corrected — the navigation would
           discard both. */
        <ErrorNotice message={submitError} className="mt-1.5" />
      )}
    </div>
  )
}

/* ── Confirm modal target ── */
type ConfirmTarget =
  | { kind: 'builtin'; id: string; description: string }
  | { kind: 'disable-all' }

/* ── Denied Commands section ────────────────────────────────────────────────
 *
 * Owns its own query, mutations and confirm modal so the rail can mount it on
 * demand: the built-in rule table is by far the panel's largest surface (137
 * rules across 10 categories) and there is no reason to build it while the
 * reader is looking at something else.
 */
export function DeniedCommandsSection({ draft, onDraftChange, noteDraft, onNoteDraftChange }: { draft: string; onDraftChange: (next: string) => void; noteDraft: string; onNoteDraftChange: (next: string) => void }) {
  const qc = useQueryClient()
  const { data: dc, isError: dcError } = useQuery<DeniedCommandsData>({ queryKey: ['denied-commands'], queryFn: api.deniedCommands })
  // One surface for the four rule writes below, keyed to the row (or the
  // disable-all switch) whose write was rejected so it renders BESIDE that
  // control — a long rule list puts a top-of-card banner off-screen from the
  // toggle that snapped back. Each used to fail silently (the 409 reason on a
  // pinned rule was invalidated away; the others had no onError at all).
  const [ruleError, setRuleError] = useState<{ id: string; message: string } | null>(null)
  // The disable-all switch is not a row, so its rejection has its own slot
  // rather than a sentinel id in `ruleError`.
  const [disableAllError, setDisableAllError] = useState('')

  const [confirm, setConfirm] = useState<ConfirmTarget | null>(null)
  const [ack, setAck] = useState(false)
  // Category accordion state. Categories are collapsed by default — an id in
  // this set is EXPANDED. Keeps the 137-rule list scannable.
  const [expandedCats, setExpandedCats] = useState<Set<string>>(() => new Set())
  const [filter, setFilter] = useState('')

  // The acknowledgment checkbox resets whenever the modal opens or closes.
  useEffect(() => { setAck(false) }, [confirm])

  const applySnapshot = (snap: DeniedCommandsData) => {
    qc.setQueryData(['denied-commands'], snap)
    qc.invalidateQueries({ queryKey: ['denied-commands'] })
  }

  const toggleBuiltin = useMutation({
    mutationFn: (v: { id: string; enabled: boolean }) => api.toggleBuiltinDeniedCommand(v.id, v.enabled),
    onMutate: () => setRuleError(null),
    onSuccess: applySnapshot,
    // A rejected toggle (409 on a pinned or floor-enforced rule — reachable
    // from a stale cached bundle) must repaint the true locked state instead
    // of leaving the optimistic-looking switch position on screen — AND say
    // why, or the snap-back reads as a switch that never moved.
    onError: (err: unknown, v) => {
      setRuleError({ id: v.id, message: trustFailureMessage(err) })
      qc.invalidateQueries({ queryKey: ['denied-commands'] })
    },
  })
  const setDisableAll = useMutation({
    mutationFn: (value: boolean) => api.setDeniedCommandsDisableAll(value),
    onMutate: () => { setRuleError(null); setDisableAllError('') },
    onSuccess: applySnapshot,
    onError: (err: unknown) => setDisableAllError(trustFailureMessage(err)),
  })
  const addUser = useMutation({
    mutationFn: ({ pattern, note }: { pattern: string; note: string }) =>
      api.addUserDeniedCommand(pattern, note),
    // Clear the drafts only once the write landed AND only if the form is still
    // provably untouched since submit -- BOTH fields matching what was sent. This
    // is deliberately all-or-nothing rather than per-field:
    //   - a rejected add (a note carrying the refusal prefix is a 400) must leave
    //     the operator's text in place to correct, not make them retype it;
    //   - the inputs stay editable while the add is in flight, so an operator who
    //     starts the next rule would otherwise have it erased on resolution;
    //   - clearing per-field loses a note the operator DELIBERATELY reused for the
    //     next rule while retyping only the pattern.
    // The condition is closed, not a heuristic: the single state we clear in is
    // "nothing new was typed", so no newer input can be discarded in any other.
    // Compared trimmed, because submit() sends the trimmed values.
    onSuccess: (snap: DeniedCommandsData, vars: { pattern: string; note: string }) => {
      applySnapshot(snap)
      const untouched = draft.trim() === vars.pattern && noteDraft.trim() === vars.note
      if (untouched) {
        onDraftChange('')
        onNoteDraftChange('')
      }
    },
  })
  const toggleUser = useMutation({
    mutationFn: (v: { id: string; enabled: boolean }) => api.toggleUserDeniedCommand(v.id, v.enabled),
    onMutate: () => setRuleError(null),
    onSuccess: applySnapshot,
    onError: (err: unknown, v) => setRuleError({ id: v.id, message: trustFailureMessage(err) }),
  })
  const deleteUser = useMutation({
    mutationFn: (id: string) => api.deleteUserDeniedCommand(id),
    onMutate: () => setRuleError(null),
    onSuccess: applySnapshot,
    onError: (err: unknown, id) => setRuleError({ id, message: trustFailureMessage(err) }),
  })

  const grouped = useMemo(() => {
    const groups: Record<string, DeniedCommandRule[]> = {}
    for (const rule of dc?.builtins ?? []) {
      (groups[rule.category] ??= []).push(rule)
    }
    return groups
  }, [dc])

  const query = filter.trim().toLowerCase()
  const filtering = query.length > 0
  // Rule ids are hyphen-joined slugs (`aws-destructive-ec2-run-instances`), but
  // the same id is naturally read -- and typed -- as `<category>.<slug>`. Accept
  // that dotted spelling too so a copied id always finds its rule.
  const idQuery = query.replace(/\./g, '-')

  /** Categories reduced to their matching rules. A category whose NAME matches
   *  keeps all of its rules, so searching "credential" reads as a category jump
   *  rather than a partial list. Matching is additive across the category key,
   *  its display label, and each rule's id, description and pattern. */
  const visibleGroups = useMemo(() => {
    if (!query) return grouped
    const out: Record<string, DeniedCommandRule[]> = {}
    for (const [category, rules] of Object.entries(grouped)) {
      const hits = category.toLowerCase().includes(idQuery) || categoryLabel(category).toLowerCase().includes(query)
        ? rules
        : rules.filter(r =>
          r.id.toLowerCase().includes(idQuery)
          || r.description.toLowerCase().includes(query)
          || r.pattern.toLowerCase().includes(query))
      if (hits.length > 0) out[category] = hits
    }
    return out
  }, [grouped, query, idQuery])

  const visibleUserRules = useMemo(() => {
    const rules = dc?.user_added ?? []
    if (!query) return rules
    return rules.filter(r =>
      r.id.toLowerCase().includes(idQuery)
      || r.pattern.toLowerCase().includes(query))
  }, [dc, query, idQuery])

  const matchedRules = Object.values(visibleGroups).reduce((n, rules) => n + rules.length, 0)
  const nothingMatches = filtering && matchedRules === 0 && visibleUserRules.length === 0

  const disableAll = dc?.disable_all ?? false
  const governanceLocked = dc?.governance_locked ?? false

  // Enabling a rule (or re-enabling all built-ins) is immediate; disabling
  // opens a confirm modal. `next` is the toggle's new value.
  const onBuiltinToggle = (rule: DeniedCommandRule, next: boolean) => {
    if (next) toggleBuiltin.mutate({ id: rule.id, enabled: true })
    else setConfirm({ kind: 'builtin', id: rule.id, description: rule.description })
  }
  const onDisableAllToggle = (next: boolean) => {
    if (next) setConfirm({ kind: 'disable-all' })
    else setDisableAll.mutate(false)
  }
  const runConfirm = () => {
    if (!confirm) return
    if (confirm.kind === 'builtin') toggleBuiltin.mutate({ id: confirm.id, enabled: false })
    else setDisableAll.mutate(true)
    setConfirm(null)
  }

  const confirmBody = !confirm ? '' : confirm.kind === 'disable-all'
    ? i18nT('pages.settings.securityPanel.disabling_all_built_in_denies_removes_kirocrew_s')
    : i18nT('pages.settings.securityPanel.disabling_weakens_protection', { name: confirm.description })

  // The one askAgent decision for every notice in this section: the add-pattern
  // form's `draft` / `noteDraft` live in the panel shell and the hand-off
  // navigation would discard them, so the button is offered only while both
  // are empty.
  const errorHandoff = !draft.trim() && !noteDraft.trim()

  // Whether the row that owns `ruleError` is on screen right now. A write can be
  // rejected after the user has typed a filter that drops the row, or folded its
  // category — the row-level notice then has nowhere to render, so the section
  // falls back to a top-level notice for exactly that case (and only that case,
  // so a visible row never shows its failure twice).
  const ruleErrorRowVisible = (() => {
    if (!ruleError) return true
    for (const [category, rules] of Object.entries(visibleGroups)) {
      if (rules.some(r => r.id === ruleError.id)) return filtering || expandedCats.has(category)
    }
    return visibleUserRules.some(r => r.id === ruleError.id)
  })()

  return (
    <SettingsSection title={i18nT('pages.settings.securityPanel.denied_commands')}>
      {/* askAgent is gated on the add form: `draft` / `noteDraft` (the custom
          pattern and note, held by the panel shell) are unsaved input, so the
          hand-off is offered only while both are empty. No hand-off otherwise:
          the deny-pattern draft. A failed list read must not render as empty
          groups. Rejected rule writes render beside their own row (see
          `rowError` below), not here. */}
      {dcError && (
        <ErrorNotice
          className="mb-3"
          message={i18nT('pages.settings.securityPanel.denied_commands_load_failed')}
          askAgent={errorHandoff}
        />
      )}
      {/* Fallback for a rejected rule write whose row is filtered out or folded
          away (see `ruleErrorRowVisible`): the failure still needs a surface. */}
      {ruleError && !ruleErrorRowVisible && (
        <ErrorNotice
          className="mb-3"
          message={ruleError.message}
          onDismiss={() => setRuleError(null)}
          askAgent={errorHandoff}
          testId="denied-rule-error-hidden-row"
        />
      )}
      {/* The tier's REACH, above BOTH cards: these rules read the command line
        * a tool call carries and not the body of a program that line runs, so an
        * operator reading either the built-in list or their own patterns as a
        * fence over-trusts it. Card B is where a pattern is authored, so the note
        * cannot live inside Card A. Same disclosure as the "What this tier cannot
        * see" section of `src/kiro_crew/docs/blocked-commands.md`, which is where
        * the closure mechanism (the OS-level sandbox) is explained. */}
      <div className="text-[12px] text-muted mb-2 leading-relaxed" data-testid="denied-indirection-limit">
        {i18nT('pages.settings.securityPanel.rules_read_the_command_line_not_the_program_body')}
      </div>

      {/* Card A — Built-in denies */}
      <SettingsCard>
        {/* data-setting-label: deep-link anchor for the manual registry entry
            (settingsManual.ts) — the highlight hook queries the rendered label. */}
        <div className="flex items-center justify-between py-1.5" data-setting-label={i18nT('pages.settings.securityPanel.denied_commands')}>
          <div className="flex-1 min-w-0 mr-4">
            <div className="flex items-center gap-1.5">
              <span className="text-[13px] font-semibold text-text">{i18nT('pages.settings.securityPanel.disable_all_built_in_denies')}</span>
              {governanceLocked && <Lock size={13} className="text-muted" />}
            </div>
            <div className="text-[12px] text-muted mt-0.5 leading-relaxed">
              {governanceLocked
                ? i18nT('pages.settings.securityPanel.turn_off_every_rule_governance_locked')
                : i18nT('pages.settings.securityPanel.turn_off_every_rule')}
            </div>
          </div>
          {/* Disable-all stays available even when governance-locked: the
              backend keeps policy-pinned rules enforced under disable_all
              (compute_effective_denied), so a pin on one rule must not block
              opting every OTHER (unpinned) rule out. When locked, show the
              pinned-policy tooltip alongside the still-functional toggle. */}
          <span className="flex items-center gap-1.5 shrink-0">
            {governanceLocked && <InfoTip text={i18nT(PINNED_TOOLTIP_KEY)} />}
            <Toggle checked={disableAll} onChange={onDisableAllToggle} disabled={!dc} label={i18nT('pages.settings.securityPanel.disable_all_built_in_denies')} />
          </span>
        </div>
        {/* The rejected disable-all write, beside its own switch. */}
        <ErrorNotice
          variant="inline"
          className="mb-1"
          message={disableAllError}
          onDismiss={() => setDisableAllError('')}
          askAgent={errorHandoff}
          testId={disableAllError ? 'denied-disable-all-error' : undefined}
        />

        <div className="text-[12px] text-muted mt-1 mb-2 leading-relaxed">
          {i18nT('pages.settings.securityPanel.disabling_a_rule_that_overlaps_an_always_on_cont')}
        </div>

        {!dc ? (
          <div className="text-[12px] text-muted py-2">{i18nT('pages.settings.securityPanel.loading_built_in_rules')}</div>
        ) : (
          <>
            <div className="mb-1.5">
              <Input
                value={filter}
                onChange={e => setFilter(e.target.value)}
                placeholder={i18nT('pages.settings.securityPanel.search_rules_placeholder')}
                aria-label={i18nT('pages.settings.securityPanel.search_rules_placeholder')}
              />
            </div>
            <div className="flex items-center justify-between mt-1 mb-0.5">
              {/* While filtering, report matched-of-total as a RATIO — but keep
                  every category badge on its full enabled/total, so a filter can
                  never make the gate read as smaller than it is. A ratio also
                  sidesteps count grammar, so this needs no plural forms. */}
              <span className="text-[11px] text-muted tabular-nums">
                {filtering
                  ? <>{matchedRules} / {dc.builtins.length} {i18nT('pages.settings.securityPanel.rules')}</>
                  : <>{Object.keys(grouped).length} {i18nT('pages.settings.securityPanel.categories')} {dc.builtins.length} {i18nT('pages.settings.securityPanel.rules')}</>}
              </span>
              {/* Hidden while filtering: matches render open regardless, so both
                  controls would record state the user cannot see take effect. */}
              {!filtering && (
                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    className="text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer p-0 transition-colors"
                    onClick={() => setExpandedCats(new Set(Object.keys(grouped)))}
                  >
                    {i18nT('pages.settings.securityPanel.expand_all')}
                  </button>
                  <button
                    type="button"
                    className="text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer p-0 transition-colors"
                    onClick={() => setExpandedCats(new Set())}
                  >
                    {i18nT('pages.settings.securityPanel.collapse_all')}
                  </button>
                </div>
              )}
            </div>
            <div>
              {Object.entries(visibleGroups).map(([category, rules]) => (
                <CategoryGroup
                  key={category}
                  category={category}
                  rules={rules}
                  // The badge's denominator is the SHIPPED category, never the
                  // filtered slice: "2/2" on a search hit inside a 21-rule
                  // category would misreport how much of that category is
                  // enforced.
                  allRules={grouped[category]}
                  // A filter that leaves its hits folded away is a filter that
                  // did nothing, so matches render open regardless of the
                  // accordion state the user left behind.
                  open={filtering || expandedCats.has(category)}
                  onToggleOpen={() => setExpandedCats(prev => {
                    const next = new Set(prev)
                    if (next.has(category)) next.delete(category)
                    else next.add(category)
                    return next
                  })}
                  disableAll={disableAll}
                  onRuleToggle={onBuiltinToggle}
                  collapsible={!filtering}
                  rowError={ruleError}
                  errorHandoff={errorHandoff}
                />
              ))}
            </div>
            {nothingMatches && (
              <div className="text-[12px] text-muted py-2">
                {i18nT('pages.settings.securityPanel.no_rules_match', { query: filter.trim() })}
              </div>
            )}
          </>
        )}
      </SettingsCard>

      {/* Card B — Your custom denies */}
      <SettingsCard index={1}>
        <div className="text-[13px] font-semibold text-text" data-setting-label={i18nT('pages.settings.securityPanel.your_custom_denies')}>{i18nT('pages.settings.securityPanel.your_custom_denies')}</div>
        <div className="text-[12px] text-muted mt-0.5 mb-1 leading-relaxed">
          {i18nT('pages.settings.securityPanel.add_your_own_deny_patterns_python_compatible_reg')}
        </div>
        {visibleUserRules.length > 0 && (
          <div className="divide-y divide-border">
            {visibleUserRules.map(rule => (
              <CustomDenyRow
                key={rule.id}
                rule={rule}
                onToggle={next => toggleUser.mutate({ id: rule.id, enabled: next })}
                onDelete={() => deleteUser.mutate(rule.id)}
                error={ruleError?.id === rule.id ? ruleError.message : undefined}
                errorHandoff={errorHandoff}
              />
            ))}
          </div>
        )}
        {/* Say so when the filter is what emptied this card, rather than letting
            it read as "you have no custom patterns". */}
        {filtering && visibleUserRules.length === 0 && (dc?.user_added.length ?? 0) > 0 && (
          <div className="text-[12px] text-muted py-1.5">
            {i18nT('pages.settings.securityPanel.custom_patterns_hidden_by_filter')}
          </div>
        )}
        <AddDenyInput value={draft} onChange={onDraftChange} note={noteDraft} onNoteChange={onNoteDraftChange} onAdd={(pattern, note) => addUser.mutate({ pattern, note })} busy={addUser.isPending} submitError={addUser.isError ? trustFailureMessage(addUser.error) : ''} />
      </SettingsCard>

      {/* ── Confirm modal (disable a built-in rule / disable all) ── */}
      <Modal
        open={confirm !== null}
        onClose={() => setConfirm(null)}
        guideTrustRoot
        title={confirm?.kind === 'disable-all'
          ? i18nT('pages.settings.securityPanel.disable_all_built_in_denies_2')
          : i18nT('pages.settings.securityPanel.disable_this_denied_command')}
        maxWidth={480}
        footer={
          <>
            <Btn onClick={() => setConfirm(null)}>{i18nT('pages.settings.securityPanel.cancel')}</Btn>
            <Btn danger disabled={!ack} onClick={runConfirm}>
              {i18nT('pages.settings.securityPanel.disable')}
            </Btn>
          </>
        }
      >
        <div className="flex items-start gap-3">
          <AlertTriangle size={18} className="text-warn shrink-0 mt-0.5" />
          <div className="text-[13px] text-text leading-relaxed">{confirmBody}</div>
        </div>
        <label className="flex items-center gap-2.5 mt-4 cursor-pointer">
          <Checkbox checked={ack} onChange={e => setAck(e.target.checked)} />
          <span className="text-[13px] text-text">{i18nT('pages.settings.securityPanel.i_understand_this_weakens_kirocrew_s_protection')}</span>
        </label>
      </Modal>
    </SettingsSection>
  )
}
