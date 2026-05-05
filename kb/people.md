# People

Layer-3 knowledge: the recurring people in the user's working life. Always in context.

Each entry follows the format:

```
## <Name or handle>
- **Role / relation:** <one line>
- **Last verified:** <YYYY-MM-DD>
- **Expires:** <YYYY-MM-DD or never>
- **Source:** <where this fact came from — file, URL, conversation, manual>

<short body — ≤80 words. Stuff that helps the assistant give better answers about
or involving this person. Avoid PII beyond what the user wants in their KB.>
```

Entries are added by the user (or by harvested decisions/threads that elevate them). The assistant never invents people.

---

## acardote / André Cardote

- **Role / relation:** the user this assistant serves; engineer at Nexar (`@getnexar.com`); operator of this repo
- **Last verified:** 2026-05-05
- **Expires:** never (refresh on role/org change)
- **Source:** `~/.claude/CLAUDE.md` (`andre.cardote@getnexar.com`); local git config; this project's commit history

The user is the only first-class identity the KB is opinionated about. When the assistant says "the user" it means André unless context says otherwise. André is comfortable with technical depth — terse, precise responses preferred over hand-holding. Bruno Method discipline is in active use on this project (see `decisions.md` and the `.bruno/` directory). Default communication tone: short, structured, no filler.
