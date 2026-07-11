# WebSocket Unlock Tracker (WIP)

Minimal Archipelago websocket listener used as the first step of the Discord bot rewrite.

## What it does
- Reads connection config from `.env`
- Connects to the Archipelago server using `CommonClient` flow
- Prints live `ItemSend` unlock events to stdout
- No Discord integration yet

## Required `.env` values
- `ARCHIPELAGO_SERVER` (or `ARCHIPELAGO_ADDRESS`, `ARCHIPELAGO_URL`)
- `ARCHIPELAGO_SLOT_NAME` (or `ARCHIPELAGO_SLOT`, `ARCHIPELAGO_NAME`)

Optional:
- `ARCHIPELAGO_PASSWORD`

Example:

```env
ARCHIPELAGO_SERVER=archipelago.gg:38281
ARCHIPELAGO_SLOT_NAME=YourSlotName
ARCHIPELAGO_PASSWORD=
```

## Run

```powershell
uv run discord_bot/websocket_based/main.py
```

