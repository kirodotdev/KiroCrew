// SettingsModal — override where specs are stored (empty = keep specs inside
// each project's .kiro/specs). Reads GET /settings, writes POST /settings.
//
// Chrome comes from the shared <Modal>: role="dialog", aria-modal, Escape and
// backdrop dismissal, scroll lock and the labelled close button are all owned
// by the host component rather than re-implemented here.
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { specApi } from '../api'
import { Btn } from './shared'
import { Input } from '../../../components/ui'
import Modal from '../../../components/Modal'

import { i18nT } from '../../../i18n/t'
export interface SettingsModalProps {
  onClose: () => void
  setErr: (msg: string) => void
}

export default function SettingsModal({ onClose, setErr }: SettingsModalProps) {
  const [basePath, setBasePath] = useState('')

  // Server read through React Query (repo `use-react-query` rule); the input
  // stays local state because it is an edit buffer, seeded once the read lands.
  const settingsQuery = useQuery({
    queryKey: ['spec-builder', 'settings'],
    queryFn: () => specApi.getSettings(),
  })
  useEffect(() => {
    if (settingsQuery.data) setBasePath(settingsQuery.data.base_path || '')
  }, [settingsQuery.data])
  // Report a failed read through the page-level error the caller already owns.
  // Save is disabled in that state (see the footer), and a control that is
  // disabled for no visible reason is its own defect.
  useEffect(() => {
    if (settingsQuery.isError) setErr((settingsQuery.error as Error).message)
  }, [settingsQuery.isError, settingsQuery.error, setErr])

  // A mutation that SEEDS the cache with the saved value. Without this the
  // settings query stayed cached for its stale window, so reopening the modal
  // within ~30s showed the OLD path and made the save look like it had not taken.
  const queryClient = useQueryClient()
  const saveMutation = useMutation({
    mutationFn: (base: string) => specApi.saveSettings(base),
    onSuccess: (_data, base) => {
      queryClient.setQueryData(['spec-builder', 'settings'], { base_path: base })
      void queryClient.invalidateQueries({ queryKey: ['spec-builder', 'settings'] })
      onClose()
    },
    onError: (e) => setErr((e as Error).message),
  })
  const busy = saveMutation.isPending
  // basePath is seeded from the query, so until the read LANDS the buffer is still
  // the initial '' -- saving then would overwrite a configured path with nothing.
  // Guarding on the write alone (`busy`) left exactly that window open.
  const unloaded = settingsQuery.isPending || settingsQuery.isError
  const save = () => saveMutation.mutate(basePath.trim())

  return (
    <Modal
      open
      onClose={onClose}
      title={i18nT('apps.specBuilder.components.settingsModal.settings')}
      maxWidth={520}
      footer={
        <>
          <Btn label={i18nT('apps.specBuilder.components.settingsModal.cancel')} onClick={onClose} />
          <Btn label={busy ? i18nT('apps.specBuilder.components.settingsModal.saving') : i18nT('apps.specBuilder.components.settingsModal.save')} primary disabled={busy || unloaded} onClick={save} />
        </>
      }
    >
      {/* The field is named via aria-label + described by the help text.
          A <label htmlFor> can't satisfy jsx-a11y/label-has-for here because
          the control is the shared <Input> component, not a native element the
          rule can see nested — aria naming is equivalent for screen readers. */}
      <div className="text-[13px] font-semibold mb-1.5">{i18nT('apps.specBuilder.components.settingsModal.where_should_specs_be_saved')}</div>
      <div id="sb-base-path-help" className="text-[12px] text-muted mb-2.5 leading-relaxed">
        {i18nT('apps.specBuilder.components.settingsModal.leave_this_empty_to_keep_specs_inside_each_proje')}
      </div>
      <Input
        id="sb-base-path"
        aria-describedby="sb-base-path-help"
        aria-label={i18nT('apps.specBuilder.components.settingsModal.spec_storage_folder_path')}
        className="w-full"
        value={basePath}
        onChange={(e) => setBasePath(e.target.value)}
        placeholder={i18nT('apps.specBuilder.components.settingsModal.empty_keep_specs_with_each_project_recommended')}
      />
    </Modal>
  )
}
