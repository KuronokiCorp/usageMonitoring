# usageMonitoring (itermon) — VecTech Limited product 75

This workspace is a **VecTech Limited product**, managed by the company (rule 8): every
agent here is recorded in the company registry
(`../../05_Organization/Employee_Registry.md`) and its canonical definition lives at HQ
(`../../.claude/agents/`). Product head: **`usagemonitoring-product-manager`** "Messi".

## The fleet and how it works together
- `usagemonitoring-product-manager` v275001 — specs, review, semver decisions
- `usagemonitoring-developer` v475001 — implements specs (Node CLI / iTerm2 automation)
- `usagemonitoring-tester` v475002 — suite + safe real-iTerm2 verification (never the user's live sessions)
- `usagemonitoring-release-manager` v375001 — semver/changelog/publish prep
- `usagemonitoring-cfo` v375002 "Reina" — product finance (functional line to group-cfo Buffon)
- `usagemonitoring-support-manager` v375003 "Lizarazu" — user/issue intake & triage

Work flows: PM spec → developer implements → tester verifies → PM review → release-manager prepares → CEO approves publish.
The PM reviews every result (ACCEPT / REQUEST CHANGES) before it counts as done.

## Session rules
- **Full fleet availability:** launch Claude Code from the company root
  (`~/Documents/vectechlimited`) — all agents are defined there and can work in this
  folder. When launched inside this folder instead, follow this file's pipeline and read
  the HQ agent definitions by path for each role's duties.
- **Daily action record (company rule 7):** every agent writes
  `docs/worklog/<agent>/` (in THIS repo)YYYY-MM-DD.md` at session end and reads its own
  last 5 entries at session start — that record is the agent's personality.
- **Public releases stay CEO-approved** (rule 6) — no publish/deploy without the CEO.
- Staffing or scope changes are **proposed to HQ** via the PM, never self-executed.

## History & archive (company rule 12)
This repo is part of the company's history of record. **Session start: `git pull` first** — cloud Zidane and HQ also commit here; an unpulled session works on stale state. Commit and push records (worklogs,
docs, agent files) at the end of any working day that changed them — unpushed history is
unarchived. Sessions load only an agent's last 5 daily entries + latest monthly rollup;
each agent writes `rollups/YYYY-MM.md` from its dailies in its first session of a new
month, after which pushed dailies older than the previous month are pruned (git history
keeps every day — retrieve via `git log --follow` / `git show`). The product head owns
this cadence. Full rule: `../../03_Operations/Worklog/README.md`.

## Who's who — callsigns & numbers
Address anyone by callsign or role slug; registry of record: `../../05_Organization/Employee_Registry.md`.

| Callsign | Role | No. |
|---|---|---|
| **Messi** | `usagemonitoring-product-manager` (head) | v275001 |
| **Lampard** | `usagemonitoring-release-manager` | v375001 |
| **Reina** | `usagemonitoring-cfo` (functional line → group-cfo Buffon) | v375002 |
| **Lizarazu** | `usagemonitoring-support-manager` | v375003 |
| **Gerrard** | `usagemonitoring-developer` | v475001 |
| **Dida** | `usagemonitoring-tester` | v475002 |

## Branch model & code review (company rule 15)
CODE follows Git Flow: `main` = production (releasable, CEO-gated), `develop` =
integration/test, `feature/<slug>` = one feature cut from `develop` and owned by the
developer. **Never commit code straight to main/develop** — a feature merges to `develop`
only after **tester PASS + Ayala (code-reviewer) APPROVE**; `develop`→`main` only on a
CEO-approved release. Records (worklogs/briefs/backlog) still commit to `main` (rule 12).
Full standard: `../../05_Organization/Coding_Standard.md`.

## Standing orders (company rule 13)
A session opened here with NO specific command is not idle: the product head reads its
worklog, runs any calendar duty due (KPI/P&L/rollup dates), then works the TOP item of
`BACKLOG.md` through the normal pipeline, ending with a worklog entry and a short brief
(what moved, what's next, what needs a CEO decision). Backlog is owned/ranked by the head.
Full rule: `../../05_Organization/Standing_Orders.md`.

## Rule 16 — CEO decisions are asked as a pick-and-submit question (CEO, 24 Jul 2026)
When this product needs a call from the CEO, do **not** bury it in prose or a long "decisions"
list. Pose it as a **discrete question with 2–4 selectable options the CEO picks and submits**,
**recommended option first** — one decision = one question = one submit. Interactive sessions
render it with the question/selector UI (`AskUserQuestion`); headless/cloud agents write each
CEO decision in that exact shape (one question, concrete options, recommended one marked) in the
brief + CEO todo so it becomes a one-click prompt at review. Reserve questions for genuinely
CEO-reserved calls (releases, money, staffing, direction, outward acts); decide the rest yourself.
Full rule: `../../05_Organization/Org_Chart.md` (rule 16).

## Rule 17 — every product "talk" routes through this product's PM (CEO, 24 Jul 2026)
All communication and all work about this product — a CEO instruction, a question, an update, a
build, a fix — goes **to and through this product's product manager**. The PM reviews/drives it
(through this product's tester + reviewer gate where it is work) and **reports the outcome to the
CEO**. This binds everyone, **including HQ / the CEO-facing session / autonomous runs** — HQ may set
direction and dispatch, but must not do this product's work or speak for it and bypass its PM. A
deliverable that never passed this PM is *unreviewed*, not done. Full rule:
`../../05_Organization/Org_Chart.md` (rule 17).
