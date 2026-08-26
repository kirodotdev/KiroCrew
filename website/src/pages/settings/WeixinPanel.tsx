import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { QrCode, Loader2, Check, TriangleAlert, RefreshCw } from 'lucide-react'
import { api, type WeixinConfigSave } from '../../api/client'
import { WeixinLogo } from '../../components/WeixinLogo'
import { SettingsInput, SettingsSelect, SettingsToggle } from '../../components/settings'
import { useChannelFolderSave } from '../../hooks/useChannelFolderSave'
import { TagListEditor } from './SlackPanel'

import { i18nT } from '../../i18n/t'
import { useImeGuard } from '../../hooks/useImeGuard'
/** Brand name — do-not-translate, so it lives here rather than in the catalog. */
const CHANNEL_NAME = "WeChat"
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
  const ime = useImeGuard()
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
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['weixin-config'] })
    },
  })
  // `onRevert` undoes an optimistic local flip when the server rejects the patch.
  // It is passed by the toggle and NOT by the name field: a rejected name must
  // keep the text the user typed (that is what lets them correct it), while a
  // rejected toggle must snap back to the server's truth, or the switch reads
  // "off" while the gateway is still filing sessions.
  //
  // `mutateAsync` rather than `mutate(patch, {…})`: per-call callbacks live on the
  // mutation OBSERVER, and this panel saves on change, so a second save starting
  // before the first resolves replaces them and the first call's handlers never
  // run. Clicking the toggle is what blurs the name field, so "rename, then
  // switch off" issues both saves back to back — the ordinary path, not a rare
  // race. Attaching the handling to each returned promise keeps every call's own
  // outcome. The mutation-level onSuccess still fires for shared work
  // (invalidating the query).
  //
  // Feedback (the error line, the folder-name "Saved." check) lives HERE, on the
  // per-call chain, guarded by a sequence: back-to-back saves resolve out of
  // order, and only the LATEST attempt may speak for the panel. A slow rename
  // resolving after a newer one was rejected must neither clear that rejection's
  // error nor paint "Saved." next to it — both would assert the failed draft
  // was stored.
  //
  // Folder field + save sequencing live in a shared hook: WeChat's and
  // WhatsApp's panels are the two QR-paired channels and carried
  // byte-identical copies of this. See useChannelFolderSave for the three
  // invariants (accepted-vs-draft name, folder-only sequencing, and
  // ownership-aware error clearing).
  const {
    folderOn,
    folderName,
    setFolderName,
    folderSaved,
    saveError,
    toggleFolder,
    commitFolderName,
    save,
  } = useChannelFolderSave<WeixinConfigSave>({
    serverFolder: data?.session_folder,
    defaultName: CHANNEL_NAME,
    mutate: patch => saveConfig.mutateAsync(patch),
  })

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
      {/* Every other channel panel renders its enable switch as SettingsToggle;
          the shared component owns the label association (visible text doubles
          as the switch's accessible name) and the keyboard/AT semantics.
          data-testid lives on this wrapper because SettingsToggle exposes only
          data-setting-label — same move as weixin-dm-policy below. */}
      {/* max-w: this panel has no SettingsCard, so an uncapped row would make
          the whole pane width a Clickable save surface (this panel autosaves —
          a stray click in the empty gap would silently disable the channel)
          and push the switch far from its label. Content-scaling matches the
          dm-policy select below. */}
      <div data-testid="weixin-enabled" className="max-w-[380px]">
        <SettingsToggle
          label={i18nT('pages.settings.weixinPanel.enable_the_wechat_channel')}
          checked={!!data?.enabled}
          disabled={readOnly}
          onChange={v => save({ enabled: v })}
        />
      </div>

      <div>
        {/* maxWidth: the trigger is w-full and this field is a stretch flex item,
            so without a cap it would span the whole panel while every
            neighbouring control stays content-scaled. data-testid stays on this
            wrapper so the Playwright drive (scripts/test-weixin-panel.mjs)
            still finds the field. */}
        <div className="block" data-testid="weixin-dm-policy" style={{ maxWidth: 280 }}>
          <SettingsSelect
            label={i18nT('pages.settings.weixinPanel.who_can_message_the_bot')}
            options={['open', 'allowlist', 'disabled']}
            optionLabels={[
              i18nT('pages.settings.weixinPanel.anyone_who_messages_the_bot'),
              i18nT('pages.settings.weixinPanel.only_allowed_user_ids'),
              i18nT('pages.settings.weixinPanel.nobody_ignore_all_messages'),
            ]}
            value={data?.dm_policy || 'allowlist'}
            disabled={readOnly}
            onChange={v => save({ dm_policy: v })}
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

      {/* Optional session filing, rendered from the same primitives, in the same
          place, with the same divider as every other channel's copy of this
          setting (`BotChannelPanel` for Telegram/Discord/WeCom, and the Slack,
          Teams and Webex panels): bottom of the panel, below a rule, switch above
          the name field. It used to be a bare checkbox wedged between the
          DM-policy picker and the allowlist, which read as part of the
          access-control block and sent users looking for it at the bottom, where
          it was not.

          Off by default: WeChat conversations stay unfiled, and a configured name
          IS the on-state (the backend has one field, where "" means off).

          This panel has no Save button — every other control saves on change — so
          the toggle must persist immediately. Revealing the field without saving
          loses the setting for anyone who turns it on, sees the name already
          filled in, and leaves. The NAME still commits on blur / Enter rather
          than per keystroke, which is why `SettingsInput` is given
          `onBlur`/`onKeyDown` here and the other panels (which have a Save
          button) pass neither. Renaming does not strand the folder it creates:
          the channel's folder is found by its stamp, so a new name relabels that
          same folder instead of building a second one. */}
      <div className="border-t border-border mt-4 pt-4" data-testid="weixin-session-folder">
        <SettingsToggle
          label={i18nT('pages.settings.botChannelPanel.file_sessions_in_folder')}
          description={i18nT('pages.settings.botChannelPanel.file_sessions_in_folder_desc', { channel: CHANNEL_NAME })}
          checked={folderOn}
          disabled={readOnly}
          onChange={toggleFolder}
        />
        {folderOn && (
          <div className="mt-4">
            <SettingsInput
              label={i18nT('pages.settings.botChannelPanel.session_folder_name')}
              description={i18nT('pages.settings.weixinPanel.created_for_you_when_you_turn_this_on_if_it_does')}
              value={folderName}
              disabled={readOnly}
              placeholder={CHANNEL_NAME}
              onChange={setFolderName}
              {...ime.bindComposition({
                onBlur: commitFolderName,
              })}
              onKeyDown={e => {
                if (e.key !== 'Enter') return
                // Early-return BEFORE the blur: a committing IME Enter must not commit.
                if (ime.isComposing(e)) return
                e.currentTarget.blur()
              }}
            />
            {folderSaved && (
              <p
                className="inline-flex items-center gap-1.5 text-[12px] text-ok mt-1 mb-0"
                role="status"
                data-testid="weixin-session-folder-saved"
              >
                <Check size={13} /> {i18nT('pages.settings.botChannelPanel.saved')}
              </p>
            )}
          </div>
        )}
        {/* Outside the `folderOn` block on purpose: when an ENABLE is rejected
            the revert returns the switch to the server's value — off, since the
            server has no folder — so an error nested in that block would unmount
            before it could paint and the failure would be silent. */}
        {saveError && (
          <p
            className="text-[11.5px] text-danger mt-1 mb-0"
            role="alert"
            data-testid="weixin-session-folder-error"
          >
            {saveError}
          </p>
        )}
      </div>

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
