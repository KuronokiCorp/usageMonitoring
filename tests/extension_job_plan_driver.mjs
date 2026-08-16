// tests/extension_job_plan_driver.mjs -- drives extension/panel.js's
// exported, PURE buildJobCreationPlan() under plain `node`, no browser
// (spec docs/specs/chrome-extension-control-panel.md, AC-38/AC-39). Mirrors
// the notify_logic.js pattern: no DOM, no chrome.*, just data in/data out.
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const EXT = path.join(HERE, "..", "extension");

const { buildJobCreationPlan } = await import(path.join(EXT, "panel.js"));

const results = [];
function check(name, fn) {
  try {
    fn();
    results.push({ name, ok: true });
  } catch (err) {
    results.push({ name, ok: false, error: String(err && err.stack ? err.stack : err) });
  }
}

const SESSIONS = [
  { index: "1.1.1", name: "alpha", job: "zsh" },
  { index: "1.1.2", name: "beta", job: "node" },
  { index: "1.1.3", name: "gamma", job: "zsh" },
];

// -- AC-39: "select all" with N sessions produces exactly N entries, one
// per session, every target index:-prefixed, and no target is __all__.
check("select_all_expands_to_one_job_per_session_index_prefixed", () => {
  const plan = buildJobCreationPlan({
    sessions: SESSIONS,
    checkedTargets: [],
    selectAll: true,
    name: "nudge",
    command: "echo hi",
    schedule: "0 * * * *",
    submit: false,
  });
  assert.equal(plan.jobs.length, SESSIONS.length);
  const targets = plan.jobs.map((j) => j.target);
  for (const t of targets) {
    assert.ok(t.startsWith("index:"), `target ${t} must start with "index:"`);
    assert.notEqual(t, "__all__");
  }
  assert.deepEqual(new Set(targets).size, SESSIONS.length, "targets must be distinct, one per session");
  // Names disambiguated when more than one job is created.
  const names = new Set(plan.jobs.map((j) => j.name));
  assert.equal(names.size, plan.jobs.length, "job names must be disambiguated");
});

// -- AC-38: explicitly-checked targets (not select-all) are ALSO
// index:-prefixed, never id:-prefixed -- the checkbox values themselves
// already carry the prefix (built by renderJobTargetCheckboxes, spec 6.2).
check("explicitly_checked_targets_are_index_prefixed", () => {
  const plan = buildJobCreationPlan({
    sessions: SESSIONS,
    checkedTargets: [{ value: "index:1.1.2", label: "1.1.2  node  beta" }],
    selectAll: false,
    name: "solo",
    command: "echo hi",
    schedule: "0 * * * *",
    submit: true,
  });
  assert.equal(plan.jobs.length, 1);
  assert.equal(plan.jobs[0].target, "index:1.1.2");
  assert.ok(!plan.jobs[0].target.startsWith("id:"));
});

// -- AC-41: submit defaults off -- the created job carries submit:false
// when the checkbox is unchecked, and submit:true only when explicitly set.
check("submit_flows_through_as_given_defaults_false", () => {
  const off = buildJobCreationPlan({
    sessions: SESSIONS,
    checkedTargets: [{ value: "index:1.1.1", label: "1.1.1" }],
    selectAll: false,
    name: "n",
    command: "c",
    schedule: "0 * * * *",
    submit: false,
  });
  assert.equal(off.jobs[0].submit, false);

  const on = buildJobCreationPlan({
    sessions: SESSIONS,
    checkedTargets: [{ value: "index:1.1.1", label: "1.1.1" }],
    selectAll: false,
    name: "n",
    command: "c",
    schedule: "0 * * * *",
    submit: true,
  });
  assert.equal(on.jobs[0].submit, true);
});

const failed = results.filter((r) => !r.ok);
const summary = { total: results.length, passed: results.length - failed.length, failed: failed.length };
console.log(JSON.stringify(summary));
if (failed.length > 0) {
  for (const f of failed) console.error(`FAIL ${f.name}: ${f.error}`);
  process.exit(1);
}
process.exit(0);
