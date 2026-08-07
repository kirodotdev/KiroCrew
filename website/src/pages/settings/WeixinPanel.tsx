import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { QrCode, Loader2, Check, TriangleAlert, RefreshCw } from 'lucide-react'
import { api, type WeixinConfigSave } from '../../api/client'
import { WeixinLogo } from '../../components/WeixinLogo'
import SimpleSelect from '../../components/SimpleSelect'
import { TagListEditor } from './SlackPanel'

import { i18nT } from '../../i18n/t'
const SETUP_GUIDE =
  'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/weixin-integration.md'

/** How often we poll the QR scan status while a login session is open. */
const POLL_MS = 1500
/** Give up on an unscanned QR after this long (Tencent expires them anyway). */
const QR_TTL_MS = 5 * 60 * 1000

type Phase = 'idle' | 'starting' | 'waiting' | 'scanned' | 'confirmed' | 'expired' | 'error'

/**
 * Weixin (personal WeChat) channel settings.
 *
 * Unlike the other channels there is no token to paste: iLink authenticates by
 * QR scan, so this panel drives the server-side login flow
 * (POST /api/channels/weixin/qr/start then poll .../status) and never handles
 * the bot credential itself.
 */
export function WeixinPanel() {
  const qc = useQueryClient()
  const { data, isError } = useQuery({
    queryKey: ['weixin-config'],
    queryFn: api.getWeixinConfig,
    retry: false,
  })

  const [phase, setPhase] = useState<Phase>('idle')
  const [qrImg, setQrImg] = useState('')
  const [errMsg, setErrMsg] = useState('')
  const [sessionId, setSessionId] = useState('')
  const deadlineRef = useRef(0)

  // Server state goes through React Query, including the QR scan poll: the
  // status endpoint is polled via refetchInterval while a login session is open
  // and stops as soon as the flow reaches a terminal phase, so there is no
  // hand-rolled timer to leak on unmount.
  const polling = phase === 'waiting' || phase === 'scanned'
  const { data: qrStatus } = useQuery({
    queryKey: ['weixin-qr-status', sessionId],
    queryFn: () => api.weixinQrStatus(sessionId),
    enabled: polling && !!sessionId,
    refetchInterval: polling ? POLL_MS : false,
    retry: false,
    // A long-poll endpoint fails transiently; keep the last value rather than
    // flipping the UI to an error state.
    gcTime: 0,
  })

  // Drive the phase machine off the polled status.
  useEffect(() => {
    if (!polling || !qrStatus) return
    if (qrStatus.status === 'confirmed' || qrStatus.connected) {
      setPhase('confirmed')
      setQrImg('')
      setSessionId('')
      qc.invalidateQueries({ queryKey: ['weixin-config'] })
      return
    }
    if (qrStatus.status === 'expired') {
      setPhase('expired')
      setQrImg('')
      setSessionId('')
      return
    }
    if (qrStatus.status === 'scaned' || qrStatus.status === 'scanned') setPhase('scanned')
  }, [qrStatus, polling, qc])

  // Give up on a code the user never scanned (Tencent expires it anyway).
  useEffect(() => {
    if (!polling) return
    const id = setTimeout(() => {
      if (Date.now() > deadlineRef.current) {
        setPhase('expired')
        setQrImg('')
        setSessionId('')
      }
    }, QR_TTL_MS)
    return () => clearTimeout(id)
  }, [polling])

  const readOnly = !!data?.read_only

  const startLogin = useMutation({
    mutationFn: () => api.weixinQrStart(),
    onMutate: () => {
      setErrMsg('')
      setPhase('starting')
    },
    onSuccess: r => {
      if (r.error || !r.session_id) {
        setErrMsg(r.error || i18nT('pages.settings.weixinPanel.could_not_reach_the_wechat_login_service'))
        setPhase('error')
        return
      }
      setSessionId(r.session_id)
      setQrImg(r.qrcode_img_content || '')
      deadlineRef.current = Date.now() + QR_TTL_MS
      setPhase('waiting')
    },
    onError: (e: unknown) => {
      setErrMsg(e instanceof Error ? e.message : i18nT('pages.settings.weixinPanel.could_not_start_the_login_flow'))
      setPhase('error')
    },
  })

  const saveConfig = useMutation({
    mutationFn: (patch: Partial<WeixinConfigSave>) => api.saveWeixinConfig(patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['weixin-config'] }),
  })
  const save = (patch: Partial<WeixinConfigSave>) => saveConfig.mutate(patch)

  const connected = !!data?.connected
  const credentialSet = !!data?.credential_set

  return (
    <div className="flex flex-col gap-5" data-testid="weixin-panel">
      {/* header */}
      <div className="flex items-start gap-3">
        <span className="mt-0.5 shrink-0">
          <WeixinLogo size={20} />
        </span>
        <div className="min-w-0">
          <h3 className="text-[15px] font-semibold text-text-strong m-0">{i18nT('pages.settings.weixinPanel.wechat')}</h3>
          <p className="text-[12.5px] text-muted mt-1 mb-0">
            {i18nT('pages.settings.weixinPanel.talk_to_your_agent_from_personal_wechat_over_ten')}
          </p>
        </div>
      </div>

      {/* status */}
      <div
        className="flex items-center gap-2 rounded-lg border border-border bg-card px-3.5 py-2.5"
        data-testid="weixin-status"
      >
        {isError ? (
          <span className="text-[12.5px] text-muted">{i18nT('pages.settings.weixinPanel.status_unavailable')}</span>
        ) : connected ? (
          <>
            <span className="w-1.5 h-1.5 rounded-full bg-ok shrink-0" />
            <span className="text-[12.5px] text-ok font-medium">{i18nT('pages.settings.weixinPanel.connected')}</span>
            {data?.account_id && (
              <span className="text-[11.5px] text-muted font-mono">{data.account_id}</span>
            )}
          </>
        ) : credentialSet ? (
          <>
            <span className="w-1.5 h-1.5 rounded-full bg-warn shrink-0" />
            <span className="text-[12.5px] text-warn font-medium">{i18nT('pages.settings.weixinPanel.signed_in_restart_to_connect')}</span>
          </>
        ) : (
          <span className="text-[12.5px] text-muted">{i18nT('pages.settings.weixinPanel.not_signed_in')}</span>
        )}
      </div>

      {/* QR login */}
      <div className="rounded-lg border border-border bg-card p-3.5">
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="text-[13px] font-semibold text-text-strong">{i18nT('pages.settings.weixinPanel.sign_in_with_wechat')}</div>
            <div className="text-[11.5px] text-muted mt-0.5">
              {i18nT('pages.settings.weixinPanel.scan_the_code_with_the_wechat_mobile_app_then_co')}
            </div>
          </div>
          {!readOnly && (
            <button
              onClick={() => startLogin.mutate()}
              disabled={phase === 'starting' || phase === 'waiting' || phase === 'scanned'}
              data-testid="weixin-connect"
              className="flex items-center gap-1.5 text-xs py-1.5 px-3.5 rounded-md border border-border bg-bg text-text cursor-pointer hover:bg-bg-hover disabled:opacity-60 disabled:cursor-default shrink-0"
            >
              {phase === 'starting' ? (
                <Loader2 size={13} className="animate-spin" />
              ) : credentialSet ? (
                <RefreshCw size={13} />
              ) : (
                <QrCode size={13} />
              )}
              {credentialSet ? i18nT('pages.settings.weixinPanel.sign_in_again') : i18nT('pages.settings.weixinPanel.connect_via_qr')}
            </button>
          )}
        </div>

        {(phase === 'waiting' || phase === 'scanned') && (
          <div className="mt-3 flex flex-col items-center gap-2" data-testid="weixin-qr">
            {qrImg ? (
              <img
                src={qrImg}
                alt={i18nT('pages.settings.weixinPanel.wechat_login_qr_code')}
                width={180}
                height={180}
                className="rounded-md bg-white p-2"
              />
            ) : (
              <div className="text-[12px] text-muted">{i18nT('pages.settings.weixinPanel.waiting_for_a_code')}</div>
            )}
            <div className="flex items-center gap-1.5 text-[12px] text-muted">
              <Loader2 size={12} className="animate-spin" />
              {phase === 'scanned' ? i18nT('pages.settings.weixinPanel.scanned_confirm_in_wechat') : i18nT('pages.settings.weixinPanel.waiting_for_scan')}
            </div>
          </div>
        )}

        {phase === 'confirmed' && (
          <div
            className="mt-3 flex items-center gap-1.5 text-[12.5px] text-ok"
            data-testid="weixin-confirmed"
          >
            <Check size={13} /> {i18nT('pages.settings.weixinPanel.signed_in_restart_the_gateway_to_start_receiving')}
          </div>
        )}

        {phase === 'expired' && (
          <div className="mt-3 flex items-center gap-1.5 text-[12.5px] text-warn" data-testid="weixin-expired">
            <TriangleAlert size={13} /> {i18nT('pages.settings.weixinPanel.the_code_expired_try_again')}
          </div>
        )}

        {phase === 'error' && (
          <div className="mt-3 flex items-center gap-1.5 text-[12.5px] text-danger" data-testid="weixin-error">
            <TriangleAlert size={13} /> {errMsg}
          </div>
        )}
      </div>

      {/* enable + access policy */}
      <label
        htmlFor="weixin-enabled-toggle"
        className="flex items-center gap-2.5 cursor-pointer"
      >
        <input
          id="weixin-enabled-toggle"
          type="checkbox"
          checked={!!data?.enabled}
          disabled={readOnly}
          onChange={e => save({ enabled: e.target.checked })}
          data-testid="weixin-enabled"
          aria-label={i18nT('pages.settings.weixinPanel.enable_the_wechat_channel')}
        />
        <span className="text-[13px] text-text">{i18nT('pages.settings.weixinPanel.enable_the_wechat_channel')}</span>
      </label>

      <div>
        {/* Not a <label>: SimpleSelect renders a button, so `htmlFor` would point
            at no form control. The caption keeps its key and is reused verbatim as
            the trigger's accessible name. data-testid moves to this wrapper so the
            Playwright drive (scripts/test-weixin-panel.mjs) still finds the field. */}
        <div className="block" data-testid="weixin-dm-policy">
          <span className="block text-[11px] text-muted mb-1.5">{i18nT('pages.settings.weixinPanel.who_can_message_the_bot')}</span>
          {/* maxWidth: the native select was content-sized; the Radix trigger is
              w-full and this field is a stretch flex item, so without a cap it
              would span the whole panel while every neighbouring control stays
              content-scaled. */}
          <SimpleSelect
            options={['open', 'allowlist', 'disabled']}
            optionLabels={[
              i18nT('pages.settings.weixinPanel.anyone_who_messages_the_bot'),
              i18nT('pages.settings.weixinPanel.only_allowed_user_ids'),
              i18nT('pages.settings.weixinPanel.nobody_ignore_all_messages'),
            ]}
            value={data?.dm_policy || 'allowlist'}
            disabled={readOnly}
            onChange={v => save({ dm_policy: v })}
            aria-label={i18nT('pages.settings.weixinPanel.who_can_message_the_bot')}
            style={{ maxWidth: 280 }}
          />
        </div>
      </div>

      {data?.dm_policy === 'allowlist' && (
        <div data-testid="weixin-allowlist">
          <TagListEditor
            label={i18nT('pages.settings.weixinPanel.allowed_user_ids')}
            description={i18nT('pages.settings.weixinPanel.allowed_wechat_user_ids_empty_deny_all_fail_clos')}
            values={data?.allowed_user_ids || []}
            placeholder={i18nT('pages.settings.weixinPanel.wxid')}
            onChange={(vals: string[]) => save({ allowed_user_ids: vals })}
            readOnly={readOnly}
          />
        </div>
      )}

      <p className="text-[11.5px] text-muted m-0">
        {i18nT('pages.settings.weixinPanel.group_chats_are_not_supported_ilink_bot_identiti')}{' '}
        <a
          href={SETUP_GUIDE}
          target="_blank"
          rel="noopener noreferrer"
          className="text-accent hover:underline"
        >
          {i18nT('pages.settings.weixinPanel.setup_guide')}
        </a>
      </p>
    </div>
  )
}
