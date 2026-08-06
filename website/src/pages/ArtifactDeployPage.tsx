import { useState } from 'react'
import Clickable from '../components/Clickable'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Globe, Copy, ExternalLink, RefreshCw, Trash2, Undo2, ShieldCheck, Terminal, ChevronDown, ChevronRight, Lock, CheckCircle, XCircle, Rocket, Plus, Star } from 'lucide-react'
import type { Artifact } from '../types'
import { PageHeader, Card, CardTitle, StatCard, Btn, Input, Toggle , Badge} from '../components/ui'
import SimpleSelect from '../components/SimpleSelect'
import InfoTip from '../components/InfoTip'
import { safeHttpUrl } from '../lib/safeUrl'
import { formatCost } from '../utils/formatCost'

import { i18nT } from '../i18n/t'
const BASE = '/api/deploy'

interface ProfileEntry { name: string; region: string; account: string; verified_at: string; note: string }
interface ProfilesResp { profiles: ProfileEntry[]; default: string; available: string[] }
interface Site { site_id: string; bucket: string; distribution_id: string; status?: string; url?: string; profile?: string }
interface Reach { reachable: boolean; account?: string; s3_reachable?: boolean; cloudfront_reachable?: boolean; note?: string; detail?: string; profile?: string; error?: string }

// Route all fetches through proper X-Session-Key header (client.ts pattern).
const _sk = { 'X-Session-Key': 'dashboard:ui' }
async function jget<T>(path: string): Promise<T> {
  const r = await fetch(BASE + path, { headers: { ..._sk } })
  return (await r.json()) as T
}
async function jsend<T>(path: string, body: unknown, method = 'POST'): Promise<{ status: number; data: T }> {
  const r = await fetch(BASE + path, { method, headers: { 'Content-Type': 'application/json', ..._sk }, body: JSON.stringify(body) })
  return { status: r.status, data: (await r.json()) as T }
}

const chip: React.CSSProperties = { background: 'var(--bg)', border: '1px solid var(--border)', color: 'var(--muted)', padding: '1px 7px', borderRadius: 9999, fontSize: 10.5, fontFamily: 'ui-monospace,Menlo,monospace' }
const cmd: React.CSSProperties = { background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 6, padding: '8px 10px', fontFamily: 'ui-monospace,Menlo,monospace', fontSize: 12, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }
const label: React.CSSProperties = { fontSize: 11, color: 'var(--muted)', marginBottom: 4, display: 'block' }
const linkBtn: React.CSSProperties = { background: 'transparent', color: 'var(--accent)', border: '1px solid var(--accent-subtle)', padding: '6px 13px', borderRadius: 9999, fontSize: 12, fontWeight: 500, display: 'inline-flex', alignItems: 'center', gap: 5, textDecoration: 'none' }

export default function ArtifactDeployPage() {
  const qc = useQueryClient()
  const [reach, setReach] = useState<Reach | null>(null)
  const [policy, setPolicy] = useState('')
  const [boundaryPolicy, setBoundaryPolicy] = useState('')
  const [boundaryNote, setBoundaryNote] = useState('')
  const [policyTier, setPolicyTier] = useState<'static' | 'fullstack'>('static')
  const [notice, setNotice] = useState<string | null>(null)
  const [showGuide, setShowGuide] = useState(true)
  const [showSecurity, setShowSecurity] = useState(false)
  const [showNewProfile, setShowNewProfile] = useState(false)
  const [npName, setNpName] = useState('')
  const [npRegion, setNpRegion] = useState('us-west-2')
  const [npAccount, setNpAccount] = useState('')
  const [npRole, setNpRole] = useState('')
  const [npCreate, setNpCreate] = useState(false)

  const { data: profilesResp } = useQuery<ProfilesResp>({
    queryKey: ['deploy-web', 'profiles'],
    queryFn: () => jget('/profiles'),
  })
  const profiles = profilesResp?.profiles || []
  const defaultProfile = profilesResp?.default || ''
  const availableProfiles = profilesResp?.available || []

  const { data: sitesResp } = useQuery<{ sites: Site[]; configured: boolean; profile_errors?: string[] }>({
    queryKey: ['deploy-web', 'sites'],
    queryFn: () => jget('/list'),
    refetchInterval: 30000,
  })
  const sites = sitesResp?.sites || []

  const { data: webappResp } = useQuery<{ artifacts: Artifact[] }>({
    queryKey: ['deploy-web', 'webapps'],
    queryFn: async () => {
      const r = await fetch('/api/artifacts?kind=webapp')
      return (await r.json()) as { artifacts: Artifact[] }
    },
    refetchInterval: 30000,
  })
  const webapps = (webappResp?.artifacts || []).filter((a) => a.webapp_metadata)
  const webappCost = (a: Artifact): number => {
    const est = a.webapp_metadata?.cost?.estimates || []
    return est.length ? Math.max(...est.map((e: { usd?: number }) => Number(e.usd) || 0)) : 0
  }
  const deployedWebapps = webapps.filter((a) => a.webapp_metadata?.deploy_target?.public_url)
  const draftWebapps = webapps.filter(
    (a) => !a.webapp_metadata?.deploy_target?.public_url && a.webapp_metadata?.lifecycle?.status !== 'expired')
  const navigate = useNavigate()
  const [draftProfiles, setDraftProfiles] = useState<Record<string, string>>({})
  const deployDraft = (slug: string) => {
    const chosen = draftProfiles[slug] || defaultProfile
    ;(window as unknown as { __mc_chat_launch?: { message: string; ts: number } }).__mc_chat_launch = {
      message:
        `Deploy the app artifact "${slug}" to my AWS account using the ` +
        `artifact-deploy skill: adapt it to the deploy contract, ship it, and give me the public link.` +
        (chosen ? ` Use the AWS profile "${chosen}".` : ''),
      ts: Date.now(),
    }
    navigate('/chat')
  }
  const totalWebappUsd = deployedWebapps.reduce((s, a) => s + webappCost(a), 0)

  const refreshProfiles = () => {
    qc.invalidateQueries({ queryKey: ['deploy-web', 'profiles'] })
    qc.invalidateQueries({ queryKey: ['deploy-web', 'sites'] })
  }
  const addProfile = useMutation({
    mutationFn: (p: { name: string; region: string; create?: boolean; account?: string; role?: string; default?: boolean }) =>
      jsend<{ error?: string }>('/profiles', p),
    onSuccess: ({ status, data }, p) => {
      if (status >= 400) { setNotice(i18nT('pages.artifactDeployPage.error', { error: data?.error || i18nT('pages.artifactDeployPage.add_failed') })); return }
      setNotice(p.create
        ? i18nT('pages.artifactDeployPage.created_and_registered_profile', { name: p.name })
        : i18nT('pages.artifactDeployPage.registered_profile', { name: p.name }))
      setShowNewProfile(false); setNpName(''); setNpAccount(''); setNpRole(''); setNpCreate(false)
      refreshProfiles()
    },
  })
  const setDefaultProfile = useMutation({
    mutationFn: (name: string) => jsend<{ error?: string }>(`/profiles/${encodeURIComponent(name)}`, { default: true }, 'PUT'),
    onSuccess: ({ status, data }) => {
      if (status >= 400) { setNotice(i18nT('pages.artifactDeployPage.error', { error: data?.error || i18nT('pages.artifactDeployPage.update_failed') })); return }
      refreshProfiles()
    },
  })
  const removeProfile = useMutation({
    mutationFn: (name: string) => jsend<{ error?: string }>(`/profiles/${encodeURIComponent(name)}`, {}, 'DELETE'),
    onSuccess: ({ status, data }) => {
      if (status >= 400) { setNotice(i18nT('pages.artifactDeployPage.error', { error: data?.error || i18nT('pages.artifactDeployPage.remove_failed') })); return }
      setNotice(i18nT('pages.artifactDeployPage.removed_from_registry_your_aws_config_is_untouche'))
      refreshProfiles()
    },
  })
  const verify = useMutation({
    mutationFn: (name: string) => jsend<Reach>('/verify', { profile: name }),
    onSuccess: ({ data }) => { setReach(data); refreshProfiles() },
  })

  const loadPolicyMut = useMutation({
    mutationFn: () => jget<{ policy: string; boundary_policy?: string; boundary_policy_name?: string; boundary_note?: string }>(`/iam-policy?tier=${policyTier}`),
    onSuccess: (data) => {
      setPolicy(data.policy)
      // Fullstack also requires the permissions-boundary policy —
      // iam:CreateRole is conditioned on it, so first deploy fails without it.
      setBoundaryPolicy(data.boundary_policy || '')
      setBoundaryNote(data.boundary_note ? `${data.boundary_note} (name: ${data.boundary_policy_name || ''})` : '')
    },
  })

  const recallMut = useMutation({
    // Two-call guard mirroring destroy — preview resolves the
    // LIVE resources, the dialog names them, and the confirmed call binds to
    // them so a recreated site is refused (409) instead of being emptied.
    mutationFn: async (s: Site) => {
      const prev = await jsend<any>('/recall', { site_id: s.site_id, profile: s.profile || '' })
      if (prev.status !== 200) throw new Error(prev.data?.error || `Recall preview failed (${prev.status})`)
      const r = prev.data.resources || {}
      const ok = window.confirm(i18nT('pages.artifactDeployPage.recall_confirm', { name: s.site_id, bucket: r.bucket || '?' }))
      if (!ok) return { status: 0, data: { cancelled: true } }
      return jsend<any>('/recall', {
        site_id: s.site_id, confirm: true, profile: s.profile || '',
        expected_bucket: r.bucket || '', expected_distribution_id: r.distribution_id || '',
      })
    },
    onSuccess: ({ status, data }, s) => {
      if (status === 0) return
      setNotice(status === 200
        ? i18nT('pages.artifactDeployPage.recalled', { name: s.site_id })
        : i18nT('pages.artifactDeployPage.error', { error: data?.error ?? '' }))
      qc.invalidateQueries({ queryKey: ['deploy-web', 'sites'] })
    },
  })

  const destroyMut = useMutation({
    // Two-call guard on the irreversible path. The preview call
    // resolves the LIVE resources; the dialog names those; the confirmed
    // call binds to them so a site recreated since preview is refused (409).
    mutationFn: async (s: Site) => {
      const prev = await jsend<any>('/destroy', { site_id: s.site_id, profile: s.profile || '' })
      if (prev.status !== 200) throw new Error(prev.data?.error || `Destroy preview failed (${prev.status})`)
      const r = prev.data.resources || {}
      const ok = window.confirm(i18nT('pages.artifactDeployPage.destroy_confirm', { name: s.site_id, bucket: r.bucket || '?', distribution: r.distribution_id || '?' }))
      if (!ok) return { status: 0, data: { cancelled: true } }
      return jsend<any>('/destroy', {
        site_id: s.site_id, confirm: true, profile: s.profile || '',
        expected_bucket: r.bucket || '', expected_distribution_id: r.distribution_id || '',
      })
    },
    onSuccess: ({ status, data }, s) => {
      if (status === 0) return
      setNotice(status === 200
        ? i18nT('pages.artifactDeployPage.destroying', { name: s.site_id })
        : i18nT('pages.artifactDeployPage.error', { error: data?.error ?? '' }))
      qc.invalidateQueries({ queryKey: ['deploy-web', 'sites'] })
    },
  })

  function loadPolicy() { loadPolicyMut.mutate() }
  function recall(s: Site) {
    // Confirmation happens inside the mutation using LIVE previewed resources.
    recallMut.mutate(s)
  }
  function destroy(s: Site) {
    // Confirmation happens inside the mutation using LIVE previewed resources.
    destroyMut.mutate(s)
  }

  const CmdRow = ({ text }: { text: string }) => (
    <div style={cmd}>
      <code style={{ overflow: 'auto', whiteSpace: 'nowrap' }}>{text}</code>
      <Btn onClick={() => navigator.clipboard.writeText(text)}><Copy size={11} /> {i18nT('pages.artifactDeployPage.copy')}</Btn>
    </div>
  )

  // Computed stats for the StatCard row
  const totalDeployments = sites.length + deployedWebapps.length
  const estCost = totalWebappUsd

  return (
    <>
      {/* Deploy is a sub-surface of Artifacts: always give the way
          back to the gallery so the console never feels like a dead end. */}
      <div className="px-6 pt-2">
        <button
          type="button"
          onClick={() => navigate('/artifacts')}
          className="inline-flex items-center gap-1.5 text-[13px] text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none px-0"
          aria-label={i18nT('pages.artifactDeployPage.back_to_artifacts')}
        >
          <ArrowLeft size={14} aria-hidden="true" />
          {i18nT('pages.artifactDeployPage.back_to_artifacts')}
        </button>
      </div>
      <PageHeader title={i18nT('pages.artifactDeployPage.artifact_deploy')} subtitle={i18nT('pages.artifactDeployPage.one_console_for_deploying_artifacts_to_your_own')} />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0" style={{ color: 'var(--text)' }}>

      {/* StatCard row — mirrors AgentsPage/ArtifactsPage pattern */}
      <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
        <StatCard label={i18nT('pages.artifactDeployPage.profiles')} value={profiles.length} />
        <StatCard label={i18nT('pages.artifactDeployPage.active_deployments')} value={totalDeployments} accent />
        <StatCard label={i18nT('pages.artifactDeployPage.ready_to_deploy')} value={draftWebapps.length} delay={60} />
        <StatCard label={i18nT('pages.artifactDeployPage.est_cost_not_a_bill')} value={estCost > 0 ? `≤ ${formatCost(estCost)}` : formatCost(0)} delay={120} />
      </div>

      {notice && (
        <Card style={{ whiteSpace: 'pre-wrap', borderColor: 'var(--accent)', fontSize: 12 }}>{notice}</Card>
      )}

      {/* Getting started guide */}
      <Card>
        <Clickable style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', marginBottom: showGuide ? 12 : 0 }}
             onClick={() => setShowGuide((v) => !v)}>
          {showGuide ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
          <CardTitle className="!mb-0"><Terminal size={15} /> {i18nT('pages.artifactDeployPage.getting_started_one_time_aws_setup')}</CardTitle>
        </Clickable>
        {showGuide && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12, fontSize: 13 }}>
            <div>
              <b>{i18nT('pages.artifactDeployPage.1_authenticate_to_aws')}</b> {i18nT('pages.artifactDeployPage.in_your_terminal_kirocrew_never_sees_your_keys_p')}
              <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 6 }}>
                <CmdRow text="aws configure sso        # recommended — short-lived, auto-refreshing" />
                <CmdRow text="aws configure --profile myweb   # or a long-lived named profile" />
              </div>
              <div style={{ marginTop: 8, padding: '8px 10px', borderRadius: 6, border: '1px solid var(--warn-border, #fde68a)', background: 'var(--warn-subtle, #fffbeb)', color: 'var(--warn)', fontSize: 11.5, lineHeight: 1.5 }}>
                <b>{i18nT('pages.artifactDeployPage.two_things_that_trip_people_up')}</b>
                <ul style={{ margin: '4px 0 0', paddingLeft: 16 }}>
                  <li>{i18nT('pages.artifactDeployPage.configure_the_profile_on_the')} <b>{i18nT('pages.artifactDeployPage.machine_running_the_gateway')}</b> {i18nT('pages.artifactDeployPage.your_host_not_your_laptop_artifact_deploy_shells')} <code>{i18nT('pages.artifactDeployPage.aws')}</code> {i18nT('pages.artifactDeployPage.from_the_gateway_process')}</li>
                  <li>{i18nT('pages.artifactDeployPage.sso_needs')} <b>{i18nT('pages.artifactDeployPage.aws_cli_v2')}</b>{i18nT('pages.artifactDeployPage.v1_fails_with')} <code>{i18nT('pages.artifactDeployPage.missing_sso_start_url_sso_region')}</code>{i18nT('pages.artifactDeployPage.make_sure_the_gateway_s')} <code>{i18nT('pages.artifactDeployPage.path')}</code> {i18nT('pages.artifactDeployPage.resolves_v2_before_any_v1')}</li>
                </ul>
              </div>
            </div>
            <div><b>{i18nT('pages.artifactDeployPage.2_enter_the_profile_name_region_below')}</b> {i18nT('pages.artifactDeployPage.and_click')} <b>{i18nT('pages.artifactDeployPage.save')}</b>{i18nT('pages.artifactDeployPage.then')} <b>{i18nT('pages.artifactDeployPage.verify_access')}</b>.</div>
            <div>
              <b>{i18nT('pages.artifactDeployPage.3_apply_the_iam_policy')}</b> {i18nT('pages.artifactDeployPage.click')} <b>{i18nT('pages.artifactDeployPage.get_iam_policy')}</b>{i18nT('pages.artifactDeployPage.then_apply_it_yourself_to_a_dedicated_role_ident')} <code>{i18nT('pages.artifactDeployPage.aws_iam')}</code> {i18nT('pages.artifactDeployPage.command_kirocrew_never_edits_your_iam_the_first')}
            </div>
            <span style={{ color: 'var(--accent)', fontSize: 12, cursor: 'default' }}>
              {i18nT('pages.artifactDeployPage.full_setup_guide_profile_aws_cli_v2_troubleshoot')}
            </span>
          </div>
        )}
      </Card>

      {/* Security model (collapsible) */}
      <Card>
        <Clickable style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', marginBottom: showSecurity ? 12 : 0 }}
             onClick={() => setShowSecurity((v) => !v)}>
          {showSecurity ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
          <CardTitle className="!mb-0"><Lock size={15} /> {i18nT('pages.artifactDeployPage.how_this_is_secured')}</CardTitle>
        </Clickable>
        {showSecurity && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, fontSize: 12.5, lineHeight: 1.55 }}>
            <div>
              <b>{i18nT('pages.artifactDeployPage.your_credentials_never_touch_kirocrew')}</b> {i18nT('pages.artifactDeployPage.only_the')} <b>{i18nT('pages.artifactDeployPage.profile_name')}</b> {i18nT('pages.artifactDeployPage.is_stored_every_aws_call_runs_through_the')} <code>{i18nT('pages.artifactDeployPage.aws')}</code> {i18nT('pages.artifactDeployPage.cli_with')} <code>{i18nT('pages.artifactDeployPage.profile')}</code> {i18nT('pages.artifactDeployPage.never_boto3_so_credential_resolution_stays_in_yo')}
            </div>
            <div>
              <b>{i18nT('pages.artifactDeployPage.the_origin_bucket_is_private')}</b> {i18nT('pages.artifactDeployPage.it_is_created_with_block_public_access_on')}
              <code>{i18nT('pages.artifactDeployPage.bucketownerenforced')}</code> {i18nT('pages.artifactDeployPage.ownership_and_sse_aes256_with')} <b>{i18nT('pages.artifactDeployPage.no_public_bucket_policy')}</b>{i18nT('pages.artifactDeployPage.only_cloudfront_can_read_it_via_an_origin_access')}
              <code>{i18nT('pages.artifactDeployPage.aws_sourcearn')}</code> {i18nT('pages.artifactDeployPage.pins_your_specific_distribution_the_bucket_name')}
            </div>
            <div>
              <b>{i18nT('pages.artifactDeployPage.the_published_url_is_public_by_link')}</b> {i18nT('pages.artifactDeployPage.content_is_served_at_a_random')}
              <code>{i18nT('pages.artifactDeployPage.cloudfront_net')}</code> {i18nT('pages.artifactDeployPage.domain')} <b>{i18nT('pages.artifactDeployPage.anyone_with_the_link_can_view_it')}</b> {i18nT('pages.artifactDeployPage.world_readable_no_auth_in_v1_don_t_publish_anyth')}
            </div>
            <div>
              <b>{i18nT('pages.artifactDeployPage.pre_publish_scan_sensitive_path_guard')}</b> {i18nT('pages.artifactDeployPage.content_is_scanned_for_secrets_and_internal_data')}<code>{i18nT('pages.artifactDeployPage.aws_2')}</code>, <code>{i18nT('pages.artifactDeployPage.ssh')}</code>{i18nT('pages.artifactDeployPage.before_any_upload')}
            </div>
            <div>
              <b>{i18nT('pages.artifactDeployPage.confirm_gate_audit')}</b> {i18nT('pages.artifactDeployPage.deploy_recall_destroy_each_require_explicit_conf')} <b>{i18nT('pages.artifactDeployPage.recall')}</b> {i18nT('pages.artifactDeployPage.takes_a_site_down_fast_url_404_reversible')} <b>{i18nT('pages.artifactDeployPage.destroy')}</b> {i18nT('pages.artifactDeployPage.tears_down_all_infra_irreversible')}
            </div>
            <span style={{ color: 'var(--accent)', fontSize: 12, cursor: 'default' }}>
              {i18nT('pages.artifactDeployPage.full_setup_security_docs')}
            </span>
          </div>
        )}
      </Card>

      {/* Profiles section — CardTitle + InfoTip pattern */}
      <Card>
        <div className="flex justify-between items-center">
          <CardTitle>
            {i18nT('pages.artifactDeployPage.aws_profiles_count', { count: profiles.length })} <InfoTip text={i18nT('pages.artifactDeployPage.aws_profiles_tip')} />
          </CardTitle>
          <span style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <Btn onClick={() => setShowNewProfile((v) => !v)}><Plus size={12} /> {i18nT('pages.artifactDeployPage.new_profile')}</Btn>
            <SimpleSelect
              options={['static', 'fullstack']}
              value={policyTier}
              onChange={(v) => setPolicyTier(v as 'static' | 'fullstack')}
              aria-label={i18nT('pages.artifactDeployPage.policy_tier')}
              style={{ minWidth: 120 }}
            />
            <Btn onClick={loadPolicy}>{i18nT('pages.artifactDeployPage.get_iam_policy')}</Btn>
          </span>
        </div>
        {profiles.length === 0 && (
          <div style={{ fontSize: 13, color: 'var(--muted)', marginBottom: 8 }}>
            {i18nT('pages.artifactDeployPage.no_profiles_yet_register_one_below_every_deploy')}
          </div>
        )}
        {/* Profiles table — table-striped pattern */}
        {profiles.length > 0 && (
          <table className="w-full border-collapse table-striped">
            <thead>
              <tr>
                {['', 'Name', 'Account', 'Region', 'Status', 'Actions'].map(h => (
                  <th key={h} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {profiles.map((p) => (
                <tr key={p.name} className="hover:bg-bg-hover transition-colors">
                  <td className="px-2.5 py-2 border-b border-border">
                    <Btn
                      title={p.name === defaultProfile ? i18nT('pages.artifactDeployPage.default_profile') : i18nT('pages.artifactDeployPage.make_default')}
                      aria-label={p.name === defaultProfile
                        ? i18nT('pages.artifactDeployPage.is_the_default_profile', { name: p.name })
                        : i18nT('pages.artifactDeployPage.make_the_default_profile', { name: p.name })}
                      onClick={() => p.name !== defaultProfile && setDefaultProfile.mutate(p.name)}
                      style={{ background: 'transparent', border: 'none', padding: 0, display: 'inline-flex' }}
                      className="!px-0 !py-0 !border-0">
                      <Star size={14} fill={p.name === defaultProfile ? 'var(--accent)' : 'none'} stroke={p.name === defaultProfile ? 'var(--accent)' : 'var(--muted)'} />
                    </Btn>
                  </td>
                  <td className="px-2.5 py-2 border-b border-border text-sm font-mono font-semibold">{p.name}</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{p.account ? `acct ${p.account}` : '—'}</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{p.region}</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm">
                    {p.verified_at
                      ? <span style={{ color: 'var(--ok)', fontSize: 11, display: 'inline-flex', alignItems: 'center', gap: 3 }}><CheckCircle size={11} /> {i18nT('pages.artifactDeployPage.verified')}</span>
                      : <span style={{ color: 'var(--muted)', fontSize: 11 }}>{i18nT('pages.artifactDeployPage.unverified')}</span>}
                  </td>
                  <td className="px-2.5 py-2 border-b border-border text-sm">
                    <span style={{ display: 'flex', gap: 6 }}>
                      <Btn onClick={() => verify.mutate(p.name)}><ShieldCheck size={11} /> {i18nT('pages.artifactDeployPage.verify')}</Btn>
                      <Btn aria-label={i18nT('pages.artifactDeployPage.remove_from_registry', { name: p.name })}
                        onClick={() => window.confirm(i18nT('pages.artifactDeployPage.remove_profile_confirm', { name: p.name })) && removeProfile.mutate(p.name)}>
                        <Trash2 size={11} /> {i18nT('pages.artifactDeployPage.remove')}
                      </Btn>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {availableProfiles.length > 0 && (
          <div style={{ marginTop: 10, fontSize: 12, color: 'var(--muted)', display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
            <span>{i18nT('pages.artifactDeployPage.found_in_your_aws_config')}</span>
            {availableProfiles.slice(0, 8).map((n) => (
              <Btn key={n} onClick={() => addProfile.mutate({ name: n, region: 'us-west-2' })}>
                <Plus size={10} /> {n}
              </Btn>
            ))}
            {availableProfiles.length > 8 && <span>+{availableProfiles.length - 8} {i18nT('pages.artifactDeployPage.more')}</span>}
          </div>
        )}
        {showNewProfile && (
          <div style={{ marginTop: 12, padding: 12, background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 6 }}>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
              <span style={{ flex: 1, minWidth: 160 }}>
                <label style={label} htmlFor="np-name">{i18nT('pages.artifactDeployPage.profile_name_2')}</label>
                <Input id="np-name" style={{width: '100%' }} placeholder={i18nT('pages.artifactDeployPage.e_g_my_sandbox')} value={npName} onChange={(e) => setNpName(e.target.value)} />
              </span>
              <span style={{ minWidth: 140 }}>
                <label style={label} htmlFor="np-region">{i18nT('pages.artifactDeployPage.region')}</label>
                <Input id="np-region" style={{width: '100%' }} placeholder={i18nT('pages.artifactDeployPage.us_west_2')} value={npRegion} onChange={(e) => setNpRegion(e.target.value)} />
              </span>
            </div>
            <div style={{ fontSize: 12, display: 'inline-flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
              <Toggle checked={npCreate} onChange={setNpCreate} label={i18nT('pages.artifactDeployPage.also_create_in_aws_config')} />
              <span style={{ color: 'var(--muted)', fontSize: 11 }}>{i18nT('pages.artifactDeployPage.writes_only_region_credential_process_via')} <code>{i18nT('pages.artifactDeployPage.aws_configure_set')}</code> {i18nT('pages.artifactDeployPage.never_credentials')}</span>
            </div>
            {npCreate && (
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
                <span style={{ minWidth: 160 }}>
                  <label style={label} htmlFor="np-account">{i18nT('pages.artifactDeployPage.account_12_digits_optional_iam_identity_style')}</label>
                  <Input id="np-account" style={{width: '100%' }} placeholder="123456789012" value={npAccount} onChange={(e) => setNpAccount(e.target.value)} />
                </span>
                <span style={{ minWidth: 140 }}>
                  <label style={label} htmlFor="np-role">{i18nT('pages.artifactDeployPage.role_optional')}</label>
                  <Input id="np-role" style={{width: '100%' }} placeholder={i18nT('pages.artifactDeployPage.admin')} value={npRole} onChange={(e) => setNpRole(e.target.value)} />
                </span>
              </div>
            )}
            <Btn primary disabled={!npName.trim()}
              onClick={() => addProfile.mutate({ name: npName.trim(), region: npRegion.trim() || 'us-west-2', create: npCreate, account: npAccount.trim(), role: npRole.trim() })}>
              {npCreate ? i18nT('pages.artifactDeployPage.create_register') : i18nT('pages.artifactDeployPage.register')}
            </Btn>
          </div>
        )}
        {reach && (
          <div style={{ marginTop: 10, fontSize: 12, color: reach.reachable ? 'var(--ok)' : 'var(--danger)' }}>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
              {reach.reachable
                ? <><CheckCircle size={12} /> {reach.profile}{i18nT('pages.artifactDeployPage.access_reachable')}{reach.account ? ` (account ${reach.account})` : ''}</>
                : <><XCircle size={12} /> {reach.detail || reach.error || i18nT('pages.artifactDeployPage.not_reachable')}</>}
            </span>
            <div style={{ color: 'var(--muted)', fontSize: 11 }}>{reach.note}</div>
          </div>
        )}
        {policy && (
          <div style={{ marginTop: 10 }}>
            <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>
              {i18nT('pages.artifactDeployPage.apply_this_policy_yourself_kirocrew_never_edits')}
              {policyTier === 'fullstack' && <span style={{ color: 'var(--accent)' }}> {i18nT('pages.artifactDeployPage.fullstack_tier_includes_lambda_api_gateway_dynam')}</span>}
            </div>
            <pre style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 6, padding: 10, fontSize: 11, maxHeight: 240, overflow: 'auto' }}>{policy}</pre>
            <Btn onClick={() => navigator.clipboard.writeText(policy)}><Copy size={12} /> {i18nT('pages.artifactDeployPage.copy_policy')}</Btn>
            {boundaryPolicy && (
              <div style={{ marginTop: 10 }}>
                <div style={{ fontSize: 11, color: 'var(--warn)', marginBottom: 4 }}>
                  {boundaryNote || i18nT('pages.artifactDeployPage.fullstack_also_requires_the_permissions_boundary')}
                </div>
                <pre style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 6, padding: 10, fontSize: 11, maxHeight: 200, overflow: 'auto' }}>{boundaryPolicy}</pre>
                <Btn onClick={() => navigator.clipboard.writeText(boundaryPolicy)}><Copy size={12} /> {i18nT('pages.artifactDeployPage.copy_boundary_policy')}</Btn>
              </div>
            )}
          </div>
        )}
        <div style={{ marginTop: 12, fontSize: 12, color: 'var(--muted)', display: 'flex', alignItems: 'center', gap: 6 }}>
          <Globe size={13} stroke={'var(--accent)'} />
          {i18nT('pages.artifactDeployPage.to_publish_open_an_artifact_and_choose')} <b style={{ color: 'var(--text)' }}>{i18nT('pages.artifactDeployPage.publish_publish_to_public_web_your_aws')}</b>.
        </div>
      </Card>

      {/* Pending confirmations — deploy previews awaiting human confirm */}
      <PendingConfirmations qc={qc} />

      {/* Ready to deploy — CardTitle + InfoTip */}
      {draftWebapps.length > 0 && (
        <Card>
          <CardTitle>
            {i18nT('pages.artifactDeployPage.ready_to_deploy_count', { count: draftWebapps.length })} <InfoTip text={i18nT('pages.artifactDeployPage.ready_to_deploy_tip')} />
          </CardTitle>
          <table className="w-full border-collapse table-striped">
            <thead>
              <tr>
                {['Name', 'Status', 'Est. Cost', 'Profile', ''].map(h => (
                  <th key={h} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {draftWebapps.map((a) => {
                const cost = webappCost(a)
                return (
                  <tr key={a.slug} className="hover:bg-bg-hover transition-colors">
                    <td className="px-2.5 py-2 border-b border-border text-sm font-semibold">
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                        <Rocket size={13} stroke={'var(--accent)'} /> {a.slug}
                      </span>
                    </td>
                    <td className="px-2.5 py-2 border-b border-border text-sm"><Badge variant="warn">{i18nT('pages.artifactDeployPage.not_deployed')}</Badge></td>
                    <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{cost > 0 ? `≤ $${cost.toFixed(4)}` : '~$0.00'}</td>
                    <td className="px-2.5 py-2 border-b border-border text-sm">
                      {profiles.length > 0 && (
                        <SimpleSelect
                          options={profiles.map((p) => p.name)}
                          value={draftProfiles[a.slug] || defaultProfile || ''}
                          onChange={(v) => setDraftProfiles((m) => ({ ...m, [a.slug]: v }))}
                          clearLabel={defaultProfile ? `${defaultProfile} (default)` : i18nT('pages.artifactDeployPage.default')}
                          aria-label={i18nT('pages.artifactDeployPage.deploy_profile_for_slug', { slug: a.slug })}
                          style={{ minWidth: 100 }}
                        />
                      )}
                    </td>
                    <td className="px-2.5 py-2 border-b border-border text-sm text-right">
                      <span style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
                        <Btn primary onClick={() => deployDraft(a.slug)} aria-label={i18nT('pages.artifactDeployPage.deploy_artifact', { name: a.slug })}>
                          <Rocket size={11} /> {i18nT('pages.artifactDeployPage.deploy')}
                        </Btn>
                        <Link to={`/artifacts/${encodeURIComponent(a.slug)}`} style={linkBtn}>
                          <ExternalLink size={11} /> {i18nT('pages.artifactDeployPage.details')}
                        </Link>
                      </span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <div style={{ paddingTop: 10, fontSize: 11, color: 'var(--muted)' }}>
            {i18nT('pages.artifactDeployPage.deploy_opens_a_new_chat_session_that_runs_the_ar')}
          </div>
        </Card>
      )}

      {/* Deployments — CardTitle + InfoTip + table-striped */}
      <Card>
        <div className="flex justify-between items-center">
          <CardTitle>
            {i18nT('pages.artifactDeployPage.deployments_count', { count: sites.length + deployedWebapps.length })} <InfoTip text={i18nT('pages.artifactDeployPage.deployments_tip')} />
          </CardTitle>
          <Btn onClick={() => { qc.invalidateQueries({ queryKey: ['deploy-web', 'sites'] }); qc.invalidateQueries({ queryKey: ['deploy-web', 'webapps'] }) }}><RefreshCw size={12} /> {i18nT('pages.artifactDeployPage.refresh')}</Btn>
        </div>
        {sites.length + deployedWebapps.length === 0 && (
          <div style={{ fontSize: 13, color: 'var(--muted)' }}>
            {i18nT('pages.artifactDeployPage.no_deployments_yet_publish_an_artifact_from_its')} <b style={{ color: 'var(--text)' }}>{i18nT('pages.artifactDeployPage.deploy')}</b>.
          </div>
        )}
        {(sites.length + deployedWebapps.length > 0) && (
          <table className="w-full border-collapse table-striped">
            <thead>
              <tr>
                {['Name', 'Type', 'Status', 'Profile', 'URL', 'Cost', 'Actions'].map(h => (
                  <th key={h} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {/* Static site rows */}
              {sites.map((s) => (
                <tr key={`static-${s.site_id}`} className="hover:bg-bg-hover transition-colors">
                  <td className="px-2.5 py-2 border-b border-border text-sm font-semibold">{s.site_id}</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm"><span style={{ border: '1px solid var(--border)', color: 'var(--muted)', padding: '1px 6px', borderRadius: 9999, fontSize: 10, fontWeight: 500 }}>{i18nT('pages.artifactDeployPage.static')}</span></td>
                  <td className="px-2.5 py-2 border-b border-border text-sm"><Badge variant={s.status === 'deployed' || s.status === 'live' ? 'ok' : s.status === 'error' ? 'err' : 'warn'}>{s.status || i18nT('pages.artifactDeployPage.unknown')}</Badge></td>
                  <td className="px-2.5 py-2 border-b border-border text-sm">{s.profile ? <span style={chip}>{s.profile}</span> : <span style={{ color: 'var(--muted)' }}>—</span>}</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm" style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.url ? (safeHttpUrl(s.url) ? <a href={safeHttpUrl(s.url)!} target="_blank" rel="noreferrer" style={{ color: 'var(--accent)' }}>{s.url}</a> : <span style={{ color: 'var(--muted)' }}>{s.url}</span>) : '—'}</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{i18nT('pages.artifactDeployPage.0_00_mo')}</td>
                  <td className="px-2.5 py-2 border-b border-border text-sm">
                    <span style={{ display: 'flex', gap: 5 }}>
                      <Btn onClick={() => recall(s)}><Undo2 size={11} /> {i18nT('pages.artifactDeployPage.recall')}</Btn>
                      <Btn danger onClick={() => destroy(s)}><Trash2 size={11} /> {i18nT('pages.artifactDeployPage.destroy')}</Btn>
                    </span>
                  </td>
                </tr>
              ))}
              {/* Webapp rows */}
              {deployedWebapps.map((a) => {
                const m = a.webapp_metadata!
                const url = m.deploy_target?.public_url || ''
                const cost = webappCost(a)
                return (
                  <tr key={`webapp-${a.slug}`} className="hover:bg-bg-hover transition-colors">
                    <td className="px-2.5 py-2 border-b border-border text-sm font-semibold">{a.slug}</td>
                    <td className="px-2.5 py-2 border-b border-border text-sm"><span style={{ background: 'var(--accent-subtle)', color: 'var(--accent)', padding: '1px 6px', borderRadius: 9999, fontSize: 10, fontWeight: 500 }}>{i18nT('pages.artifactDeployPage.webapp_kind_badge')}</span></td>
                    <td className="px-2.5 py-2 border-b border-border text-sm"><Badge variant={m.lifecycle?.status === 'deployed' || m.lifecycle?.status === 'live' ? 'ok' : m.lifecycle?.status === 'error' ? 'err' : 'warn'}>{m.lifecycle?.status || i18nT('pages.artifactDeployPage.unknown')}</Badge></td>
                    <td className="px-2.5 py-2 border-b border-border text-sm">{m.deploy_target?.profile ? <span style={chip}>{m.deploy_target.profile}</span> : <span style={{ color: 'var(--muted)' }}>—</span>}</td>
                    <td className="px-2.5 py-2 border-b border-border text-sm" style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{url ? (safeHttpUrl(url) ? <a href={safeHttpUrl(url)!} target="_blank" rel="noreferrer" style={{ color: 'var(--accent)' }}>{url}</a> : <span style={{ color: 'var(--muted)' }}>{url}</span>) : '—'}</td>
                    <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{cost > 0 ? `≤$${cost.toFixed(4)}` : '~$0.00'}</td>
                    <td className="px-2.5 py-2 border-b border-border text-sm">
                      <Link to={`/artifacts/${encodeURIComponent(a.slug)}`} style={linkBtn}>
                        <ExternalLink size={11} /> {i18nT('pages.artifactDeployPage.details')}
                      </Link>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
        {/* Account-level total */}
        {(sites.length + deployedWebapps.length > 0) && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, paddingTop: 12, fontSize: 12.5, fontWeight: 600 }}>
            <span>{i18nT('pages.artifactDeployPage.estimated_total')} {i18nT('pages.artifactDeployPage.static_site', { count: sites.length })} + {i18nT('pages.artifactDeployPage.webapp', { count: deployedWebapps.length })}:</span>
            <span style={{ color: 'var(--accent)' }}>~${totalWebappUsd.toFixed(4)}</span>
            <span style={{ color: 'var(--muted)', fontWeight: 400, fontSize: 11 }}>
              {i18nT('pages.artifactDeployPage.worst_case_tiers_over_each_ttl_window_estimate_n')}
            </span>
          </div>
        )}
      </Card>
      </div>
    </>
  )
}

// ── Pending confirmations component ─────────────────────────────────────

interface PendingEntry {
  id: string
  site_id: string
  artifact_slug: string
  local_dir: string
  profile: string
  region: string
  ttl_hours: number
  scan_summary: string
  override_scan_required?: boolean
  created_at_epoch: number
}

function PendingConfirmations({ qc }: { qc: ReturnType<typeof useQueryClient> }) {
  const { data } = useQuery<{ pending: PendingEntry[] }>({
    queryKey: ['deploy-web', 'pending'],
    queryFn: async () => {
      const r = await fetch(BASE + '/pending', { headers: { 'X-Session-Key': 'dashboard:ui' } })
      return (await r.json()) as { pending: PendingEntry[] }
    },
    refetchInterval: 10000,
  })
  const pending = data?.pending || []

  const confirmMut = useMutation({
    mutationFn: async ({ id, overrideScan }: { id: string; overrideScan?: boolean }) => {
      // Entries flagged override_scan_required are blocked by
      // overridable (non-credential) findings — the human's explicit
      // "Deploy anyway" sends override_scan so the backend clears them.
      const res = await fetch(BASE + `/pending/${id}/confirm`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Session-Key': 'dashboard:ui' }, body: JSON.stringify(overrideScan ? { override_scan: true } : {}) })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || `Confirm failed (${res.status})`)
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['deploy-web', 'pending'] })
      qc.invalidateQueries({ queryKey: ['deploy-web', 'sites'] })
    },
  })

  const dismissMut = useMutation({
    mutationFn: async (id: string) => {
      const res = await fetch(BASE + `/pending/${id}/dismiss`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Session-Key': 'dashboard:ui' } })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || `Dismiss failed (${res.status})`)
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['deploy-web', 'pending'] })
    },
  })

  if (!pending.length) return null

  return (
    <Card>
      <CardTitle>
        <Rocket size={15} /> {i18nT('pages.artifactDeployPage.pending_confirmations_count', { count: pending.length })}
      </CardTitle>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10, fontSize: 13 }}>
        {pending.map((e) => {
          const age = Math.round((Date.now() / 1000 - e.created_at_epoch) / 60)
          const source = e.artifact_slug || e.local_dir || i18nT('pages.artifactDeployPage.unknown_2')
          return (
            <div key={e.id} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg)' }}>
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 500 }}>{e.site_id}</div>
                  <div style={{ color: 'var(--muted)', fontSize: 11 }}>
                    {i18nT('pages.artifactDeployPage.source')} {source} {i18nT('pages.artifactDeployPage.profile_2')} {e.profile || i18nT('pages.artifactDeployPage.default')} {i18nT('pages.artifactDeployPage.ttl')} {e.ttl_hours}{i18nT('pages.artifactDeployPage.h_scan')} {e.scan_summary} &middot; {age}{i18nT('pages.artifactDeployPage.m_ago')}
                  </div>
                  {e.override_scan_required && (
                    <div style={{ color: 'var(--warn)', fontSize: 11, marginTop: 2 }}>
                      {i18nT('pages.artifactDeployPage.blocked_by_non_credential_scan_findings_review_a')}
                    </div>
                  )}
                </div>
                <Btn danger onClick={() => confirmMut.mutate({ id: e.id, overrideScan: !!e.override_scan_required })} disabled={confirmMut.isPending}>
                  {e.override_scan_required ? i18nT('pages.artifactDeployPage.deploy_anyway') : i18nT('pages.artifactDeployPage.confirm_deploy')}
                </Btn>
                <Btn onClick={() => dismissMut.mutate(e.id)} disabled={dismissMut.isPending}>
                  {i18nT('pages.artifactDeployPage.dismiss')}
                </Btn>
              </div>
              {(confirmMut.isError || dismissMut.isError) && (
                <div style={{ color: 'var(--error, #dc2626)', fontSize: 11, padding: '2px 10px' }}>
                  {(confirmMut.error as Error)?.message || (dismissMut.error as Error)?.message}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </Card>
  )
}
