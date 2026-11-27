import { ExternalLink, Monitor, Undo2 } from 'lucide-react'
import { DropdownMenuItem } from './ui/dropdown-menu'
import { useArtifactPopouts } from '../hooks/useArtifactPopouts'
import { i18nT } from '../i18n/t'

/**
 * Pop-out entries of an artifact page's More menu: "Pop out to window" while the
 * artifact is here, or Focus + Bring back once it lives in its own window. Shared
 * by the generic and canvas detail pages. Not rendered inside the popout window
 * itself (the frame's Return button closes it), so the `useArtifactPopouts`
 * subscription only runs on the main dashboard.
 */
export function ArtifactPopoutMenuItems({ slug, name }: { slug: string; name: string }) {
  const { isPoppedOut, open, focus, bringBack } = useArtifactPopouts()
  if (isPoppedOut(slug)) {
    return (
      <>
        <DropdownMenuItem onSelect={() => focus(slug)}>
          <Monitor size={13} className="shrink-0 text-accent" aria-hidden="true" />
          <span>{i18nT('pages.artifactDetailPage.focus_popped_out_window')}</span>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => bringBack(slug)}>
          <Undo2 size={13} className="shrink-0 text-muted" aria-hidden="true" />
          <span>{i18nT('pages.artifactDetailPage.bring_artifact_back_to_this_window')}</span>
        </DropdownMenuItem>
      </>
    )
  }
  return (
    <DropdownMenuItem onSelect={() => open(slug, name)}>
      <ExternalLink size={13} className="shrink-0 text-muted" aria-hidden="true" />
      <span>{i18nT('pages.artifactDetailPage.pop_out_to_window')}</span>
    </DropdownMenuItem>
  )
}
