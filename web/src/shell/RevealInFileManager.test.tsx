import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { FileViewerContext } from "./FileViewerContext";
import { FolderTree } from "./FolderTree";

const HOST = "this-mac";
const revealFile = vi.fn(() => Promise.resolve(true));

const files: WorkspaceFile[] = [
  { path: "src/main.ts", name: "main.ts", type: "file", bytes: 10, modified_at: null },
];

function contextFor(sessionHostId: string) {
  return {
    openFile: vi.fn(),
    registerNavigationGuard: () => () => {},
    openGithubTab: vi.fn(),
    isChangedPath: () => false,
    conversationId: "conv_1",
    workspaceRoot: "/Users/me/project",
    workspaceHome: null,
    sessionHostId,
  };
}

async function renderTree({
  sessionHostId = HOST,
  browseLocation,
}: { sessionHostId?: string; browseLocation?: string } = {}) {
  const context = contextFor(sessionHostId);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <FileViewerContext.Provider value={context}>
        <FolderTree
          files={files}
          isLoading={false}
          isError={false}
          error={null}
          onFileSelect={vi.fn()}
          conversationId="conv_1"
          showHidden={false}
          changedFiles={undefined}
          sort="alpha"
          browseLocation={browseLocation}
        />
      </FileViewerContext.Provider>
    </QueryClientProvider>,
  );
  // Let the desktop shell's host identity resolve.
  await act(() => Promise.resolve());
}

function useUserAgent(value: string) {
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue(value);
}

beforeEach(() => {
  revealFile.mockClear();
  useUserAgent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)");
  Object.assign(window, {
    omnigentDesktop: {
      kind: "electron",
      revealFile,
      getHostIdentity: () => Promise.resolve({ cliInstalled: true, hostId: HOST }),
    },
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  delete (window as { omnigentDesktop?: unknown }).omnigentDesktop;
});

it("reveals a local file selected in its folder", async () => {
  await renderTree();
  fireEvent.contextMenu(screen.getByText("main.ts"));
  fireEvent.click(await screen.findByRole("menuitem", { name: "Show in Finder" }));
  expect(revealFile).toHaveBeenCalledWith(HOST, "/Users/me/project/src/main.ts");
});

it("opens a local folder without toggling it", async () => {
  await renderTree();
  const folder = screen.getByRole("button", { name: "src/" });
  expect(folder).toHaveAttribute("aria-expanded", "true");
  fireEvent.contextMenu(folder);
  fireEvent.click(await screen.findByRole("menuitem", { name: "Open in Finder" }));
  expect(revealFile).toHaveBeenCalledWith(HOST, "/Users/me/project/src");
  expect(folder).toHaveAttribute("aria-expanded", "true");
});

it("resolves rows against the folder being browsed", async () => {
  await renderTree({ browseLocation: "packages/app" });
  fireEvent.contextMenu(screen.getByText("main.ts"));
  fireEvent.click(await screen.findByRole("menuitem", { name: "Show in Finder" }));
  expect(revealFile).toHaveBeenCalledWith(HOST, "/Users/me/project/packages/app/src/main.ts");
});

it.each([
  ["Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Show in File Explorer"],
  ["Mozilla/5.0 (X11; Linux x86_64)", "Show in file manager"],
])("names the platform's file manager (%s)", async (userAgent, label) => {
  useUserAgent(userAgent);
  await renderTree();
  fireEvent.contextMenu(screen.getByText("main.ts"));
  expect(await screen.findByRole("menuitem", { name: label })).toBeInTheDocument();
});

it.each([
  ["a remote session", () => renderTree({ sessionHostId: "other-host" })],
  [
    "a browser tab",
    () => {
      delete (window as { omnigentDesktop?: unknown }).omnigentDesktop;
      return renderTree();
    },
  ],
])("leaves the native context menu alone in %s", async (_case, setup) => {
  await setup();
  // fireEvent returns false only when a handler called preventDefault().
  expect(fireEvent.contextMenu(screen.getByText("main.ts"))).toBe(true);
  expect(screen.queryByRole("menuitem")).toBeNull();
});
