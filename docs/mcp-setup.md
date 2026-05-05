# MCP setup pointers

The personal-assistant skill harvests via the Slack, Gmail, and Granola MCPs configured in your Claude Code account. MCP setup happens outside this repo (it's a Claude Code account-level thing); this doc points you at the right places and documents the minimum surface the skill needs.

## Scope

- **What's IN scope here**: minimum tool surface + scopes the skill expects, install pointers, common gotchas.
- **What's NOT in scope here**: the actual install/auth flow for each MCP — those are documented by the MCP authors. Links below.

## Slack MCP

The skill uses Slack MCP for live thread harvest. Tools the skill calls:

- `mcp__claude_ai_Slack__slack_search_public_and_private` — for `from:@you`, `has::pencil:`, and similar discovery queries.
- `mcp__claude_ai_Slack__slack_search_channels` — for name-pattern matches (`external-*`, `customer-*`, `partner-*`).
- `mcp__claude_ai_Slack__slack_read_thread` — for fetching thread bodies.
- `mcp__claude_ai_Slack__slack_read_user_profile` — for resolving user ids to names.

Required Slack scopes (broadly): `conversations:history`, `conversations:read`, `users:read`, `search:read`. Read-only.

Setup: see your Claude Code MCP configuration. The skill will refuse to run live Slack harvest if the MCP is not configured; the synthetic `slack-fixture` source path works without it.

## Gmail MCP

Tools the skill calls:

- `mcp__claude_ai_Gmail__*` — list / get threads / extract body content.

Required Google scope: `https://www.googleapis.com/auth/gmail.readonly`.

Setup: configure the Gmail MCP in your Claude Code account; run the OAuth flow once when prompted. The skill will refuse to run live Gmail harvest if the MCP is not configured.

## Granola MCP

Tools the skill calls:

- `mcp__granola__list_meetings`
- `mcp__granola__get_meeting_transcript`
- `mcp__granola__query_granola_meetings`

The skill will fall back to a folder-watch source (`tools/harvest.py --source granola --folder <path-to-granola-exports>`) if the MCP is not configured.

## Google Meet transcripts

Meet doesn't expose a public MCP for transcripts. The skill harvests via:

- The Google Drive folder where Meet auto-saves transcripts. Sync that folder to your machine and run `tools/harvest.py --source gmeet --folder <local-path>`.
- Or drop transcript files manually into the configured transcripts folder and run `--source transcripts --folder <path>`.

## Generic transcript drop

For any transcript file (`.vtt`, `.srt`, `.txt`) not covered by the above:

```
tools/harvest.py --source transcripts --folder ~/transcript-drop
```

This source is purely file-based and has no MCP dependency.

## Verifying MCP availability

The bootstrap walker (`tools/bootstrap.py`) does NOT verify MCP availability — verifying remote MCP reachability would require a Claude Code session, and bootstrap is a CLI script. To test that an MCP is reachable for harvest, open a Claude Code session in the method-repo checkout and ask the skill to list available MCPs or attempt a small harvest. Missing MCPs aren't fatal — the skill works with whatever subset you have configured; missing MCPs just mean the corresponding live-harvest path isn't available, and you fall back to the file-based source for that channel (where one exists).
