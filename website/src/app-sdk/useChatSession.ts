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
import { sendTurn } from '../chat-core/transport/sendTurn'
import { type ChatFolderRow, ensureChatFolder } from '../utils/ensureChatFolder'
import { appApiSendWire } from './appSendWire'
import { i18nT } from '../i18n/t'

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
      const folderName = appName.charAt(0).toUpperCase() + appName.slice(1)
      // Through the app's permission-scoped client: the shared helper takes the
      // transport as parameters precisely so the SDK never imports the host `api`.
      const folderId = await ensureChatFolder({
        list: () => api.get<ChatFolderRow[]>('/api/chat/folders'),
        create: name => api.post<ChatFolderRow>('/api/chat/folders', { name }),
        name: folderName,
      })
      if (folderId) {
        await api.patch('/api/chat/slots/' + encodeURIComponent(slotKey) + '/folder', { folder_id: folderId })
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
      // The returned slot is what the hook stands on until the lookup lists it,
      // so a create that names no slot is a failure, not a session to wait for.
      if (!slot?.key) throw new Error(i18nT('appSdk.chatSession.create_returned_no_session') as string)
      const isPackage = workspacePath.includes('/src/')
      const seedMsg = seed({ label, path: workspacePath, isPackage })
      // The seed rides the same transport as every other send (JSON receipt,
      // shared classification) instead of the bare SSE endpoint whose parse
      // failure used to be swallowed as success. Fire-and-forget: the slot is
      // the deliverable, `sendTurn` never rejects, and the receipt is
      // deliberately not acted on -- a seed that did not go out is a session
      // that starts empty, not a lost user message -- so creation must not
      // wait out the transport deadline for an answer it will ignore.
      void sendTurn({ message: seedMsg, slot: slot.key, wire: appApiSendWire(api, agent) })
      folderAssignedRef.current = slot.key
      await assignFolder(slot.key)
      return slot
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['app-sdk-session', slotName] })
    },
  })

  const slot = slotData?.found ? slotData.slot : null
  // The lookup can lag the create, or never list its key; the created slot is
  // already real, so it makes the session ready on its own.
  const status: ChatSessionState['status'] = isLoading
    ? 'loading'
    : queryError
      ? 'error'
      : createMutation.error
        ? 'error'
        : (slot || createMutation.data)
          ? 'ready'
          : createMutation.isPending
            ? 'loading'
            : 'no-session'

  // One navigation, so one history entry and a single Back returns to the app.
  // The routed chat resolves `?sid=` on hot navigation, as `useChatLauncher` relies on.
  // The created key, when the lookup has not listed it, is the session to open.
  const slotKey = slot?.key ?? (createMutation.data?.key ?? null)
  const openChat = useCallback(() => {
    hostNavigate('/chat?sid=' + encodeURIComponent(slotKey ?? slotName))
  }, [hostNavigate, slotKey, slotName])

  const createSession = useCallback(async () => {
    await createMutation.mutateAsync()
  }, [createMutation])

  const resetSession = useCallback(() => {
    queryClient.removeQueries({ queryKey: ['app-sdk-session', slotName] })
    createMutation.reset()
  }, [queryClient, slotName, createMutation])

  return {
    status,
    slotKey,
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
