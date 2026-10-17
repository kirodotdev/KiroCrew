import { useEffect, useId, useState, type FormEvent, type Ref } from 'react'
import { RefreshCw } from 'lucide-react'
import { ProviderLogo } from '../../components/ProviderBadge'
import type { GitLabGroup } from '../../api'
import ErrorNotice from '../../../../components/ErrorNotice'
import { useIssueRadar } from '../../context'
import { gitlabGroupErrorText, useGitlabGroupActions, useGitlabGroups } from '../../lib/gitlabGroups'
import { safeGetSessionItem, safeSetSessionItem } from '../../../../utils/safeStorage'

import { i18nT } from '../../../../i18n/t'

const GITLAB_REF = { provider: 'gitlab' as const }

/** The group path being typed, mirrored to this tab's sessionStorage. An
 * "Ask the agent" hand-off navigates to the chat and unmounts this section, so
 * the draft lives outside it and the field shows it again on return. */
const GITLAB_GROUP_DRAFT_KEY = 'kc:issue-radar:gitlab-group-draft'

const groupKey = (g: GitLabGroup) => `${g.host}/${g.path}`

/** Settings section for following GitLab groups. Following a group adds the
 * "Review requests" dashboard, which lists the merge requests waiting for your
 * review across the group and all its subgroups. This section only follows and
 * unfollows: it never calls GitLab except to confirm a group exists when it is
 * followed. */
export default function GitLabGroupsSection({ sectionRef }: { sectionRef?: Ref<HTMLElement> }) {
  const { openDashboard } = useIssueRadar()
  const groups = useGitlabGroups()
  const { follow, unfollow } = useGitlabGroupActions()
  const [value, setValue] = useState(() => safeGetSessionItem(GITLAB_GROUP_DRAFT_KEY) ?? '')
  const [pending, setPending] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
  const inputId = useId()
  const hintId = useId()

  useEffect(() => {
    safeSetSessionItem(GITLAB_GROUP_DRAFT_KEY, value)
  }, [value])

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    const text = value.trim()
    if (!text || pending) return
    setPending(true)
    setAddError(null)
    try {
      await follow(text)
      // Clear only what was submitted: text typed while the request was in
      // flight is a new draft and stays.
      setValue((current) => (current.trim() === text ? '' : current))
    } catch (err) {
      setAddError(gitlabGroupErrorText(err))
    } finally {
      setPending(false)
    }
  }

  const followed = groups.data ?? []

  return (
    <section ref={sectionRef} className="mt-10 scroll-mt-8" aria-labelledby={`${inputId}-title`}>
      <div className="flex items-baseline justify-between mb-3">
        <h2 id={`${inputId}-title`} className="text-[13px] font-semibold text-muted uppercase tracking-[.06em]">
          {i18nT('apps.issueRadar.views.settings.gitlabGroups.title')}
        </h2>
      </div>
      <div className="rounded-xl border border-border bg-bg-elevated shadow-sm p-5">
        <p className="text-[13px] text-muted mb-4">
          {i18nT('apps.issueRadar.views.settings.gitlabGroups.intro')}
        </p>

        {groups.isError && (
          <ErrorNotice
            message={i18nT('apps.issueRadar.views.settings.gitlabGroups.list_error', { error: gitlabGroupErrorText(groups.error) })}
            variant="inline"
            askAgent
            className="mb-3"
          />
        )}

        {groups.data && followed.length === 0 && (
          <p className="text-[13px] text-muted mb-4">
            {i18nT('apps.issueRadar.views.settings.gitlabGroups.empty')}
          </p>
        )}

        {followed.length > 0 && (
          <>
            <ul className="flex flex-col gap-2 mb-3 list-none p-0 m-0">
              {followed.map((g) => (
                <GroupRow key={groupKey(g)} group={g} onUnfollow={unfollow} />
              ))}
            </ul>
            <button
              type="button"
              onClick={() => openDashboard('reviews')}
              className="mb-4 text-[12px] text-accent hover:underline cursor-pointer bg-transparent border-0 p-0"
            >
              {i18nT('apps.issueRadar.views.settings.gitlabGroups.open_reviews')}
            </button>
          </>
        )}

        <form onSubmit={submit} className="flex flex-col gap-1.5">
          <label id={`${inputId}-label`} htmlFor={inputId} className="text-[13px] font-medium">
            {i18nT('apps.issueRadar.views.settings.gitlabGroups.group_label')}
          </label>
          <div className="flex flex-col gap-2 sm:flex-row">
            <input
              id={inputId}
              type="text"
              aria-labelledby={`${inputId}-label`}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder={i18nT('apps.issueRadar.views.settings.gitlabGroups.group_placeholder')}
              aria-describedby={hintId}
              aria-invalid={addError ? true : undefined}
              spellCheck={false}
              autoComplete="off"
              className="flex-1 min-w-0 px-2.5 py-1.5 rounded-md border border-border bg-bg text-[13px] text-text outline-hidden focus:border-accent"
            />
            <button
              type="submit"
              disabled={!value.trim() || pending}
              className="inline-flex items-center justify-center gap-1 px-4 py-1.5 rounded-md border border-accent text-accent bg-transparent text-xs font-semibold cursor-pointer hover:bg-accent-subtle disabled:opacity-30 disabled:cursor-default"
            >
              {pending && <RefreshCw size={12} className="animate-spin" aria-hidden="true" />}
              {pending
                ? i18nT('apps.issueRadar.views.settings.gitlabGroups.following')
                : i18nT('apps.issueRadar.views.settings.gitlabGroups.follow')}
            </button>
          </div>
          <p id={hintId} className="text-[12px] text-muted">
            {i18nT('apps.issueRadar.views.settings.gitlabGroups.group_hint')}
          </p>
          {/* Hand-off on: the group path in the field above is mirrored to
              sessionStorage, so the chat round trip keeps it. */}
          <ErrorNotice message={addError} variant="inline" askAgent />
        </form>
      </div>
    </section>
  )
}

/** One followed group, with a labelled Unfollow button. */
function GroupRow({ group, onUnfollow }: {
  group: GitLabGroup
  onUnfollow: (group: GitLabGroup) => Promise<void>
}) {
  const [removing, setRemoving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const unfollow = async () => {
    setRemoving(true)
    setError(null)
    try {
      await onUnfollow(group)
    } catch (err) {
      setError(gitlabGroupErrorText(err))
      setRemoving(false)
    }
  }

  return (
    <li className="rounded-lg border border-border px-3 py-2" data-testid="gitlab-group-row">
      <div className="flex items-center gap-2">
        <ProviderLogo repoRef={GITLAB_REF} size={14} />
        <span className="text-[14px] font-medium truncate">{group.path}</span>
        {group.host !== 'gitlab.com' && (
          <span className="text-[12px] text-muted truncate">{group.host}</span>
        )}
        <span className="flex-1" />
        <button
          type="button"
          onClick={unfollow}
          disabled={removing}
          aria-label={i18nT('apps.issueRadar.views.settings.gitlabGroups.unfollow_group', { path: group.path })}
          className="px-2.5 py-1 rounded-md border border-border text-[12px] text-text bg-transparent cursor-pointer hover:bg-bg-hover disabled:opacity-30 disabled:cursor-default"
        >
          {removing
            ? i18nT('apps.issueRadar.views.settings.gitlabGroups.unfollowing')
            : i18nT('apps.issueRadar.views.settings.gitlabGroups.unfollow')}
        </button>
      </div>
      {error && (
        <ErrorNotice
          message={i18nT('apps.issueRadar.views.settings.gitlabGroups.unfollow_error', { error })}
          variant="inline"
          askAgent
          className="mt-2"
        />
      )}
    </li>
  )
}
