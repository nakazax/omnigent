"use strict";

const fs = require("node:fs");
const path = require("node:path");

/**
 * Reveal this machine's files in the OS file manager, never executing them: a
 * file (or macOS package such as Foo.app) is selected in its folder, and a
 * folder is opened.
 */
function registerFileReveal({
  ipcMain,
  shell,
  isPinnedOriginSender,
  localHostId,
  platform = process.platform,
}) {
  ipcMain.handle("omnigent:reveal-file", async (event, hostId, rawPath) => {
    if (!isPinnedOriginSender(event)) return false;
    if (typeof hostId !== "string" || !hostId || hostId !== localHostId()) return false;
    if (typeof rawPath !== "string" || rawPath.includes("\0") || !path.isAbsolute(rawPath)) {
      return false;
    }
    // The SPA joins paths with "/"; normalize to this platform's separators.
    const filePath = path.normalize(rawPath);
    try {
      const isPackage = platform === "darwin" && path.extname(filePath) !== "";
      if (!fs.statSync(filePath).isDirectory() || isPackage) {
        shell.showItemInFolder(filePath);
        return true;
      }
      // Resolves "" on success, otherwise the platform's error message.
      return (await shell.openPath(filePath)) === "";
    } catch {
      return false;
    }
  });
}

module.exports = { registerFileReveal };
