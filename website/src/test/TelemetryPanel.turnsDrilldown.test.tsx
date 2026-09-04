/**
 * Telemetry panel: the Spend table's per-turn drill-down.
 *
 * The session ranking answers "which conversation cost the most"; the
 * drill-down answers "which TURNS did it". A mid-session model switch or one
 * runaway turn is invisible in an average, so each session row grows a chevron
 * that fetches GET /api/usage/turns for that slot and renders the rows beneath
 * it. The rows come from the same always-written shard store as the totals, so
 * the drill-down needs no OTEL switch and no extra recording.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import TelemetryPanel from "../pages/TelemetryPanel";

const convo = (over: Record<string, unknown> = {}) => ({
  slot: "chat-1-1700000000",
  channel: "dashboard",
  category: "dashboard",
  credits: 100,
  turns: 10,
  peak_pct: 42,
  span_days: 1,
  first_ts: 1700000000,
  growth_pct_per_turn: null,
  turns_to_compaction: null,
  ...over,
});

const resp = (conversations: Record<string, unknown>[]) => ({
  enabled: true,
  window_days: 14,
  shard_count: 3,
  metrics_dir: "/tmp/metrics",
  startup: null,
  turn: null,
  context: null,
  other: [],
  cost: {
    window_days: 7,
    credits: 1000,
    turns: 100,
    per_turn: 10,
    prior_credits: 500,
    prior_turns: 50,
    prior_per_turn: 10,
    delta_pct: 100,
    by_category: [],
    priciest: { credits: 50, slot: "chat-1-1700000000", ts: "2026-08-01" },
    by_model: [],
    by_channel: [],
    context_bands: [],
    conversations,
    navigable_category: "dashboard",
    conversation_count: conversations.length,
  },
});

vi.mock("../api/client", () => ({
  api: { telemetryStartup: vi.fn(), usageTurns: vi.fn() },
}));

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>
    <MemoryRouter>{children}</MemoryRouter>
  </QueryClientProvider>
);

async function mount(turns: Record<string, unknown>[]) {
  const { api } = await import("../api/client");
  vi.mocked(api.telemetryStartup).mockResolvedValue(
    resp([convo({ title: "Costly one" })]) as never,
  );
  vi.mocked(api.usageTurns).mockResolvedValue({
    slot: "chat-1-1700000000",
    turns,
  } as never);
  render(<TelemetryPanel />, { wrapper: Wrapper });
  await screen.findByRole("link", { name: "Costly one" });
}

describe("TelemetryPanel — per-turn drill-down", () => {
  beforeEach(() => {
    qc.clear();
    vi.clearAllMocks();
  });

  it("opens a session row into its per-turn rows, querying that slot", async () => {
    await mount([
      {
        ts: "2026-08-20T10:00:00Z",
        model: "claude-x",
        credits: 3.5,
        duration_ms: 42_000,
        context_used: 50_000,
        context_window: 200_000,
      },
      { ts: "2026-08-20T10:05:00Z", model: "claude-y", credits: 1.25 },
    ]);
    fireEvent.click(
      screen.getAllByRole("button", { name: "Show per-turn detail" })[0],
    );
    expect(await screen.findByText("claude-x")).toBeInTheDocument();
    const { api } = await import("../api/client");
    expect(vi.mocked(api.usageTurns)).toHaveBeenCalledWith("chat-1-1700000000");
    // Both turns render, each with its own model and credits — the row the
    // average hides is exactly what this surface exists to show.
    expect(screen.getByText("claude-y")).toBeInTheDocument();
    expect(screen.getByText("3.5")).toBeInTheDocument();
    expect(screen.getByText("1.25")).toBeInTheDocument();
    // The second row has no duration/context: unknown renders as a dash, not 0.
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("says so when the window holds no per-turn rows", async () => {
    await mount([]);
    fireEvent.click(
      screen.getAllByRole("button", { name: "Show per-turn detail" })[0],
    );
    expect(
      await screen.findByText("No per-turn usage recorded in this window."),
    ).toBeInTheDocument();
  });

  it('renders a failed fetch as an error with a retry, never as "no rows"', async () => {
    const { api } = await import("../api/client");
    vi.mocked(api.telemetryStartup).mockResolvedValue(
      resp([convo({ title: "Costly one" })]) as never,
    );
    vi.mocked(api.usageTurns).mockRejectedValue(new Error("boom"));
    render(<TelemetryPanel />, { wrapper: Wrapper });
    await screen.findByRole("link", { name: "Costly one" });
    fireEvent.click(
      screen.getAllByRole("button", { name: "Show per-turn detail" })[0],
    );
    expect(await screen.findByText("Couldn't load the turns.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    // The empty-state copy must NOT appear: a failure is not an empty window.
    expect(
      screen.queryByText("No per-turn usage recorded in this window."),
    ).toBeNull();
  });

  it("collapses again on a second click, flipping the accessible label", async () => {
    await mount([
      { ts: "2026-08-20T10:00:00Z", model: "claude-x", credits: 3.5 },
    ]);
    const toggle = screen.getAllByRole("button", {
      name: "Show per-turn detail",
    })[0];
    fireEvent.click(toggle);
    expect(await screen.findByText("claude-x")).toBeInTheDocument();
    // Expanded: the label must announce the action a click now performs.
    expect(toggle).toHaveAccessibleName("Hide per-turn detail");
    fireEvent.click(toggle);
    expect(screen.queryByText("claude-x")).toBeNull();
    expect(toggle).toHaveAccessibleName("Show per-turn detail");
  });
});
