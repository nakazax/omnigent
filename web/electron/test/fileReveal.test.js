"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { registerFileReveal } = require("../src/fileReveal");

function setup(t, platform = "linux") {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "omni-reveal-"));
  const file = path.join(directory, "a file.txt");
  fs.writeFileSync(file, "test");
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  let handler;
  const shown = [];
  const opened = [];
  const shell = {
    showItemInFolder: (value) => shown.push(value),
    openPath: async (value) => {
      opened.push(value);
      return "";
    },
  };
  registerFileReveal({
    ipcMain: { handle: (_channel, callback) => (handler = callback) },
    shell,
    isPinnedOriginSender: (event) => event.trusted === true,
    localHostId: () => "local",
    platform,
  });
  const reveal = (value, hostId = "local", event = { trusted: true }) =>
    handler(event, hostId, value);
  return { directory, file, shown, opened, shell, reveal };
}

test("selects a file in its folder and opens a folder", async (t) => {
  const { reveal, shown, opened, file, directory } = setup(t);
  assert.equal(await reveal(file), true);
  assert.equal(await reveal(directory), true);
  assert.deepEqual(shown, [file]);
  assert.deepEqual(opened, [directory]);
});

test("selects a macOS package instead of launching it", async (t) => {
  const { reveal, shown, opened, directory } = setup(t, "darwin");
  const app = path.join(directory, "Tool.app");
  fs.mkdirSync(app);
  assert.equal(await reveal(app), true);
  assert.deepEqual(shown, [app]);
  assert.deepEqual(opened, []);
});

test("rejects untrusted senders, other hosts, and bad or missing paths", async (t) => {
  const { reveal, shown, opened, file } = setup(t);
  const results = await Promise.all([
    reveal(file, "local", {}),
    reveal(file, "remote"),
    reveal(file, null),
    reveal("relative.txt"),
    reveal(`${file}\0`),
    reveal(`${file}.missing`),
  ]);
  assert.deepEqual(results, [false, false, false, false, false, false]);
  assert.deepEqual([shown, opened], [[], []]);
});

test("reports native failures", async (t) => {
  const { reveal, shell, file, directory } = setup(t);
  shell.showItemInFolder = () => {
    throw new Error("Unavailable");
  };
  shell.openPath = async () => "No application found";
  assert.equal(await reveal(file), false);
  assert.equal(await reveal(directory), false);
});
