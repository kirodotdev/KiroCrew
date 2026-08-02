import { type ReactNode } from 'react';
import { Hourglass, RefreshCw, CheckCircle, Search, XCircle, SkipForward, Square, Wrench, Shield } from 'lucide-react';
import type { TaskDetail } from '../../types';

import { i18nT } from '../../i18n/t'
interface Props {
  tasks: TaskDetail[];
  onTaskClick?: (index: number) => void;
  selectedIndex?: number | null;
  pendingEditIndexes?: Set<number>;
}

type Column = { label: string; statuses: string[]; icon: ReactNode };
const COLUMNS: Column[] = [
  { label: 'To do', statuses: ['pending'], icon: <Hourglass className="lucide-inline" /> },
  { label: 'In progress', statuses: ['in_progress', 'reviewing', 'cancelling'], icon: <RefreshCw className="lucide-inline" /> },
  { label: 'Done', statuses: ['passed', 'done', 'completed'], icon: <CheckCircle className="lucide-inline" /> },
];

const statusIcon: Record<string, ReactNode> = {
  pending: <Hourglass className="lucide-inline" />, in_progress: <RefreshCw className="lucide-inline" />, reviewing: <Search className="lucide-inline" />, passed: <CheckCircle className="lucide-inline" />, done: <CheckCircle className="lucide-inline" />,
  completed: <CheckCircle className="lucide-inline" />, failed: <XCircle className="lucide-inline" />, skipped: <SkipForward className="lucide-inline" />, cancelled: <Square className="lucide-inline" />, cancelling: <Hourglass className="lucide-inline" />,
};

// Column and card surfaces use theme tokens that exist across all 34 theme
// blocks. `--bg-secondary` / `--bg-tertiary` / `--text-muted` are not defined in
// any theme, so relying on them with dark-only literal fallbacks leaves the board
// navy-on-white in light themes. `--bg-elevated` raises the column off the page
// and `--bg-hover` raises the card off the column, in both directions.
const COLUMN_STYLE = { background: 'var(--bg-elevated)', border: '1px solid var(--border)',
  borderRadius: 8, padding: 12, minHeight: 80 };

const CARD_STYLE = { padding: '8px 12px', cursor: 'pointer', borderRadius: 6, marginBottom: 4,
  background: 'var(--bg-hover)', display: 'flex' as const, alignItems: 'center' as const, gap: 8 };

/** Selected-card ring and the unsaved-edit dot, as tokens rather than literals. */
const SELECTED_RING = { boxShadow: '0 0 0 2px var(--accent)' };
const EDIT_DOT = { width: 8, height: 8, borderRadius: '50%', background: 'var(--warn)',
  flexShrink: 0 } as const;

/** Surface for the tinted (failed) group. Written out rather than interpolated from
 * a tint argument: there is exactly one tinted group, and a module-level style
 * constant matches the shape the other style constants here already use. color-mix
 * keeps the tint readable on light and dark alike; a raw rgba() of a hardcoded red
 * did not. */
const DANGER_GROUP_STYLE = {
  background: 'color-mix(in srgb, var(--danger) 10%, var(--bg-elevated))',
  border: '1px solid color-mix(in srgb, var(--danger) 35%, transparent)',
  color: 'var(--danger)',
} as const;

function TaskGroup({ icon, label, items, onTaskClick, danger, opacity, showError, selectedIndex, pendingEditIndexes }: {
  icon: ReactNode; label: string; items: TaskDetail[]; onTaskClick?: (i: number) => void
  /** Tint the group with the danger token (the failed group). */
  danger?: boolean; opacity?: number; showError?: boolean; selectedIndex?: number | null; pendingEditIndexes?: Set<number>
}) {
  if (!items.length) return null;
  // Untinted groups intentionally carry no border: the columns above already supply
  // the visual framing, and this matches the shape the view had before.
  const background = danger ? DANGER_GROUP_STYLE.background : COLUMN_STYLE.background;
  const border = danger ? DANGER_GROUP_STYLE.border : undefined;
  const headingColor = danger ? DANGER_GROUP_STYLE.color : 'var(--muted)';
  return (
    <div style={{ marginTop: 12, background, borderRadius: 8, padding: 12, opacity: opacity ?? 1, border }}>
      <div style={{ fontSize: 12, color: headingColor, marginBottom: 8, fontWeight: 600 }}>{icon} {label} ({items.length})</div>
      {items.map(t => (
        <div
          key={t.index}
          role="button"
          tabIndex={0}
          onClick={() => onTaskClick?.(t.index)}
          onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onTaskClick?.(t.index) } }}
          style={{
            ...CARD_STYLE,
            ...(t.index === selectedIndex ? SELECTED_RING : {}),
          }}
        >
          <span>{icon}</span>
          <span style={{ flex: 1, fontSize: 13, opacity: opacity ?? 1 }}>{i18nT('pages.aidlc.phasedView.task')} {t.index}: {t.title}</span>
          {pendingEditIndexes?.has(t.index) && <span style={EDIT_DOT} />}
          {showError && t.error && <span style={{ fontSize: 11, color: 'var(--danger)', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.error}</span>}
        </div>
      ))}
    </div>
  );
}

export default function PhasedView({ tasks, onTaskClick, selectedIndex, pendingEditIndexes }: Props) {
  return (
    <div>
      <div className="grid grid-cols-3 gap-3">
        {COLUMNS.map(col => {
          const items = tasks.filter(t => col.statuses.includes(t.status));
          return (
            <div key={col.label} style={COLUMN_STYLE}>
              <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 8, fontWeight: 600 }}>
                {col.icon} {col.label} ({items.length})
              </div>
              {items.map(t => {
                const icon = t.task_type === 'fix' ? <Wrench className="lucide-inline" /> : t.task_type === 'checkpoint' ? <Shield className="lucide-inline" /> : statusIcon[t.status] ?? <Hourglass className="lucide-inline" />;
                return (
                  <div
                    key={t.index}
                    role="button"
                    tabIndex={0}
                    onClick={() => onTaskClick?.(t.index)}
                    onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onTaskClick?.(t.index) } }}
                    style={{
                      ...CARD_STYLE,
                      ...(t.index === selectedIndex ? SELECTED_RING : {}),
                    }}
                  >
                    <span>{icon}</span>
                    <span style={{ flex: 1, fontSize: 13 }}>{i18nT('pages.aidlc.phasedView.task')} {t.index}: {t.title}</span>
                    {pendingEditIndexes?.has(t.index) && <span style={EDIT_DOT} />}
                  </div>
                );
              })}
              {items.length === 0 && (
                <div style={{ fontSize: 12, color: 'var(--muted)', fontStyle: 'italic', padding: '8px 0' }}>{i18nT('pages.aidlc.phasedView.none')}</div>
              )}
            </div>
          );
        })}
      </div>
      <TaskGroup icon={<XCircle className="lucide-inline" />} label={i18nT('pages.aidlc.phasedView.failed')} items={tasks.filter(t => t.status === 'failed')} onTaskClick={onTaskClick} danger showError selectedIndex={selectedIndex} pendingEditIndexes={pendingEditIndexes} />
      <TaskGroup icon={<SkipForward className="lucide-inline" />} label={i18nT('pages.aidlc.phasedView.skipped')} items={tasks.filter(t => t.status === 'skipped')} onTaskClick={onTaskClick} opacity={0.7} selectedIndex={selectedIndex} pendingEditIndexes={pendingEditIndexes} />
      <TaskGroup icon={<Square className="lucide-inline" />} label={i18nT('pages.aidlc.phasedView.cancelled')} items={tasks.filter(t => t.status === 'cancelled')} onTaskClick={onTaskClick} opacity={0.7} selectedIndex={selectedIndex} pendingEditIndexes={pendingEditIndexes} />
    </div>
  );
}
