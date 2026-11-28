/**
 * The Profile tab's "Asks you first" section: the limits a Bot's upgrade move could
 * not apply as written, which make it ask a person before the actions they cover,
 * and the one control that ends them all
 * (docs/decisions/2026-11-17-a-person-can-end-a-bots-held-limits-from-its-profile-tab.md).
 *
 * Shown only while one of them makes the Bot ask, and lists only those. Once a
 * release ends them, a short notice takes the section's place, so the person sees
 * that it worked. A caller the owner gate refuses (403) cannot release them, so it
 * sees nothing. A limit's text is stored text, rendered as a value.
 */
import { useId } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type MemberHeldLimit, type MemberRosterRow } from '../../api/client'
import type { CronJob } from '../../types'
import { crewDisplayName } from '../../components/AgentSelector'
import { useConfirm } from '../../components/ConfirmDialog'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'

/** What each capability covers; `tool:<name>` covers that one tool, and an unknown one every action. */
const COVERS: Record<string, string> = {
  shell: 'pages.membersPage.held_limits_cap_shell',
  fs_read: 'pages.membersPage.held_limits_cap_fs_read',
  fs_write: 'pages.membersPage.held_limits_cap_fs_write',
  web_fetch: 'pages.membersPage.held_limits_cap_web_fetch',
  web_search: 'pages.membersPage.held_limits_cap_web_search',
  subagent: 'pages.membersPage.held_limits_cap_subagent',
  mcp: 'pages.membersPage.held_limits_cap_mcp',
}
const EVERY_ACTION = 'pages.membersPage.held_limits_cap_any'
const TOOL_PREFIX = 'tool:'

/** One held limit as the card and the confirm both show it: what it asks before, and beneath it the stored text of the setting it came from. */
function HeldLimitLines({ limit }: { limit: MemberHeldLimit }) {
  const { t } = useTranslation()
  const cap = limit.capability
  return (
    <>
      <span className="text-text">
        {Object.hasOwn(COVERS, cap) ? t(COVERS[cap])
          : cap.startsWith(TOOL_PREFIX) ? t('pages.membersPage.held_limits_cap_tool', { tool: cap.slice(TOOL_PREFIX.length) })
          : t(EVERY_ACTION)}
      </span>
      <span className="block text-[12px] text-muted break-words">{t('pages.membersPage.held_limits_was', { text: limit.text })}</span>
    </>
  )
}

/** The section's own container, which the released notice keeps so it sits where the section was. */
const SECTION = 'rounded-2xl border border-border bg-bg px-3.5 py-3'

const status = (e: unknown): number | undefined => (e as { status?: number } | null)?.status

/** The Bot's approval mode in the words its Settings picker describes it with. */
const MODE_DESC: Record<string, string> = {
  normal: 'pages.membersPage.profile_mode_normal_desc',
  trust_reads: 'pages.membersPage.profile_mode_reads_desc',
}

export default function CrewHeldLimits({ member, schedules, schedulesLoading, schedulesError }: {
  member: MemberRosterRow
  schedules: CronJob[]
  schedulesLoading: boolean
  schedulesError?: boolean
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const schedulesLabelId = useId()
  const queryKey = ['member-held-limits', member.slug, member.name]
  const held = useQuery({
    queryKey,
    queryFn: () => api.memberHeldLimits(member.slug, member.name),
  })
  const release = useMutation({
    mutationFn: (limits: MemberHeldLimit[]) => api.memberHeldLimitsRelease(member.slug, member.name, limits),
    onSettled: () => queryClient.invalidateQueries({ queryKey }),
  })

  if (held.isError) {
    if (status(held.error) === 403) return null
    return <ErrorNotice message={t('pages.membersPage.held_limits_load_failed')} askAgent testId="crew-held-limits-error" />
  }
  const limits = held.data?.limits ?? []
  const name = crewDisplayName(member)
  // A limit that never makes the Bot ask is not listed, but the release still ends it.
  const asking = limits.filter(l => l.capability !== 'none')
  if (asking.length === 0) {
    if (!release.isSuccess) return null
    return (
      <section className={SECTION} role="status" data-testid="crew-held-limits-released">
        <p className="text-[13px] leading-relaxed text-text">{t('pages.membersPage.held_limits_released', { name })}</p>
      </section>
    )
  }
  const readsOwnFolder = held.data?.kiro_cli && limits.some(l => l.capability === 'fs_read' || l.capability === '*')
  const mode = String(member.approval_mode || 'trust')
  // A schedule that approves on its own runs the released actions whatever the mode says.
  // An unread schedule list is not an empty one, so the release waits for a successful read.
  const autos = schedules.filter(j => j.enabled && j.approval_mode === 'auto')

  const onRelease = async () => {
    const ok = await confirm({
      title: t('pages.membersPage.held_limits_confirm_title'),
      body: (
        <>
          <p>{t('pages.membersPage.held_limits_confirm_body', { name })}</p>
          <ul className="mt-2 list-disc pl-5" data-testid="crew-held-limits-confirm-list">
            {asking.map((l, i) => <li key={i}><HeldLimitLines limit={l} /></li>)}
          </ul>
          <p className="mt-2" data-testid="crew-held-limits-confirm-mode">
            {t('pages.membersPage.held_limits_mode_after', {
              mode: t(Object.hasOwn(MODE_DESC, mode) ? MODE_DESC[mode] : 'pages.membersPage.profile_mode_trust_desc', { name }),
            })}
          </p>
          {autos.length > 0 && (
            <div className="mt-3">
              <p id={schedulesLabelId}>{t('pages.membersPage.held_limits_auto_schedules', { count: autos.length })}</p>
              <ul className="mt-1 list-disc pl-5" aria-labelledby={schedulesLabelId} data-testid="crew-held-limits-confirm-schedules">
                {autos.map(j => <li key={j.id}>{j.name}</li>)}
              </ul>
            </div>
          )}
        </>
      ),
      confirmLabel: t('pages.membersPage.held_limits_confirm'),
      danger: true,
    })
    if (ok) release.mutate(limits)
  }

  return (
    <section className={SECTION} data-testid="crew-held-limits">
      <div className="text-[11px] font-semibold tracking-wide uppercase text-muted mb-1">{t('pages.membersPage.held_limits_heading')}</div>
      <p className="text-[13px] leading-relaxed text-text">{t('pages.membersPage.held_limits_lead')}</p>
      {readsOwnFolder && (
        <p className="mt-1 text-[12.5px] leading-relaxed text-muted" data-testid="crew-held-limits-kiro-cli">{t('pages.membersPage.held_limits_kiro_cli')}</p>
      )}
      <ul className="mt-2 flex flex-col gap-1.5">
        {asking.map((l, i) => (
          <li key={i} className="text-[13px] leading-snug" data-testid="crew-held-limit">
            <HeldLimitLines limit={l} />
          </li>
        ))}
      </ul>
      {release.isError && (
        <div className="mt-2">
          <ErrorNotice
            message={t(status(release.error) === 409 ? 'pages.membersPage.held_limits_changed' : 'pages.membersPage.held_limits_release_failed')}
            askAgent
            testId="crew-held-limits-release-error"
          />
        </div>
      )}
      {schedulesError && (
        <div className="mt-2">
          <ErrorNotice message={t('pages.membersPage.schedule_load_failed')} askAgent testId="crew-held-limits-schedules-error" />
        </div>
      )}
      <Btn
        type="button"
        danger
        className="mt-2.5"
        onClick={() => void onRelease()}
        disabled={release.isPending || schedulesLoading || !!schedulesError}
        data-testid="crew-held-limits-release"
      >
        {t('pages.membersPage.held_limits_release')}
      </Btn>
      {confirmDialog}
    </section>
  )
}
