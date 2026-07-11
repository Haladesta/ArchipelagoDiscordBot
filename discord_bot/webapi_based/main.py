from dataclasses import dataclass
from typing import Dict, Set, List, Optional, Callable, Awaitable
import random
import discord
from discord import app_commands
import aiohttp
import asyncio
import os
import json
import logging

from aiohttp import ClientSession
from dotenv import load_dotenv

logger = logging.getLogger()
handler = logging.StreamHandler()
handler.setFormatter(
    logging.Formatter('[{asctime}] [{levelname:<8}] {name}: {message}', '%Y-%m-%d %H:%M:%S', style='{')
)
logger.setLevel(logging.INFO)
logger.addHandler(handler)

# Get directory of script file
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)  # Change working directory to script directory

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if DISCORD_BOT_TOKEN is None:
    raise ValueError("DISCORD_BOT_TOKEN not set in .env file")
BASE_URL = os.getenv("ARCHIPELAGO_BASE_URL", "https://archipelago.gg")
ROOM_ID = os.getenv("ARCHIPELAGO_ROOM_ID")
LOCATION_ITEMS_FILE = None
# Find filename in cur dir using pattern "AP_\d+_Spoiler.txt
for file in os.listdir('.'):
    if file.startswith("AP_") and file.endswith("_Spoiler.txt"):
        LOCATION_ITEMS_FILE = file
        break
if not LOCATION_ITEMS_FILE:
    raise FileNotFoundError("Could not find spoiler file in current directory.")

# ARCHIPELAGO_COLORS = ["#eee391", "#c97682", "#75c275", "#ca94c2", "#d9a07d", "#767ebd"]
ARCHIPELAGO_COLORS = [15655825, 13203074, 7717493, 13276354, 14262397, 7765693]

SUBSCRIPTION_FILE = "subscriptions.json"
subscribed_channels: Set[int] = set()
PING_MAPPING_FILE = "ping_mappings.json"
ping_mappings: Dict[str, Set[int]] = {}


@dataclass
class PlayerInfo:
    id: int
    name: str
    game: str
    team: int


@dataclass
class LocationItemInfo:
    location_name: str
    item_name: str
    from_player: str
    to_player: str


@dataclass
class UnlockInfo:
    from_player: str
    to_player: str
    location_name: str
    item_name: str

    def as_string(self) -> str:
        return (
            f"[Unlock] "
            f"{self.from_player} unlocked '{self.item_name}' "
            f"for {self.to_player} by checking '{self.location_name}'."
        )


class ArchipelagoTracker:
    room_id: str
    tracker_id: Optional[int]
    players: Dict[int, PlayerInfo]
    location_id_map: Dict[str, Dict[int, str]]
    item_id_map: Dict[str, Dict[int, str]]
    seen_locations: Dict[int, Set[int]]
    seen_items: Dict[int, Set[int]]
    location_to_item_player: Dict[str, Dict[str, LocationItemInfo]]
    message_callback: Optional[Callable[[List[UnlockInfo]], Awaitable[None]]]

    def __init__(
            self, room_id: str, base_url: str,
            message_callback: Optional[Callable[[List[UnlockInfo]], Awaitable[None]]] = None
    ):
        self.room_id = room_id
        self.base_url = base_url.rstrip('/')
        self.tracker_id = None
        self.players = {}
        self.message_callback = message_callback
        # game_name -> {location_id -> location_name}
        self.location_id_map = {}
        # game_name -> {item_id -> item_name}
        self.item_id_map = {}
        # player_id -> set(location_ids)
        self.seen_locations = {}
        # player_id -> set(item_ids)
        self.seen_items = {}

        self.location_to_item_player = parse_locations_file(LOCATION_ITEMS_FILE)
        logger.info(
            f"Loaded {sum(len(pl) for pl in self.location_to_item_player.values())} "
            f"location-item mappings from spoiler file."
        )

    async def fetch_json(self, session: ClientSession, url: str):
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json()

    async def initialize(self, session: ClientSession):
        logger.info(f"Initializing tracker for room {self.room_id}...")

        # 1. Get Room Status to find Tracker ID and basic player list
        room_url = f"{self.base_url}/api/room_status/{self.room_id}"
        room_data = await self.fetch_json(session, room_url)
        self.tracker_id = room_data.get("tracker")
        if not self.tracker_id:
            raise ValueError("Could not find tracker ID for this room.")

        # Initialize basic player info from room data (Slot Name, Game)
        # AP uses 1-based indexing for slots
        raw_players = room_data.get("players", [])
        for idx, (name, game) in enumerate(raw_players):
            # idx is 0-based, player slots usually start at 1
            player_id = idx + 1
            self.players[player_id] = PlayerInfo(
                id=player_id,
                name=name,
                game=game,
                team=0  # Team info can be filled later if needed
            )
            self.seen_locations[player_id] = set()

        # Print found Players
        logger.info("Found players:")
        for pid, pdata in self.players.items():
            logger.info(f" - ID {pid}: {pdata.name} playing {pdata.game}")

        logger.info(f"Found tracker ID: {self.tracker_id}")

        # 2. Get Static Tracker data for Game Checksums and precise mappings
        static_url = f"{self.base_url}/api/static_tracker/{self.tracker_id}"
        static_data = await self.fetch_json(session, static_url)

        # Update player games if available (more reliable source)
        for p_game in static_data.get("player_game", []):
            pid = p_game["player"]
            if pid in self.players:
                self.players[pid].game = p_game["game"]

        # 3. Fetch Datapackages for required games
        games_data = static_data.get("datapackage", {})
        required_games = {p.game for p in self.players.values()}
        logger.info(f"Fetching datapackages for: {', '.join(required_games)}")
        for game_name, game_info in games_data.items():
            if game_name not in required_games:
                continue

            checksum = game_info.get("checksum")
            if checksum:
                dp_url = f"{self.base_url}/api/datapackage/{checksum}"
                try:
                    dp_data = await self.fetch_json(session, dp_url)

                    # Handle potential variations in API response structure
                    game_data = dp_data
                    if "games" in dp_data and game_name in dp_data["games"]:
                        game_data = dp_data["games"][game_name]

                    loc_name_to_id = game_data.get("location_name_to_id", {})
                    if not loc_name_to_id:
                        logger.warning(f"Warning: No location data found for {game_name}")
                    # We need ID -> Name
                    self.location_id_map[game_name] = {v: k for k, v in loc_name_to_id.items()}
                    logger.info(f"Loaded {len(self.location_id_map[game_name])} locations for {game_name}")

                    item_name_to_id = game_data.get("item_name_to_id", {})
                    if not item_name_to_id:
                        logger.warning(f"Warning: No item data found for {game_name}")
                    self.item_id_map[game_name] = {v: k for k, v in item_name_to_id.items()}
                    logger.info(f"Loaded {len(self.item_id_map[game_name])} items for {game_name}")
                except Exception as e:
                    logger.error(f"Failed to load datapackage for {game_name}: {e}")

        logger.info("Initialization complete.")

    async def poll(self):
        async with aiohttp.ClientSession() as session:
            await self.initialize(session)

            # Fetch initial state to avoid spamming old checks
            logger.info("Fetching initial state...")
            try:
                tracker_url = f"{self.base_url}/api/tracker/{self.tracker_id}"
                data = await self.fetch_json(session, tracker_url)
                for p_check in data.get("player_checks_done", []):
                    player_id = p_check["player"]
                    if player_id in self.players:
                        self.seen_locations[player_id] = set(p_check.get("locations", []))
                for p_items in data.get("player_items_received", []):
                    player_id = p_items["player"]
                    if player_id in self.players:
                        self.seen_items[player_id] = {item[0] for item in p_items.get("items", [])}
                logger.info(f"Initial state loaded:")
                logger.info(f" - Locations: { {pid: len(locs) for pid, locs in self.seen_locations.items()} }")
                logger.info(f" - Items: { {pid: len(items) for pid, items in self.seen_items.items()} }")
            except Exception as e:
                logger.error(f"Failed to fetch initial state: {e}")

            logger.info("Starting polling loop...")
            while True:
                try:
                    tracker_url = f"{self.base_url}/api/tracker/{self.tracker_id}"
                    data = await self.fetch_json(session, tracker_url)

                    # Check for updates
                    for p_check in data.get("player_checks_done", []):
                        player_id = p_check["player"]
                        # Filter out checks only for queried team if needed, assuming team 0 for now
                        if player_id not in self.players:
                            continue

                        current_checks = set(p_check.get("locations", []))
                        previous_checks = self.seen_locations[player_id]
                        new_checks = current_checks - previous_checks
                        if new_checks:
                            new_unlock_infos: List[UnlockInfo] = []
                            player_name = self.players[player_id].name
                            game_name = self.players[player_id].game

                            for loc_id in new_checks:
                                if game_name in self.location_id_map:
                                    loc_name = self.location_id_map[game_name].get(loc_id, f"ID: {loc_id}")
                                else:
                                    loc_name = f"ID: {loc_id}"

                                location_item_info = self.location_to_item_player[player_name].get(loc_name, None)
                                if location_item_info is not None:
                                    if location_item_info.from_player != player_name:
                                        logger.error(
                                            f"WARNING: Mismatch in from_player for location '{loc_name}': "
                                            f"Got '{player_name}', expected '{location_item_info.to_player}'."
                                        )
                                else:
                                    logger.error(f"WARNING: No location-item mapping found for location '{loc_name}'.")
                                new_unlock_infos.append(
                                    UnlockInfo(
                                        from_player=player_name,
                                        to_player=location_item_info.to_player if location_item_info else "Unknown",
                                        location_name=loc_name,
                                        item_name=location_item_info.item_name if location_item_info else "Unknown"
                                    )
                                )

                            if self.message_callback:
                                await self.message_callback(new_unlock_infos)
                            for unlock_info in new_unlock_infos:
                                logger.info(unlock_info.as_string())

                        self.seen_locations[player_id] = current_checks
                except Exception as e:
                    logger.error(f"Error checking tracker: {e}")

                await asyncio.sleep(5)


def parse_locations_file(file_path: str):
    # Playername -> { location_name -> LocationItemInfo }
    location_to_item_player_map: Dict[str, Dict[str, LocationItemInfo]] = {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except OSError as e:
        raise OSError(f"Failed to read locations file: {e}") from e

    in_locations_section = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        elif line.startswith("Locations:"):
            in_locations_section = True
            continue
        elif line.startswith("Playthrough:"):
            break
        elif in_locations_section:
            if not "): " in line:
                logger.warning(f"Warning: Malformed line in locations section: {line}")
                continue  # Run Won (Loud) (Nanz): Run Won (Nanz)
            location_part, item_part = line.split("): ", 1)
            location_split = location_part.rsplit(" (", 1)
            if len(location_split) != 2:
                logger.warning(f"Warning: Malformed location line: {line}")
                continue
            location_name = location_split[0].strip()
            player_from_name = location_split[1].strip()

            item_split = item_part.rsplit(" (", 1)
            if len(item_split) != 2:
                logger.warning(f"Warning: Malformed item line: {line}")
                continue
            item_name = item_split[0].strip()
            player_to_name = item_split[1].strip().strip(')')

            if player_from_name not in location_to_item_player_map:
                location_to_item_player_map[player_from_name] = {}
            location_to_item_player_map[player_from_name][location_name] = LocationItemInfo(
                location_name=location_name,
                item_name=item_name,
                from_player=player_from_name,
                to_player=player_to_name
            )

    return location_to_item_player_map


def load_subscriptions():
    global subscribed_channels
    if not os.path.exists(SUBSCRIPTION_FILE):
        raise FileNotFoundError("No subscription file found!")

    try:
        with open(SUBSCRIPTION_FILE, "r") as f:
            data = json.load(f)
        subscribed_channels = set(data)
        logger.info(f"Loaded {len(subscribed_channels)} subscribed channels.")
    except Exception as e:
        raise RuntimeError(f"Failed to load subscriptions: {e}") from e


def save_subscriptions():
    try:
        with open(SUBSCRIPTION_FILE, "w") as f:
            json.dump(list(subscribed_channels), f)
    except Exception as e:
        logger.info(f"Failed to save subscriptions: {e}")


def load_ping_mappings():
    global ping_mappings
    if not os.path.exists(PING_MAPPING_FILE):
        raise FileNotFoundError("No ping mapping file found!")

    try:
        with open(PING_MAPPING_FILE, "r") as f:
            data = json.load(f)
        # data is expected to be { "player_name": [id1, id2] }
        ping_mappings = {name: set(ids) for name, ids in data.items()}
        logger.info(f"Loaded ping mappings for {len(ping_mappings)} players.")
    except Exception as e:
        raise RuntimeError(f"Failed to load ping mappings: {e}") from e


def save_ping_mappings():
    try:
        # Convert sets to lists for JSON serialization
        data = {
            name: list(ids)
            for name, ids in ping_mappings.items()
        }
        with open(PING_MAPPING_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.info(f"Failed to save ping mappings: {e}")


def main():
    load_subscriptions()
    load_ping_mappings()

    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)

    @tree.command(name="subscribe", description="Subscribe this channel to Archipelago alerts")
    async def subscribe(interaction: discord.Interaction):
        if interaction.channel_id not in subscribed_channels:
            subscribed_channels.add(interaction.channel_id)
            save_subscriptions()
            await interaction.response.send_message(
                f"Channel {interaction.channel.mention} subscribed to alerts!")
        else:
            await interaction.response.send_message(
                f"Channel {interaction.channel.mention} is already subscribed.",
                ephemeral=True
            )

    @tree.command(name="unsubscribe", description="Unsubscribe this channel from Archipelago alerts")
    async def unsubscribe(interaction: discord.Interaction):
        if interaction.channel_id in subscribed_channels:
            subscribed_channels.remove(interaction.channel_id)
            save_subscriptions()
            await interaction.response.send_message(
                f"Channel {interaction.channel.mention} unsubscribed from alerts."
            )
        else:
            await interaction.response.send_message(
                f"Channel {interaction.channel.mention} was not subscribed.",
                ephemeral=True
            )

    @tree.command(name="pingme", description="Link your Discord user to an Archipelago player name for pings")
    async def pingme(interaction: discord.Interaction, player_name: str):
        if player_name not in ping_mappings:
            ping_mappings[player_name] = set()

        ping_mappings[player_name].add(interaction.user.id)
        save_ping_mappings()
        await interaction.response.send_message(
            f"You will now be pinged for **{player_name}**.",
            ephemeral=True
        )

    @tree.command(name="dontpingme", description="Unlink your Discord user from Archipelago unlock pings")
    async def dontpingme(interaction: discord.Interaction):
        user_removed = False
        for player_name, user_ids in list(ping_mappings.items()):
            if interaction.user.id in user_ids:
                user_ids.remove(interaction.user.id)
                user_removed = True
                if len(user_ids) == 0:
                    del ping_mappings[player_name]

        if user_removed:
            save_ping_mappings()
            await interaction.response.send_message(
                f"You will no longer be pinged.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"You were not subscribed to pings.",
                ephemeral=True
            )

    @client.event
    async def on_ready():
        logger.info(f'Logged in as {client.user} (ID: {client.user.id})')
        await client.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(type=discord.ActivityType.watching, name="Tracking unlocks...")
        )

        await tree.sync()
        logger.info("Slash commands synced.")

        async def send_message(unlock_info: List[UnlockInfo]):
            if not subscribed_channels:
                return

            def resolve_player_name(name: str) -> str:
                user_ids = ping_mappings.get(name, None)
                if user_ids:
                    return f"**{name}** ({', '.join(f'<@{uid}>' for uid in user_ids)})"
                else:
                    return f"**{name}**"

            embeds = [
                discord.Embed(
                    description=(
                        f"{resolve_player_name(cur_info.to_player)} received **'{cur_info.item_name}'** "
                        f"from {cur_info.from_player} by checking '{cur_info.location_name}'."
                    ),
                    color=random.choice(ARCHIPELAGO_COLORS)
                )
                for cur_info in unlock_info
            ]

            channels_to_remove = set()
            for channel_id in subscribed_channels:
                channel = client.get_channel(channel_id)
                if not channel:
                    try:
                        channel = await client.fetch_channel(channel_id)
                    except discord.NotFound:
                        logger.info(f"Channel {channel_id} not found. Removing from subscriptions.")
                        channels_to_remove.add(channel_id)
                        continue
                    except Exception as e:
                        logger.info(f"Failed to fetch channel {channel_id}: {e}")
                        continue

                try:
                    await channel.send(embeds=embeds)
                except discord.Forbidden:
                    logger.info(f"Missing permissions to send to channel {channel_id}. Removing.")
                    channels_to_remove.add(channel_id)
                except Exception as e:
                    logger.info(f"Failed to send to channel {channel_id}: {e}")

            if channels_to_remove:
                for cid in channels_to_remove:
                    subscribed_channels.discard(cid)
                save_subscriptions()

        tracker = ArchipelagoTracker(ROOM_ID, BASE_URL, message_callback=send_message)
        asyncio.create_task(tracker.poll())

    client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    if ROOM_ID and ROOM_ID != "place_your_room_id_here":
        main()
    else:
        logger.info("Error: Please set a valid ARCHIPELAGO_ROOM_ID in the .env file")
