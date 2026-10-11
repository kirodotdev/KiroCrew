/**
 * The Members side panel's Dashboard tab renders the crewmate's published
 * document and nothing around it (crewmate-panel IA): no summary card, no
 * Contained bar, no Expand control. The containment that matters is unchanged:
 * the frame carries the same `allow-scripts`-only sandbox as the drawer.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
const dashboardSpy = vi.fn();
// The face is the real `CrewAvatar`'s concern (own tests); here only the
// identity it is handed matters, so it is a marker carrying its props.
const avatarSpy = vi.fn();
vi.mock("../components/CrewAvatar", () => ({
  default: (props: { seed: string; avatar?: unknown; size?: number }) => {
    avatarSpy(props);
    return <div data-testid="crew-avatar-stub" />;
  },
}));

vi.mock("../api/client", () => ({
  api: {
    sandboxDocUrl: (html: string) => mintSpy(html),
    memberPanel: (slug: string, member: string) => panelSpy(slug, member),
    memberDashboard: (slug: string, member: string) => dashboardSpy(slug, member),
  },
  ApiError: class extends Error {},
}));

import {
  CrewDashboardFrame,
  CREW_WEBVIEW_SANDBOX,
} from "../pages/members/CrewWebview";

function mount(props: { displayName?: string; avatar?: unknown; onAct?: (text: string) => void; onOpenPreviews?: () => void } = {}) {
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
    dashboardSpy.mockReset();
    // Nothing adopted: the gateway answers `empty` with version 0.
    dashboardSpy.mockResolvedValue({ instance_version: 0, state: "empty" });
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

  it("the empty state is the crewmate speaking for itself, with its own face, minting nothing", async () => {
    panelSpy.mockResolvedValue({ panel: null, html: null });
    avatarSpy.mockReset();
    const avatar = { kind: "ghost", traits: { eyes: "canon" } };
    mount({ displayName: "Radar Ops", avatar });
    const empty = await screen.findByTestId("crew-webview-empty");
    expect(empty).toHaveTextContent("I haven't published a dashboard yet.");
    expect(empty).toHaveTextContent("Tell me in the chat what you want on it.");
    // The region is named after the crewmate, so assistive tech hears who "I" is.
    expect(empty).toHaveAttribute("aria-label", expect.stringContaining("Radar Ops"));
    // The face is the roster's: the exact member as seed, the record's avatar verbatim.
    expect(avatarSpy).toHaveBeenCalledWith(expect.objectContaining({ seed: "Radar", avatar }));
    // No set-up control: nothing in the crew editor makes a crewmate publish.
    expect(screen.queryByTestId("crew-webview-setup")).toBeNull();
    expect(mintSpy).not.toHaveBeenCalled();
    // The read stays keyed on the exact member, not the display name.
    expect(panelSpy).toHaveBeenCalledWith("radar", "Radar");
  });

  it("withholds the prompts when there is no chat box to put them in", async () => {
    panelSpy.mockResolvedValue({ panel: null, html: null });
    mount();
    await screen.findByTestId("crew-webview-empty");
    expect(screen.queryByTestId("crew-webview-empty-prompts")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("offers three prompts that land in the chat box through onAct, and publish nothing", async () => {
    panelSpy.mockResolvedValue({ panel: null, html: null });
    const onAct = vi.fn();
    mount({ onAct });
    await screen.findByTestId("crew-webview-empty-prompts");
    const prompts = screen.getAllByTestId("crew-webview-empty-prompt");
    expect(prompts).toHaveLength(3);
    expect(prompts[0]).toHaveTextContent("Publish a dashboard");
    fireEvent.click(prompts[0]);
    expect(onAct).toHaveBeenCalledTimes(1);
    // The exact sentence shown is what lands: the person sends it unchanged.
    expect(onAct).toHaveBeenCalledWith(prompts[0].textContent);
    expect(mintSpy).not.toHaveBeenCalled();
    // Each prompt is described by the lead line, the one statement that clicking
    // does not act -- so a screen reader hears the caveat with the prompt's name.
    const leadId = prompts[0].getAttribute("aria-describedby");
    expect(leadId).toBeTruthy();
    expect(document.getElementById(leadId!)).toHaveTextContent("for you to edit and send");
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

  describe("the newer-dashboard hint", () => {
    const HINT =
      "This tab shows the published view. This crewmate also has a dynamic dashboard: turn on Dynamic Dashboard in Feature Previews to see it.";
    // Over the empty state the line says nothing is published yet, so it never
    // asserts a page the bubble below says does not exist.
    const HINT_EMPTY =
      "This tab shows published views, and none exists yet. This crewmate's dynamic dashboard appears once you turn on Dynamic Dashboard in Feature Previews.";

    it("says a newer dashboard exists when this crewmate adopted one", async () => {
      dashboardSpy.mockResolvedValue({ instance_version: 3, state: "live" });
      mount();
      const hint = await screen.findByTestId("crew-dashboard-newer-hint", {}, { timeout: 2000 });
      expect(hint).toHaveTextContent(HINT);
      expect(dashboardSpy).toHaveBeenCalledWith("radar", "Radar");
      // Above the frame, never instead of it: the published record still draws.
      expect(await screen.findByTestId("crew-dashboard-iframe", {}, { timeout: 2000 })).toBeInTheDocument();
    });

    it("opens Feature Previews through the page's own exit when one is given", async () => {
      dashboardSpy.mockResolvedValue({ instance_version: 2, state: "live" });
      const onOpenPreviews = vi.fn();
      mount({ onOpenPreviews });
      const open = await screen.findByTestId("crew-dashboard-newer-hint-open", {}, { timeout: 2000 });
      expect(open).toHaveTextContent("Open Feature Previews");
      fireEvent.click(open);
      expect(onOpenPreviews).toHaveBeenCalledTimes(1);
    });

    it("offers no button when there is no exit to take", async () => {
      dashboardSpy.mockResolvedValue({ instance_version: 2, state: "live" });
      mount();
      await screen.findByTestId("crew-dashboard-newer-hint", {}, { timeout: 2000 });
      expect(screen.queryByTestId("crew-dashboard-newer-hint-open")).toBeNull();
    });

    it("says it over the empty state too, where the reader most needs it", async () => {
      panelSpy.mockResolvedValue({ panel: null, html: null });
      dashboardSpy.mockResolvedValue({ instance_version: 1, state: "live" });
      mount();
      expect(await screen.findByTestId("crew-dashboard-newer-hint", {}, { timeout: 2000 })).toHaveTextContent(HINT_EMPTY);
      expect(screen.getByTestId("crew-webview-empty")).toBeInTheDocument();
    });

    it("stays silent when nothing was adopted", async () => {
      mount();
      await screen.findByTestId("crew-dashboard-iframe", {}, { timeout: 2000 });
      await waitFor(() => expect(dashboardSpy).toHaveBeenCalled(), { timeout: 2000 });
      expect(screen.queryByTestId("crew-dashboard-newer-hint")).toBeNull();
    });

    it("stays silent when the dashboard read is refused", async () => {
      dashboardSpy.mockRejectedValue(new Error("owner_only"));
      mount();
      await screen.findByTestId("crew-dashboard-iframe", {}, { timeout: 2000 });
      await waitFor(() => expect(dashboardSpy).toHaveBeenCalled(), { timeout: 2000 });
      expect(screen.queryByTestId("crew-dashboard-newer-hint")).toBeNull();
    });
  });
});
