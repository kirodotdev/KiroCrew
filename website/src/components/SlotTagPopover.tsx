import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { X, Check } from 'lucide-react'
import type { ChatTag } from '../types'
import { api } from '../api/client'
import { useAppSelector } from '../store'
import { useTagPopover } from '../hooks/useTagPopover'
import { useImeGuard } from '../hooks/useImeGuard'
import { isTouchDevice } from '../utils/isTouchDevice'
import { Input } from './ui'
import ErrorNotice from './ErrorNotice'

import { i18nT } from '../i18n/t'

const sameTagIds = (left: string[], right: string[]) => left.length === right.length
  && left.every((tag, i) => tag === right[i])

/**
 * The single app-wide per-slot tag-assignment popover. Which slot's picker is
 * open comes from the ChatPage-scoped TagPopover context, so any surface (the
 * sidebar row menus or the chat-header menu) opens it via useTagPopover().open
 * and one instance renders for whichever slot is set — nothing when none is.
 */
export default function SlotTagPopover() {
  const { slotKey, close } = useTagPopover()
  const slot = useAppSelector(s => (slotKey ? s.dashboard.slots.find(x => x.key === slotKey) : undefined))
  const queryClient = useQueryClient()
  const ime = useImeGuard()
  const listRef = useRef<HTMLDivElement>(null)
  const { data: tags = [] } = useQuery<ChatTag[]>({ queryKey: ['chat-tags'], queryFn: () => api.chatTags(), enabled: !!slotKey })

  const setSlotTagsMutation = useMutation({
    mutationFn: ({ slot, nextTags }: { slot: string; nextTags: string[] }) => api.setSlotTags(slot, nextTags),
  })
  const createTagMutation = useMutation({
    mutationFn: (name: string) => api.createChatTag(name),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-tags'] }),
  })

  // Optimistic overlay for the currently-open slot only. `pending` drives the
  // render; `pendingRef` mirrors it so `toggle` reads the latest value
  // synchronously (rapid-burst composition) without a state closure.
  const [pending, setPending] = useState<string[] | null>(null)
  const [writeError, setWriteError] = useState<string | null>(null)
  const [confirmation, setConfirmation] = useState<{
    baseline: string[]
    desired: string[]
    expectedRevisions: string[]
  } | null>(null)
  const pendingRef = useRef<string[] | null>(null)
  const slotKeyRef = useRef(slotKey)
  slotKeyRef.current = slotKey
  const slotTagsRef = useRef<string[]>(slot?.tags ?? [])
  const slotTagsRevisionRef = useRef<string | null>(slot?.tags_revision ?? null)
  const writeQueuesRef = useRef<Map<string, Promise<boolean>>>(new Map())
  const lastAcceptedTagsRef = useRef<Map<string, string[]>>(new Map())
  const lastAcceptedRevisionRef = useRef<Map<string, string>>(new Map())
  useEffect(() => {
    const slotTags = slot?.tags ?? []
    const tagsRevision = slot?.tags_revision ?? null
    slotTagsRef.current = slotTags
    slotTagsRevisionRef.current = tagsRevision
    if (slotKey && !pendingRef.current) {
      lastAcceptedTagsRef.current.set(slotKey, [...slotTags])
      if (tagsRevision) lastAcceptedRevisionRef.current.set(slotKey, tagsRevision)
      else lastAcceptedRevisionRef.current.delete(slotKey)
    }
  }, [slotKey, slot?.tags, slot?.tags_revision])
  useEffect(() => {
    if (slotKey) {
      lastAcceptedTagsRef.current.set(slotKey, [...slotTagsRef.current])
      const tagsRevision = slotTagsRevisionRef.current
      if (tagsRevision) lastAcceptedRevisionRef.current.set(slotKey, tagsRevision)
      else lastAcceptedRevisionRef.current.delete(slotKey)
    }
    pendingRef.current = null
    setPending(null)
    setConfirmation(null)
    setWriteError(null)
  }, [slotKey])
  useEffect(() => {
    if (!confirmation || pendingRef.current !== confirmation.desired) return
    const slotTags = slot?.tags ?? []
    const tagsRevision = slot?.tags_revision ?? null
    if (tagsRevision && confirmation.expectedRevisions.length > 0) {
      if (confirmation.expectedRevisions.includes(tagsRevision)) return
    } else {
      const stillBaseline = sameTagIds(slotTags, confirmation.baseline)
      if (stillBaseline) return
    }
    if (slotKey) {
      lastAcceptedTagsRef.current.set(slotKey, [...slotTags])
      if (tagsRevision) lastAcceptedRevisionRef.current.set(slotKey, tagsRevision)
      else lastAcceptedRevisionRef.current.delete(slotKey)
    }
    pendingRef.current = null
    setPending(null)
    setConfirmation(null)
  }, [confirmation, slotKey, slot?.tags, slot?.tags_revision])

  // Move focus into the tag list on open so keyboard users can operate it
  // (skip on touch to avoid hijacking focus). Deferred a tick so the list has
  // painted its options.
  useEffect(() => {
    if (!slotKey || isTouchDevice()) return
    const t = window.setTimeout(() => {
      listRef.current?.querySelector<HTMLElement>('[data-option]')?.focus()
    }, 0)
    return () => clearTimeout(t)
  }, [slotKey])

  if (!slotKey) return null
  const currentTags = new Set(pending ?? slot?.tags ?? [])
  const toggle = (tagId: string) => {
    const base = pendingRef.current ?? slot?.tags ?? []
    const baselineTags = [...slotTagsRef.current]
    const baselineRevision = slotTagsRevisionRef.current
    const previousTags = [...base]
    const nextTags = base.includes(tagId) ? base.filter(t => t !== tagId) : [...base, tagId]
    pendingRef.current = nextTags
    setPending(nextTags)
    setConfirmation(null)
    setWriteError(null)
    const targetSlot = slotKey
    if (!lastAcceptedTagsRef.current.has(targetSlot)) {
      lastAcceptedTagsRef.current.set(targetSlot, [...baselineTags])
    }
    const priorWrite = writeQueuesRef.current.get(targetSlot) ?? Promise.resolve(true)
    let queuedWrite!: Promise<boolean>
    queuedWrite = priorWrite.then(async canContinue => {
      if (!canContinue) return false
      const acceptedBeforeWriteRevision = lastAcceptedRevisionRef.current.get(targetSlot) ?? null
      const expectedRevisions = [...new Set(
        [baselineRevision, acceptedBeforeWriteRevision]
          .filter((revision): revision is string => Boolean(revision)),
      )]
      try {
        const result = await setSlotTagsMutation.mutateAsync({ slot: targetSlot, nextTags })
        if (slotKeyRef.current === targetSlot) setWriteError(null)
        const response = result as {
          tags?: unknown
          tags_revision?: unknown
          prior_tags_revision?: unknown
        } | undefined
        const responseTags = response?.tags
        const confirmedTags = Array.isArray(responseTags)
          ? responseTags.filter((tag): tag is string => typeof tag === 'string')
          : nextTags
        const confirmedRevision = typeof response?.tags_revision === 'string'
          ? response.tags_revision
          : null
        const serverPriorRevision = typeof response?.prior_tags_revision === 'string'
          ? response.prior_tags_revision
          : null
        if (serverPriorRevision && !expectedRevisions.includes(serverPriorRevision)) {
          expectedRevisions.push(serverPriorRevision)
        }
        lastAcceptedTagsRef.current.set(targetSlot, [...confirmedTags])
        if (confirmedRevision) lastAcceptedRevisionRef.current.set(targetSlot, confirmedRevision)

        // A newer click owns the overlay. This write still commits in order for
        // its own slot, but it must not overwrite that newer visible intent.
        if (pendingRef.current !== nextTags) return true
        const currentSlotTags = slotTagsRef.current
        const currentSlotRevision = slotTagsRevisionRef.current
        const canCompareRevision = Boolean(currentSlotRevision && expectedRevisions.length > 0)
        const isConfirmed = confirmedRevision && currentSlotRevision
          ? currentSlotRevision === confirmedRevision
          : sameTagIds(currentSlotTags, confirmedTags)
        const isExpectedPredecessor = canCompareRevision
          ? expectedRevisions.includes(currentSlotRevision as string)
          : sameTagIds(currentSlotTags, baselineTags)
            || sameTagIds(currentSlotTags, previousTags)
        if (!isExpectedPredecessor) {
          lastAcceptedTagsRef.current.set(targetSlot, [...currentSlotTags])
          if (currentSlotRevision) {
            lastAcceptedRevisionRef.current.set(targetSlot, currentSlotRevision)
          }
        }
        if (isConfirmed || !isExpectedPredecessor) {
          pendingRef.current = null
          setPending(null)
          setConfirmation(null)
          return true
        }

        // The store still carries an expected predecessor revision/list. Keep
        // the overlay until the matching result or a concurrent winner arrives.
        setConfirmation({
          baseline: [...currentSlotTags],
          desired: nextTags,
          expectedRevisions,
        })
        return true
      } catch (error) {
        if (slotKeyRef.current === targetSlot) {
          setWriteError(error instanceof Error && error.message
            ? error.message
            : i18nT('pages.chatPage.unknown_error'))
        } else {
          return false
        }
        // A failed predecessor invalidates every later intent already queued
        // behind it. Roll the active slot back, then propagate cancellation.
        const currentSlotTags = slotTagsRef.current
        const currentSlotRevision = slotTagsRevisionRef.current
        const canCompareRevision = Boolean(currentSlotRevision && expectedRevisions.length > 0)
        const isExpectedPredecessor = canCompareRevision
          ? expectedRevisions.includes(currentSlotRevision as string)
          : sameTagIds(currentSlotTags, baselineTags)
            || sameTagIds(currentSlotTags, previousTags)
        if (!isExpectedPredecessor) {
          lastAcceptedTagsRef.current.set(targetSlot, [...currentSlotTags])
          if (currentSlotRevision) {
            lastAcceptedRevisionRef.current.set(targetSlot, currentSlotRevision)
          }
          pendingRef.current = null
          setPending(null)
          setConfirmation(null)
          return false
        }

        const fallbackTags = [...(lastAcceptedTagsRef.current.get(targetSlot) ?? baselineTags)]
        if (sameTagIds(currentSlotTags, fallbackTags)) {
          pendingRef.current = null
          setPending(null)
          setConfirmation(null)
          return false
        }
        pendingRef.current = fallbackTags
        setPending(fallbackTags)
        setConfirmation({
          baseline: [...currentSlotTags],
          desired: fallbackTags,
          expectedRevisions: currentSlotRevision ? [currentSlotRevision] : [],
        })
        return false
      }
    }).finally(() => {
      if (writeQueuesRef.current.get(targetSlot) === queuedWrite) {
        writeQueuesRef.current.delete(targetSlot)
      }
    })
    writeQueuesRef.current.set(targetSlot, queuedWrite)
  }

  return (
    <div role="button" tabIndex={0} aria-label={i18nT('components.slotTagPopover.close_tag_picker')}
      className="fixed inset-0 z-[9999]"
      onClick={e => { if (e.target === e.currentTarget) close() }}
      onKeyDown={e => {
        // Only handle keys originating directly on the backdrop — events
        // bubbling up from inner dialog buttons/inputs must not dismiss it.
        if (e.target !== e.currentTarget) return
        if (e.key === 'Enter' || e.key === ' ' || e.key === 'Escape') { e.preventDefault(); close() }
      }}>
      {/* Both handlers below belong to the modal container, not to a control the
          user activates: `stopPropagation` keeps a click inside the dialog from
          reaching the backdrop's dismiss, and `onKeyDown` gives Escape a home
          when focus sits on an inner button whose events the backdrop ignores. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- dialog-level dismissal plumbing; the operable controls are the menuitems and the input inside */}
      <div role="dialog" aria-modal="true" aria-label={i18nT('components.slotTagPopover.assign_tags')} data-testid="slot-tag-picker"
        className="absolute bg-bg-elevated border border-border rounded-lg shadow-lg p-2 min-w-[240px] text-[13px]"
        style={{ left: '50%', top: '30%', transform: 'translate(-50%, 0)' }}
        onClick={e => e.stopPropagation()}
        onKeyDown={e => { if (e.key === 'Escape') close() }}>
        <div className="flex items-center justify-between mb-1">
          <span className="text-[11px] font-semibold text-muted uppercase tracking-wider px-1">{i18nT('components.slotTagPopover.assign_tags')}</span>
          <button type="button" className="text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 leading-none" onClick={close} aria-label={i18nT('components.slotTagPopover.close')}><X size={13} /></button>
        </div>
        {/* tabIndex=-1: the roving-focus menu owns the arrow keys, so it must be
            able to hold focus itself (and be a focus target when it has no
            options yet) without ever joining the tab order — the menuitems are
            reached with the arrows, not with Tab. */}
        <div ref={listRef} role="menu" tabIndex={-1} aria-label={i18nT('components.slotTagPopover.tags')} className="flex flex-col gap-0.5 max-h-[260px] overflow-y-auto"
          onKeyDown={e => {
            // Roving focus across the tag options. Deliberately does NOT handle
            // Tab (keeps the "New tag" input reachable) or Escape (the dialog
            // handles that); Enter/Space activate the focused button natively.
            const opts = Array.from(listRef.current?.querySelectorAll<HTMLElement>('[data-option]') ?? [])
            if (opts.length === 0) return
            const i = opts.indexOf(document.activeElement as HTMLElement)
            if (e.key === 'ArrowDown') { e.preventDefault(); (opts[i + 1] ?? opts[0]).focus() }
            else if (e.key === 'ArrowUp') { e.preventDefault(); (opts[i - 1] ?? opts[opts.length - 1]).focus() }
            else if (e.key === 'Home') { e.preventDefault(); opts[0].focus() }
            else if (e.key === 'End') { e.preventDefault(); opts[opts.length - 1].focus() }
          }}>
          {tags.length === 0 && <div className="text-muted px-2 py-1 text-[12px]">{i18nT('components.slotTagPopover.no_tags_yet_create_one_below')}</div>}
          {[...tags].sort((a, b) => a.order - b.order).map(t => {
            const on = currentTags.has(t.id)
            return (
              <button key={t.id} role="menuitemcheckbox" aria-checked={on} type="button" data-option tabIndex={-1}
                className={`flex items-center gap-2 px-2 py-1 rounded text-left cursor-pointer bg-transparent border-none transition-all ${on ? 'bg-accent-subtle text-text-strong' : 'text-text hover:bg-bg-hover'}`}
                onClick={() => toggle(t.id)}>
                <span className="w-3 h-3 rounded-sm border border-border shrink-0" style={{ background: t.color }} />
                <span className="flex-1 truncate">{t.name}</span>
                {on && <span className="text-accent"><Check size={11} /></span>}
              </button>
            )
          })}
        </div>
        {/* No hand-off: the adjacent new-tag input is an uncontrolled draft, so
            navigating away for agent hand-off would discard unsaved text. */}
        <ErrorNotice
          message={writeError}
          variant="inline"
          onDismiss={() => setWriteError(null)}
          className="mt-2 px-1"
        />
        <div className="mt-2 border-t border-border pt-2 flex items-center gap-1">
          <Input
            className="flex-1 text-[12px] py-1"
            placeholder={i18nT('components.slotTagPopover.new_tag')}
            {...ime.bindEnter<HTMLInputElement>({
              onEnter: () => {
                const el = document.activeElement as HTMLInputElement | null
                const name = (el?.value || '').trim()
                if (!name) return
                createTagMutation.mutate(name)
                if (el) el.value = ''
              },
              onEscape: close,
              onBlur: () => {},
            })}
          />
        </div>
      </div>
    </div>
  )
}
