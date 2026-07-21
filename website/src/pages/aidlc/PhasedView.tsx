import { type ReactNode } from 'react';
import { Hourglass, RefreshCw, CheckCircle, Search, XCircle, SkipForward, Square, Wrench, Shield } from 'lucide-react';
import type { TaskDetail } from '../../types';

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

const CARD_STYLE = { padding: '8px 12px', cursor: 'pointer', borderRadius: 6, marginBottom: 4,
  background: 'var(--bg-tertiary, #16213e)', display: 'flex' as const, alignItems: 'center' as const, gap: 8 };

function TaskGroup({ icon, label, items, onTaskClick, color, opacity, showError, selectedIndex, pendingEditIndexes }: {
  icon: ReactNode; label: string; items: TaskDetail[]; onTaskClick?: (i: number) => void
  color?: string; opacity?: number; showError?: boolean; selectedIndex?: number | null; pendingEditIndexes?: Set<number>
}) {
  if (!items.length) return null;
  const bg = color ? `rgba(${color},0.08)` : 'var(--bg-secondary, #1a1a2e)';
  const border = color ? `1px solid rgba(${color},0.3)` : undefined;
  return (
    <div style={{ marginTop: 12, background: bg, borderRadius: 8, padding: 12, opacity: opacity ?? 1, border }}>
      <div style={{ fontSize: 12, color: color ? `rgb(${color})` : 'var(--text-muted, #888)', marginBottom: 8, fontWeight: 600 }}>{icon} {label} ({items.length})</div>
      {items.map(t => (
        <div
          key={t.index}
          role="button"
          tabIndex={0}
          onClick={() => onTaskClick?.(t.index)}
          onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onTaskClick?.(t.index) } }}
          style={{
            ...CARD_STYLE,
            ...(t.index === selectedIndex ? { boxShadow: '0 0 0 2px var(--accent, #6366f1)' } : {}),
          }}
        >
          <span>{icon}</span>
          <span style={{ flex: 1, fontSize: 13, opacity: opacity ?? 1 }}>Task {t.index}: {t.title}</span>
          {pendingEditIndexes?.has(t.index) && <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#f59e32', flexShrink: 0 }} />}
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
            <div key={col.label} style={{ background: 'var(--bg-secondary, #1a1a2e)', borderRadius: 8, padding: 12, minHeight: 80 }}>
              <div style={{ fontSize: 12, color: 'var(--text-muted, #888)', marginBottom: 8, fontWeight: 600 }}>
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
                      ...(t.index === selectedIndex ? { boxShadow: '0 0 0 2px var(--accent, #6366f1)' } : {}),
                    }}
                  >
                    <span>{icon}</span>
                    <span style={{ flex: 1, fontSize: 13 }}>Task {t.index}: {t.title}</span>
                    {pendingEditIndexes?.has(t.index) && <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#f59e32', flexShrink: 0 }} />}
                  </div>
                );
              })}
              {items.length === 0 && (
                <div style={{ fontSize: 12, color: 'var(--text-muted, #555)', fontStyle: 'italic', padding: '8px 0' }}>None</div>
              )}
            </div>
          );
        })}
      </div>
      <TaskGroup icon={<XCircle className="lucide-inline" />} label="Failed" items={tasks.filter(t => t.status === 'failed')} onTaskClick={onTaskClick} color="239,68,68" showError selectedIndex={selectedIndex} pendingEditIndexes={pendingEditIndexes} />
      <TaskGroup icon={<SkipForward className="lucide-inline" />} label="Skipped" items={tasks.filter(t => t.status === 'skipped')} onTaskClick={onTaskClick} opacity={0.7} selectedIndex={selectedIndex} pendingEditIndexes={pendingEditIndexes} />
      <TaskGroup icon={<Square className="lucide-inline" />} label="Cancelled" items={tasks.filter(t => t.status === 'cancelled')} onTaskClick={onTaskClick} opacity={0.7} selectedIndex={selectedIndex} pendingEditIndexes={pendingEditIndexes} />
    </div>
  );
}
