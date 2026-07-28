import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { QrCode, Loader2, Check, TriangleAlert, RefreshCw } from 'lucide-react'
import { api, type WeixinConfigSave } from '../../api/client'
import { WeixinLogo } from '../../components/WeixinLogo'
import { TagListEditor } from './SlackPanel'

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
        setErrMsg(r.error || 'Could not reach the WeChat login service.')
        setPhase('error')
        return
      }
      setSessionId(r.session_id)
      setQrImg(r.qrcode_img_content || '')
      deadlineRef.current = Date.now() + QR_TTL_MS
      setPhase('waiting')
    },
    onError: (e: unknown) => {
      setErrMsg(e instanceof Error ? e.message : 'Could not start the login flow.')
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
          <h3 className="text-[15px] font-semibold text-text-strong m-0">WeChat</h3>
          <p className="text-[12.5px] text-muted mt-1 mb-0">
            Talk to your agent from personal WeChat over Tencent's iLink bot API. Sign in by
            scanning a QR code — direct messages only.
          </p>
        </div>
      </div>

      {/* status */}
      <div
        className="flex items-center gap-2 rounded-lg border border-border bg-card px-3.5 py-2.5"
        data-testid="weixin-status"
      >
        {isError ? (
          <span className="text-[12.5px] text-muted">Status unavailable</span>
        ) : connected ? (
          <>
            <span className="w-1.5 h-1.5 rounded-full bg-ok shrink-0" />
            <span className="text-[12.5px] text-ok font-medium">Connected</span>
            {data?.account_id && (
              <span className="text-[11.5px] text-muted font-mono">{data.account_id}</span>
            )}
          </>
        ) : credentialSet ? (
          <>
            <span className="w-1.5 h-1.5 rounded-full bg-warn shrink-0" />
            <span className="text-[12.5px] text-warn font-medium">Signed in — restart to connect</span>
          </>
        ) : (
          <span className="text-[12.5px] text-muted">Not signed in</span>
        )}
      </div>

      {/* QR login */}
      <div className="rounded-lg border border-border bg-card p-3.5">
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="text-[13px] font-semibold text-text-strong">Sign in with WeChat</div>
            <div className="text-[11.5px] text-muted mt-0.5">
              Scan the code with the WeChat mobile app, then confirm on your phone.
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
              {credentialSet ? 'Sign in again' : 'Connect via QR'}
            </button>
          )}
        </div>

        {(phase === 'waiting' || phase === 'scanned') && (
          <div className="mt-3 flex flex-col items-center gap-2" data-testid="weixin-qr">
            {qrImg ? (
              <img
                src={qrImg}
                alt="WeChat login QR code"
                width={180}
                height={180}
                className="rounded-md bg-white p-2"
              />
            ) : (
              <div className="text-[12px] text-muted">Waiting for a code…</div>
            )}
            <div className="flex items-center gap-1.5 text-[12px] text-muted">
              <Loader2 size={12} className="animate-spin" />
              {phase === 'scanned' ? 'Scanned — confirm in WeChat' : 'Waiting for scan…'}
            </div>
          </div>
        )}

        {phase === 'confirmed' && (
          <div
            className="mt-3 flex items-center gap-1.5 text-[12.5px] text-ok"
            data-testid="weixin-confirmed"
          >
            <Check size={13} /> Signed in. Restart the gateway to start receiving messages.
          </div>
        )}

        {phase === 'expired' && (
          <div className="mt-3 flex items-center gap-1.5 text-[12.5px] text-warn" data-testid="weixin-expired">
            <TriangleAlert size={13} /> The code expired. Try again.
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
        />
        <span className="text-[13px] text-text">Enable the WeChat channel</span>
      </label>

      <div>
        <label htmlFor="weixin-dm-policy" className="block">
          <span className="block text-[11px] text-muted mb-1.5">Who can message the bot</span>
          <select
            id="weixin-dm-policy"
            value={data?.dm_policy || 'allowlist'}
            disabled={readOnly}
            onChange={e => save({ dm_policy: e.target.value })}
            data-testid="weixin-dm-policy"
            className="text-sm px-2.5 py-2 rounded-md bg-bg border border-border text-text"
          >
            <option value="open">Anyone who messages the bot</option>
            <option value="allowlist">Only allowed user IDs</option>
            <option value="disabled">Nobody (ignore all messages)</option>
          </select>
        </label>
      </div>

      {data?.dm_policy === 'allowlist' && (
        <div data-testid="weixin-allowlist">
          <TagListEditor
            label="Allowed user IDs"
            description="Allowed WeChat user IDs. Empty = deny all (fail closed)."
            values={data?.allowed_user_ids || []}
            placeholder="wxid_…"
            onChange={(vals: string[]) => save({ allowed_user_ids: vals })}
            readOnly={readOnly}
          />
        </div>
      )}

      <p className="text-[11.5px] text-muted m-0">
        Group chats are not supported: iLink bot identities do not receive ordinary WeChat group
        events.{' '}
        <a
          href={SETUP_GUIDE}
          target="_blank"
          rel="noopener noreferrer"
          className="text-accent hover:underline"
        >
          Setup guide
        </a>
      </p>
    </div>
  )
}
