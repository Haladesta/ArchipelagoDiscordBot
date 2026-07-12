from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import sys
from typing import Any, Callable, Coroutine

import discord
from discord import app_commands
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from CommonClient import CommonContext, server_loop
from NetUtils import NetworkItem

logger = logging.getLogger(__name__)
handler = logging.StreamHandler(stream=sys.stdout)
handler.setFormatter(
    logging.Formatter("[{asctime}] [{levelname:<8}] {name}: {message}", "%Y-%m-%d %H:%M:%S", style="{")
)
logger.setLevel(logging.INFO)
logger.addHandler(handler)

# Item flag bitmasks (matches BaseClasses.ItemClassification)
_FLAG_PROGRESSION = 0b00001
_FLAG_USEFUL = 0b00010
_FLAG_TRAP = 0b00100


def first_env(*keys: str) -> str | None:
    for key in keys:
        value = os.getenv(key)
        if value:
            return value.strip()
    return None


def item_flag_tag(flags: int) -> str:
    """Return a short tag string for notable item classifications."""
    if flags & _FLAG_PROGRESSION:
        return "[Progression] "
    if flags & _FLAG_TRAP:
        return "[Trap] "
    if flags & _FLAG_USEFUL:
        return "[Useful] "
    return ""


class UnlockPrinterContext(CommonContext):
    tags = CommonContext.tags | {"TextOnly"}
    game = ""
    items_handling = 0b111
    want_slot_data = False

    def __init__(
        self,
        server_address: str,
        slot_name: str,
        password: str | None,
        notify_callback: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        super().__init__(server_address=server_address, password=password)
        self.auth = slot_name
        self._seen_unlocks: set[tuple[int, int, int, int]] = set()
        self._notify_callback = notify_callback

    async def server_auth(self, password_requested: bool = False) -> None:
        if password_requested and not self.password:
            logger.error("Server requested a password but no password is configured.")
            self.disconnected_intentionally = True
            self.exit_event.set()
            return
        await self.send_connect(game="")

    def on_package(self, cmd: str, args: dict) -> None:
        if cmd == "Connected" and self.slot is not None:
            self.game = self.slot_info[self.slot].game
            logger.info(f"Connected as {self.auth} (slot {self.slot}, game {self.game})")

    def on_print_json(self, args: dict) -> None:
        if args.get("type") != "ItemSend":
            return

        receiving = args.get("receiving")
        network_item = self.coerce_network_item(args.get("item"))
        if not isinstance(receiving, int) or network_item is None:
            return

        event_key = (network_item.player, receiving, network_item.item, network_item.location)
        # ItemSend packets can be replayed on reconnect/sync; suppress duplicates in this process.
        if event_key in self._seen_unlocks:
            return
        self._seen_unlocks.add(event_key)

        sender_name = self.player_names.get(network_item.player, f"Slot {network_item.player}")
        receiver_name = self.player_names.get(receiving, f"Slot {receiving}")
        item_name = self.lookup_item_name(network_item.item, receiving)
        location_name = self.lookup_location_name(network_item.location, network_item.player)
        tag = item_flag_tag(network_item.flags)

        if network_item.player == receiving:
            message = f"""{tag}{sender_name} found "{item_name}" for themselves by checking "{location_name}"."""
        else:
            message = f"""{tag}{sender_name} found "{item_name}" for {receiver_name} by checking "{location_name}"."""

        logger.info(message)

        if self._notify_callback is not None:
            asyncio.create_task(self._notify_callback(message))

    def lookup_item_name(self, item_id: int, receiving_slot: int) -> str:
        try:
            return self.item_names.lookup_in_slot(item_id, receiving_slot)
        except Exception:
            return f"Item {item_id}"

    def lookup_location_name(self, location_id: int, sender_slot: int) -> str:
        try:
            return self.location_names.lookup_in_slot(location_id, sender_slot)
        except Exception:
            return f"Location {location_id}"

    @staticmethod
    def coerce_network_item(raw: Any) -> NetworkItem | None:
        if isinstance(raw, NetworkItem):
            return raw

        if isinstance(raw, dict):
            try:
                return NetworkItem(
                    item=int(raw["item"]),
                    location=int(raw["location"]),
                    player=int(raw["player"]),
                    flags=int(raw.get("flags", 0)),
                )
            except (TypeError, ValueError, KeyError):
                return None

        if isinstance(raw, (list, tuple)) and len(raw) >= 3:
            try:
                flags = int(raw[3]) if len(raw) >= 4 else 0
                return NetworkItem(item=int(raw[0]), location=int(raw[1]), player=int(raw[2]), flags=flags)
            except (TypeError, ValueError):
                return None

        return None


# ---------------------------------------------------------------------------
# Active connection state (one connection at a time, global to the process)
# ---------------------------------------------------------------------------

_active_ctx: UnlockPrinterContext | None = None
_active_task: asyncio.Task[None] | None = None


async def _run_connection(ctx: UnlockPrinterContext) -> None:
    ctx.server_task = asyncio.create_task(server_loop(ctx), name="server_loop")
    try:
        await ctx.exit_event.wait()
    finally:
        await ctx.shutdown()


async def _disconnect_current() -> None:
    global _active_ctx, _active_task
    if _active_ctx is not None:
        _active_ctx.disconnected_intentionally = True
        _active_ctx.exit_event.set()
        _active_ctx = None
    if _active_task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(_active_task), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _active_task.cancel()
        _active_task = None


# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------


def main() -> None:
    load_dotenv(SCRIPT_DIR / ".env")
    load_dotenv()

    token = first_env("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN is not set in environment or .env file.")

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    @client.event
    async def on_ready() -> None:
        logger.info(f"Logged in as {client.user} (ID: {client.user.id})")
        await tree.sync()
        logger.info("Slash commands synced.")

    @tree.command(name="connect", description="Connect to an Archipelago session and stream item events here.")
    @app_commands.describe(
        server="Archipelago server address, e.g. 'archipelago.gg:38281'",
        slot="Your slot/player name in the multiworld",
        password="Room password (leave blank if none)",
    )
    async def connect_cmd(
        interaction: discord.Interaction,
        server: str,
        slot: str,
        password: str | None = None,
    ) -> None:
        global _active_ctx, _active_task

        await interaction.response.defer()

        # Tear down any existing connection before starting a new one.
        await _disconnect_current()

        channel = interaction.channel

        async def notify(message: str) -> None:
            try:
                await channel.send(message)
            except discord.Forbidden:
                logger.error(f"Missing permissions to send to channel {channel.id}.")
            except Exception as exc:
                logger.error(f"Failed to send Discord message: {exc}")

        ctx = UnlockPrinterContext(
            server_address=server,
            slot_name=slot,
            password=password or None,
            notify_callback=notify,
        )
        _active_ctx = ctx
        _active_task = asyncio.create_task(_run_connection(ctx), name="ap_connection")

        logger.info(f"Connecting to {server} as {slot} (channel {channel.id})")
        await interaction.followup.send(f"Connecting to `{server}` as `{slot}`\u2026")

    @tree.command(name="disconnect", description="Disconnect from the current Archipelago session.")
    async def disconnect_cmd(interaction: discord.Interaction) -> None:
        if _active_ctx is None:
            await interaction.response.send_message("No active connection.", ephemeral=True)
            return
        await interaction.response.defer()
        await _disconnect_current()
        await interaction.followup.send("Disconnected from Archipelago.")

    try:
        client.run(token)
    except KeyboardInterrupt:
        logger.info("Shutting down Discord bot.")
    except ValueError as exc:
        logger.error(str(exc))


if __name__ == "__main__":
    main()
