from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import sys
from typing import Any

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


@dataclass(frozen=True)
class BotSettings:
    server_address: str
    slot_name: str
    password: str | None


def first_env(*keys: str) -> str | None:
    for key in keys:
        value = os.getenv(key)
        if value:
            return value.strip()
    return None


def load_settings() -> BotSettings:
    load_dotenv(SCRIPT_DIR / ".env")
    load_dotenv()

    server_address = first_env("ARCHIPELAGO_SERVER", "ARCHIPELAGO_ADDRESS", "ARCHIPELAGO_URL")
    slot_name = first_env("ARCHIPELAGO_SLOT_NAME", "ARCHIPELAGO_SLOT", "ARCHIPELAGO_NAME")
    password = first_env("ARCHIPELAGO_PASSWORD")

    missing_keys: list[str] = []
    if not server_address:
        missing_keys.append("ARCHIPELAGO_SERVER")
    if not slot_name:
        missing_keys.append("ARCHIPELAGO_SLOT_NAME")

    if missing_keys:
        joined = ", ".join(missing_keys)
        raise ValueError(f"Missing required .env values: {joined}")

    assert server_address is not None
    assert slot_name is not None

    return BotSettings(server_address=server_address, slot_name=slot_name, password=password)


class UnlockPrinterContext(CommonContext):
    tags = CommonContext.tags | {"TextOnly"}
    game = ""
    items_handling = 0b111
    want_slot_data = False

    def __init__(self, server_address: str, slot_name: str, password: str | None) -> None:
        super().__init__(server_address=server_address, password=password)
        self.auth = slot_name
        self._seen_unlocks: set[tuple[int, int, int, int]] = set()

    async def server_auth(self, password_requested: bool = False) -> None:
        if password_requested and not self.password:
            logger.error("Server requested a password but ARCHIPELAGO_PASSWORD is not set.")
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
        # ItemSend packets can be replayed around reconnect/sync; suppress duplicates in this process.
        if event_key in self._seen_unlocks:
            return
        self._seen_unlocks.add(event_key)

        sender_name = self.player_names.get(network_item.player, f"Slot {network_item.player}")
        receiver_name = self.player_names.get(receiving, f"Slot {receiving}")
        item_name = self.lookup_item_name(network_item.item, receiving)
        location_name = self.lookup_location_name(network_item.location, network_item.player)

        if network_item.player == receiving:
            logger.info(f"""{sender_name} found "{item_name}" for themselves at "{location_name}".""")
        else:
            logger.info(f"""{sender_name} sent "{item_name}" to {receiver_name} by checking "{location_name}".""")

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


async def run() -> None:
    settings = load_settings()
    logger.info(f"Connecting to {settings.server_address} as {settings.slot_name}")

    ctx = UnlockPrinterContext(
        server_address=settings.server_address,
        slot_name=settings.slot_name,
        password=settings.password,
    )
    ctx.server_task = asyncio.create_task(server_loop(ctx), name="server loop")

    try:
        await ctx.exit_event.wait()
    finally:
        await ctx.shutdown()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Shutting down websocket unlock tracker.")
    except ValueError as exc:
        logger.error(str(exc))


if __name__ == "__main__":
    main()
