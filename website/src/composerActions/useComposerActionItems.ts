import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { resolveComposerActions, type ComposerActionItem } from './composerActionRegistry'

const NONE: ComposerActionItem[] = []

function select(d: unknown): ComposerActionItem[] {
  const apps = Array.isArray(d) ? d : (d as { apps?: unknown[] })?.apps
  return resolveComposerActions((apps as never[]) || [])
}

/** The approved composer actions of every enabled app, from the shared apps list. */
export function useComposerActionItems(): readonly ComposerActionItem[] {
  const { data } = useQuery({
    queryKey: ['apps'],
    queryFn: () => (typeof api?.listApps === 'function' ? api.listApps() : []),
    select,
  })
  return data ?? NONE
}
