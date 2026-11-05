/**
 * The owner's reveal of a removed credential, inside the credential card.
 * The value lives only in gateway memory, and only the owner's browser may
 * read it (POST /api/security/redacted-value refuses every agent, app and
 * non-owner). See RFC rfc-redaction-explain-and-reveal.md, Owner reveal.
 */
import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'
import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { HOST_ONLY_ATTR } from '../utils/visibleText'
import { ActionRow, btnCls, btnPrimaryCls, ErrorLine, FeedbackButton } from './redactionPrimitives'


/**
 * Values the owner revealed in one rendered reply, by handle. Memory only:
 * never in storage or a query cache. The reply's RedactionProvider owns it,
 * so closing and reopening a card keeps the value shown until Hide, and the
 * reply unmounting (leaving the page, switching session) clears it; a reveal
 * still in flight when that happens is dropped instead of stored.
 */
export interface RevealCache {
  values: Map<string, string>
  /** The newest request per handle. A response for an older one (a card
   *  reopened and asked again, or Hide pressed since) is dropped. */
  requests: Map<string, number>
  alive: boolean
}

/** One cache for the lifetime of the calling component, cleared on unmount. */
export function useRevealCache(): RevealCache {
  const ref = useRef<RevealCache | null>(null)
  if (ref.current === null) ref.current = { values: new Map(), requests: new Map(), alive: true }
  const cache = ref.current
  useEffect(() => {
    cache.alive = true
    return () => {
      cache.alive = false
      cache.values.clear()
      cache.requests.clear()
    }
  }, [cache])
  return cache
}

type RevealState = 'idle' | 'busy' | 'shown' | 'gone' | 'denied' | 'failed'

/**
 * Show / Copy / Hide for one removed value. The value lives only in gateway
 * memory, and only the owner's browser may read it (POST
 * /api/security/redacted-value refuses every agent, app and non-owner).
 */
export function RevealValue({ revealId, slotKey, cache }: { revealId: string | null; slotKey?: string; cache: RevealCache }) {
  useLanguageGeneration()
  const revealed = cache.values
  const [state, setState] = useState<RevealState>(() => (revealId && revealed.has(revealId) ? 'shown' : 'idle'))
  const [copyFailed, setCopyFailed] = useState(false)
  const value = revealId ? revealed.get(revealId) : undefined
  if (!revealId || !slotKey) {
    return <div data-testid="reveal-unavailable" className="mb-1 text-[12px] leading-[18px] text-muted">{i18nT('components.redaction.reveal_not_kept')}</div>
  }
  const show = async () => {
    setState('busy')
    const request = (cache.requests.get(revealId) ?? 0) + 1
    cache.requests.set(revealId, request)
    const current = () => cache.alive && cache.requests.get(revealId) === request
    try {
      const res = await api.revealRedactedValue(slotKey, revealId)
      if (typeof res?.value !== 'string') throw new Error('bad answer')
      // The reply went away, Hide was pressed, or a newer Show is in flight:
      // this answer is stale, so keep nothing.
      if (!current()) return
      revealed.set(revealId, res.value)
      setState('shown')
    } catch (e) {
      if (!current()) return
      const status = e instanceof ApiError ? e.status : 0
      setState(status === 410 ? 'gone' : status === 403 ? 'denied' : 'failed')
    }
  }
  const hide = () => {
    cache.requests.set(revealId, (cache.requests.get(revealId) ?? 0) + 1)
    revealed.delete(revealId)
    setCopyFailed(false)
    setState('idle')
  }
  if (state === 'shown' && value !== undefined) {
    return (
      <div data-testid="reveal-shown" {...{ [HOST_ONLY_ATTR]: '' }} className="mt-1.5 rounded-lg border border-border bg-card px-2 py-1.5 text-[12px] leading-[18px] text-card-fg">
        <div className="text-[11px] text-muted">{i18nT('components.redaction.reveal_note')}</div>
        <code data-testid="reveal-value" className="my-1 block select-all whitespace-pre-wrap break-all rounded bg-bg-elevated px-[.4em] py-[.15em] font-mono text-[12px] text-text">{value}</code>
        <div className="flex flex-wrap items-center gap-2">
          <FeedbackButton
            className={btnPrimaryCls}
            label={i18nT('components.redaction.reveal_copy')}
            done={i18nT('components.redaction.copied')}
            testId="reveal-copy"
            onClick={async () => {
              const ok = await copyToClipboard(value)
              setCopyFailed(!ok)
              return ok
            }}
          />
          <button type="button" className={btnCls} data-testid="reveal-hide" onClick={hide}>{i18nT('components.redaction.reveal_hide')}</button>
        </div>
        {copyFailed && <ErrorLine testId="reveal-copy-error" message={i18nT('components.redaction.copy_failed')} />}
      </div>
    )
  }
  return (
    <>
      <ActionRow title={i18nT('components.redaction.reveal_title')} note={i18nT('components.redaction.reveal_hint')}>
        <button type="button" className={btnCls} data-testid="reveal-show" disabled={state === 'busy' || state === 'gone'} onClick={show}>
          {i18nT('components.redaction.reveal_show')}
        </button>
      </ActionRow>
      {state === 'gone' && <ErrorLine testId="reveal-gone" message={i18nT('components.redaction.reveal_gone')} />}
      {state === 'denied' && <ErrorLine testId="reveal-denied" message={i18nT('components.redaction.reveal_denied')} />}
      {state === 'failed' && <ErrorLine testId="reveal-failed" message={i18nT('components.redaction.reveal_failed')} />}
    </>
  )
}
