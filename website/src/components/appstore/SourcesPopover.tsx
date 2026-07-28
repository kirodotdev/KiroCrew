/**
 * SourcesPopover — the gear affordance on the Apps page header.
 *
 * Houses the store's supply-side controls, out of the shopper flow per the
 * locked hybrid mock: external registry management (add/remove/sync) and the
 * developer Install-from-Path escape hatch. Controlled open state so the
 * rail's "Add source" button can open it programmatically.
 */
import { useState } from 'react'
import { Database, FolderOpen } from 'lucide-react'
import { api } from '../../api/client'
import { useQueryClient } from '@tanstack/react-query'
import { recordEvent } from '../../rum'
import { Btn, Input, IconButton } from '../ui'
import { Popover, PopoverTrigger, PopoverContent } from '../ui/popover'
import RegistryManager from '../RegistryManager'

import { i18nT } from '../../i18n/t'
export default function SourcesPopover({ open, onOpenChange, onError }: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onError: (message: string) => void
}) {
  const queryClient = useQueryClient()
  const [installPath, setInstallPath] = useState('')
  const [installing, setInstalling] = useState(false)

  const handleInstall = async () => {
    if (!installPath.trim() || installing) return
    setInstalling(true)
    try {
      const result = await api.installApp(installPath.trim())
      recordEvent('app_install', { app: result.name || installPath.trim(), source: 'local' })
      setInstallPath('')
      queryClient.invalidateQueries({ queryKey: ['apps'] })
      window.dispatchEvent(new Event('mc:apps-changed'))
      onOpenChange(false)
    } catch (e) {
      onError((e as Error)?.message || 'Install failed')
    } finally {
      setInstalling(false)
    }
  }

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <IconButton aria-label={i18nT('components.appstore.sourcesPopover.manage_app_sources')} title={i18nT('components.appstore.sourcesPopover.manage_app_sources')}>
          <Database size={15} />
        </IconButton>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[480px] max-w-[92vw] max-h-[70vh] overflow-y-auto">
        <RegistryManager bare />
        <div className="border-t border-border mt-4 pt-4">
          <div className="text-sm font-semibold tracking-tight text-text-strong mb-2 flex items-center gap-2">
            {i18nT('components.appstore.sourcesPopover.install_from_path')}
          </div>
          <p className="text-[12px] text-muted mb-2.5">{i18nT('components.appstore.sourcesPopover.developer_install_of_a_local_app_directory_equiv')} <code className="bg-bg-elevated px-1 py-0.5 rounded">{i18nT('components.appstore.sourcesPopover.kirocrew_app_install_path')}</code>).</p>
          <div className="flex items-center gap-2">
            <FolderOpen size={15} className="text-muted shrink-0" />
            <Input
              placeholder={i18nT('components.appstore.sourcesPopover.path_to_app_directory')}
              value={installPath}
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => setInstallPath(e.target.value)}
              onKeyDown={(e: React.KeyboardEvent<HTMLInputElement>) => e.key === 'Enter' && handleInstall()}
              className="flex-1"
            />
            <Btn onClick={handleInstall} disabled={installing || !installPath.trim()}>
              {installing ? 'Installing…' : 'Install'}
            </Btn>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  )
}
