/* Offline only. Usage: node docs/campaign-discovery/derive-claim-hash.cjs ..
 * The argument is the existing .investigations directory containing the three
 * previously downloaded public bundles. No downloads, credentials, or requests.
 * Exit 2 means the claim was reproduced but the known dashboard control could
 * not be verified; this does not authorize a live claim or a journal reset.
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");

const EXPECTED_DASHBOARD = "69750554e0a81492f2d343558f84bdf3e324767650a2dbb6e79a3c629b4548cf";
const LEGACY_CLAIM = "a455deea71bdc9015b78eb49f4acfbce8baa7ccbedd28e549bb025bd0f751930";
const BUNDLES = [
  "twitch-assets/49198-87edf11f696f172878f7.js",
  "twitch-assets/21956-b5a5c32dd4e02f095dd2.js",
  "twitch-drops-root.js",
];
const sha256 = (text) => crypto.createHash("sha256").update(text).digest("hex");

function loadBundles(directory) {
  const factories = Object.create(null);
  const modules = Object.create(null);
  const sources = [];
  // Register module factories only. The webpack runtime callback is never run.
  // Browser/network globals and Node process/require are not exposed to the VM.
  const context = vm.createContext({
    self: { webpackChunktwitch_twilight: {
      push: (registration) => Object.assign(factories, registration[1]),
    } },
  });
  for (const filename of BUNDLES) {
    const source = fs.readFileSync(path.join(directory, filename), "utf8");
    sources.push({ filename, sha256: sha256(source) });
    vm.runInContext(source, context, { filename, timeout: 5000 });
  }
  function localRequire(id) {
    if (modules[id]) return modules[id].exports;
    if (!factories[id]) {
      const error = new Error("offline_bundle_module_missing");
      error.moduleId = id;
      throw error;
    }
    const module = { exports: {} };
    modules[id] = module;
    try {
      factories[id](module, module.exports, localRequire);
    } catch (error) {
      delete modules[id];
      throw error;
    }
    return module.exports;
  }
  localRequire.d = (target, definitions) => {
    for (const [name, get] of Object.entries(definitions)) {
      Object.defineProperty(target, name, { get, enumerable: true });
    }
  };
  localRequire.r = (target) => Object.defineProperty(target, "__esModule", { value: true });
  localRequire.o = (target, name) => Object.prototype.hasOwnProperty.call(target, name);
  localRequire.n = (target) => {
    const get = () => target && target.__esModule ? target.default : target;
    localRequire.d(get, { a: get });
    return get;
  };
  localRequire.g = context;
  return { localRequire, sources };
}

async function derive(localRequire, document) {
  const Cache = localRequire(225558).D;
  const print = localRequire(80928).y;
  const Observable = localRequire(973401).c;
  const transformed = new Cache({}).transformDocument(document);
  let input;
  const link = localRequire(25931).e({
    sha256: (value) => { input = value; return sha256(value); },
  });
  const operation = {
    query: transformed,
    extensions: {},
    setContext() {},
    getContext() { return {}; },
  };
  const definition = document.definitions.find((item) => item.kind === "OperationDefinition");
  // Exercise the actual persisted-query link, substituting an in-memory forward
  // observer for its transport. No HTTP implementation exists in this script.
  return await new Promise((resolve, reject) => {
    link.request(operation, (forwarded) => new Observable((observer) => {
      const result = {
        operation_name: definition.name.value,
        operation_kind: definition.operation,
        variables: definition.variableDefinitions.map((item) => item.variable.name.value),
        sha256Hash: forwarded.extensions.persistedQuery.sha256Hash,
        version: forwarded.extensions.persistedQuery.version,
        hash_input_bytes: Buffer.byteLength(input),
        input_equals_bundled_printer: input === print(transformed),
        trailing_newline: input.endsWith("\n"),
      };
      observer.next({ data: {} });
      observer.complete();
      resolve(result);
    })).subscribe({ error: reject });
  });
}

async function main() {
  if (process.argv.length !== 3) throw new Error("usage_requires_existing_investigations_directory");
  const { localRequire, sources } = loadBundles(path.resolve(process.argv[2]));
  const report = { offline_only: true, sources };
  const document = localRequire(820599);
  report.claim = await derive(localRequire, document);
  report.claim.live_validated = false;
  try {
    const dashboard = await derive(localRequire, localRequire(474137));
    report.dashboard_control = {
      ...dashboard,
      expected_observed_hash: EXPECTED_DASHBOARD,
      matched: dashboard.sha256Hash === EXPECTED_DASHBOARD,
    };
  } catch (error) {
    if (error.moduleId === undefined) throw error;
    report.dashboard_control = {
      expected_observed_hash: EXPECTED_DASHBOARD,
      matched: null,
      state: "unavailable_missing_module",
      missing_module: error.moduleId,
    };
  }
  // A separately labelled hypothesis, never represented as recovered old code.
  const minimal = JSON.parse(JSON.stringify(document));
  const rootField = minimal.definitions[0].selectionSet.selections[0];
  rootField.selectionSet.selections = rootField.selectionSet.selections.filter(
    (item) => item.name.value === "status",
  );
  const legacyHypothesis = await derive(localRequire, minimal);
  report.legacy_status_only_hypothesis = {
    ...legacyHypothesis,
    expected_legacy_hash: LEGACY_CLAIM,
    matched: legacyHypothesis.sha256Hash === LEGACY_CLAIM,
    recovered_historical_ast: false,
  };
  report.state = report.dashboard_control.matched === true
    ? "offline_derived_with_dashboard_control"
    : "offline_derived_control_unverified";
  console.log(JSON.stringify(report, null, 2));
  process.exitCode = report.dashboard_control.matched === true ? 0 : 2;
}

main().catch((error) => {
  console.error(JSON.stringify({ state: "offline_derivation_failed", error: error.message }));
  process.exitCode = 1;
});
