import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";
import { CopyIcon, FolderOpenIcon } from "lucide-react";
import { toast } from "sonner";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { copyText } from "@/lib/clipboard";
import {
  getHostIdentity,
  isMacElectronShell,
  revealFile,
  supportsFileReveal,
} from "@/lib/nativeBridge";

const FileMenuContext = createContext<{ root: string | null; localHostId: string | null }>({
  root: null,
  localHostId: null,
});

export function FileMenuProvider({
  root,
  hostId,
  children,
}: {
  root: string | null;
  hostId: string | null;
  children: ReactNode;
}) {
  const [machineHostId, setMachineHostId] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    if (supportsFileReveal()) {
      void getHostIdentity().then((identity) => {
        if (active) setMachineHostId(identity?.hostId ?? null);
      });
    }
    return () => {
      active = false;
    };
  }, []);
  const value = useMemo(
    () => ({ root, localHostId: hostId && hostId === machineHostId ? hostId : null }),
    [root, hostId, machineHostId],
  );
  return <FileMenuContext.Provider value={value}>{children}</FileMenuContext.Provider>;
}

/** A row's paths are relative to the currently browsed directory. */
export function FileContextMenu({
  path,
  deleted = false,
  children,
}: {
  path: string;
  deleted?: boolean;
  children: ReactElement;
}) {
  const { root, localHostId } = useContext(FileMenuContext);
  const absolutePath = resolveFileMenuPath(root, path);
  async function copy(value: string) {
    try {
      await copyText(value);
      toast.success("Path copied");
    } catch {
      toast.error("Failed to copy path");
    }
  }
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>{children}</ContextMenuTrigger>
      <ContextMenuContent>
        {localHostId && absolutePath && !deleted && (
          <ContextMenuItem
            onSelect={() => {
              void revealFile(localHostId, absolutePath).then((ok) => {
                if (!ok) toast.error("Could not show this item in the file manager");
              });
            }}
          >
            <FolderOpenIcon />
            {isMacElectronShell()
              ? "Show in Finder"
              : navigator.userAgent.includes("Windows")
                ? "Show in File Explorer"
                : "Show in File Manager"}
          </ContextMenuItem>
        )}
        <ContextMenuItem
          disabled={!absolutePath}
          onSelect={() => {
            if (absolutePath) void copy(absolutePath);
          }}
        >
          <CopyIcon />
          Copy Path
        </ContextMenuItem>
        <ContextMenuItem
          onSelect={() => {
            void copy(path);
          }}
        >
          <CopyIcon />
          Copy Relative Path
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  );
}

/** Directory API rows use slash-separated paths, including on Windows hosts. */
export function resolveFileMenuPath(root: string | null, relativePath: string): string | null {
  if (!root) return null;
  const windows = /^[A-Za-z]:[/\\]/.test(root) || root.startsWith("\\\\");
  const separator = windows ? "\\" : "/";
  const normalizedRoot = windows ? root.replace(/\//g, "\\") : root;
  const base = normalizedRoot.replace(/[/\\]+$/, "");
  return `${base}${separator}${windows ? relativePath.replace(/\//g, "\\") : relativePath}`;
}
