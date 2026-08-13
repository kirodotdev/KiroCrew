import { useEffect } from 'react'
import { useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { PictureInPicture2 } from 'lucide-react'
import { api } from '../api/client'
import { Btn } from '../components/ui'
import { registerPopout, returnSelfToMain } from '../utils/artifactPopout'
import ArtifactDetailPage from './ArtifactDetailPage'
import type { Artifact } from '../types'

import { i18nT } from '../i18n/t'
/**
 * The window shell for a popped-out artifact (`/popout/artifact/:slug`).
 *
 * Renders the full artifact detail page (`ArtifactDetailPage popout`) — a
 * separate JS context with its own WebSocket + store, so it stays live and
 * independent (versions, comments, edits all work). On top it (1) registers as
 * a live popout so the main dashboard can track / focus / bring it back, (2)
 * mirrors the artifact name into the OS window/taskbar, and (3) offers a
 * "Return" affordance that focuses the opener and closes the popout.
 *
 * The `useQuery` here shares react-query's `['artifact', slug]` cache with the
 * inner detail page, so reading the name for the title costs no extra fetch.
 */
export default function ArtifactPopoutFrame() {
  const { slug = '' } = useParams<{ slug: string }>()
  const { data: artifact } = useQuery<Artifact>({
    queryKey: ['artifact', slug],
    queryFn: () => api.artifact(slug),
    enabled: !!slug,
  })

  // Announce presence for `slug` and wire the heartbeat/control responder.
  useEffect(() => {
    if (!slug) return
    return registerPopout(slug)
  }, [slug])

  // Reflect the artifact in the window/taskbar title so multiple popouts are
  // distinguishable at the OS level.
  useEffect(() => {
    const label = artifact?.name || slug || i18nT('pages.artifactPopoutFrame.artifact')
    document.title = i18nT('pages.artifactPopoutFrame.window_title', { label })
  }, [slug, artifact?.name])

  return (
    <div className="h-screen w-screen overflow-hidden bg-bg flex flex-col relative">
      <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
        <ArtifactDetailPage popout />
      </div>
      <Btn
        onClick={returnSelfToMain}
        className="fixed bottom-3 right-3 z-[60] opacity-60 hover:opacity-100 bg-bg-elevated"
        aria-label={i18nT('pages.artifactPopoutFrame.return_to_main_window_and_close_this_popout')}
        title={i18nT('pages.artifactPopoutFrame.return_to_main_window_and_close_this_popout')}
      >
        <PictureInPicture2 size={13} /> {i18nT('pages.artifactPopoutFrame.return')}
      </Btn>
    </div>
  )
}
