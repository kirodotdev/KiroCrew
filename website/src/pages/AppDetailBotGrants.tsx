/**
 * The app detail page's "Bots this app can reach" card: the Bot scopes the
 * manifest declares (`permissions.bots`) in plain words, and one checkbox per
 * Bot. A ticked Bot is the only kind the app can see or wake; a fresh install
 * reaches none. Shown only for an installed app that declares a Bot scope.
 *
 * Owner only, like every app setting: `PUT /api/apps/{name}/bot-grants` refuses
 * any other caller, the app's own token included.
 */
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { AppBotGrants } from '../api/client/apps'
import { membersRosterQuery } from '../api/membersQuery'
import { Btn, Card, CardTitle, Checkbox } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'
import { compareText } from '../i18n/format'

const SCOPE_TEXT: Record<string, () => string> = {
  read: () => i18nT('pages.appDetailPage.bots_scope_read'),
  wake: () => i18nT('pages.appDetailPage.bots_scope_wake'),
}

/** The declared Bot scopes outside the approved set (`approvedGrants.bots` is omitted while empty). */
export function stagedBotScopes(app: { approvedGrants?: { bots?: string[] }; manifest?: { permissions?: { bots?: string[] } } } | null | undefined): string[] {
  if (!app?.approvedGrants) return []
  const approved = app.approvedGrants.bots || []
  return (app.manifest?.permissions?.bots || []).filter(s => !approved.includes(s))
}

/** The Bot scopes an update added that wait for approval, for the reconsent notice. */
export function StagedBotScopes({ scopes }: { scopes: string[] }) {
  if (scopes.length === 0) return null
  return (
    <div>
      <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_bots')}</div>
      <ul className="mt-1 text-[13px] list-disc pl-5" data-testid="staged-bot-scopes">
        {scopes.map(s => <li key={s}>{SCOPE_TEXT[s]?.() ?? s}</li>)}
      </ul>
    </div>
  )
}

export default function AppDetailBotGrants({ appName, declared }: { appName: string; declared: unknown }) {
  const scopes = Array.isArray(declared) ? declared.filter((s): s is string => typeof s === 'string' && s in SCOPE_TEXT) : []
  const roster = useQuery({ ...membersRosterQuery, enabled: scopes.length > 0 })
  const grants = useQuery<AppBotGrants>({
    queryKey: ['apps', appName, 'bot-grants'],
    queryFn: () => api.getAppBotGrants(appName),
    enabled: scopes.length > 0,
  })
  const [ticked, setTicked] = useState<string[] | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { setTicked(null); setSaved(false); setError('') }, [appName])
  if (scopes.length === 0) return null

  const current = ticked ?? grants.data?.bots ?? []
  const approved = grants.data?.scopes ?? []
  const bots = [...(roster.data ?? [])].sort((a, b) => compareText(a.display_name || a.name, b.display_name || b.name))
  // With no Bot to tick, the card points at creating one instead of a Save that can never apply.
  const noBots = Boolean(roster.data) && bots.length === 0
  const toggle = (slug: string) => {
    setSaved(false)
    setTicked(current.includes(slug) ? current.filter(s => s !== slug) : [...current, slug])
  }
  const save = async () => {
    setSaving(true)
    setError('')
    try {
      const result = await api.setAppBotGrants(appName, current)
      setTicked(result.bots)
      setSaved(true)
      await grants.refetch()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  // A failed load of either query, as an Error (the query client types it loosely).
  const queryError = (grants.error ?? roster.error ?? null) as Error | null
  return (
    <Card data-testid="app-bot-grants">
      <CardTitle>{i18nT('pages.appDetailPage.bots_title')}</CardTitle>
      <ul className="mt-2 grid gap-1 text-[13px] text-text list-disc pl-5">
        {scopes.map(s => (
          <li key={s}>
            {SCOPE_TEXT[s]()}
            {grants.data && !approved.includes(s) && <span className="ml-1 text-warn">· {i18nT('pages.appDetailPage.grant_not_approved')}</span>}
          </li>
        ))}
      </ul>
      {noBots ? (
        <div className="mt-3 text-[13px] text-muted">
          {i18nT('pages.appDetailPage.bots_none')}{' '}
          <Link to="/members?create=1" className="text-accent hover:underline">{i18nT('pages.membersPage.add_member')}</Link>
        </div>
      ) : (
        <div className="mt-3 text-[12px] text-muted">{i18nT('pages.appDetailPage.bots_pick_hint')}</div>
      )}
      <div className="mt-2 grid gap-1">
        {bots.map(bot => {
          const id = `bot-grant-${appName}-${bot.slug}`
          return (
            <label key={bot.slug} htmlFor={id} className="flex items-center gap-2 text-[13px] text-text cursor-pointer select-none">
              <Checkbox id={id} aria-label={bot.display_name || bot.name} checked={current.includes(bot.slug)} onChange={() => toggle(bot.slug)} disabled={!grants.data || saving} />
              <span className="truncate">{bot.display_name || bot.name}</span>
            </label>
          )
        })}
      </div>
      {(error || queryError) && (
        ticked === null ? (
          <ErrorNotice message={error || String(queryError?.message ?? '')} variant="inline" askAgent />
        ) : (
          // No hand-off: the unsaved ticks are local state that the hand-off's
          // navigation to chat would discard.
          <ErrorNotice message={error || String(queryError?.message ?? '')} variant="inline" />
        )
      )}
      {!noBots && (
        <div className="mt-3 flex items-center gap-2">
          <Btn onClick={save} disabled={!grants.data || saving || ticked === null}>{i18nT('pages.appDetailPage.bots_save')}</Btn>
          {saved && <span className="text-[12px] text-ok-text" role="status">{i18nT('pages.appDetailPage.bots_saved')}</span>}
        </div>
      )}
    </Card>
  )
}
