#!/usr/bin/env node

// Thin Node launcher for the hermes-infoflow plugin installer.
//
// hermes-infoflow is a Python plugin: the deploy orchestrator
// (hermes_infoflow/deploy.py) MUST run under a host Python interpreter so it
// can align ~/.hermes/hermes-agent and verify the gateway import. This script
// therefore mirrors the extract/normalize flow of the Python
// `hermes-infoflow-tools` CLI (tools/hermes-infoflow-tools/.../cli.py):
//
//   1. detect a Python 3.11+ interpreter
//   2. `python -m pip download` the hermes-infoflow sdist from PyPI
//   3. untar it and locate deploy.py
//   4. run deploy.py under the detected Python
//
// Requirements on the host: Python 3.11+, pip, and `tar`.

import { mkdtempSync, rmSync, readdirSync, statSync, existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { homedir, tmpdir } from "node:os";
import { join, resolve, basename, isAbsolute } from "node:path";

const DEFAULT_PACKAGE = "hermes-infoflow";
const DEFAULT_INDEX_URL = "https://pypi.org/simple";
const DEFAULT_CHANNEL_ID = "infoflow";
const MIN_PYTHON = [3, 11];

function printHelp() {
  console.log(`hermes-infoflow-tools — install/update the hermes-infoflow plugin.

Requires Python ${MIN_PYTHON.join(".")}+ and pip on the host (this is a thin
launcher around the plugin's Python deploy orchestrator).

Usage:
  npx -y @chbo297/hermes-infoflow-tools update [options]
  npx -y @chbo297/hermes-infoflow-tools normalize [options]

Commands:
  update                       Download the plugin sdist and deploy it into
                               ~/.hermes/plugins/<channel-id>/
  normalize                    Normalize an existing plugin directory into the
                               canonical ~/.hermes/plugins/infoflow layout

update options:
  --version <version>          PyPI version specifier (default: latest)
  --index-url <url>            PyPI index URL (default: ${DEFAULT_INDEX_URL})
  --package-name <name|path>   Plugin package on PyPI, or a local checkout path
                               (default: ${DEFAULT_PACKAGE})
  --channel-id <id>            Plugin id under ~/.hermes/plugins/ (only
                               '${DEFAULT_CHANNEL_ID}' is supported)
  --mode <extract|pip>         extract (default); pip is a deprecated alias
  --port <1-65535>             Webhook port, written to ~/.hermes/.env
  --python <path>              Explicit Python interpreter to use
  --dry-run                    Print commands without executing them
  -h, --help                   Show help

normalize options:
  --source <dir>               Source plugin/check-out dir (default:
                               $HERMES_HOME/plugins/<channel-id>)
  --channel-id <id>            Only '${DEFAULT_CHANNEL_ID}' is supported
  --port <1-65535>             Webhook port, written to ~/.hermes/.env
  --python <path>              Explicit Python interpreter to use
  --dry-run                    Print commands without executing them
`);
}

function fail(message) {
  console.error(`error: ${message}`);
  process.exit(1);
}

function parsePort(value) {
  if (!/^\d+$/.test(value)) fail(`--port must be an integer 1-65535 (got: ${value})`);
  const port = Number(value);
  if (port < 1 || port > 65535) fail(`--port must be an integer 1-65535 (got: ${value})`);
  return String(port);
}

function looksLikeLocalPath(value) {
  if (!value) return false;
  if (value.startsWith("/") || value.startsWith("./") || value.startsWith("../") || value.startsWith("~")) {
    return true;
  }
  return isAbsolute(value);
}

function expandUser(p) {
  if (p === "~") return homedir();
  if (p.startsWith("~/")) return join(homedir(), p.slice(2));
  return p;
}

function pythonVersion(candidate) {
  const res = spawnSync(candidate, ["-c", "import sys; print('%d %d' % sys.version_info[:2])"], {
    encoding: "utf8",
  });
  if (res.status !== 0 || !res.stdout) return null;
  const parts = res.stdout.trim().split(/\s+/).map(Number);
  if (parts.length < 2 || Number.isNaN(parts[0])) return null;
  return parts;
}

function detectPython(explicit) {
  const candidates = explicit ? [explicit] : ["python3", "python"];
  for (const candidate of candidates) {
    const ver = pythonVersion(candidate);
    if (!ver) continue;
    const [major, minor] = ver;
    if (major > MIN_PYTHON[0] || (major === MIN_PYTHON[0] && minor >= MIN_PYTHON[1])) {
      return { exe: candidate, version: ver };
    }
    if (explicit) {
      fail(
        `Python at '${explicit}' is ${major}.${minor}; hermes-infoflow requires ` +
          `Python ${MIN_PYTHON.join(".")}+`,
      );
    }
  }
  fail(
    `Could not find Python ${MIN_PYTHON.join(".")}+ on PATH. Install Python ` +
      `${MIN_PYTHON.join(".")}+ (and pip), or pass --python <path>.`,
  );
}

function runOrFail(command, args, cwd, dryRun) {
  const cwdLabel = cwd ? `(${cwd}) ` : "";
  console.log(`$ ${cwdLabel}${[command, ...args].join(" ")}`);
  if (dryRun) return;
  const result = spawnSync(command, args, { cwd, stdio: "inherit" });
  if (result.error) fail(`failed to run ${command}: ${result.error.message}`);
  if (result.status !== 0) process.exit(result.status ?? 1);
}

function normalizedStem(name) {
  const stem = basename(name) || DEFAULT_PACKAGE;
  return stem.replace(/[-_.]+/g, "_").toLowerCase();
}

function findSdist(tmpDir, packageName) {
  const normalized = normalizedStem(packageName);
  const entries = readdirSync(tmpDir).filter((f) => f.endsWith(".tar.gz"));
  const preferred = entries.filter((f) => f.toLowerCase().startsWith(`${normalized}-`)).sort();
  if (preferred.length) return join(tmpDir, preferred[preferred.length - 1]);
  const any = entries.sort();
  if (any.length) return join(tmpDir, any[any.length - 1]);
  fail(`failed to locate sdist tarball under ${tmpDir}`);
}

function findExtractedDir(tmpDir, packageName) {
  const normalized = normalizedStem(packageName);
  const dirs = readdirSync(tmpDir).filter((f) => {
    const full = join(tmpDir, f);
    return statSync(full).isDirectory();
  });
  const preferred = dirs.filter((d) => d.toLowerCase().startsWith(`${normalized}-`)).sort();
  if (preferred.length) return join(tmpDir, preferred[preferred.length - 1]);
  const hyphenated = dirs.filter((d) => d.includes("-")).sort();
  if (hyphenated.length) return join(tmpDir, hyphenated[hyphenated.length - 1]);
  fail(`failed to locate extracted package directory under ${tmpDir}`);
}

function findDeployScript(source) {
  const candidates = [join(source, "hermes_infoflow", "deploy.py"), join(source, "deploy.py")];
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

function hermesHome() {
  return process.env.HERMES_HOME ? expandUser(process.env.HERMES_HOME) : join(homedir(), ".hermes");
}

function parseCommon(args, opts) {
  for (let i = 0; i < args.length; i += 1) {
    const val = args[i];
    if (val === "--channel-id") {
      const id = args[++i];
      if (id !== DEFAULT_CHANNEL_ID) {
        fail(`hermes-infoflow only supports plugin id '${DEFAULT_CHANNEL_ID}'; got '${id}'`);
      }
      opts.channelId = id;
    } else if (val === "--port") {
      opts.port = parsePort(args[++i] ?? "");
    } else if (val === "--python") {
      opts.python = args[++i];
    } else if (val === "--dry-run") {
      opts.dryRun = true;
    } else {
      return val;
    }
  }
  return null;
}

function doUpdate(rawArgs) {
  const opts = {
    version: "latest",
    indexUrl: DEFAULT_INDEX_URL,
    packageName: DEFAULT_PACKAGE,
    channelId: DEFAULT_CHANNEL_ID,
    mode: "extract",
    port: null,
    python: null,
    dryRun: false,
  };

  for (let i = 0; i < rawArgs.length; i += 1) {
    const val = rawArgs[i];
    if (val === "--version") opts.version = rawArgs[++i] ?? opts.version;
    else if (val === "--index-url") opts.indexUrl = rawArgs[++i] ?? opts.indexUrl;
    else if (val === "--package-name") opts.packageName = rawArgs[++i] ?? opts.packageName;
    else if (val === "--mode") {
      const mode = rawArgs[++i];
      if (mode !== "extract" && mode !== "pip") fail(`--mode must be 'extract' or 'pip' (got: ${mode})`);
      opts.mode = mode;
    } else {
      const leftover = parseCommon([val, ...rawArgs.slice(i + 1)], opts);
      if (leftover) fail(`Unknown option: ${leftover}`);
      break;
    }
  }

  if (opts.mode === "pip") {
    console.log(
      "[pip mode] deprecated: deploying directory-style to " +
        `${join(hermesHome(), "plugins", opts.channelId)} so it can safely overwrite ` +
        "deploy.sh/extract installs.",
    );
  }

  const python = detectPython(opts.python);
  const home = hermesHome();
  const configFile = join(home, "config.yaml");

  let localSource = null;
  if (looksLikeLocalPath(opts.packageName)) {
    const candidate = resolve(expandUser(opts.packageName));
    if (existsSync(candidate) && statSync(candidate).isDirectory()) localSource = candidate;
  }

  const tmpRoot = mkdtempSync(join(tmpdir(), "hermes-infoflow-tools-"));
  try {
    let extractedDir;
    if (localSource) {
      console.log(`$ use local source ${localSource}`);
      if (opts.version !== "" && opts.version !== "latest") {
        console.log(`  note: --version '${opts.version}' is ignored in local-source mode`);
      }
      extractedDir = localSource;
    } else {
      const spec =
        opts.version === "" || opts.version === "latest"
          ? opts.packageName
          : `${opts.packageName}==${opts.version}`;
      runOrFail(
        python.exe,
        [
          "-m",
          "pip",
          "download",
          "--no-deps",
          "--no-binary=:all:",
          "-d",
          tmpRoot,
          "-i",
          opts.indexUrl,
          spec,
        ],
        undefined,
        opts.dryRun,
      );

      if (opts.dryRun) {
        console.log(`$ tar -xzf <sdist tarball under ${tmpRoot}>`);
        extractedDir = join(tmpRoot, `<extracted ${opts.packageName}>`);
      } else {
        const tarball = findSdist(tmpRoot, opts.packageName);
        runOrFail("tar", ["-xzf", tarball, "-C", tmpRoot], undefined, false);
        extractedDir = findExtractedDir(tmpRoot, opts.packageName);
      }
    }

    let deployScript = null;
    if (localSource || !opts.dryRun) deployScript = findDeployScript(extractedDir);
    if (!deployScript) {
      if (opts.dryRun) deployScript = join(extractedDir, "hermes_infoflow", "deploy.py");
      else fail(`Cannot find hermes-infoflow deploy.py under ${extractedDir}`);
    }

    const deployArgs = [
      deployScript,
      "--source",
      extractedDir,
      "--hermes-home",
      home,
      "--config-file",
      configFile,
    ];
    if (opts.port !== null) deployArgs.push("--port", opts.port);
    if (opts.dryRun) deployArgs.push("--dry-run");
    runOrFail(python.exe, deployArgs, undefined, opts.dryRun);
  } finally {
    if (!opts.dryRun) rmSync(tmpRoot, { recursive: true, force: true });
  }
}

function doNormalize(rawArgs) {
  const opts = { source: null, channelId: DEFAULT_CHANNEL_ID, port: null, python: null, dryRun: false };

  for (let i = 0; i < rawArgs.length; i += 1) {
    const val = rawArgs[i];
    if (val === "--source") opts.source = rawArgs[++i] ?? opts.source;
    else {
      const leftover = parseCommon([val, ...rawArgs.slice(i + 1)], opts);
      if (leftover) fail(`Unknown option: ${leftover}`);
      break;
    }
  }

  const python = detectPython(opts.python);
  const home = hermesHome();
  const source = opts.source ? resolve(expandUser(opts.source)) : join(home, "plugins", opts.channelId);
  const configFile = process.env.HERMES_CONFIG_FILE
    ? expandUser(process.env.HERMES_CONFIG_FILE)
    : join(home, "config.yaml");

  const deployScript = findDeployScript(source);
  if (!deployScript && !opts.dryRun) {
    fail(
      `Cannot find hermes-infoflow deploy.py under ${source}. Install with ` +
        "`hermes-infoflow-tools update` or point --source at a current " +
        "hermes-infoflow checkout/plugin directory.",
    );
  }

  const cmd = deployScript ? [deployScript] : ["-m", "hermes_infoflow.deploy"];
  cmd.push("--source", source, "--hermes-home", home, "--config-file", configFile);
  if (opts.port !== null) cmd.push("--port", opts.port);
  if (opts.dryRun) cmd.push("--dry-run");
  runOrFail(python.exe, cmd, undefined, opts.dryRun);
}

function main() {
  const argv = process.argv.slice(2);
  const cmd = argv[0];
  if (!cmd || cmd === "-h" || cmd === "--help") {
    printHelp();
    process.exit(cmd ? 0 : 1);
  }
  if (cmd === "update") return doUpdate(argv.slice(1));
  if (cmd === "normalize") return doNormalize(argv.slice(1));
  console.error(`Unknown command: ${cmd}`);
  printHelp();
  process.exit(1);
}

main();
