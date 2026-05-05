# Org

Layer-3 knowledge: organizational context that frames the user's working life. Always in context.

Each entry follows the format:

```
## <Org / unit / team name>
- **Relation to user:** <one line>
- **Last verified:** <YYYY-MM-DD>
- **Expires:** <YYYY-MM-DD or never>
- **Source:** <file / URL / manual>

<short body — ≤120 words.>
```

Add teams, divisions, recurring vendor relationships, etc. The assistant never invents org details — populate from real sources.

---

## Nexar

- **Relation to user:** employer
- **Last verified:** 2026-05-05
- **Expires:** never (refresh on role/org change)
- **Source:** user's email domain `@getnexar.com` from `~/.claude/CLAUDE.md`

Nexar is the user's employer at the time this entry was written. Internal team / product / role details are not yet captured in this KB; the user is expected to populate them as the assistant gets used. References to "NAP" (Nexar Agent / Nexar AI Platform) appear elsewhere in this project's tooling (e.g., the `nap:*` skills the user has access to in Claude Code) — that is the relevant internal platform, but its scope and surface-area should be added here by the user before the assistant treats it as ground truth.
