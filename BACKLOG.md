# BACKLOG — usageMonitoring / itermon

> **🟡 CEO DISPATCH (2026-07-24) — FYI:** DeepSeek V4 正式版全量上线,旧接口今日永久停用。
> No current DeepSeek usage found in this product — note for future model selection; no action needed.
> Full dispatch: `../../03_Operations/Dispatches/2026-07-24-deepseek-v4.md`.

*Owned and ranked by Messi (usagemonitoring-product-manager). Seeded by HQ 22 Jul 2026 — head MUST review and re-rank in
its first standing session (rule 13). Anyone proposes via the head; empty backlog = head's
failure. Top item is what a no-command session works on.*

1. **[CEO DECISION — release gate] Release itermon v1.2.0 to npm.** Code is built, tester
   PASS + Ayala APPROVE, merged to `develop` (2026-07-24). Remaining is the CEO-gated release:
   promote `develop`→`main`, Lampard preps changelog/tag, `npm publish`. Pick-and-submit below.
2. Issue triage with Lizarazu.
3. *(future, non-blocking)* MCP server: align unknown-tool `tools/call` to return an `isError`
   content result rather than a raw JSON-RPC `-32603` (softer MCP convention some clients prefer).
   Found during Dida's v1.2.0 verification; pre-existing, not a defect.

### Done this session (2026-07-24, rule-13)
- ~~Ship itermon v1.2.0: MCP server into the npm package~~ — DONE through code+gates, on `develop`.
  files/bin/`npm run mcp`/version 1.2.0/README section/symlinked-bin fix. (was #1)
- ~~Dida verification pass over the MCP server (list/read/send, safe-session rules)~~ — PASS. (was #2)

### CEO decision — pick and submit (rule 16)
**Q: Release itermon v1.2.0 to npm now?** (new backward-compatible MCP-server packaging;
tester PASS + reviewer APPROVE; on `develop`.)
- **[Recommended] Approve release** — promote `develop`→`main`, Lampard preps changelog/tag,
  then `npm publish`. Additive change, both gates green, itermon is stable.
- **Hold** — keep on `develop`, publish bundled with the next feature.
- **Approve, but I want to see the changelog/diff first** — Lampard drafts the release notes for
  review before publish.
