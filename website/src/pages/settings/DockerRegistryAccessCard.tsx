import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Lock, ShieldAlert } from 'lucide-react'

import { api, ApiError, type DockerRegistryAccessData } from '../../api/client'
import Modal from '../../components/Modal'
import { SettingsCard, SettingsToggle } from '../../components/settings'
import { Btn, Checkbox } from '../../components/ui'
import { i18nT } from '../../i18n/t'

export default function DockerRegistryAccessCard() {
  const qc = useQueryClient()
  const [confirm, setConfirm] = useState(false)
  const [ack, setAck] = useState(false)
  const [permanent, setPermanent] = useState(false)
  const accessQuery = useQuery<DockerRegistryAccessData>({
    queryKey: ['docker-registry-access'],
    queryFn: api.getDockerRegistryAccess,
  })
  const { data, isLoading, isError } = accessQuery
  const save = useMutation({
    mutationFn: ({ enabled: next, permanent: persistent }: { enabled: boolean; permanent?: boolean }) =>
      api.saveDockerRegistryAccess(next, persistent === true),
    onSuccess: snap => qc.setQueryData(['docker-registry-access'], snap),
  })
  const enabled = data?.enabled === true
  const supported = data?.supported === true
  const effective = supported && enabled

  useEffect(() => { setAck(false) }, [confirm])

  const onToggle = (next: boolean) => {
    if (next) setConfirm(true)
    else save.mutate({ enabled: false })
  }

  let ownerOnlyError = false
  if (save.error instanceof ApiError) {
    try {
      const body = JSON.parse(save.error.body) as { code?: unknown }
      if (body.code === 'owner_only') {
        ownerOnlyError = true
      }
    } catch {
      // Non-JSON failures use the generic translated message.
    }
  }

  if (isError || (!isLoading && data === undefined)) {
    return (
      <SettingsCard>
        <div className="text-[13px] font-semibold text-text">
          {i18nT('pages.settings.securityPanel.docker_access')}
        </div>
        <div className="text-[12px] text-danger mt-1 leading-relaxed">
          {i18nT('pages.settings.securityPanel.docker_access_unavailable')}{' '}
          <Btn disabled={accessQuery.isFetching} onClick={() => accessQuery.refetch()}>
            {i18nT('pages.settings.securityPanel.docker_access_retry')}
          </Btn>
        </div>
      </SettingsCard>
    )
  }

  if (isLoading) {
    return (
      <SettingsCard>
        <div className="h-4 w-48 rounded bg-border/60 animate-pulse" />
        <div className="h-3 w-72 max-w-full rounded bg-border/40 animate-pulse mt-2" />
      </SettingsCard>
    )
  }

  return (
    <>
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.securityPanel.docker_access')}
          description={i18nT('pages.settings.securityPanel.docker_access_description')}
          checked={enabled}
          onChange={onToggle}
          // A stored grant may always be revoked after moving the data home to
          // an unsupported host; only its rising edge requires Linux.
          disabled={save.isPending || (!supported && !enabled)}
        />
        {!supported && (
          <div className="text-[12px] text-muted mt-1.5 flex items-center gap-1.5">
            <Lock size={12} className="shrink-0" />
            {i18nT('pages.settings.securityPanel.docker_access_unsupported')}
          </div>
        )}
        {effective && (
          <div className="flex items-start gap-1.5 text-[12px] text-warn mt-1.5 leading-relaxed">
            <ShieldAlert size={13} className="shrink-0 mt-0.5" />
            <span>{i18nT('pages.settings.securityPanel.docker_access_enabled_warning')}</span>
          </div>
        )}
        {save.isError && (
          <div className="text-[12px] text-danger mt-1.5">
            {ownerOnlyError
              ? i18nT('pages.settings.securityPanel.docker_access_owner_only')
              : i18nT('pages.settings.securityPanel.docker_access_save_failed')}
          </div>
        )}
      </SettingsCard>

      <Modal
        open={confirm}
        onClose={() => setConfirm(false)}
        title={i18nT('pages.settings.securityPanel.docker_access')}
        maxWidth={480}
        footer={
          <>
            <Btn onClick={() => setConfirm(false)}>{i18nT('pages.settings.securityPanel.cancel')}</Btn>
            <Btn
              danger
              disabled={!ack || save.isPending}
              onClick={() => {
                save.mutate({ enabled: true, permanent })
                setConfirm(false)
              }}
            >
              {i18nT('pages.settings.securityPanel.docker_access_confirm_action')}
            </Btn>
          </>
        }
      >
        <div className="flex items-start gap-3">
          <AlertTriangle size={18} className="text-warn shrink-0 mt-0.5" />
          <div className="text-[13px] text-text leading-relaxed">
            {i18nT('pages.settings.securityPanel.docker_access_confirm_body')}
          </div>
        </div>
        <div
          className="mt-4 grid grid-cols-2 gap-2"
          role="radiogroup"
          aria-label={i18nT('pages.settings.securityPanel.docker_access_duration')}
        >
          <button
            type="button"
            role="radio"
            aria-checked={!permanent}
            className={`rounded-md border px-3 py-2 text-left text-[12px] ${!permanent ? 'border-accent text-text' : 'border-border text-muted'}`}
            onClick={() => setPermanent(false)}
          >
            {i18nT('pages.settings.securityPanel.docker_access_six_hours')}
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={permanent}
            className={`rounded-md border px-3 py-2 text-left text-[12px] ${permanent ? 'border-warn text-text' : 'border-border text-muted'}`}
            onClick={() => setPermanent(true)}
          >
            {i18nT('pages.settings.securityPanel.docker_access_until_disabled')}
          </button>
        </div>
        <label
          htmlFor="docker-registry-access-confirm-ack"
          className="flex items-center gap-2.5 mt-4 cursor-pointer"
        >
          <Checkbox
            id="docker-registry-access-confirm-ack"
            checked={ack}
            onChange={e => setAck(e.target.checked)}
          />
          <span className="text-[13px] text-text">
            {i18nT('pages.settings.securityPanel.docker_access_confirm_ack')}
          </span>
        </label>
      </Modal>
    </>
  )
}
