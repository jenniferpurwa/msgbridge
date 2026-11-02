# msgbridge

A read-only [MCP](https://modelcontextprotocol.io) connector that lets Claude or ChatGPT
read your own iMessage, SMS, RCS and WhatsApp history on your Mac, so you can ask things
like *"what did Maya say about the trip?"* or *"who have I not replied to this week?"*
without copying and pasting conversations.

It never sends messages. Everything runs on your Mac: msgbridge builds a local index from
the Messages and WhatsApp databases, and the AI app queries it through eight tools.

Design docs: [PRD](docs/PRD.md) · [Tech design](docs/tech-design.md)

## Tools

| Tool | What it does |
| --- | --- |
| `find_conversations` | Find a 1:1 or group thread by person, name or members, and flag ambiguous matches |
| `get_messages` | Page through a thread (reads over 500 messages need your OK) |
| `search_messages` | Full-text search, optionally within one thread or person |
| `get_message_context` | The messages around a search hit |
| `get_relationship_stats` | Exact counts: who reaches out, reply times, how it changed over time |
| `get_style_examples` | Samples of how you write, for drafting replies in your voice |
| `list_unreplied` | Conversations waiting on you |
| `get_status` | Index coverage and freshness |

People are matched across apps through your Contacts, so stats for someone you text on both
iMessage and WhatsApp are combined.

## Requirements

- macOS with Messages (and optionally WhatsApp for Mac) signed in
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Full Disk Access for the launcher (macOS protects `~/Library/Messages`)

## Setup with Claude Desktop

```sh
uv sync
launcher/build.sh          # builds bin/msgbridge-launch
```

1. In **System Settings → Privacy & Security → Full Disk Access**, add `bin/msgbridge-launch`.
   The launcher exists so you grant access to msgbridge alone, not to every `uv` script.
   After each rebuild, turn its switch off and on again.
2. Build the index: `bin/msgbridge-launch index`
3. Add the server to Claude Desktop's `claude_desktop_config.json`:

   ```json
   { "mcpServers": { "msgbridge": { "command": "/path/to/msgbridge/bin/msgbridge-launch" } } }
   ```

4. Quit Claude fully (Cmd+Q) and reopen it.

## Setup with ChatGPT (remote, over OAuth)

ChatGPT needs a public HTTPS address, so this mode runs a local web server behind a
[Cloudflare tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/),
protected by OAuth with a single account: you, with a password plus an authenticator code.

1. Create a named tunnel called `msgbridge` that points your hostname at `http://127.0.0.1:8765`,
   with its config in `~/.cloudflared/msgbridge.yml`.
2. Tell msgbridge its public address, either with `MSGBRIDGE_PUBLIC_URL` or:

   ```sh
   echo https://msg.example.com > ~/Library/Application\ Support/MsgBridge/public_url
   ```

3. Set your sign-in in your own terminal: `bin/msgbridge-launch auth setup`
4. Install the server and tunnel as login services: `launcher/services.sh install`
5. In ChatGPT, add a custom connector with the URL `https://msg.example.com/mcp`.

Sign every app out with `bin/msgbridge-launch auth revoke-all`.

## Other commands

```sh
bin/msgbridge-launch sync                         # pick up new messages
bin/msgbridge-launch status                       # index coverage
bin/msgbridge-launch call find_conversations '{"query": "Maya"}'
```

## Privacy

- Read-only: the Messages, WhatsApp and Contacts databases are opened with SQLite's
  read-only mode, and there is no send tool.
- Your index, credentials and tokens live in `~/Library/Application Support/MsgBridge`, never in this repo.
- Message text is treated as untrusted data; the server tells the model never to follow
  instructions found inside messages.

## Development

```sh
uv run pytest
```

Tests run against synthetic Messages and WhatsApp databases (`tests/fixture.py`,
`tests/wa_fixture.py`), never real data.
