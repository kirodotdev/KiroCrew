import { i18nT } from '../i18n/t'

/** Who created a schedule, shown only for a Shared Crew guest: their display name, else "Guest". */
export default function JobCreator({ creator }: { creator?: string | null }) {
  if (creator == null) return null
  return <span data-testid="job-creator" className="block truncate text-[11px] font-normal text-muted">{creator || i18nT('pages.schedulePage.guest')}</span>
}
