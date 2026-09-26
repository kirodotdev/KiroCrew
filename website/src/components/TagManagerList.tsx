import { Fragment, useId, useRef, useState } from 'react'
import { useMutation, useMutationState, useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import { Bot, Check, Plus, Repeat2, UserRound, X, Zap } from 'lucide-react'
import type { AgentTagPolicy, ChatTag } from '../types'
import { useImeGuard } from '../hooks/useImeGuard'
import { api } from '../api/client'
import ErrorNotice from './ErrorNotice'
import SegmentedControl from './SegmentedControl'
import { Btn } from './ui'
import { FOLDER_COLOR_PALETTE } from './folderColorCatalog'

import { i18nT } from '../i18n/t'
import { crudErrorMessage, policyErrorMessage } from './tagMutationError'

/**
 * One failed mutation's notice. `crud` notices (create, rename, recolor, status,
 * delete) carry no agent hand-off: a failed create leaves the typed `newTagName`
 * draft in the create input and a failed rename leaves the typed text in that
 * row's uncontrolled name input, and navigating away would discard either.
 * `policy` notices (policy save, adoption) carry the hand-off: their input is a
 * single choice on a row that is already saved, so leaving loses nothing.
 * `rowId` is the tag the failure belongs to, rendered beneath that row so the
 * user can see which save failed; `null` is the create input.
 */
interface MutationErrorEntry {
  kind: 'crud' | 'policy'
  rowId: string | null
  message: string
}

/**
 * Identifies ONE user action on ONE target. A failure is suppressed only when
 * a newer attempt of the SAME action on the SAME target has superseded it; a
 * mutation on another tag, or another action on this tag, never hides it,
 * because that failure still describes a write that did not persist.
 */
interface MutationErrorContext {
  key: string
  generation: number
}

/** One policy save's input; `useMutationState` reads it back per row. */
interface PolicySave {
  id: string
  agent: AgentTagPolicy
}

const POLICY_MUTATION_KEY = ['chat-tag-policy'] as const

/**
 * The inline notice lays its message and the "Ask the agent" button out on one
 * line, which at this panel's width squeezes the message to a few characters
 * per line. Let the row wrap and give the message a floor width, so the
 * hand-off drops below the message instead of crushing it.
 */
const NARROW_NOTICE_LAYOUT = {
  className: '!flex w-full flex-wrap',
  messageClassName: 'flex-1 basis-[8rem]',
} as const

function withoutKey<V>(record: Record<string, V>, key: string): Record<string, V> {
  if (!(key in record)) return record
  const next = { ...record }
  delete next[key]
  return next
}

export interface TagManagerListProps {
  /**
   * Governs the leading swatch:
   *   'manage'        — swatch is a static colour chip (no filter toggle). Used
   *                     by the header "Manage tags…" panel, where the list is a
   *                     pure tag CRUD surface with no column context.
   *   'column-filter' — swatch is an include/exclude checkbox that toggles the
   *                     owning board column's `tag_ids`. Requires `selectedIds`
   *                     (the column's current tag_ids) + `onToggleTag`, so the
   *                     board keeps mutating its column exactly as before.
   */
  mode: 'manage' | 'column-filter'
  /** Column's current tag_ids (column-filter mode only). */
  selectedIds?: string[]
  /** Called with the tag toggled and the resulting next id list (column-filter mode only). */
  onToggleTag?: (tagId: string, nextIds: string[]) => void
  /** data-testid for the "New tag" input. Board passes `tag-create-<colId>` for byte-identical behaviour. */
  createTestId?: string
}

/**
 * The tag-management list rendered by both the board column-filter popover and
 * the header Manage-tags panel: a scrollable list of tag rows (leading swatch ·
 * inline-rename input · status ⚡ toggle · delete ✕) plus a "New tag… ↵" create
 * input. Self-contained — it queries `['chat-tags']` and owns the
 * create/update/delete mutations, so every surface that renders it (board column
 * popover, header Manage-tags panel) shares one live source and stays in lockstep
 * via the query cache. Row testids (tag-row / tag-name / tag-status /
 * tag-delete-<id>) match the board's selectors.
 */
export default function TagManagerList({ mode, selectedIds = [], onToggleTag, createTestId = 'tag-create' }: TagManagerListProps) {
  const queryClient = useQueryClient()
  const ime = useImeGuard()
  const descriptionIdPrefix = useId()
  const policyUnavailableId = `${descriptionIdPrefix}-tag-policy-unavailable`
  const { data: tags = [] } = useQuery<ChatTag[]>({ queryKey: ['chat-tags'], queryFn: () => api.chatTags() })
  /** Tag id whose inline colour palette is expanded (manage mode only). */
  const [openColorId, setOpenColorId] = useState<string | null>(null)
  const [mutationErrors, setMutationErrors] = useState<Record<string, MutationErrorEntry>>({})
  const [newTagName, setNewTagName] = useState('')
  const newTagDraftRevision = useRef(0)
  const mutationGenerations = useRef(new Map<string, number>())
  const beginMutation = (key: string): MutationErrorContext => {
    const generation = (mutationGenerations.current.get(key) ?? 0) + 1
    mutationGenerations.current.set(key, generation)
    setMutationErrors(current => withoutKey(current, key))
    return { key, generation }
  }
  const failMutation = (context: MutationErrorContext | undefined, entry: MutationErrorEntry) => {
    if (!context || mutationGenerations.current.get(context.key) !== context.generation) return
    setMutationErrors(current => ({ ...current, [context.key]: entry }))
  }
  const dismissMutationError = (key: string) => setMutationErrors(current => withoutKey(current, key))
  const errorEntries = Object.entries(mutationErrors)
  const renderMutationErrors = (rowId: string | null) => errorEntries
    .filter(([, entry]) => entry.rowId === rowId)
    .map(([key, entry]) => (
      <ErrorNotice
        key={key}
        // No hand-off: `crud` notices only. A failed create keeps the typed
        // `newTagName` draft and a failed rename keeps the row's uncontrolled
        // name input text, and the agent hand-off navigates away and would
        // discard them. `policy` notices hand off: their row is already saved.
        variant="inline"
        askAgent={entry.kind === 'policy'}
        {...NARROW_NOTICE_LAYOUT}
        className={`${NARROW_NOTICE_LAYOUT.className}${rowId !== null ? ' pl-7 pr-1.5 pb-1' : ''}`}
        message={entry.message}
        onDismiss={() => dismissMutationError(key)}
        testId={entry.kind === 'policy' ? 'tag-policy-mutation-error' : 'tag-crud-mutation-error'}
      />
    ))
  const createSubmitInFlight = useRef(false)
  /** Tag whose adopt button the user just activated; its policy group takes focus on mount. */
  const focusAdoptedPolicyRef = useRef<string | null>(null)
  const storeDegraded = tags.some(tag => !!tag.agent_store_degraded)
  const policySegments = [
    {
      key: 'none' as const,
      label: i18nT('components.tagManagerList.human_only'),
      tooltip: i18nT('components.tagManagerList.human_only_tooltip'),
      icon: <UserRound size={11} aria-hidden />,
    },
    {
      key: 'add-only' as const,
      label: i18nT('components.tagManagerList.agent_add_only'),
      tooltip: i18nT('components.tagManagerList.agent_add_only_tooltip'),
      icon: <Bot size={11} aria-hidden />,
    },
    {
      key: 'add-remove' as const,
      label: i18nT('components.tagManagerList.agent_add_remove'),
      tooltip: i18nT('components.tagManagerList.agent_add_remove_tooltip'),
      icon: <Repeat2 size={11} aria-hidden />,
    },
  ]

  const createTagMutation = useMutation({
    mutationFn: ({ name, color, status }: { name: string; draft: string; draftRevision: number; color?: string; status?: boolean }) => api.createChatTag(name, color, status),
    onMutate: () => beginMutation('create'),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['chat-tags'] })
      // Every keystroke bumps the revision, so an unchanged revision means the
      // input still holds exactly the submitted draft. Kept outside a state
      // updater: StrictMode double-invokes updaters, and a ref bump inside one
      // made the kept pass see a moved revision and leave the created name.
      if (newTagDraftRevision.current !== variables.draftRevision) return
      newTagDraftRevision.current += 1
      setNewTagName('')
    },
    onError: (error: unknown, _variables, context) => {
      failMutation(context, { kind: 'crud', rowId: null, message: crudErrorMessage(error) })
    },
    onSettled: () => { createSubmitInFlight.current = false },
  })
  const updateTagMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { name?: string; color?: string; status?: boolean } }) => api.updateChatTag(id, body),
    // A rename, a recolor and a status flip on one tag are separate actions:
    // the field set is part of the key, so one cannot hide another's failure.
    onMutate: ({ id, body }) => beginMutation(`update:${id}:${Object.keys(body).sort().join(',')}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-tags'] }),
    onError: (error: unknown, variables, context) => {
      failMutation(context, { kind: 'crud', rowId: variables.id, message: crudErrorMessage(error) })
    },
  })
  const policyTagMutation = useMutation({
    mutationKey: POLICY_MUTATION_KEY,
    mutationFn: ({ id, agent }: PolicySave) => api.updateChatTag(id, { agent }),
    onMutate: ({ id }) => beginMutation(`policy:${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-tags'] }),
    onError: (error: unknown, variables, context) => {
      failMutation(context, {
        kind: 'policy',
        rowId: variables.id,
        message: policyErrorMessage(error, i18nT('components.tagManagerList.policy_save_failed')),
      })
    },
  })
  // Every in-flight policy save, not just the hook's latest `variables`: a save
  // on another row moves `variables`, and a row gated only on it would re-enable
  // while its own older save is still in flight, so narrowing that row could be
  // overwritten by the older, broader save landing last.
  const pendingPolicySaves = useMutationState({
    filters: { mutationKey: POLICY_MUTATION_KEY, status: 'pending' },
    select: mutation => mutation.state.variables as PolicySave | undefined,
  })
  const adoptTagMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: boolean }) => api.adoptChatTag(id, status),
    onMutate: ({ id }) => beginMutation(`adopt:${id}`),
    onSuccess: (_data, variables) => {
      // Adoption unmounts the button that had focus and mounts the policy
      // group in its place; hand focus to that group instead of <body>.
      focusAdoptedPolicyRef.current = variables.id
      return queryClient.invalidateQueries({ queryKey: ['chat-tags'] })
    },
    onError: (error: unknown, variables, context) => {
      failMutation(context, {
        kind: 'policy',
        rowId: variables.id,
        message: policyErrorMessage(error, i18nT('components.tagManagerList.adoption_failed')),
      })
    },
  })
  const deleteTagMutation = useMutation({
    mutationFn: (id: string) => api.deleteChatTag(id),
    onMutate: id => beginMutation(`delete:${id}`),
    onSuccess: () => {
      // Deleting a tag also prunes column filters, so refresh that cache too
      // (both board views react without a manual reload).
      //
      // The un-tagged SLOT rows are deliberately NOT refreshed from here. Slot
      // tags live in the Redux dashboard slice, and `api_chat_tag_delete`
      // strips the id from every slot and then calls `push_slots_update()` on
      // its one success path -- so the authoritative frame `applySlots` lands
      // already carries the stripped tags. The third invalidate this handler
      // used to end with named a plain ['chat-slots'] key, which refreshes
      // nothing at all: no query is registered on it (#10204).
      queryClient.invalidateQueries({ queryKey: ['chat-tags'] })
      queryClient.invalidateQueries({ queryKey: ['tag-columns'] })
    },
    onError: (error: unknown, id, context) => {
      failMutation(context, { kind: 'crud', rowId: id, message: crudErrorMessage(error) })
    },
  })

  return (
    <>
      {storeDegraded && (
        <ErrorNotice
          id={policyUnavailableId}
          variant="inline"
          askAgent
          {...NARROW_NOTICE_LAYOUT}
          message={i18nT('components.tagManagerList.policy_store_unavailable')}
          testId="tag-policy-store-error"
        />
      )}
      {renderMutationErrors(null)}
      <div
        data-testid="tag-scroll-region"
        className={mode === 'manage'
          ? 'flex flex-col gap-0.5 max-h-[min(52vh,420px)] sm:max-h-[min(60vh,520px)] overflow-y-scroll overscroll-contain pb-2 pr-1 [scrollbar-gutter:stable]'
          : 'flex flex-col gap-0.5 max-h-[260px] overflow-y-auto'}
        {...(mode === 'column-filter' ? { role: 'group', 'aria-label': i18nT('components.tagManagerList.filter_by_tag') } : {})}
      >
        {[...tags].sort((a, b) => a.order - b.order).map(t => {
          const on = mode === 'column-filter' && selectedIds.includes(t.id)
          const nextIds = on ? selectedIds.filter(x => x !== t.id) : [...selectedIds, t.id]
          const legacyReasonId = `${descriptionIdPrefix}-tag-adoption-required-${t.id}`
          const tagStoreDegraded = !!t.agent_store_degraded
          const statusPending = updateTagMutation.isPending
            && updateTagMutation.variables?.id === t.id
            && updateTagMutation.variables.body.status !== undefined
          const statusDisabled = !t.agent_provenanced || tagStoreDegraded || statusPending
          const statusDescribedBy = [
            !t.agent_provenanced ? legacyReasonId : null,
            tagStoreDegraded ? policyUnavailableId : null,
          ].filter(Boolean).join(' ') || undefined
          const deletePending = deleteTagMutation.isPending && deleteTagMutation.variables === t.id
          // Row-scoped like `statusPending`: one row's save must not freeze
          // every other row's policy control or adopt button.
          const rowPolicySaves = pendingPolicySaves.filter(save => save?.id === t.id)
          const policyPending = rowPolicySaves.length > 0
          const adoptPending = adoptTagMutation.isPending && adoptTagMutation.variables?.id === t.id
          // Show the choice being saved immediately; the server value takes
          // over again when the save settles (success refetches, failure
          // falls back to the unchanged row).
          const policyValue: AgentTagPolicy = rowPolicySaves.at(-1)?.agent ?? t.agent ?? 'none'
          const deleteDisabled = tagStoreDegraded || deletePending
          return (
            <Fragment key={t.id}>
            <div data-testid={`tag-row-${t.id}`} className={`group/tag flex items-center gap-1.5 px-1.5 py-1 rounded transition-all ${on ? 'bg-accent-subtle' : 'hover:bg-bg-hover'}`}>
              {mode === 'column-filter' ? (
                /* Filter toggle — the colour swatch is the click target. role=checkbox
                 *  (not menuitemcheckbox) because the row lives in a form popover, not a
                 *  menu: a native <button> is Tab-reachable and Space/Enter-operable, and
                 *  the owning popover owns focus/Escape (no orphan menuitem ARIA). */
                <button type="button" role="checkbox" aria-checked={on} aria-label={i18nT('components.tagManagerList.include_in_filter', { name: t.name })}
                  className="w-4 h-4 rounded-sm border border-border shrink-0 cursor-pointer relative outline-hidden focus-visible:ring-2 focus-visible:ring-accent"
                  style={{ background: t.color }}
                  onClick={() => onToggleTag?.(t.id, nextIds)}>
                  {on && <span className="absolute inset-0 flex items-center justify-center" style={{ color: t.color === '#ffffff' ? '#000' : '#fff' }}><Check size={10} /></span>}
                </button>
              ) : (
                /* Manage mode — the swatch is a button that expands an inline
                 *  colour palette row beneath the tag (backend PATCH already
                 *  supports recolor; this is its first UI surface). */
                <button type="button" data-testid={`tag-color-${t.id}`}
                  aria-expanded={openColorId === t.id}
                  aria-label={i18nT('components.tagManagerList.change_color', { name: t.name })}
                  title={i18nT('components.tagManagerList.change_color', { name: t.name })}
                  className="w-4 h-4 rounded-sm border border-border shrink-0 cursor-pointer outline-hidden focus-visible:ring-2 focus-visible:ring-accent"
                  style={{ background: t.color }}
                  onClick={() => setOpenColorId(cur => (cur === t.id ? null : t.id))} />
              )}
              {/* Inline rename */}
              {/* key={t.name} remounts the uncontrolled input when the canonical
                *  name changes — so a rename in one rendered instance (board popover
                *  or the header Manage-tags panel) reflects in the other, which a
                *  bare defaultValue would not. */}
              <input
                key={t.name}
                type="text"
                data-testid={`tag-name-${t.id}`}
                aria-label={i18nT('components.tagManagerList.rename_tag', { name: t.name })}
                defaultValue={t.name}
                className="flex-1 min-w-0 bg-transparent border-none outline-hidden text-[12px] text-text py-0 px-0.5 rounded focus-visible:bg-bg-elevated focus-visible:border focus-visible:border-accent/50"
                {...ime.bindComposition<HTMLInputElement>({
                  onBlur: e => { const v = e.target.value.trim(); if (!v) { e.target.value = t.name; return } if (v !== t.name) updateTagMutation.mutate({ id: t.id, body: { name: v } }) },
                })}
                onKeyDown={e => {
                  const el = e.currentTarget as HTMLInputElement
                  if (e.key !== 'Enter' && e.key !== 'Escape') return
                  // Escape restores the canonical name first, so its path can never
                  // persist a draft. Enter commits through the focus move below (it
                  // fires this input's onBlur), so a committing IME Enter — whose
                  // candidate text is still intermediate — must not reach it. Rule 1:
                  // single-line input, so the declined key is left unconsumed.
                  if (e.key === 'Escape') el.value = t.name
                  else if (ime.isComposing(e)) return
                  e.stopPropagation()
                  // Move focus to the row's first button (swatch in column-filter mode,
                  // status ⚡ in manage mode) instead of blur()ing to <body>. This still
                  // fires the input's onBlur (commit) but keeps focus inside the owning
                  // popover so its Tab-trap isn't defeated after a rename.
                  const sib = el.closest('[data-testid^="tag-row-"]')?.querySelector<HTMLElement>('button')
                  if (sib) sib.focus(); else el.blur()
                }}
                onClick={e => e.stopPropagation()}
              />
              {mode === 'column-filter' && !t.agent_provenanced && (
                <span id={legacyReasonId} className="sr-only">
                  {i18nT('components.tagManagerList.legacy_unverified')}
                </span>
              )}
              {/* Status lightning — filled for status tags, muted ghost for non-status on hover */}
              <button type="button" data-testid={`tag-status-${t.id}`}
                className={`shrink-0 cursor-pointer bg-transparent border-none p-[2px] transition-all outline-hidden focus-visible:ring-1 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-40 ${t.status ? 'text-accent hover:text-accent-hover' : 'text-transparent group-hover/tag:text-muted focus-visible:!text-muted hover:!text-text'}`}
                title={!t.agent_provenanced
                  // A legacy row's status flip is refused until adoption; name the
                  // unlock instead of promising an action the click ignores.
                  ? i18nT('components.tagManagerList.status_needs_setup')
                  : t.status ? i18nT('components.tagManagerList.status_tag_mutually_exclusive_on_cards_click_to') : i18nT('components.tagManagerList.make_status_tag')}
                aria-pressed={!!t.status}
                aria-disabled={statusDisabled}
                aria-describedby={statusDescribedBy}
                disabled={statusDisabled}
                aria-label={t.status ? i18nT('components.tagManagerList.remove_status_flag_from', { name: t.name }) : i18nT('components.tagManagerList.make_a_status_tag', { name: t.name })}
                onClick={() => {
                  if (statusDisabled) return
                  updateTagMutation.mutate({ id: t.id, body: { status: !t.status } })
                }}>
                <Zap size={11} fill={t.status ? 'currentColor' : 'none'} />
              </button>
              {/* Delete */}
              <button type="button" data-testid={`tag-delete-${t.id}`}
                className="shrink-0 cursor-pointer bg-transparent border-none p-[2px] text-transparent group-hover/tag:text-muted focus-visible:!text-muted hover:!text-danger transition-all outline-hidden focus-visible:ring-1 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-40"
                title={i18nT('components.tagManagerList.delete_tag', { name: t.name })}
                aria-disabled={deleteDisabled}
                aria-describedby={tagStoreDegraded ? policyUnavailableId : undefined}
                disabled={deleteDisabled}
                aria-label={i18nT('components.tagManagerList.delete_tag_2', { name: t.name })}
                onClick={() => {
                  if (deleteDisabled) return
                  if (confirm(`Delete tag "${t.name}"?`)) deleteTagMutation.mutate(t.id)
                }}>
                <X size={11} />
              </button>
            </div>
            {mode === 'manage' && (
              <AnimatePresence initial={false} mode="wait">
                {t.agent_provenanced ? (
                  <motion.div
                    key={`policy-${t.id}`}
                    ref={node => {
                      if (!node || focusAdoptedPolicyRef.current !== t.id) return
                      focusAdoptedPolicyRef.current = null
                      node.querySelector<HTMLElement>('[role="radio"][tabindex="0"]')?.focus()
                    }}
                    layout
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: 4 }}
                    transition={{ duration: 0.16 }}
                    className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 pl-7 pr-1.5 pb-1 min-w-0"
                  >
                    {/* Visible caption so a sighted reader knows what the pills
                      *  govern; the group's own accessible name already says it
                      *  (with the tag name), so screen readers skip this copy. */}
                    <span aria-hidden className="text-[11px] text-muted shrink-0">
                      {i18nT('components.tagManagerList.permissions_caption')}
                    </span>
                    <SegmentedControl<AgentTagPolicy>
                      segments={policySegments.map(segment => ({
                        ...segment,
                        disabled: !!t.agent_store_degraded || policyPending,
                      }))}
                      value={policyValue}
                      onChange={agent => {
                        if (t.agent_store_degraded || policyPending) return
                        policyTagMutation.mutate({ id: t.id, agent })
                      }}
                      layoutId={`tag-agent-policy-${t.id}`}
                      ariaLabel={i18nT('components.tagManagerList.policy_for_tag', { name: t.name })}
                      ariaDescribedBy={t.agent_store_degraded ? policyUnavailableId : undefined}
                      collapse={false}
                      wrap
                    />
                  </motion.div>
                ) : (
                  <motion.div
                    key={`adopt-${t.id}`}
                    layout
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: 4 }}
                    transition={{ duration: 0.16 }}
                    className="flex flex-wrap items-center gap-1.5 pl-7 pr-1.5 pb-1"
                  >
                    <span id={legacyReasonId} className="text-[12px] text-muted">
                      {i18nT('components.tagManagerList.legacy_unverified')}
                    </span>
                    <Btn
                      type="button"
                      aria-disabled={!!t.agent_store_degraded || adoptPending}
                      aria-describedby={t.agent_store_degraded ? policyUnavailableId : undefined}
                      disabled={!!t.agent_store_degraded || adoptPending}
                      onClick={() => adoptTagMutation.mutate({ id: t.id, status: !!t.status })}
                    >
                      <Plus size={11} aria-hidden />
                      {adoptPending
                        ? i18nT('components.tagManagerList.adopting_tag')
                        : i18nT('components.tagManagerList.adopt_tag')}
                    </Btn>
                  </motion.div>
                )}
              </AnimatePresence>
            )}
            {/* Inline colour palette — expanded by the manage-mode swatch. Reuses
              *  the folder palette so tags and folders speak one visual language.
              *  Picking a colour PATCHes the tag and returns focus to the swatch
              *  (the palette unmounts, so focus would otherwise fall to <body>). */}
            {mode === 'manage' && openColorId === t.id && (
              // The group's only listener is keyboard-only Escape-to-dismiss,
              // delegated here so it fires whichever swatch holds focus. The
              // group activates nothing itself — every affordance inside it is a
              // real <button> — so there is no mouse action a keyboard misses.
              // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- container-level Escape dismissal, which IS the keyboard path rather than a substitute for one
              <div
                role="group"
                data-testid={`tag-palette-${t.id}`}
                aria-label={i18nT('components.tagManagerList.change_color', { name: t.name })}
                className="flex items-center gap-1 flex-wrap pl-7 pr-1.5 pb-1"
                onKeyDown={e => {
                  if (e.key !== 'Escape') return
                  e.stopPropagation()
                  setOpenColorId(null)
                  document.querySelector<HTMLElement>(`[data-testid="tag-color-${t.id}"]`)?.focus()
                }}
              >
                {FOLDER_COLOR_PALETTE.map(({ value, label }) => {
                  const colorName = label()
                  return (
                    <button
                      key={value}
                      type="button"
                      data-testid={`tag-color-${t.id}-${value.slice(1)}`}
                      title={i18nT('components.tagManagerList.set_color_to_name', { name: colorName })}
                      aria-label={i18nT('components.tagManagerList.set_color_to_name', { name: colorName })}
                      aria-pressed={t.color === value}
                      className={`w-4 h-4 rounded-full cursor-pointer border hover:brightness-125 swatch-cue outline-hidden focus-visible:ring-2 focus-visible:ring-accent ${t.color === value ? 'ring-1 ring-accent ring-offset-1 ring-offset-bg' : ''}`}
                      style={{ background: `color-mix(in srgb, ${value} 30%, var(--bg-elevated))`, borderColor: value }}
                      onClick={() => {
                        updateTagMutation.mutate({ id: t.id, body: { color: value } })
                        setOpenColorId(null)
                        document.querySelector<HTMLElement>(`[data-testid="tag-color-${t.id}"]`)?.focus()
                      }}
                    />
                  )
                })}
              </div>
            )}
            {renderMutationErrors(t.id)}
            </Fragment>
          )
        })}
      </div>
      {/* Create new tag */}
      <div className="mt-2 border-t border-border pt-2 flex items-center gap-1.5">
        <span className="w-4 h-4 rounded-sm border border-dashed border-border shrink-0 flex items-center justify-center text-muted"><Plus size={10} /></span>
        <input
          type="text"
          data-testid={createTestId}
          placeholder={i18nT('components.tagManagerList.new_tag')}
          value={newTagName}
          aria-disabled={storeDegraded}
          aria-describedby={storeDegraded ? policyUnavailableId : undefined}
          disabled={storeDegraded}
          className="flex-1 min-w-0 bg-transparent border-none text-[12px] text-text py-0 px-0.5 placeholder:text-muted/60 disabled:cursor-not-allowed disabled:opacity-50"
          {...ime.bindComposition()}
          onChange={e => {
            newTagDraftRevision.current += 1
            setNewTagName(e.currentTarget.value)
          }}
          onKeyDown={e => {
            if (e.key !== 'Enter') return
            // Rule 1: single-line input — the guard alone; emptiness stays outside.
            if (ime.isComposing(e)) return
            if (createSubmitInFlight.current) return
            const draft = newTagName
            const v = draft.trim()
            if (!v) return
            createSubmitInFlight.current = true
            createTagMutation.mutate({
              name: v,
              draft,
              draftRevision: newTagDraftRevision.current,
            })
          }}
          onClick={e => e.stopPropagation()}
        />
      </div>
    </>
  )
}
