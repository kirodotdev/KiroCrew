/**
 * useChatSession — workspace-scoped chat session management for apps.
 *
 * Handles slot discovery, creation, seeding, and navigation.
 * Apps provide a workspace path; the hook manages the rest.
 *
 * Usage:
 *   const { useChatSession } = window.__kirocrew_modules['@kirocrew/app-sdk']
 *   const chat = useChatSession({ workspacePath: '/path/to/ws', label: 'CVS', agent: 'privacy-dev' })
 */
import { useCallback, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useAppApi, useNavigate } from './index'

export function hashStr(s: string): string {
  let h = 0
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0
  return (h >>> 0).toString(36)
}

export interface ChatSessionOptions {
  workspacePath: string
  label: string
  agent?: string
  appName?: string
  seedTemplate?: (opts: { label: string; path: string; isPackage: boolean }) => string
}

export interface ChatSessionState {
  status: 'loading' | 'ready' | 'no-session' | 'error'
  slotKey: string | null
  slotInfo: { key: string; title: string; messages: number; running: boolean } | null
  creating: boolean
  error: string | null
  openChat: () => void
  createSession: () => Promise<void>
  resetSession: () => void
}

/** Minimal shape of a chat folder returned by /api/chat/folders. */
interface ChatFolder {
  id: string
  name: string
}

/** Minimal shape of a chat slot returned by /api/chat/slots. */
interface ChatSlot {
  key: string
  title?: string
  messages?: number
  running?: boolean
}

const DEFAULT_SEED = ({ label, path, isPackage }: { label: string; path: string; isPackage: boolean }) =>
  `Session: ${label}\nWorking directory: ${path}\n` +
  `You are a developer assistant for this ${isPackage ? 'package' : 'workspace'}.\n\n` +
  `IMPORTANT: Always \`cd ${path}\` before running any commands.\n` +
  `You have full read access to all files.` +
  `\nKeep responses concise. Say "Ready" with the path you will work in.`

export function useChatSession(opts: ChatSessionOptions): ChatSessionState {
  const { workspacePath, label, agent = '', appName = 'workbench', seedTemplate } = opts
  const api = useAppApi()
  const hostNavigate = useNavigate()
  const queryClient = useQueryClient()

  const slotName = appName + '-' + hashStr(workspacePath)
  const seed = seedTemplate || DEFAULT_SEED

  // Best-effort folder assignment — extracted to avoid running on every refetch
  const assignFolder = useCallback(async (slotKey: string) => {
    try {
      const folders = await api.get<ChatFolder[]>('/api/chat/folders')
      const folderList = Array.isArray(folders) ? folders : []
      const folderName = appName.charAt(0).toUpperCase() + appName.slice(1)
      let folder = folderList.find((f: ChatFolder) => f.name === folderName)
      if (!folder) {
        folder = await api.post<ChatFolder>('/api/chat/folders', { name: folderName })
      }
      if (folder?.id) {
        await api.patch('/api/chat/slots/' + encodeURIComponent(slotKey) + '/folder', { folder_id: folder.id })
      }
    } catch {} // Folder assignment is best-effort — session works without it
  }, [api, appName])

  const { data: slotData, isLoading, error: queryError } = useQuery({
    queryKey: ['app-sdk-session', slotName],
    queryFn: async () => {
      const slots = await api.get<ChatSlot[]>('/api/chat/slots')
      const list = Array.isArray(slots) ? slots : []
      const found = list.find(s => s.key === slotName)
      return found ? { found: true as const, slot: found } : { found: false as const, slot: null }
    },
    staleTime: 30_000,
  })

  // Assign folder once when slot is first discovered (not on every refetch)
  const folderAssignedRef = useRef<string | null>(null)
  useEffect(() => {
    const key = slotData?.slot?.key
    if (key && key !== folderAssignedRef.current) {
      folderAssignedRef.current = key
      assignFolder(key)
    }
  }, [slotData?.slot?.key, assignFolder])

  const createMutation = useMutation({
    mutationFn: async () => {
      const slot = await api.post<ChatSlot>('/api/chat/slots', { name: slotName, agent })
      const isPackage = workspacePath.includes('/src/')
      const seedMsg = seed({ label, path: workspacePath, isPackage })
      try {
        await api.post('/api/chat', { message: seedMsg, slot: slot.key, agent })
      } catch {
        // Seed send may fail (SSE response parsed as JSON) — slot still created
      }
      folderAssignedRef.current = slot.key
      await assignFolder(slot.key)
      return slot
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['app-sdk-session', slotName] })
    },
  })

  const slot = slotData?.found ? slotData.slot : null
  const status: ChatSessionState['status'] = isLoading
    ? 'loading'
    : queryError
      ? 'error'
      : createMutation.error
        ? 'error'
        : slot
          ? 'ready'
          : (createMutation.isPending || (createMutation.isSuccess && !slot))
            ? 'loading'
            : 'no-session'

  const openChat = useCallback(() => {
    hostNavigate('/')
    setTimeout(() => hostNavigate('/chat?sid=' + encodeURIComponent(slotName)), 50)
  }, [hostNavigate, slotName])

  const createSession = useCallback(async () => {
    await createMutation.mutateAsync()
  }, [createMutation])

  const resetSession = useCallback(() => {
    queryClient.removeQueries({ queryKey: ['app-sdk-session', slotName] })
    createMutation.reset()
  }, [queryClient, slotName, createMutation])

  return {
    status,
    slotKey: slot?.key ?? (createMutation.data?.key ?? null),
    slotInfo: slot
      ? { key: slot.key, title: slot.title || label, messages: slot.messages || 0, running: !!slot.running }
      : createMutation.data
        ? { key: createMutation.data.key, title: label, messages: 0, running: true }
        : null,
    creating: createMutation.isPending,
    error: queryError?.message ?? createMutation.error?.message ?? null,
    openChat,
    createSession,
    resetSession,
  }
}
