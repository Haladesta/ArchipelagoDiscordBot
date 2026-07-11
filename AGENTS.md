# Archipelago Agent Guide

## Project goal (this workspace)
- Build and maintain a Discord bot that integrates with Archipelago sessions and posts real-time notifications to Discord.
- The target behavior is: when a player performs a check (clears a location), notify the receiving player/item event in Discord with minimal delay.
- Prefer direct WebSocket protocol integration over web polling for freshness and completeness.

## What Archipelago is (quick context)
- Archipelago is a multi-game, multiworld randomizer framework: multiple players (sometimes across different games) share one connected item network.
- A **location** is a place in a game world that can contain an item.
- A **check** is the act of clearing/reaching a location and revealing/sending its item.
- Items found in one player’s game can belong to another player and are sent through the server session.

## Discord notification vocabulary
- Keep notification wording event-first and consistent; include sender player, receiving player, item, and location when available.
- **Check sent**: player clears a location and sends an item to another slot. Example: `Alice checked "Kakariko Well" -> sent Hookshot to Bob`.
- **Item received**: local slot receives an item from another player/location. Example: `Bob received Hookshot from Alice (Kakariko Well)`.
- **Self-find**: player checks a location and receives own item. Example: `Alice checked "Hyrule Castle" and found own item: Lamp`.
- **Remote-find**: a non-local player receives an item that another non-local player found; optional for global-feed channels.
- **Progression emphasis**: if item flags indicate progression/useful/trap, prepend a short tag (e.g., `[Progression]`, `[Useful]`, `[Trap]`).
- **Avoid noise**: collapse repeats and avoid duplicate posts when reconnecting/resyncing previously seen checks.

## Scope for this workspace task
- Prioritize core networking + bot integration work; ignore `worlds/` and game-specific clients like `Zelda1Client.py`.
- Existing prototype bot is in `discord_bot/webapi_based/main.py`; websocket-first work should align with `CommonClient.py` + `NetUtils.py` protocol behavior.

## Big picture architecture
- `MultiServer.py` is the async WebSocket server (`websockets`), command/event dispatch, and room session state.
- `CommonClient.py` is the reusable async client context (connect/login, receive loop, command handling, item/location updates).
- `NetUtils.py` defines protocol primitives (e.g., `NetworkItem`, `NetworkPlayer`, enums like `ClientStatus`) and JSON encoding/decoding helpers.
- `docs/network protocol.md` is the packet contract (`RoomInfo`, `Connected`, `ReceivedItems`, etc.); treat it as source of truth for payload shapes.
- `WebHostLib/` is Flask + Pony ORM web hosting/API; useful for web views/status, but real-time tracking should prefer WebSocket stream over web polling.

## Data flow patterns to reuse
- Server and clients exchange JSON command objects over one WebSocket session; handlers route by `cmd` field.
- Client lifecycle pattern in `CommonClient.py`: connect -> process `RoomInfo` -> send connect/auth -> consume `ReceivedItems` and location/check updates.
- Keep local state in a context object (slot/team/items/locations) and emit side effects (logs/notifications) from command handlers.
- SSL/network setup and retry logic already exist in core client flow; copy those patterns before inventing new connection code.

## Discord bot integration notes
- `discord_bot/webapi_based/main.py` shows bot structure: env loading, aiohttp usage, dataclass state, and Discord message posting.
- For websocket-based bot, mirror `CommonClient.py` semantics for packet handling instead of polling APIs.
- Expect room + slot metadata from protocol packets rather than web endpoints; use web API only for optional enrichment.
- Keep secrets/config in environment variables (token, server address, slot/password), not hardcoded constants.

## Developer workflows (discoverable)
- Python baseline is 3.13 (see `pyproject.toml`).
- Install deps with `pip install -r requirements.txt` (bot-specific deps are also declared in `pyproject.toml`: `discord`, `aiohttp`).
- Run tests with `pytest` (configured in `pytest.ini`; test roots include `test/`).
- Run server locally with `python MultiServer.py` (default AP port is 38281 unless overridden).

## Project conventions that matter here
- Style in `docs/style.md`: 120-char line limit, type hints required, PEP 8 naming, double quotes.
- Prefer modern typing syntax (`list[int]`, `str | None`) and explicit async signatures.
- Reuse existing logging/setup patterns from `Utils.py` and client/server modules.
- Keep protocol structs/enums centralized in `NetUtils.py`; do not duplicate packet schema constants across modules.

## High-value files to read first
- `CommonClient.py`
- `NetUtils.py`
- `MultiServer.py`
- `docs/network protocol.md`
- `discord_bot/webapi_based/main.py`
- `WebHostLib/api/__init__.py`
- `docs/style.md`
