/**
 * The Members side panel's Dashboard tab renders the crewmate's published
 * document and nothing around it (crewmate-panel IA): no summary card, no
 * Contained bar, no Expand control. The containment that matters is unchanged:
 * the frame carries the same `allow-scripts`-only sandbox as the drawer.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const DOC_URL = "/sandbox-doc/panel123/1700000000.mac";

vi.mock("../hooks/useTheme", () => ({
  useTheme: () => ({ theme: "light", colorTheme: "default", themeVersion: 0 }),
}));

vi.mock("../lib/widgetSrcdoc", () => ({
  THEME_VAR_NAMES: [] as string[],
  readThemeVars: () => ({}) as Record<string, string>,
  buildSrcdoc: (opts: { html: string }) => opts.html,
}));

const mintSpy = vi.fn();
const panelSpy = vi.fn();
vi.mock("../api/client", () => ({
  api: {
    sandboxDocUrl: (html: string) => mintSpy(html),
    memberPanel: (slug: string, member: string) => panelSpy(slug, member),
  },
  ApiError: class extends Error {},
}));

import {
  CrewDashboardFrame,
  CREW_WEBVIEW_SANDBOX,
} from "../pages/members/CrewWebview";

function mount(props: { displayName?: string } = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <CrewDashboardFrame slug="radar" member="Radar" {...props} />
    </QueryClientProvider>,
  );
}

describe("CrewDashboardFrame", () => {
  beforeEach(() => {
    mintSpy.mockReset();
    mintSpy.mockResolvedValue({ url: DOC_URL });
    panelSpy.mockReset();
    panelSpy.mockResolvedValue({
      panel: { template: "report", title: "Radar", crew: "Radar", published_at: "2026-10-02T10:00:00", data: {} },
      html: "<main>report</main>",
    });
  });

  it("mints on mount and renders the document bare, in the drawer's sandbox", async () => {
    mount();
    const frame = await screen.findByTestId("crew-dashboard-iframe");
    expect(frame).toHaveAttribute("src", DOC_URL);
    expect(frame).toHaveAttribute("sandbox", CREW_WEBVIEW_SANDBOX);
    expect(panelSpy).toHaveBeenCalledWith("radar", "Radar");
    expect(mintSpy).toHaveBeenCalledTimes(1);
    // None of the drawer's chrome.
    expect(screen.queryByTestId("crew-webview-summary")).toBeNull();
    expect(screen.queryByTestId("crew-webview-expand")).toBeNull();
    expect(screen.queryByTestId("crew-webview-age")).toBeNull();
  });

  it("says nothing is published yet and points at the chat, with no set-up control, minting nothing", async () => {
    panelSpy.mockResolvedValue({ panel: null, html: null });
    mount();
    expect(await screen.findByTestId("crew-webview-empty")).toHaveTextContent(
      "Radar has not published a dashboard yet. Ask it in this chat",
    );
    expect(screen.queryByTestId("crew-webview-setup")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(mintSpy).not.toHaveBeenCalled();
  });

  it("names the crewmate by its display name in the empty state, while the read stays keyed on the exact member", async () => {
    panelSpy.mockResolvedValue({ panel: null, html: null });
    mount({ displayName: "Radar Ops" });
    expect(await screen.findByTestId("crew-webview-empty")).toHaveTextContent(
      "Radar Ops has not published a dashboard yet.",
    );
    expect(screen.getByTestId("crew-webview-empty")).not.toHaveTextContent(/^Radar has/);
    expect(panelSpy).toHaveBeenCalledWith("radar", "Radar");
  });

  it("shows the failure band with a retry when the mint fails, and no agent hand-off", async () => {
    mintSpy.mockRejectedValue(new Error("mint refused"));
    mount();
    await waitFor(() => expect(screen.getByTestId("crew-webview-mint-error-band")).toBeInTheDocument());
    expect(screen.queryByTestId("crew-dashboard-iframe")).toBeNull();
    // The hand-off is a raw navigate to /chat that unmounts the Members page
    // around this frame — its Profile card's New schedule draft included —
    // without asking the page's leave guard. Retry is the recovery here.
    expect(screen.queryByRole("button", { name: /ask the agent/i })).toBeNull();
    expect(screen.getByRole("button", { name: /retry|try again/i })).toBeInTheDocument();
  });

  it("the read-error state offers retry only, for the same reason", async () => {
    panelSpy.mockRejectedValue(new Error("boom"));
    mount();
    await screen.findByTestId("crew-webview-error");
    expect(screen.queryByRole("button", { name: /ask the agent/i })).toBeNull();
    expect(screen.getByTestId("crew-webview-error-retry")).toBeInTheDocument();
  });
});
