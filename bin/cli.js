#!/usr/bin/env node
"use strict";

const { spawn, execSync } = require("child_process");
const http = require("http");
const https = require("https");
const path = require("path");
const fs = require("fs");
const os = require("os");
const readline = require("readline");

const ROOT = path.join(__dirname, "..");
const DAEMON_DIR = path.join(ROOT, "daemon");
const STATIC_DIR = path.join(ROOT, "static");
const HOOKS_DIR = path.join(ROOT, "hooks");
const DATA_DIR = path.join(os.homedir(), ".vibe-audit");
const VENV_DIR = path.join(DATA_DIR, "venv");
const PORT = process.env.VIBE_AUDIT_PORT || "8000";
const ONBOARDING_FILE = path.join(DATA_DIR, ".onboarding-complete.json");
const LEGACY_DB_PATH = path.join(DAEMON_DIR, "vibe_audit.db");
const LEGACY_SETTINGS_PATH = path.join(DAEMON_DIR, "settings.json");
const COLOR_ENABLED = process.stdout.isTTY && !process.env.NO_COLOR;

// ── Helpers ──────────────────────────────────────────────

function log(msg) {
  console.log(`${style("[vibe-audit]", "1;36")} ${msg}`);
}

function error(msg) {
  console.error(`${style("[vibe-audit]", "1;31")} ${msg}`);
}

function style(text, code) {
  if (!COLOR_ENABLED) return text;
  return `\x1b[${code}m${text}\x1b[0m`;
}

function palette() {
  return {
    title: "1;95",
    accent: "1;92",
    divider: "90",
    subtle: "2;37",
    selected: "1;97",
    pointer: "1;95",
    checked: "1;92",
    unchecked: "90",
    ok: "32",
    warn: "33",
  };
}

function findPython() {
  const candidates = process.platform === "win32"
    ? ["python", "python3", "py -3"]
    : ["python3", "python"];

  for (const cmd of candidates) {
    try {
      const ver = execSync(`${cmd} --version 2>&1`, { encoding: "utf-8" }).trim();
      if (ver.includes("3.")) return cmd.split(" ")[0] === "py" ? "py" : cmd;
    } catch {}
  }
  return null;
}

function getPip() {
  if (process.platform === "win32") {
    return path.join(VENV_DIR, "Scripts", "pip.exe");
  }
  return path.join(VENV_DIR, "bin", "pip");
}

function getVenvPython() {
  if (process.platform === "win32") {
    return path.join(VENV_DIR, "Scripts", "python.exe");
  }
  return path.join(VENV_DIR, "bin", "python");
}

function openBrowser(url) {
  const cmd = process.platform === "darwin" ? "open"
    : process.platform === "win32" ? "start"
    : "xdg-open";
  try { execSync(`${cmd} ${url}`, { stdio: "ignore" }); } catch {}
}

function fileExists(filePath) {
  try {
    return fs.existsSync(filePath);
  } catch {
    return false;
  }
}

function readJson(filePath, fallback = {}) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf-8"));
  } catch {
    return fallback;
  }
}

function writeJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf-8");
}

function toSqliteUrl(filePath) {
  return `sqlite:///${path.resolve(filePath).replace(/\\/g, "/")}`;
}

function migrateLegacyDataFiles() {
  const dbPath = path.join(DATA_DIR, "vibe_audit.db");
  const settingsPath = path.join(DATA_DIR, "settings.json");

  if (!fileExists(dbPath) && fileExists(LEGACY_DB_PATH)) {
    try {
      fs.copyFileSync(LEGACY_DB_PATH, dbPath);
      log(`Migrated legacy DB to ${dbPath}`);
    } catch (e) {
      error(`Failed to migrate legacy DB: ${e.message}`);
    }
  }

  if (!fileExists(settingsPath) && fileExists(LEGACY_SETTINGS_PATH)) {
    try {
      fs.copyFileSync(LEGACY_SETTINGS_PATH, settingsPath);
      log(`Migrated legacy settings to ${settingsPath}`);
    } catch (e) {
      error(`Failed to migrate legacy settings: ${e.message}`);
    }
  }
}

function resolvePythonForHooks() {
  const venvPython = getVenvPython();
  if (fileExists(venvPython)) return `"${venvPython}"`;

  const systemPython = findPython();
  if (!systemPython) return null;
  return systemPython;
}

function runHookInstaller(target = "all") {
  const python = resolvePythonForHooks();
  if (!python) {
    error("Python 3 is required. Install from https://python.org");
    process.exit(1);
  }
  const scopeLabel = target === "all" ? "Claude + Codex" : target;
  log(`Registering hooks (${scopeLabel})...`);
  execSync(
    `${python} "${path.join(HOOKS_DIR, "setup_hooks.py")}" --target ${target}`,
    { stdio: "inherit" }
  );
}

function isCodexHookInstalled() {
  try {
    const codexPath = path.join(os.homedir(), ".codex", "config.toml");
    if (!fileExists(codexPath)) return false;
    const raw = fs.readFileSync(codexPath, "utf-8");
    return (
      raw.includes("# >>> vibe-audit codex notify >>>")
      || raw.includes("# >>> vibe-audit codex hooks >>>")
      || /notify\s*=\s*\[[^\]]*codex_hook\.py[^\]]*\]/i.test(raw)
    );
  } catch {
    return false;
  }
}

function isClaudeHookInstalled() {
  try {
    const claudePath = path.join(os.homedir(), ".claude", "settings.json");
    if (!fileExists(claudePath)) return false;
    const settings = readJson(claudePath, {});
    const hooks = settings.hooks || {};
    return ["UserPromptSubmit", "PostToolUse", "Stop"].every((eventType) =>
      Array.isArray(hooks[eventType]) &&
      hooks[eventType].some((group) =>
        Array.isArray(group?.hooks) &&
        group.hooks.some((hook) => String(hook?.command || "").includes("claude_hook.py"))
      )
    );
  } catch {
    return false;
  }
}

function upsertGeminiHook(projectDir) {
  const geminiSettingsPath = path.join(projectDir, ".gemini", "settings.json");
  const hookCommand = `python "${path.join(HOOKS_DIR, "after_model.py")}"`;
  const hookEntry = {
    name: "vibe-audit-capture",
    type: "command",
    command: hookCommand,
    timeout: 60000,
  };

  const settings = readJson(geminiSettingsPath, {});
  settings.hooksConfig = {
    enabled: true,
    notifications: true,
    disabled: [],
    ...(settings.hooksConfig || {}),
    enabled: true,
  };

  settings.hooks = settings.hooks || {};
  const groups = Array.isArray(settings.hooks.AfterModel) ? settings.hooks.AfterModel : [];
  let patched = false;

  for (const group of groups) {
    if (!Array.isArray(group?.hooks)) continue;
    for (const hook of group.hooks) {
      if (!hook || typeof hook !== "object") continue;
      const cmd = String(hook.command || "");
      if (hook.name === "vibe-audit-capture" || cmd.includes("after_model.py")) {
        hook.name = "vibe-audit-capture";
        hook.type = "command";
        hook.command = hookCommand;
        hook.timeout = hook.timeout || 60000;
        patched = true;
      }
    }
  }

  if (!patched) {
    groups.push({
      matcher: "",
      hooks: [hookEntry],
    });
  }

  settings.hooks.AfterModel = groups;
  writeJson(geminiSettingsPath, settings);
  return geminiSettingsPath;
}

function isGeminiHookInstalled(projectDir) {
  try {
    const settingsPath = path.join(projectDir, ".gemini", "settings.json");
    if (!fileExists(settingsPath)) return false;
    const settings = readJson(settingsPath, {});
    const groups = settings?.hooks?.AfterModel;
    if (!Array.isArray(groups)) return false;
    return groups.some((group) =>
      Array.isArray(group?.hooks) &&
      group.hooks.some((hook) =>
        hook &&
        (hook.name === "vibe-audit-capture" || String(hook.command || "").includes("after_model.py"))
      )
    );
  } catch {
    return false;
  }
}

function isInteractiveTty() {
  return Boolean(process.stdin.isTTY && process.stdout.isTTY);
}

function askQuestion(rl, prompt) {
  return new Promise((resolve) => {
    rl.question(prompt, (answer) => resolve(answer.trim()));
  });
}

async function askYesNo(rl, prompt, defaultValue = true) {
  const hint = defaultValue ? "[Y/n]" : "[y/N]";
  const answer = (await askQuestion(rl, `${prompt} ${hint} `)).toLowerCase();
  if (!answer) return defaultValue;
  if (["y", "yes", "ㅛ", "ㅇ", "예"].includes(answer)) return true;
  if (["n", "no", "ㅜ", "ㄴ", "아니오"].includes(answer)) return false;
  return defaultValue;
}

function printStageHeader(stage, title) {
  const c = palette();
  const divider = style("==================================================", c.divider);
  console.log(`\n${divider}`);
  console.log(`${style("[Onboarding]", c.title)} ${style(stage, c.accent)}`);
  console.log(style(`${title}`, c.selected));
  console.log(divider);
}

function printStateLine(name, installed) {
  const c = palette();
  const state = installed
    ? style("installed", c.ok)
    : style("not installed", c.warn);
  console.log(`${style("-", c.divider)} ${style(name, c.selected)}: ${state}`);
}

function getNodeMajorVersion() {
  const raw = String(process.version || "").replace(/^v/i, "");
  const major = Number(raw.split(".")[0] || 0);
  return Number.isFinite(major) ? major : 0;
}

function safeExec(command) {
  try {
    return execSync(command, { encoding: "utf-8" }).trim();
  } catch {
    return "";
  }
}

function canWriteDirectory(dirPath) {
  try {
    fs.mkdirSync(dirPath, { recursive: true });
    const probe = path.join(dirPath, `.doctor-write-${Date.now()}-${Math.random().toString(36).slice(2)}.tmp`);
    fs.writeFileSync(probe, "ok", "utf-8");
    fs.unlinkSync(probe);
    return true;
  } catch {
    return false;
  }
}

function formatStateTag(state) {
  const c = palette();
  if (state === "ok") return style("OK", c.ok);
  if (state === "warn") return style("WARN", c.warn);
  return style("FAIL", "31");
}

function printDoctorLine(state, label, detail = "") {
  const prefix = state === "ok" ? style("✓", palette().ok)
    : state === "warn" ? style("!", palette().warn)
      : style("x", "31");
  const right = detail ? ` ${style(detail, palette().subtle)}` : "";
  console.log(`${prefix} ${style(`[${formatStateTag(state)}]`, palette().selected)} ${label}${right}`);
}

function healthCheck(baseUrl, timeoutMs = 2000) {
  return new Promise((resolve) => {
    let url;
    try {
      url = new URL("/health", baseUrl);
    } catch {
      resolve({ ok: false, detail: "invalid URL" });
      return;
    }
    const lib = url.protocol === "https:" ? https : http;
    const req = lib.request(url, { method: "GET", timeout: timeoutMs }, (res) => {
      const chunks = [];
      res.on("data", (chunk) => chunks.push(chunk));
      res.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf-8");
        if (res.statusCode !== 200) {
          resolve({ ok: false, detail: `HTTP ${res.statusCode}` });
          return;
        }
        try {
          const json = JSON.parse(text);
          resolve({
            ok: json?.status === "ok",
            detail: `status=${json?.status || "unknown"} version=${json?.version || "unknown"}`,
          });
        } catch {
          resolve({ ok: true, detail: "HTTP 200 (non-JSON response)" });
        }
      });
    });
    req.on("timeout", () => {
      req.destroy(new Error("timeout"));
    });
    req.on("error", (err) => {
      resolve({ ok: false, detail: err.message || "connection error" });
    });
    req.end();
  });
}

function quoteShellPath(value) {
  if (!value) return value;
  if (/\s/.test(value)) return `"${value}"`;
  return value;
}

async function runDoctor(args = []) {
  const projectIdx = args.indexOf("--project");
  const projectDir = projectIdx >= 0 && args[projectIdx + 1]
    ? path.resolve(args[projectIdx + 1])
    : process.cwd();
  const urlIdx = args.indexOf("--url");
  const baseUrl = urlIdx >= 0 && args[urlIdx + 1]
    ? args[urlIdx + 1]
    : (process.env.VIBE_AUDIT_URL || `http://localhost:${PORT}`);

  const dbPath = path.join(DATA_DIR, "vibe_audit.db");
  const settingsPath = path.join(DATA_DIR, "settings.json");
  const venvPython = getVenvPython();
  const venvExists = fileExists(venvPython);
  const onboardingDone = hasOnboardingMarker();

  let hasHardFail = false;
  const suggestions = [];

  console.log("");
  console.log(style("Vibe Audit Doctor", "1;95"));
  console.log(style("Environment and integration diagnostics", palette().subtle));
  console.log(style("--------------------------------------------------", palette().divider));
  console.log(`${style("Project", palette().selected)}: ${projectDir}`);
  console.log(`${style("Data dir", palette().selected)}: ${DATA_DIR}`);
  console.log(`${style("Health URL", palette().selected)}: ${baseUrl}`);
  console.log("");

  // Runtime
  console.log(style("Runtime", "1;92"));
  const nodeMajor = getNodeMajorVersion();
  if (nodeMajor >= 18) {
    printDoctorLine("ok", `Node.js ${process.version}`, "supported (>=18)");
  } else {
    printDoctorLine("fail", `Node.js ${process.version}`, "unsupported (requires >=18)");
    hasHardFail = true;
    suggestions.push("Install Node.js 18+ and rerun `npx vibe-audit doctor`.");
  }

  const pythonCmd = findPython();
  if (pythonCmd) {
    const pyVersion = safeExec(`${pythonCmd} --version 2>&1`) || "Python found";
    printDoctorLine("ok", `Python command: ${pythonCmd}`, pyVersion);
  } else if (venvExists) {
    printDoctorLine("warn", "Python command not on PATH", "venv Python exists and will be used");
    suggestions.push("Optional: add Python to PATH for easier manual hook tooling.");
  } else {
    printDoctorLine("fail", "Python 3 not found", "required");
    hasHardFail = true;
    suggestions.push("Install Python 3 and ensure `python` or `python3` is on PATH.");
  }

  if (venvExists) {
    printDoctorLine("ok", "Python venv", quoteShellPath(venvPython));
  } else {
    printDoctorLine("warn", "Python venv", "not created yet (will be created on start)");
    suggestions.push("Run `npx vibe-audit` once to create runtime venv.");
  }

  // Storage
  console.log("");
  console.log(style("Storage", "1;92"));
  if (canWriteDirectory(DATA_DIR)) {
    printDoctorLine("ok", "Data directory writable", DATA_DIR);
  } else {
    printDoctorLine("warn", "Data directory not writable", DATA_DIR);
    suggestions.push("Grant write permission to `%USERPROFILE%/.vibe-audit` (or create the folder manually).");
  }
  printDoctorLine(fileExists(dbPath) ? "ok" : "warn", "Database file", dbPath);
  if (!fileExists(dbPath)) {
    suggestions.push("Database will be created automatically when server starts.");
  }
  printDoctorLine(fileExists(settingsPath) ? "ok" : "warn", "Settings file", settingsPath);
  if (!fileExists(settingsPath)) {
    suggestions.push("Open Settings in UI once to create settings file.");
  }
  printDoctorLine(onboardingDone ? "ok" : "warn", "Onboarding marker", ONBOARDING_FILE);
  if (!onboardingDone) {
    suggestions.push("Run `npx vibe-audit init` to complete onboarding wizard.");
  }

  // Hooks
  console.log("");
  console.log(style("Hooks", "1;92"));
  const claudeInstalled = isClaudeHookInstalled();
  const codexInstalled = isCodexHookInstalled();
  const geminiInstalled = isGeminiHookInstalled(projectDir);
  printDoctorLine(claudeInstalled ? "ok" : "warn", "Claude hook", claudeInstalled ? "installed" : "not installed");
  printDoctorLine(codexInstalled ? "ok" : "warn", "Codex hook", codexInstalled ? "installed" : "not installed");
  printDoctorLine(geminiInstalled ? "ok" : "warn", "Gemini hook (project-local)", geminiInstalled ? "installed" : "not installed");
  if (!claudeInstalled || !codexInstalled) {
    suggestions.push("Install global hooks: `npx vibe-audit setup-hooks`.");
  }
  if (!geminiInstalled) {
    suggestions.push(`Install Gemini hook for this project: npx vibe-audit setup-gemini --project ${quoteShellPath(projectDir)}`);
  }

  // Server connectivity
  console.log("");
  console.log(style("Server", "1;92"));
  const health = await healthCheck(baseUrl);
  if (health.ok) {
    printDoctorLine("ok", "Daemon health", health.detail);
  } else {
    printDoctorLine("warn", "Daemon health", health.detail || "not reachable");
    suggestions.push("Start daemon with `npx vibe-audit` and retry doctor.");
  }

  console.log("");
  console.log(style("Summary", "1;92"));
  if (hasHardFail) {
    printDoctorLine("fail", "Doctor result", "critical issues found");
  } else {
    printDoctorLine("ok", "Doctor result", "no critical issues");
  }
  if (suggestions.length) {
    console.log("");
    console.log(style("Recommended actions", "1;95"));
    const unique = [...new Set(suggestions)];
    unique.forEach((tip, idx) => {
      console.log(`${style(String(idx + 1) + ".", palette().accent)} ${tip}`);
    });
  }
  console.log("");

  return hasHardFail ? 1 : 0;
}

function supportsCheckboxPrompt() {
  return Boolean(process.stdin.isTTY && typeof process.stdin.setRawMode === "function");
}

async function runCheckboxStage(options) {
  const {
    stage,
    title,
    subtitleLines = [],
    items = [],
    footerLines = [],
  } = options;

  const state = items.map((item) => ({
    id: item.id,
    label: item.label,
    checked: Boolean(item.checked),
    detail: item.detail || "",
  }));

  if (!supportsCheckboxPrompt()) {
    return state;
  }

  return new Promise((resolve) => {
    let cursor = 0;
    const stdin = process.stdin;
    const c = palette();

    function render() {
      const lines = [];
      lines.push("");
      lines.push(style("==================================================", c.divider));
      lines.push(`${style("[Onboarding]", c.title)} ${style(stage, c.accent)}`);
      lines.push(style(`${title}`, c.selected));
      lines.push(style("==================================================", c.divider));
      lines.push("");
      for (const line of subtitleLines) {
        lines.push(style(line, c.subtle));
      }
      if (subtitleLines.length) lines.push("");
      lines.push(style("Controls: ↑/↓ move, Space toggle, Enter next", c.subtle));
      lines.push("");
      for (let i = 0; i < state.length; i++) {
        const item = state[i];
        const marker = i === cursor ? style(">", c.pointer) : " ";
        const box = item.checked ? style("[x]", c.checked) : style("[ ]", c.unchecked);
        const label = i === cursor ? style(item.label, c.selected) : item.label;
        lines.push(`${marker} ${box} ${label}`);
        if (item.detail) {
          lines.push(`    ${style(item.detail, c.subtle)}`);
        }
      }
      if (footerLines.length) {
        lines.push("");
        for (const line of footerLines) {
          lines.push(style(line, c.subtle));
        }
      }
      process.stdout.write("\x1b[2J\x1b[H");
      process.stdout.write(`${lines.join("\n")}\n`);
    }

    function cleanup() {
      stdin.removeListener("keypress", onKeypress);
      try { stdin.setRawMode(false); } catch {}
      process.stdout.write("\n");
    }

    function onKeypress(str, key = {}) {
      if (key.ctrl && key.name === "c") {
        cleanup();
        process.exit(130);
      }

      if (key.name === "up" || key.name === "k") {
        cursor = (cursor - 1 + state.length) % state.length;
        render();
        return;
      }
      if (key.name === "down" || key.name === "j") {
        cursor = (cursor + 1) % state.length;
        render();
        return;
      }
      if (key.name === "space" || str === " ") {
        state[cursor].checked = !state[cursor].checked;
        render();
        return;
      }
      if (key.name === "return" || key.name === "enter") {
        cleanup();
        resolve(state);
      }
    }

    readline.emitKeypressEvents(stdin);
    stdin.setRawMode(true);
    stdin.resume();
    stdin.on("keypress", onKeypress);
    render();
  });
}

function hasOnboardingMarker() {
  return fileExists(ONBOARDING_FILE);
}

function writeOnboardingMarker(status) {
  writeJson(ONBOARDING_FILE, {
    completed_at: new Date().toISOString(),
    ...status,
  });
}

async function runOnboardingIfNeeded(force = false) {
  if ((!force && hasOnboardingMarker()) || !isInteractiveTty()) return;

  const projectDir = process.cwd();
  let enableClaude = false;
  let enableCodex = false;
  let enableGemini = false;
  const claudeReady = isClaudeHookInstalled();
  const codexReady = isCodexHookInstalled();
  const geminiReady = isGeminiHookInstalled(projectDir);

  printStageHeader("Onboarding Stage", "Welcome to Vibe Audit setup wizard");
  console.log(style("Goal: configure hooks in 2 stages with checkbox selections.", palette().subtle));
  console.log(style("No numeric input required.\n", palette().subtle));

  if (supportsCheckboxPrompt()) {
    const stage1 = await runCheckboxStage({
      stage: "Hook Stage 1",
      title: "Global hooks (one-time per machine)",
      subtitleLines: [
        `Current status: Claude=${claudeReady ? "installed" : "not installed"}, Codex=${codexReady ? "installed" : "not installed"}`,
      ],
      items: [
        {
          id: "claude",
          label: "Install Claude hook",
          checked: !claudeReady,
          detail: "Applies globally via ~/.claude/settings.json",
        },
        {
          id: "codex",
          label: "Install Codex hook",
          checked: !codexReady,
          detail: "Applies globally via ~/.codex/config.toml",
        },
      ],
      footerLines: ["Press Enter to continue to Hook Stage 2."],
    });
    enableClaude = stage1.find((x) => x.id === "claude")?.checked || false;
    enableCodex = stage1.find((x) => x.id === "codex")?.checked || false;

    const stage2 = await runCheckboxStage({
      stage: "Hook Stage 2",
      title: "Gemini hook (project-local)",
      subtitleLines: [
        `Current status: ${geminiReady ? "installed in this project" : "not installed in this project"}`,
        `Target file: ${path.join(projectDir, ".gemini", "settings.json")}`,
        "Use this stage to enable or skip for current project.",
      ],
      items: [
        {
          id: "gemini",
          label: "Install Gemini hook for current project",
          checked: !geminiReady,
          detail: "Project-local setting; not global.",
        },
      ],
      footerLines: [
        "For another project later:",
        "- npx vibe-audit setup-gemini --project <path>",
        "- node bin/cli.js setup-gemini --project <path>",
      ],
    });
    enableGemini = stage2.find((x) => x.id === "gemini")?.checked || false;
  } else {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    try {
      printStageHeader("Hook Stage 1", "Global hooks (fallback mode)");
      printStateLine("Claude", claudeReady);
      printStateLine("Codex", codexReady);
      enableClaude = await askYesNo(rl, "Install Claude hook?", !claudeReady);
      enableCodex = await askYesNo(rl, "Install Codex hook?", !codexReady);

      printStageHeader("Hook Stage 2", "Gemini hook (project-local, fallback mode)");
      printStateLine("Gemini (current project)", geminiReady);
      console.log(`Target file: ${path.join(projectDir, ".gemini", "settings.json")}`);
      console.log("For another project: npx vibe-audit setup-gemini --project <path>\n");
      enableGemini = await askYesNo(
        rl,
        "Install Gemini hook for current project?",
        !geminiReady
      );
    } finally {
      rl.close();
    }
  }

  const result = {
    onboarding: "done",
    claude: enableClaude ? "selected" : "skipped",
    codex: enableCodex ? "selected" : "skipped",
    gemini: enableGemini ? "selected" : "skipped",
  };

  if (enableClaude) {
    runHookInstaller("claude");
    result.claude = "applied";
  }

  if (enableCodex) {
    runHookInstaller("codex");
    result.codex = "applied";
  }

  if (enableGemini) {
    const settingsPath = upsertGeminiHook(projectDir);
    log(`Gemini project hook installed: ${settingsPath}`);
    log("If hooks are disabled in Gemini CLI, run '/hooks enable vibe-audit-capture'.");
    log("For another project, run: npx vibe-audit setup-gemini --project <path>");
    result.gemini = "applied";
  }

  if (!enableClaude && !enableCodex && !enableGemini) {
    log("No setup selected. You can run onboarding again anytime.");
  }

  writeOnboardingMarker(result);
  log("Onboarding completed. Re-run with `npx vibe-audit init` (or `node bin/cli.js init`).");
}

// ── Main ─────────────────────────────────────────────────

async function main() {
  const args = process.argv.slice(2);

  if (args.includes("--help") || args.includes("-h")) {
    console.log(`
  vibe-audit - Agent Session Auditor

  Usage:
    node bin/cli.js               Start the server (local repository)
    node bin/cli.js doctor        Run environment diagnostics
    node bin/cli.js setup-hooks   Register Claude/Codex hooks
    node bin/cli.js setup-gemini  Register Gemini hook for current project
    node bin/cli.js init          Run first-run setup wizard
    node bin/cli.js --port 9000   Use custom port
    npm exec vibe-audit -- --help Run installed/local package binary

  Options:
    --port <port>   Server port (default: 8000)
    --project <dir> Project directory (for setup-gemini)
    --url <url>     Daemon URL override (for doctor)
    --no-open       Don't open browser automatically
    --skip-onboarding  Skip first-run setup wizard
    -h, --help      Show this help
`);
    return;
  }

  // ── Setup hooks command (Claude/Codex) ──
  if (args[0] === "setup-hooks") {
    runHookInstaller();
    return;
  }

  // ── Setup Gemini hook command (project-local) ──
  if (args[0] === "setup-gemini") {
    const projectIdx = args.indexOf("--project");
    const projectDir = projectIdx >= 0 && args[projectIdx + 1]
      ? path.resolve(args[projectIdx + 1])
      : process.cwd();
    const settingsPath = upsertGeminiHook(projectDir);
    log(`Gemini project hook installed: ${settingsPath}`);
    log("If hooks are disabled in Gemini CLI, run '/hooks enable vibe-audit-capture'.");
    return;
  }

  // ── Onboarding wizard command ──
  if (args[0] === "init") {
    if (!isInteractiveTty()) {
      error("Interactive terminal required for init wizard.");
      process.exit(1);
    }
    fs.mkdirSync(DATA_DIR, { recursive: true });
    await runOnboardingIfNeeded(true);
    return;
  }

  // ── Diagnostics command ──
  if (args[0] === "doctor") {
    const code = await runDoctor(args.slice(1));
    process.exit(code);
    return;
  }

  // ── Parse flags ──
  const portIdx = args.indexOf("--port");
  const port = portIdx >= 0 && args[portIdx + 1] ? args[portIdx + 1] : PORT;
  const noOpen = args.includes("--no-open");
  const skipOnboarding = args.includes("--skip-onboarding");

  // ── Check Python ──
  const python = findPython();
  if (!python) {
    error("Python 3 is required but not found.");
    error("Install from https://python.org and try again.");
    process.exit(1);
  }
  log(`Found ${execSync(`${python} --version`, { encoding: "utf-8" }).trim()}`);

  // ── Create venv + install deps ──
  fs.mkdirSync(DATA_DIR, { recursive: true });
  migrateLegacyDataFiles();

  const pip = getPip();
  const venvPython = getVenvPython();

  if (!fs.existsSync(venvPython)) {
    log("Creating Python environment (first run, may take a minute)...");
    const venvCmd = python === "py"
      ? `py -3 -m venv "${VENV_DIR}"`
      : `${python} -m venv "${VENV_DIR}"`;
    execSync(venvCmd, { stdio: "inherit" });
  }

  // Install/update requirements
  const reqFile = path.join(DAEMON_DIR, "requirements.txt");
  const stampFile = path.join(DATA_DIR, ".deps-stamp");
  const reqMtime = fs.statSync(reqFile).mtimeMs;
  const needsInstall = !fs.existsSync(stampFile) ||
    parseFloat(fs.readFileSync(stampFile, "utf-8")) < reqMtime;

  if (needsInstall) {
    log("Installing Python dependencies...");
    execSync(`"${pip}" install -r "${reqFile}" --quiet`, { stdio: "inherit" });
    fs.writeFileSync(stampFile, String(reqMtime));
    log("Dependencies installed.");
  }

  if (!skipOnboarding) {
    await runOnboardingIfNeeded(false);
  }

  // ── Start server ──
  log(`Starting server on port ${port}...`);

  const dbPath = path.join(DATA_DIR, "vibe_audit.db");
  const settingsPath = path.join(DATA_DIR, "settings.json");

  const env = {
    ...process.env,
    VIBE_AUDIT_STATIC: fs.existsSync(STATIC_DIR) ? STATIC_DIR : "",
    VIBE_AUDIT_HOOKS: HOOKS_DIR,
    VIBE_AUDIT_DATA_DIR: DATA_DIR,
    VIBE_AUDIT_SETTINGS: settingsPath,
    DATABASE_URL: process.env.DATABASE_URL || toSqliteUrl(dbPath),
  };

  const server = spawn(
    `"${venvPython}"`,
    ["-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", port],
    {
      cwd: DAEMON_DIR,
      env,
      stdio: "inherit",
      shell: true,
    }
  );

  server.on("error", (err) => {
    error(`Failed to start server: ${err.message}`);
    process.exit(1);
  });

  // Open browser after server has time to start
  if (!noOpen) {
    setTimeout(() => {
      const url = `http://localhost:${port}`;
      log(`Opening ${url}`);
      openBrowser(url);
    }, 2500);
  }

  // Cleanup on exit
  function cleanup() {
    server.kill();
    process.exit();
  }
  process.on("SIGINT", cleanup);
  process.on("SIGTERM", cleanup);

  server.on("exit", (code) => {
    if (code !== null && code !== 0) {
      error(`Server exited with code ${code}`);
    }
    process.exit(code || 0);
  });
}

main().catch((err) => {
  error(err?.message || String(err));
  process.exit(1);
});
