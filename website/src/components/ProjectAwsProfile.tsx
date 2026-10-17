import { Check, ChevronDown, ChevronRight, Cloud, TriangleAlert } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, type ProjectAwsProfile } from '../api/client'
import { ApiError } from '../api/apiError'
import { i18nT } from '../i18n/t'
import ErrorNotice from './ErrorNotice'

/** The AWS profile-name charset the server enforces (`AWS_PROFILE_NAME_RE`). A UX hint only:
 *  the PUT re-validates and refuses anything else. */
const AWS_PROFILE_NAME = /^[A-Za-z0-9_.+][A-Za-z0-9_.+-]{0,127}$/

export const projectAwsProfileQueryKey = (project: string) => ['project-aws-profile', project] as const

/** The binding read shared by the pill status and the picker section. A 403 is the
 *  owner-only gate answering a non-owner, which is an expected outcome, not a failure. */
function useProjectAwsProfile(project: string) {
  const query = useQuery({
    queryKey: projectAwsProfileQueryKey(project),
    queryFn: () => api.projectAwsProfile(project),
    staleTime: 60_000,
    retry: false,
    enabled: !!project,
  })
  const refused = query.error instanceof ApiError && query.error.status === 403
  return { ...query, refused }
}

function statusTitle(data: ProjectAwsProfile, profile: string): string {
  const head = data.status === 'expired'
    ? i18nT('components.projectAwsProfile.expired_title', { profile })
    : data.status === 'unavailable'
      ? i18nT('components.projectAwsProfile.unavailable_title', { profile })
      : i18nT('components.projectAwsProfile.bound_title', { profile })
  const inherited = !!data.boundDir && data.boundDir !== data.path
  return inherited ? `${head}\n${i18nT('components.projectAwsProfile.inherited_from', { dir: data.boundDir })}` : head
}

/**
 * The AWS profile a project is bound to (`aws.project_profiles`), shown in the composer's
 * project pill: which account a session's AWS commands reach by default, amber when that
 * profile's sign-in no longer resolves (an expired SSO session reads `expired`).
 *
 * Read-only on purpose: the pill already carries its two controls (the folder picker and the
 * branch copy), so the binding is changed from the project picker the folder opens
 * (`ProjectAwsProfileSection`). Renders nothing for an unbound project or a refused read.
 */
export default function ProjectAwsProfileStatus({ project, shelfCompact }: { project: string; shelfCompact?: boolean }) {
  const { data, error, refused } = useProjectAwsProfile(project)
  if (error && !refused) {
    // A failed read must not look like an unbound project. Compact on purpose: the pill
    // has no room for a sentence, so the reason rides the tooltip.
    // No hand-off: this segment renders inside the chat composer (ChatInput), whose unsent
    // message draft shares this tree; the hand-off's navigation would unmount it.
    return (
      <span className="inline-flex items-center gap-1 min-w-0" data-aws-status="read-failed">
        <span className="opacity-40 shrink-0" aria-hidden="true">·</span>
        <ErrorNotice
          variant="inline"
          className="min-w-0"
          messageClassName="truncate"
          message={i18nT('components.projectAwsProfile.read_failed')}
          messageTooltip={error.message}
        />
      </span>
    )
  }
  const profile = data?.profile || ''
  if (!data || !profile) return null
  const warn = data.status === 'expired' || data.status === 'unavailable'
  const title = statusTitle(data, profile)
  return (
    <span
      className={`inline-flex items-center gap-1 min-w-0 ${warn ? 'text-warn' : 'text-muted'}`}
      title={title}
      data-aws-status={data.status || 'ok'}
    >
      <span className="opacity-40 shrink-0" aria-hidden="true">·</span>
      {warn
        ? <TriangleAlert size={12} className="shrink-0" aria-hidden="true" />
        : <Cloud size={12} className="shrink-0 opacity-70" aria-hidden="true" />}
      {!shelfCompact && <span className="truncate max-w-[140px] font-mono" aria-hidden="true">{profile}</span>}
      <span className="sr-only">{title}</span>
    </span>
  )
}

/**
 * The project picker's AWS profile row: binds the chat's project to a named profile, to a
 * typed name, or back to the default credential chain. A binding applies to sessions
 * started after it. Collapsed to one line until opened, so the directory listing above keeps
 * its room. Hidden when the read is refused (owner-only) and for an unbound project when no
 * profile name is known, so a dashboard that never uses AWS gains no control.
 *
 * `errorHandoff` follows the picker's own mount opt-in: the read-failure notice hands off to
 * the agent only where nothing unsaved sits beneath the picker.
 */
export function ProjectAwsProfileSection({ project, errorHandoff = false, onHandoff }: { project: string; errorHandoff?: boolean; onHandoff?: () => void }) {
  const queryClient = useQueryClient()
  const { data, error, refused } = useProjectAwsProfile(project)
  const [expanded, setExpanded] = useState(false)
  const [custom, setCustom] = useState('')
  // Whether the save in flight (or last failed) came from the typed-name field, so a
  // failed list pick is not told to check a name the user did not type.
  const [typedSave, setTypedSave] = useState(false)
  const listId = useId()
  const saveErrorRef = useRef<HTMLDivElement | null>(null)
  const save = useMutation({
    mutationFn: (profile: string) => api.setProjectAwsProfile(project, profile),
    onSuccess: (next: ProjectAwsProfile) => {
      queryClient.setQueryData(projectAwsProfileQueryKey(project), next)
      // The PUT answers without a health word; re-read for it.
      void queryClient.invalidateQueries({ queryKey: projectAwsProfileQueryKey(project) })
      setCustom('')
    },
  })

  const saveError = save.error
  useEffect(() => {
    // jsdom has no layout, so scrollIntoView may be absent there.
    if (saveError) saveErrorRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [saveError])

  if (!project || refused) return null
  if (error) {
    return (
      <div className="px-3 py-2 border-t border-border" data-testid="pp-aws-profile">
        <ErrorNotice
          variant="inline"
          messagePlacement="below"
          className="whitespace-normal"
          askAgent={errorHandoff}
          onHandoff={onHandoff}
          title={i18nT('components.projectAwsProfile.read_failed')}
          message={error.message}
        />
      </div>
    )
  }
  if (!data) return null
  const profile = data.profile || ''
  if (!profile && data.profiles.length === 0) return null

  const inherited = !!profile && !!data.boundDir && data.boundDir !== data.path
  const names = profile && !data.profiles.includes(profile) ? [...data.profiles, profile] : data.profiles
  const customValid = AWS_PROFILE_NAME.test(custom.trim())
  const warn = !!profile && (data.status === 'expired' || data.status === 'unavailable')
  // Named after the directory it binds, so the row cannot be read as belonging to
  // whichever project row is highlighted in the listing above it.
  const rowTitle = i18nT('components.projectAwsProfile.row_title', {
    name: data.path.split(/[\\/]/).filter(Boolean).pop() || data.path,
  })
  const pick = (name: string, typed = false) => {
    if (save.isPending || name === profile) return
    save.reset()
    setTypedSave(typed)
    save.mutate(name)
  }

  const option = (name: string, label: string, disabled = false) => {
    const checked = name === profile
    return (
      <button
        key={name || '__default__'}
        type="button"
        role="radio"
        aria-checked={checked}
        disabled={disabled || save.isPending}
        onClick={() => pick(name)}
        className={`w-full flex items-center gap-2 px-2 py-1 rounded-md text-left bg-transparent border-none cursor-pointer hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-50 ${checked ? 'text-accent' : 'text-text'}`}
      >
        <span className="w-3 shrink-0">{checked && <Check size={12} aria-hidden="true" />}</span>
        <span className={`truncate ${name ? 'font-mono' : ''}`}>{label}</span>
      </button>
    )
  }

  return (
    <div className="px-3 py-1.5 border-t border-border flex flex-col gap-1 text-[12px] max-h-[55%] overflow-y-auto shrink-0" data-testid="pp-aws-profile">
      <button
        type="button"
        aria-expanded={expanded}
        aria-controls={listId}
        onClick={() => setExpanded(v => !v)}
        title={profile ? statusTitle(data, profile) : undefined}
        className="w-full flex items-center gap-1.5 py-1 bg-transparent border-none cursor-pointer text-left text-muted hover:text-text"
      >
        {expanded ? <ChevronDown size={12} aria-hidden="true" /> : <ChevronRight size={12} aria-hidden="true" />}
        {warn
          ? <TriangleAlert size={12} className="text-warn shrink-0" aria-hidden="true" />
          : <Cloud size={12} className="opacity-70 shrink-0" aria-hidden="true" />}
        <span className="min-w-0 truncate">{rowTitle}</span>
        <span className={`ml-auto shrink-0 max-w-[50%] truncate ${profile ? 'font-mono' : ''} ${warn ? 'text-warn' : 'text-text'}`}>
          {profile || i18nT('components.projectAwsProfile.none_short')}
        </span>
      </button>
      {expanded && (
        <div id={listId} className="flex flex-col gap-1 pl-4">
          {/* At the moment of choice, above the choices: what a pick does and does not do. */}
          <div className="text-[11px] leading-snug text-text">{i18nT('components.projectAwsProfile.applies_next')}</div>
          {inherited && (
            <div className="text-[11px] leading-snug text-muted">
              {i18nT('components.projectAwsProfile.inherited_note', { dir: data.boundDir })}
            </div>
          )}
          <div className="text-[11px] font-mono text-muted truncate" title={data.path}>{data.path}</div>
          {warn && (
            <div className="flex gap-1.5 items-start text-[11px] leading-snug text-warn">
              <TriangleAlert size={12} className="shrink-0 mt-px" aria-hidden="true" />
              {data.status === 'expired'
                ? i18nT('components.projectAwsProfile.expired_title', { profile })
                : i18nT('components.projectAwsProfile.unavailable_title', { profile })}
            </div>
          )}
          <div role="radiogroup" aria-label={rowTitle} className="flex flex-col">
            {names.map(name => option(name, name))}
            {option('', i18nT('components.projectAwsProfile.default_chain'), inherited)}
          </div>
          <form
            className="flex items-center gap-1.5"
            onSubmit={e => { e.preventDefault(); if (customValid) pick(custom.trim(), true) }}
          >
            <input
              value={custom}
              onChange={e => setCustom(e.target.value)}
              aria-label={i18nT('components.projectAwsProfile.other_profile')}
              placeholder={i18nT('components.projectAwsProfile.other_profile')}
              maxLength={128}
              spellCheck={false}
              className="flex-1 min-w-0 px-1.5 py-1 font-mono rounded bg-bg border border-border text-text"
            />
            <button
              type="submit"
              disabled={!customValid || save.isPending}
              className="px-2 py-1 text-[11px] bg-accent/20 text-accent rounded hover:bg-accent/30 disabled:opacity-40 disabled:cursor-not-allowed border-none cursor-pointer"
            >
              {i18nT('components.projectAwsProfile.bind')}
            </button>
          </form>
          {!!custom.trim() && !customValid && (
            <div className="text-[11px] leading-snug text-warn">{i18nT('components.projectAwsProfile.invalid_name')}</div>
          )}
        </div>
      )}
      {/* Below the list, and scrolled into view when it appears, so a failed save is seen
          whichever row was clicked.
          No hand-off: the typed profile name in the field above is not saved yet. */}
      <div ref={saveErrorRef}>
        <ErrorNotice
          variant="inline"
          className="whitespace-normal pl-4"
          message={save.error
            ? i18nT(typedSave ? 'components.projectAwsProfile.save_failed_typed' : 'components.projectAwsProfile.save_failed')
            : null}
          messageTooltip={save.error ? save.error.message : undefined}
        />
      </div>
    </div>
  )
}
