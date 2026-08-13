// SettingsModal — override where specs are stored (empty = keep specs inside
// each project's .kiro/specs) and which model runs spec generation (empty =
// inherit the chat default). Reads GET /settings, writes POST /settings.
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
import SimpleSelect from '../../../components/SimpleSelect'
import { useAvailableModels } from '../../../hooks/useAvailableModels'

import { i18nT } from '../../../i18n/t'
export interface SettingsModalProps {
  onClose: () => void
  setErr: (msg: string) => void
}

export default function SettingsModal({ onClose, setErr }: SettingsModalProps) {
  const [basePath, setBasePath] = useState('')
  // Explicit model pick for spec generation. '' = inherit whatever the chat
  // session would resolve to — mirrors the Research app's per-campaign picker.
  const [model, setModel] = useState('')
  const availableModels = useAvailableModels()

  // Server read through React Query (repo `use-react-query` rule); the inputs
  // stay local state because they are edit buffers, seeded once the read lands.
  const settingsQuery = useQuery({
    queryKey: ['spec-builder', 'settings'],
    queryFn: () => specApi.getSettings(),
  })
  useEffect(() => {
    if (settingsQuery.data) {
      setBasePath(settingsQuery.data.base_path || '')
      setModel(settingsQuery.data.model || '')
    }
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
    mutationFn: (next: { base_path: string; model: string }) =>
      specApi.saveSettings(next.base_path, next.model),
    onSuccess: (_data, next) => {
      queryClient.setQueryData(['spec-builder', 'settings'], next)
      void queryClient.invalidateQueries({ queryKey: ['spec-builder', 'settings'] })
      onClose()
    },
    onError: (e) => setErr((e as Error).message),
  })
  const busy = saveMutation.isPending
  // basePath/model are seeded from the query, so until the read LANDS the buffers
  // are still the initial '' -- saving then would overwrite configured values with
  // nothing. Guarding on the write alone (`busy`) left exactly that window open.
  const unloaded = settingsQuery.isPending || settingsQuery.isError
  const save = () => saveMutation.mutate({ base_path: basePath.trim(), model })

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
      <div className="text-[13px] font-semibold mt-4 mb-1.5">{i18nT('apps.specBuilder.components.settingsModal.which_model_should_run_spec_generation')}</div>
      <div className="text-[12px] text-muted mb-2.5 leading-relaxed">
        {i18nT('apps.specBuilder.components.settingsModal.default_keeps_the_model_your_chat_agent_resolves')}
      </div>
      {/* Options come from the shared advertised-models hook (GET /api/models),
          never a static list; 'auto' is excluded like the Research picker — an
          app-level pin of 'auto' would shadow the chain it means to defer to.
          triggerFallback keeps a RETAINED pin visible when it is not in the
          advertised list (cold model cache on first open, or a model the
          account no longer serves): without it the trigger would claim
          "Default (inherit)" while the stamp keeps applying that model. */}
      <SimpleSelect
        aria-label={i18nT('apps.specBuilder.components.settingsModal.spec_generation_model')}
        options={availableModels.map((m) => m.name).filter((n) => n !== 'auto')}
        clearLabel={i18nT('apps.specBuilder.components.settingsModal.model_default_inherit')}
        triggerFallback={model || undefined}
        value={model}
        onChange={setModel}
      />
    </Modal>
  )
}
