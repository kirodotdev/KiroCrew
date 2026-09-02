/**
 * The Mochi instance switcher — which gateway's Mochi the one pet shows.
 *
 * This is NOT a new mechanism. Core already owns multi-instance
 * (`/api/instances/*`: registry, SSH tunnels, tokens, liveness), and Mochi
 * already has the setting that points at it — `petInstance`, documented in
 * builtins/mochi/settings.py: `"self"` (the local gateway, default) or an
 * instance id from that list. All this component does is let the user pick.
 *
 * So there is no shell IPC here and no separate Mochi-side notion of an
 * instance: the list is core's, the choice is one ordinary Mochi setting, and
 * the shell resolves that setting to a base URL when it opens the pet.
 *
 * WHY THE CHOICE LIVES ON THE LOCAL GATEWAY: one pet is a per-MACHINE resource,
 * so the pointer is a property of this machine — settings.py stores it on the
 * local instance and does not sync it. It is also stored OPAQUELY and not
 * validated against the live list, on purpose: instances come and go (TTL
 * expiry, tunnel down) and a saved choice must survive a temporarily absent
 * instance rather than being silently reset. Resolution — and the fallback to
 * `"self"` when it fails — happens when the pet is opened.
 *
 * Consequence worth saying out loud in the UI: appearance and chat history both
 * live in the chosen gateway's data dir, so switching swaps both.
 *
 * SECOND consequence, only visible once a remote is picked: the "is there a pet
 * at all" switch belongs to the HOST gateway — whichever one answers on the
 * shell's `localhost:<port>`, which for a remote setup is an `ssh -L` forward and
 * so is NOT necessarily this machine. Switching Mochi off there stops that
 * gateway's own background work (its `on_shutdown` cancels the pollers, watchlist
 * guard and stats) but no longer removes a pet being served by another crew — see
 * instanceGate.hostDisabledMeansTeardown. The App Store toggle that does it is
 * generic core UI with no Mochi hook, so this pane is where the boundary is said.
 */
import { useEffect, useState } from 'react'
import { listInstances, type CoreInstance, type InstancesView } from './panelBridge'
import { api } from '../src/mochiApi'
import { i18nT } from '../../../i18n/t'

/** settings.py SELF_INSTANCE — the local gateway. */
const SELF_INSTANCE = 'self'

/**
 * How often the pane re-reads the instance list.
 *
 * Matches the shell's own reconcile cadence so the two never disagree for long.
 */
const REFRESH_MS = 5000

/** Only a live tunnel with an allocated local port can actually host the pet. */
export function isUsable(inst: CoreInstance): boolean {
  return (inst.local_port || 0) > 0 && inst.status?.state === 'connected'
}

/**
 * Why the kind is separate from the key: `check-i18n-keys.mjs` resolves only file-scope
 * bindings, so returning the key itself and holding it in a local would make the render
 * site unresolvable — and an unresolvable key is one the gate cannot verify exists.
 * Returning a discriminant and indexing this map in place checks all four keys.
 */
const INSTANCE_STATE_KEY = {
  mochi_off: 'apps.mochi.instances.mochi_off',
  connecting: 'apps.mochi.instances.connecting',
  errored: 'apps.mochi.instances.errored',
  not_connected: 'apps.mochi.instances.not_connected',
} as const

type InstanceStateKind = keyof typeof INSTANCE_STATE_KEY

function stateKind(inst: CoreInstance): InstanceStateKind | undefined {
  if (isUsable(inst)) return undefined
  switch (inst.status?.state) {
    case 'connecting':
      return 'connecting'
    case 'error':
      return 'errored'
    default:
      return 'not_connected'
  }
}

/**
 * Which instances the switcher lists: the ones you could actually attach to.
 *
 * Only CONNECTED instances are offered, because only a live tunnel can serve the
 * pet's page — and Mochi deliberately never opens a tunnel itself (core owns
 * them). Listing a disconnected instance would be offering something that cannot
 * work and would invite the user to click it expecting a connection.
 *
 * The ONE exception is the currently-saved choice, which stays listed even when it
 * has gone away. `petInstance` is stored opaquely and survives an absent instance
 * on purpose (see builtins/mochi/settings.py), so hiding it would make a
 * remembered choice look lost: the row would vanish AND nothing would be
 * highlighted, since the saved value is not 'self'. Shown with its real state
 * instead, so "your choice is remembered, it just isn't reachable right now" is
 * visible rather than inferred.
 */
export function visibleRows(instances: CoreInstance[], current: string): CoreInstance[] {
  return instances.filter((inst) => isUsable(inst) || inst.id === current)
}

export function MochiInstancesList({
  value,
  onChange,
}: {
  /** Current `petInstance` — 'self' or an instance id. */
  value: string
  /** Save a new `petInstance`. The parent owns the settings write + dirty state. */
  onChange: (petInstance: string) => void
}) {
  const [view, setView] = useState<InstancesView | null>(null)
  /**
   * instance id -> Mochi enabled there. From the SHELL, because asking a remote
   * needs that remote's token and only the shell has it. Absent id = not known
   * yet (the shell fills this as it resolves), which is treated as "fine" rather
   * than blocking the row on a fact we simply have not learned.
   */
  const [enabledMap, setEnabledMap] = useState<Record<string, boolean>>({})

  useEffect(() => {
    // POLLED, not fetched once. Now that the list shows only CONNECTED instances,
    // a tunnel finishing its handshake means a row APPEARS — a one-shot fetch would
    // leave the user staring at a pane that never offers the instance they just
    // connected, with no way to refresh but closing and reopening Settings.
    //
    // Cheap by construction: the list is a local HTTP call, and the enabled-map
    // probe skips every instance it already has a fresh answer for, so polling adds
    // no tunnel traffic beyond the first pass.
    let cancelled = false

    const load = async () => {
      // Prefer the SHELL's answer: `/api/instances` is same-origin, so inside a pet
      // that is already showing a remote it returns the REMOTE's registry — a
      // different set of crews, or none at all if that gateway has the feature off,
      // so the crew the user wants to return to can be missing entirely. The host
      // owns the registry the stored ids refer to. Falls back to the direct fetch
      // with no shell (a browser tab, where the tab IS the host).
      //
      // The shell hands back a full InstancesView, so `disabled` and `inactive`
      // survive: rebuilding the view from a boolean here is what erased them.
      const next: InstancesView =
        (api?.instancesList ? await api.instancesList() : null) ?? (await listInstances())
      if (cancelled) return
      setView(next)
      if (api?.instancesEnabledMap) {
        try {
          const map = (await api.instancesEnabledMap()) || {}
          if (!cancelled) setEnabledMap(map)
        } catch {
          /* the badge is additive — never let it break the list */
        }
      }
    }

    void load()
    const timer = setInterval(() => void load(), REFRESH_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  const current = value || SELF_INSTANCE

  /** Mochi explicitly reported off there. `undefined` means not yet known. */
  const mochiOff = (id: string) => enabledMap[id] === false

  const row = (
    key: string,
    label: string,
    sub: string,
    selected: boolean,
    disabled: boolean,
    onPick: () => void,
  ) => (
    // A row picks an instance, so it is a control: role + focus + Enter/Space,
    // and aria-disabled rather than a silently inert div when it cannot be picked.
    <div
      key={key}
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-pressed={selected}
      aria-disabled={disabled || undefined}
      onClick={disabled ? undefined : onPick}
      onKeyDown={(e) => {
        if (!disabled && e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) {
          e.preventDefault()
          onPick()
        }
      }}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '7px 8px',
        marginBottom: 4,
        borderRadius: 6,
        background: selected ? 'var(--accent-glow)' : 'var(--bg-input)',
        border: `1px solid ${selected ? 'var(--accent)' : 'transparent'}`,
        opacity: disabled && !selected ? 0.55 : 1,
        cursor: disabled ? 'default' : 'pointer',
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 12,
            color: 'var(--text)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {label}
        </div>
        {sub && (
          <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 2 }}>{sub}</div>
        )}
      </div>
      {selected && (
        <span style={{ fontSize: 10, color: 'var(--accent)' }}>{i18nT('apps.mochi.instances.current')}</span>
      )}
    </div>
  )

  return (
    <>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 8, lineHeight: 1.6 }}>
        {i18nT('apps.mochi.instances.blurb')}
      </div>

      {/* 'self' is always offered and always usable — it is the gateway serving
          this window, and it is what an unresolvable choice falls back to. */}
      {row(
        SELF_INSTANCE,
        i18nT('apps.mochi.instances.this_computer'),
        '',
        current === SELF_INSTANCE,
        false,
        () => onChange(SELF_INSTANCE),
      )}

      {view === null && (
        <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{i18nT('apps.mochi.instances.loading')}</div>
      )}

      {view?.state === 'disabled' && (
        <div style={{ fontSize: 11, color: 'var(--text-muted)', lineHeight: 1.6 }}>
          {i18nT('apps.mochi.instances.feature_off')}
        </div>
      )}

      {view?.state === 'error' && (
        <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{i18nT('apps.mochi.instances.list_failed')}</div>
      )}

      {view?.state === 'inactive' && (
        <div style={{ fontSize: 11, color: 'var(--danger)', lineHeight: 1.6 }}>
          {i18nT('apps.mochi.instances.needs_restart')}
        </div>
      )}

      {(view?.state === 'ready' || view?.state === 'inactive') &&
        visibleRows(view.instances, current).map((inst) => {
          // Mochi being off there outranks the tunnel state in the label: the
          // tunnel is fine, so "not connected" would be actively misleading.
          const kind: InstanceStateKind | undefined =
            mochiOff(inst.id) ? 'mochi_off' : stateKind(inst)
          const selected = current === inst.id
          const usable = isUsable(inst) && !mochiOff(inst.id)
          return row(
            inst.id,
            inst.name || inst.id,
            kind ? i18nT(INSTANCE_STATE_KEY[kind]) : '',
            selected,
            // The saved instance stays visible and highlighted even when it is not
            // usable — it IS the saved value — it just cannot be newly picked.
            !usable && !selected,
            () => onChange(inst.id),
          )
        })}

      <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 8, lineHeight: 1.6 }}>
        {i18nT('apps.mochi.instances.switch_note')}
      </div>

      {/* Only for a REMOTE choice, because only then are the two gateways
          different and the split worth explaining: with 'self' the crew serving
          this window IS the crew being shown. Informational rather than a
          warning — since the pet now survives a local disable, this describes a
          boundary instead of announcing a loss. */}
      {current !== SELF_INSTANCE && (
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 6, lineHeight: 1.6 }}>
          {i18nT('apps.mochi.instances.host_disable_note')}
        </div>
      )}
    </>
  )
}
