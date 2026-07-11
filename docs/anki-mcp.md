# Anki reader over MCP (use it from Claude)

The `anki-chatgpt-app` MCP server (built with Codex) exposes your live Anki
collection as read-only tools. It was originally wired for the ChatGPT app;
this repo also registers it for **Claude Code** via `.mcp.json` at the repo root.

## How it connects

```
Anki (desktop) ──► AnkiConnect add-on ──► Node/Express MCP server ──► Cloudflare tunnel ──► Claude / ChatGPT
   your cards      HTTP :8765 (local)      serves the MCP tools        public https URL
```

All of Anki, AnkiConnect, and the MCP server run on **your** computer. Claude
reaches them through the public tunnel URL.

## Tools it provides (all read-only)

| Tool | What it does |
|------|--------------|
| `get_deck_tree` | Deck hierarchy + due/new/review counts |
| `get_schedule_preview` | Preview upcoming cards for a deck (`deckName`, `limit`, `includeNewCards`) |
| `search` | Search deck names → deck IDs |
| `fetch` | Fetch a deck's schedule preview by ID |

> These **preview** the queue — they do not flip, grade, or modify cards.

## Using it from Claude Code

1. Make sure Anki is open, AnkiConnect is installed, the MCP server is running,
   and the Cloudflare tunnel is up on your machine.
2. Open this repo in Claude Code. It reads `.mcp.json` and (after you approve
   the server) the four `anki` tools become available.
3. Ask things like *"show my due Anki cards"* or *"preview 20 Cardiology cards."*

## Using it from the Claude desktop app

Settings → Connectors → **Add custom connector** → paste the tunnel URL
(`.../mcp`). Note: the visual card **widget** is an OpenAI Apps SDK component
and does **not** render in Claude — only the tool results do.

## Caveats

- **The tunnel URL is ephemeral.** `trycloudflare.com` quick-tunnels get a new
  hostname every restart. When it changes, update the `url` in `.mcp.json`.
- Nothing works unless Anki + AnkiConnect + the server + the tunnel are all
  running on your computer.
