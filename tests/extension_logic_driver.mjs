// tests/extension_logic_driver.mjs -- drives extension/notify_logic.js's
// pure decision layer under plain `node`, no browser (spec docs/specs/
// chrome-extension-control-panel.md, section 10.2). Cases L1..L10 from the
// spec's table. Prints a one-line JSON summary and exits non-zero on any
// failure -- a Python test in tests/run_tests.py's G12 shells this out and
// surfaces stderr on failure.
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const EXT = path.join(HERE, "..", "extension");

const { logKey, diffLogEntries, detectJobDrops, planNotifications, parseServerTime } = await import(
  path.join(EXT, "notify_logic.js")
);

const results = [];

function check(name, fn) {
  try {
    fn();
    results.push({ name, ok: true });
  } catch (err) {
    results.push({ name, ok: false, error: String(err && err.stack ? err.stack : err) });
  }
}

const SETTINGS = { notifyErrors: true, notifyJobDrops: true };
const GRACE_MS = 120000;

function errEntry(t, message) {
  return { t, kind: "error", message };
}

// -- L1: first poll, log already has 3 errors -> 0 notifications; anchor seeded to newest.
check("L1_first_poll_seeds_silently", () => {
  const entries = [
    errEntry("2026-08-16 09:00:00", "old error 1"),
    errEntry("2026-08-16 09:01:00", "old error 2"),
    errEntry("2026-08-16 09:02:00", "old error 3"),
  ];
  const state = { lastLogKey: null, lastLogT: null };
  const { newEntries, nextLogAnchor } = diffLogEntries(entries, state);
  assert.equal(newEntries.length, 0);
  assert.equal(nextLogAnchor.lastLogKey, logKey(entries[2]));
  assert.equal(nextLogAnchor.lastLogT, entries[2].t);
  const notifications = planNotifications(newEntries, [], SETTINGS);
  assert.equal(notifications.length, 0);
});

// -- L2: one new error line since the anchor -> exactly 1 notification.
check("L2_one_new_error_since_anchor", () => {
  const base = [
    errEntry("2026-08-16 09:00:00", "old error 1"),
    errEntry("2026-08-16 09:01:00", "old error 2"),
  ];
  const state = { lastLogKey: logKey(base[1]), lastLogT: base[1].t };
  const withNew = [...base, errEntry("2026-08-16 09:05:00", "new error")];
  const { newEntries } = diffLogEntries(withNew, state);
  assert.equal(newEntries.length, 1);
  assert.equal(newEntries[0].message, "new error");
  const notifications = planNotifications(newEntries, [], SETTINGS);
  assert.equal(notifications.length, 1);
});

// -- L3: the same tick's data polled twice -> 1 notification total, not 2.
check("L3_same_data_polled_twice_dedups", () => {
  const base = [errEntry("2026-08-16 09:00:00", "old")];
  let state = { lastLogKey: logKey(base[0]), lastLogT: base[0].t };
  const entries = [...base, errEntry("2026-08-16 09:05:00", "new error")];

  const first = diffLogEntries(entries, state);
  const firstNotifications = planNotifications(first.newEntries, [], SETTINGS);
  state = first.nextLogAnchor;

  // Poll again with the SAME entries -- anchor now points at the newest,
  // so nothing new should be found.
  const second = diffLogEntries(entries, state);
  const secondNotifications = planNotifications(second.newEntries, [], SETTINGS);

  const total = firstNotifications.length + secondNotifications.length;
  assert.equal(total, 1, `expected 1 total notification across both polls, got ${total}`);
});

// -- L4: anchor missing (buffer rolled), 50 newer entries by t -> <=20 processed, 1 collapsed notification.
check("L4_anchor_missing_caps_at_20_collapsed", () => {
  const state = { lastLogKey: "some-stale-key-not-present", lastLogT: "2026-08-16 08:00:00" };
  const entries = [];
  for (let i = 0; i < 50; i++) {
    const minute = String(i).padStart(2, "0");
    entries.push(errEntry(`2026-08-16 09:${minute}:00`, `error ${i}`));
  }
  const { newEntries } = diffLogEntries(entries, state);
  assert.ok(newEntries.length <= 20, `expected <=20 processed, got ${newEntries.length}`);
  const notifications = planNotifications(newEntries, [], SETTINGS);
  assert.equal(notifications.length, 1, "expected exactly one collapsed notification");
});

const JOB = { id: "jobA", name: "nudge" };

// -- L5: enabled job, previous next_run 5 min past, last_run unchanged -> exactly 1 drop notification.
check("L5_enabled_job_missed_slot_notifies_once", () => {
  const prevJobsById = {
    jobA: { last_run: "2026-08-16 10:00", next_run: "2026-08-16 10:05", dropReportedFor: null },
  };
  const jobs = [{ ...JOB, enabled: true, last_run: "2026-08-16 10:00", next_run: "2026-08-16 10:05" }];
  const nowMs = parseServerTime("2026-08-16 10:10");
  const { drops, nextJobsById } = detectJobDrops(jobs, prevJobsById, nowMs, GRACE_MS);
  assert.equal(drops.length, 1);
  assert.equal(drops[0].id, "jobA");
  const notifications = planNotifications([], drops, SETTINGS);
  assert.equal(notifications.length, 1);
  assert.equal(nextJobsById.jobA.dropReportedFor, "2026-08-16 10:05");
});

// -- L6: same missed slot on the next tick -> 0 further notifications.
check("L6_same_missed_slot_not_reported_twice", () => {
  const prevJobsById = {
    jobA: { last_run: "2026-08-16 10:00", next_run: "2026-08-16 10:05", dropReportedFor: null },
  };
  const jobs = [{ ...JOB, enabled: true, last_run: "2026-08-16 10:00", next_run: "2026-08-16 10:05" }];
  const tick1 = detectJobDrops(jobs, prevJobsById, parseServerTime("2026-08-16 10:10"), GRACE_MS);
  assert.equal(tick1.drops.length, 1, "sanity: tick1 must have flagged the drop");

  const tick2 = detectJobDrops(jobs, tick1.nextJobsById, parseServerTime("2026-08-16 10:15"), GRACE_MS);
  assert.equal(tick2.drops.length, 0, "same slot must not be re-reported");
  const notifications = planNotifications([], tick2.drops, SETTINGS);
  assert.equal(notifications.length, 0);
});

// -- L7: disabled job in the same situation -> 0 notifications.
check("L7_disabled_job_never_flagged", () => {
  const prevJobsById = {
    jobA: { last_run: "2026-08-16 10:00", next_run: "2026-08-16 10:05", dropReportedFor: null },
  };
  const jobs = [{ ...JOB, enabled: false, last_run: "2026-08-16 10:00", next_run: "2026-08-16 10:05" }];
  const { drops } = detectJobDrops(jobs, prevJobsById, parseServerTime("2026-08-16 10:10"), GRACE_MS);
  assert.equal(drops.length, 0);
});

// -- L8: job fired (last_run advanced) -> 0 drop notifications.
check("L8_job_fired_last_run_advanced_no_drop", () => {
  const prevJobsById = {
    jobA: { last_run: "2026-08-16 10:00", next_run: "2026-08-16 10:05", dropReportedFor: null },
  };
  const jobs = [{ ...JOB, enabled: true, last_run: "2026-08-16 10:05", next_run: "2026-08-16 10:35" }];
  const { drops } = detectJobDrops(jobs, prevJobsById, parseServerTime("2026-08-16 10:10"), GRACE_MS);
  assert.equal(drops.length, 0);
});

// -- L9: 5 new error lines in one tick -> 1 collapsed notification whose title names 5.
check("L9_five_errors_collapse_title_names_five", () => {
  const newEntries = [];
  for (let i = 0; i < 5; i++) {
    newEntries.push(errEntry(`2026-08-16 09:0${i}:00`, `error ${i}`));
  }
  const notifications = planNotifications(newEntries, [], SETTINGS);
  assert.equal(notifications.length, 1);
  assert.match(notifications[0].title, /5/);
});

// -- L10: parseServerTime("2026-08-16 14:30") -> correct local epoch ms; no NaN.
check("L10_parseServerTime_no_seconds_no_nan", () => {
  const ms = parseServerTime("2026-08-16 14:30");
  assert.ok(!Number.isNaN(ms), "parseServerTime produced NaN");
  const expected = new Date(2026, 7, 16, 14, 30).getTime();
  assert.equal(ms, expected);
});

const failed = results.filter((r) => !r.ok);
const summary = { total: results.length, passed: results.length - failed.length, failed: failed.length };
console.log(JSON.stringify(summary));
if (failed.length > 0) {
  for (const f of failed) {
    console.error(`FAIL ${f.name}: ${f.error}`);
  }
  process.exit(1);
}
process.exit(0);
