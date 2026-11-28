import { useCallback, useRef } from 'react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { Artifact } from '../../types'
import { artifactSlugFromHref } from '../../components/markdown/linkTargets'
import { dashboardPreviewTargetFromRef } from '../../utils/dashboardPreview'
import { i18nT } from '../../i18n/t'

/**
 * Where a link on a Bot's Dashboard tab opens, once the link bridge has passed it
 * (`useFrameLinkBridge`: this host's own frame, a web URL, a click in the frame).
 *
 * - `/artifacts/<slug>` on this dashboard's origin, for a slug the artifact library
 *   holds, opens that artifact in the Bot's side panel.
 * - An absolute path inside the Bot's workspace, written as a root-relative link
 *   (`/home/me/.../drafts/receipt.md`, the form a chat file link takes), opens in
 *   the side panel's file viewer through `openFile(path)`, the same open a chat
 *   file link makes. The workspace check is lexical, on whole segments, and a
 *   path with a `.`, `..` or empty segment is refused.
 * - A path on the workspace's first segment that fails that check, or a slug
 *   the library does not hold, opens nothing and says why.
 * - Anything else is left to the bridge, which opens it as before (a dashboard
 *   page in place, anything else in a new tab): another origin, another
 *   dashboard route, a deeper `/artifacts/...` route and a staged-dashboard
 *   reference.
 *
 * Nothing is posted back to the frame: the page cannot learn whether a file exists
 * or what it holds.
 */
export type DashboardLink =
  | { kind: 'artifact'; slug: string }
  | { kind: 'file'; path: string }
  | { kind: 'refused'; target: string }
  | { kind: 'external' }

/** The workspace directory with no trailing separator, or null when file links
 *  cannot be routed: unknown, the filesystem root itself, or not a POSIX path.
 *  shortcut: a Windows workspace (`C:\...`) routes no file links; add a drive-path
 *  link form if Windows Bots need them. */
function workspaceRoot(dir: string | null | undefined): string | null {
  if (!dir || !dir.startsWith('/') || dir.includes('\\')) return null
  const root = dir.replace(/\/+$/, '')
  return root ? root : null
}

/** Classify a URL the link bridge accepted. Pure: `origin` is the dashboard's own. */
export function classifyDashboardLink(url: string, origin: string, workspaceDir: string | null | undefined): DashboardLink {
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return { kind: 'external' }
  }
  if (parsed.origin !== origin) return { kind: 'external' }
  const slug = artifactSlugFromHref(parsed.pathname)
  if (slug !== null) {
    // Only `/artifacts/<slug>` itself, and only a plain slug; a deeper route keeps
    // opening as before. A staged-dashboard reference does too: in the panel it
    // would switch this page to another crewmate's preview.
    const exact = /^\/artifacts\/[^/]+\/?$/.test(parsed.pathname)
    if (!exact || dashboardPreviewTargetFromRef(slug)) return { kind: 'external' }
    if (!slug || /[\u0000-\u001f\u007f/\\]/.test(slug)) return { kind: 'refused', target: parsed.pathname }
    return { kind: 'artifact', slug }
  }
  const root = workspaceRoot(workspaceDir)
  if (!root) return { kind: 'external' }
  let path: string
  try {
    path = decodeURIComponent(parsed.pathname)
  } catch {
    return { kind: 'refused', target: parsed.pathname }
  }
  // A dashboard route (`/chat`, `/members`) never shares the workspace's first
  // segment (`/home`, `/Users`); a path that does is a file link, inside or not.
  const firstSegment = root.split('/')[1]
  if (path.split('/')[1] !== firstSegment) return { kind: 'external' }
  const segments = path.split('/').slice(1)
  const clean = !parsed.search && !parsed.hash
    && !/[\u0000-\u001f\u007f\\]/.test(path)
    && segments.every((s) => s !== '' && s !== '.' && s !== '..')
  if (!clean || !path.startsWith(root + '/')) return { kind: 'refused', target: path }
  return { kind: 'file', path }
}

async function artifactKnown(queryClient: QueryClient, slug: string): Promise<boolean> {
  try {
    // The key openArtifact reads, so the open that follows is served from it.
    await queryClient.fetchQuery<Artifact>({ queryKey: ['artifact', slug], queryFn: () => api.artifact(slug), staleTime: 10_000 })
    return true
  } catch {
    return false
  }
}

/** The claim the Bots page hands its Dashboard tab (`CrewDynamicDashboard`'s
 *  `onOpenLink`): true for a link it handled (opened in the panel, or refused). */
export function useDashboardPanelLinkOpener({ workspaceDir, slot, botId, botName, openArtifact, openFile, showRefusal }: {
  /** The Bot's workspace: the directory the panel's Workspace tab is rooted in. */
  workspaceDir: string | null | undefined
  slot: string | null | undefined
  /** Stable Bot identity, independent of the thread's pending slot confirmation. */
  botId: string
  botName: string
  openArtifact: (slug: string) => void
  openFile: (path: string) => void
  showRefusal: (message: string) => void
}): (url: string) => boolean {
  const queryClient = useQueryClient()
  const latestSlotRef = useRef(slot)
  latestSlotRef.current = slot
  const latestBotIdRef = useRef(botId)
  latestBotIdRef.current = botId
  return useCallback((url: string) => {
    const initiatingSlot = slot
    const initiatingBotId = botId
    const link = classifyDashboardLink(url, window.location.origin, workspaceDir)
    const refuse = (target: string) => showRefusal(i18nT('pages.membersPage.dashboard_link_refused', { target, name: botName }))
    if (link.kind === 'external') return false
    if (link.kind === 'refused') {
      refuse(link.target)
    } else if (link.kind === 'artifact') {
      void artifactKnown(queryClient, link.slug).then((known) => {
        // The Bot or its thread may have changed while the lookup ran.
        if (latestSlotRef.current !== initiatingSlot || latestBotIdRef.current !== initiatingBotId) return
        if (known) openArtifact(link.slug)
        else refuse(link.slug)
      })
    } else {
      openFile(link.path)
    }
    return true
  }, [queryClient, workspaceDir, slot, botId, botName, openArtifact, openFile, showRefusal])
}
