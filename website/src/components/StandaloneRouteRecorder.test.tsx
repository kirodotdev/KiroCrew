import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { act } from "react";
import { StandaloneRouteRecorder } from "./StandaloneRouteRecorder";
import { usePersistedBool } from "../hooks/usePersistedBool";
import {
  STANDALONE_LAST_ROUTE_KEY,
  STANDALONE_REOPEN_KEY,
} from "../lib/standaloneLastRoute";

function setStandalone(on: boolean): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: on && query === "(display-mode: standalone)",
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

let navigateTo: (to: string) => void = () => {};
function Nav() {
  navigateTo = useNavigate();
  return null;
}

describe("StandaloneRouteRecorder", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("records the shown route, including later navigations, when standalone", () => {
    setStandalone(true);
    localStorage.setItem(STANDALONE_REOPEN_KEY, "1");
    const { container } = render(
      <MemoryRouter initialEntries={["/members?member=Raven"]}>
        <StandaloneRouteRecorder />
        <Nav />
      </MemoryRouter>,
    );
    expect(container.innerHTML).toBe("");
    expect(localStorage.getItem(STANDALONE_LAST_ROUTE_KEY)).toBe(
      "/members?member=Raven",
    );
    act(() => navigateTo("/schedule"));
    expect(localStorage.getItem(STANDALONE_LAST_ROUTE_KEY)).toBe("/schedule");
  });

  it("records the page showing when the switch is turned on, with no navigation", () => {
    setStandalone(true);
    let setReopen: (v: boolean) => void = () => {};
    function Switch() {
      const [, set] = usePersistedBool(STANDALONE_REOPEN_KEY, false);
      setReopen = set;
      return null;
    }
    render(
      <MemoryRouter initialEntries={["/settings/display/view"]}>
        <StandaloneRouteRecorder />
        <Switch />
      </MemoryRouter>,
    );
    expect(localStorage.getItem(STANDALONE_LAST_ROUTE_KEY)).toBeNull();
    act(() => setReopen(true));
    expect(localStorage.getItem(STANDALONE_LAST_ROUTE_KEY)).toBe(
      "/settings/display/view",
    );
  });

  it("records nothing in the installed app until the person opts in", () => {
    setStandalone(true);
    render(
      <MemoryRouter initialEntries={["/members"]}>
        <StandaloneRouteRecorder />
      </MemoryRouter>,
    );
    expect(localStorage.getItem(STANDALONE_LAST_ROUTE_KEY)).toBeNull();
  });

  it("records nothing in a browser tab", () => {
    setStandalone(false);
    localStorage.setItem(STANDALONE_REOPEN_KEY, "1");
    render(
      <MemoryRouter initialEntries={["/members"]}>
        <StandaloneRouteRecorder />
      </MemoryRouter>,
    );
    expect(localStorage.getItem(STANDALONE_LAST_ROUTE_KEY)).toBeNull();
  });
});
