from __future__ import annotations

import asyncio
import json
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


# ---------------------------------------------------------------------------
# Ping mappings  (slot name -> set of Discord user IDs)
# ---------------------------------------------------------------------------

_ping_mappings: dict[str, set[int]] = {}
_PING_MAPPINGS_FILE = SCRIPT_DIR / "ping_mappings.json"


def _load_ping_mappings() -> None:
    global _ping_mappings
    if not _PING_MAPPINGS_FILE.exists():
        return
    try:
        data: dict[str, list[int]] = json.loads(_PING_MAPPINGS_FILE.read_text(encoding="utf-8"))
        _ping_mappings = {name: set(ids) for name, ids in data.items()}
        logger.info(f"Loaded ping mappings for {len(_ping_mappings)} slot(s).")
    except Exception as exc:
        logger.error(f"Failed to load ping mappings: {exc}")


def _save_ping_mappings() -> None:
    try:
        data = {name: list(ids) for name, ids in _ping_mappings.items() if ids}
        _PING_MAPPINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.error(f"Failed to save ping mappings: {exc}")


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
        # second arg is the receiving slot's display name (for ping resolution)
        notify_callback: Callable[[str, str], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        super().__init__(server_address=server_address, password=password)
        self.auth = slot_name
        self._seen_unlocks: set[tuple[int, int, int, int]] = set()
        self._notify_callback = notify_callback
        self._connected_event: asyncio.Event = asyncio.Event()
        self._failure_event: asyncio.Event = asyncio.Event()
        self._failure_reason: str = ""

    def handle_connection_loss(self, msg: str) -> None:
        """Override to immediately signal failure to any waiting /connect handler."""
        super().handle_connection_loss(msg)
        if not self._connected_event.is_set():
            self._failure_reason = msg
            self._failure_event.set()

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
            self._connected_event.set()

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
        actual_receiver_name = receiver_name  # keep unmodified for ping lookup
        item_name = self.lookup_item_name(network_item.item, receiving)
        location_name = self.lookup_location_name(network_item.location, network_item.player)
        tag = item_flag_tag(network_item.flags)

        if network_item.player == receiving:
            receiver_name = "themselves"
        if network_item.flags & _FLAG_TRAP:
            message = f"""{sender_name} sent a trap "{item_name}" to {receiver_name} by checking "{location_name}"."""
        else:
            # removed {tag} for now
            message = f"""{sender_name} found "{item_name}" for {receiver_name} by checking "{location_name}"."""

        logger.info(message)

        if self._notify_callback is not None:
            asyncio.create_task(self._notify_callback(message, actual_receiver_name))

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

        async def notify(message: str, receiver_slot_name: str) -> None:
            user_ids = _ping_mappings.get(receiver_slot_name, set())
            mentions = " ".join(f"<@{uid}>" for uid in user_ids)
            full_message = f"{message} ({mentions})" if mentions else message
            try:
                await channel.send(full_message)
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

        # Wait a few seconds for a successful login, an explicit failure, or an early exit.
        _CONNECT_TIMEOUT = 10.0
        connected_fut = asyncio.ensure_future(ctx._connected_event.wait())
        failure_fut = asyncio.ensure_future(ctx._failure_event.wait())
        exit_fut = asyncio.ensure_future(ctx.exit_event.wait())
        done, pending = await asyncio.wait(
            {connected_fut, failure_fut, exit_fut},
            timeout=_CONNECT_TIMEOUT,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for fut in pending:
            fut.cancel()

        if ctx._connected_event.is_set():
            await interaction.followup.send(
                f"✅ Connected to `{server}` as `{slot}` (game: {ctx.game or 'unknown'})."
            )
        elif ctx._failure_event.is_set():
            reason = ctx._failure_reason or "Unknown error."
            await interaction.followup.send(
                f"❌ Failed to connect to `{server}` as `{slot}`.\n> {reason}"
            )
        elif ctx.exit_event.is_set():
            await interaction.followup.send(
                f"❌ Connection to `{server}` as `{slot}` was aborted."
            )
        else:
            await interaction.followup.send(
                f"⏳ Still connecting to `{server}` as `{slot}` — no response within {_CONNECT_TIMEOUT:.0f} s. "
                "Events will be posted here if the connection succeeds later."
            )

    @tree.command(name="disconnect", description="Disconnect from the current Archipelago session.")
    async def disconnect_cmd(interaction: discord.Interaction) -> None:
        if _active_ctx is None:
            await interaction.response.send_message("No active connection.", ephemeral=True)
            return
        await interaction.response.defer()
        await _disconnect_current()
        await interaction.followup.send("Disconnected from Archipelago.")

    @tree.command(name="subscribe", description="Ping me whenever a certain slot receives an item.")
    @app_commands.describe(slot_name="Archipelago slot/player name to watch.")
    async def subscribe_cmd(interaction: discord.Interaction, slot_name: str) -> None:
        _ping_mappings.setdefault(slot_name, set()).add(interaction.user.id)
        _save_ping_mappings()
        await interaction.response.send_message(
            f"✅ You will be pinged for items received by **{slot_name}**. "
            f"Please check the spelling, this does not check if the slot exists.",
            ephemeral=True
        )

    @tree.command(name="unsubscribe", description="Stop being pinged for a slot (or all slots).")
    @app_commands.describe(slot_name="Slot to unsubscribe from. Leave blank to remove all your subscriptions.")
    async def unsubscribe_cmd(interaction: discord.Interaction, slot_name: str | None = None) -> None:
        removed: list[str] = []
        targets = [slot_name] if slot_name else list(_ping_mappings.keys())
        for name in targets:
            if interaction.user.id in _ping_mappings.get(name, set()):
                _ping_mappings[name].discard(interaction.user.id)
                if not _ping_mappings[name]:
                    del _ping_mappings[name]
                removed.append(name)
        if removed:
            _save_ping_mappings()
            slots_str = ", ".join(f"**{n}**" for n in removed)
            await interaction.response.send_message(
                f"✅ Removed your ping subscription(s) for: {slots_str}.", ephemeral=True
            )
        else:
            await interaction.response.send_message("You had no matching subscriptions.", ephemeral=True)

    _load_ping_mappings()

    try:
        client.run(token)
    except KeyboardInterrupt:
        logger.info("Shutting down Discord bot.")
    except ValueError as exc:
        logger.error(str(exc))


if __name__ == "__main__":
    main()
