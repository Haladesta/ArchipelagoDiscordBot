This is a fork of the original [Archipelago repository](https://github.com/ArchipelagoMW/Archipelago) to create a
discord bot that monitors the item unlocks in a given game and report those to a discord channel, as well as optionally
ping users for their unlocks.

Bot code is located in `discord_bot/websocket_based` and is based on the websocket implementation of the Archipelago
client.
The dir `discord_bot/webapi_based` also exists but is basically deprecated as it requires the spoiler-file of a game to
function.

## Requirements:

- uv installed, etc.
- registered bot on discord with a token
- create .env file `discord_bot/websocket_based/.env` with the variable `DISCORD_BOT_TOKEN` or pass value at runtime

## Launch the bot:

```bash
# Install dependencies (only needs to be done once)
uv sync
# Launch the bot
uv run discord_bot/websocket_based/main.py
```