import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
from PIL import Image, ImageDraw, ImageFont
import io
import asyncio
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- CONFIGURATION ---
FPL_LEAGUE_ID = os.getenv("FPL_LEAGUE_ID")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# --- FILE PATHS ---
BACKGROUND_IMAGE_PATH = "pitch-graphic-t77-OTdp.png"
FONT_PATH = "font.ttf"
HEADSHOTS_DIR = "player_headshots"
JERSEYS_DIR = "team_jerseys"

# --- API & DATA ---
BASE_API_URL = "https://fantasy.premierleague.com/api/"
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36'
}

# --- LAYOUT & STYLING (from your original script) ---
PITCH_X_START, PITCH_X_END = 200, 1216
GK_Y, DEF_Y, MID_Y, FWD_Y = 65, 265, 465, 665
BENCH_X, BENCH_Y_START, BENCH_Y_SPACING = 120, 65, 180
SUMMARY_X, SUMMARY_Y_START, SUMMARY_LINE_SPACING = 1300, 40, 35
NAME_FONT_SIZE, POINTS_FONT_SIZE, CAPTAIN_FONT_SIZE, SUMMARY_FONT_SIZE = 22, 20, 22, 24


class FPLBot(commands.Bot):
    """A Discord bot for displaying FPL league and team information."""
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print(f"Synced slash commands for {self.user}.")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print("Bot is ready and online.")

bot = FPLBot()

# --- FPL API HELPER FUNCTIONS (Async) ---
async def fetch_fpl_api(session, url):
    """Fetches data from the FPL API asynchronously."""
    try:
        async with session.get(url, headers=REQUEST_HEADERS) as response:
            if response.status == 200:
                return await response.json()
            else:
                print(f"Error fetching {url}: Status {response.status}")
                return None
    except aiohttp.ClientError as e:
        print(f"Request error for {url}: {e}")
        return None

async def get_current_gameweek(session):
    """Determines the current FPL gameweek."""
    bootstrap_data = await fetch_fpl_api(session, f"{BASE_API_URL}bootstrap-static/")
    if bootstrap_data:
        current_event = next((event for event in bootstrap_data['events'] if event['is_current']), None)
        return current_event['id'] if current_event else None
    return None

async def get_last_completed_gameweek(session):
    """Determines the most recently completed FPL gameweek."""
    bootstrap_data = await fetch_fpl_api(session, f"{BASE_API_URL}bootstrap-static/")
    if bootstrap_data:
        completed_events = [event for event in bootstrap_data['events'] if event['finished']]
        if completed_events:
            return max(completed_events, key=lambda x: x['id'])['id']
    return None

async def get_league_managers(session):
    """Fetches all manager names and IDs for the configured league."""
    league_url = f"{BASE_API_URL}leagues-classic/{FPL_LEAGUE_ID}/standings/"
    league_data = await fetch_fpl_api(session, league_url)
    if league_data and 'standings' in league_data and 'results' in league_data['standings']:
        return {
            manager['player_name']: manager['entry']
            for manager in league_data['standings']['results']
        }
    return {}

# --- NEW REFACTORED HELPER FOR LIVE POINT CALCULATION ---
async def get_live_manager_details(session, manager_entry, current_gw, live_points_map):
    """Fetches picks/history for a manager and calculates their live score, accounting for chips."""
    manager_id = manager_entry['entry']
    picks_task = fetch_fpl_api(session, f"{BASE_API_URL}entry/{manager_id}/event/{current_gw}/picks/")
    history_task = fetch_fpl_api(session, f"{BASE_API_URL}entry/{manager_id}/history/")
    picks_data, history_data = await asyncio.gather(picks_task, history_task)

    if not picks_data or not history_data:
        return None

    # Determine which players' points to count based on active chip
    active_chip = picks_data.get('active_chip')
    players_to_score = []
    if active_chip == 'bboost':
        players_to_score = picks_data['picks']  # Count all 15 players for Bench Boost
    else:
        players_to_score = [p for p in picks_data['picks'] if p['position'] <= 11]  # Count starting 11

    # Calculate points
    live_gw_points = sum(live_points_map.get(p['element'], {}).get('total_points', 0) * p['multiplier'] for p in players_to_score)
    transfer_cost = picks_data['entry_history']['event_transfers_cost']
    final_gw_points = live_gw_points - transfer_cost

    # Calculate total points
    pre_gw_total = 0
    if current_gw > 1:
        prev_gw_history = next((gw for gw in history_data['current'] if gw['event'] == current_gw - 1), None)
        if prev_gw_history:
            pre_gw_total = prev_gw_history['total_points']
    
    live_total_points = pre_gw_total + final_gw_points

    # Calculate players played for the table view (always just the starting XI)
    starters = [p for p in picks_data['picks'] if p['position'] <= 11]
    players_played_count = sum(1 for p in starters if live_points_map.get(p['element'], {}).get('minutes', 0) > 0)

    return {
        "id": manager_id,
        "name": manager_entry['player_name'],
        "live_total_points": live_total_points,
        "final_gw_points": final_gw_points,
        "players_played": players_played_count,
        "picks_data": picks_data
    }

# --- IMAGE GENERATION LOGIC ---
def calculate_player_coordinates(picks, all_players):
    starters = [p for p in picks if p['position'] <= 11]
    bench = [p for p in picks if p['position'] > 11]
    positions = {1: [], 2: [], 3: [], 4: []}
    for p in starters:
        player_type = all_players[p['element']]['element_type']
        positions[player_type].append(p)
    coords = {}
    pitch_width = PITCH_X_END - PITCH_X_START
    if positions[1]:
        coords[positions[1][0]['element']] = ((PITCH_X_START + PITCH_X_END) // 2, GK_Y)
    for pos_type, y_coord in [(2, DEF_Y), (3, MID_Y), (4, FWD_Y)]:
        num_players = len(positions[pos_type])
        for i, p in enumerate(positions[pos_type]):
            x = PITCH_X_START + ((i + 1) * pitch_width) / (num_players + 1)
            coords[p['element']] = (int(x), y_coord)
    for i, p in enumerate(bench):
        coords[p['element']] = (BENCH_X, BENCH_Y_START + (i * BENCH_Y_SPACING))
    return coords

def generate_team_image(fpl_data, summary_data):
    try:
        background = Image.open(BACKGROUND_IMAGE_PATH).convert("RGBA")
        draw = ImageDraw.Draw(background)
        name_font = ImageFont.truetype(FONT_PATH, NAME_FONT_SIZE)
        points_font = ImageFont.truetype(FONT_PATH, POINTS_FONT_SIZE)
        captain_font = ImageFont.truetype(FONT_PATH, CAPTAIN_FONT_SIZE)
        summary_font = ImageFont.truetype(FONT_PATH, SUMMARY_FONT_SIZE)
    except Exception as e:
        print(f"Error loading resources: {e}")
        return None

    all_players = {p['id']: p for p in fpl_data['bootstrap']['elements']}
    all_teams = {t['id']: t for t in fpl_data['bootstrap']['teams']}
    live_points = {p['id']: p['stats']['total_points'] for p in fpl_data['live']['elements']}
    coordinates = calculate_player_coordinates(fpl_data['picks']['picks'], all_players)

    for player_pick in fpl_data['picks']['picks']:
        player_id = player_pick['element']
        player_info = all_players[player_id]
        player_name = player_info['web_name']
        player_points = live_points.get(player_id, 0) * player_pick.get('multiplier', 1)
        asset_img = None
        asset_size = (88, 112)
        headshot_path = os.path.join(HEADSHOTS_DIR, f"{player_name.replace(' ', '_')}_{player_id}.png")
        try:
            asset_img = Image.open(headshot_path).convert("RGBA")
        except FileNotFoundError:
            team_id = player_info['team']
            team_name = all_teams[team_id]['name'].replace(' ', '_')
            kit = 'goalkeeper' if player_info['element_type'] == 1 else 'home'
            jersey_path = os.path.join(JERSEYS_DIR, f"{team_name}_{kit}.webp")
            asset_size = (110, 110)
            try:
                asset_img = Image.open(jersey_path).convert("RGBA")
            except FileNotFoundError:
                continue
        asset_img = asset_img.resize(asset_size, Image.LANCZOS)
        x, y = coordinates[player_id]
        paste_x, paste_y = x - asset_img.width // 2, y - asset_img.height // 2
        background.paste(asset_img, (paste_x, paste_y), asset_img)

        name_text, points_text = player_name, f"{player_points} pts"
        name_bbox = draw.textbbox((0, 0), name_text, font=name_font)
        points_bbox = draw.textbbox((0, 0), points_text, font=points_font)
        box_width = max(name_bbox[2], points_bbox[2]) + 10
        name_box_height = (name_bbox[3] - name_bbox[1]) + 4
        name_box_x = x - box_width // 2
        name_box_y = y + 55
        points_box_height = (points_bbox[3] - points_bbox[1]) + 4
        points_box_x = name_box_x
        points_box_y = name_box_y + name_box_height
        draw.rounded_rectangle([name_box_x, name_box_y, name_box_x + box_width, name_box_y + name_box_height], radius=5, fill=(0, 0, 0, 100))
        draw.rounded_rectangle([points_box_x, points_box_y, points_box_x + box_width, points_box_y + points_box_height], radius=5, fill=(0, 135, 81, 150))
        draw.text((x - name_bbox[2] / 2, name_box_y - 4), name_text, font=name_font, fill="white")
        draw.text((x - points_bbox[2] / 2, points_box_y), points_text, font=points_font, fill="white")

        if player_pick['is_captain']:
            draw.text((paste_x + 80, paste_y - 5), "C", font=captain_font, fill="black", stroke_width=2, stroke_fill="yellow")
        elif player_pick['is_vice_captain']:
            draw.text((paste_x + 80, paste_y - 5), "V", font=captain_font, fill="black", stroke_width=2, stroke_fill="white")

    summary_strings = [f"League Rank: {summary_data['rank']}", f"GW Points: {summary_data['gw_points']}", f"Total Points: {summary_data['total_points']}"]
    for i, text in enumerate(summary_strings):
        y_pos = SUMMARY_Y_START + (i * SUMMARY_LINE_SPACING)
        text_bbox = draw.textbbox((0, 0), text, font=summary_font)
        text_width = text_bbox[2] - text_bbox[0]
        x_pos = SUMMARY_X - text_width
        draw.text((x_pos, y_pos), text, font=summary_font, fill="white", stroke_width=1, stroke_fill="black")

    img_byte_arr = io.BytesIO()
    background.save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)
    return img_byte_arr

def generate_dreamteam_image(fpl_data, summary_data):
    """Generate dream team image with Player of the Week graphic."""
    try:
        background = Image.open(BACKGROUND_IMAGE_PATH).convert("RGBA")
        draw = ImageDraw.Draw(background)
        name_font = ImageFont.truetype(FONT_PATH, NAME_FONT_SIZE)
        points_font = ImageFont.truetype(FONT_PATH, POINTS_FONT_SIZE)
        summary_font = ImageFont.truetype(FONT_PATH, SUMMARY_FONT_SIZE)
        potw_font = ImageFont.truetype(FONT_PATH, 20)  # Player of the Week font
    except Exception as e:
        print(f"Error loading resources: {e}")
        return None

    all_players = {p['id']: p for p in fpl_data['bootstrap']['elements']}
    all_teams = {t['id']: t for t in fpl_data['bootstrap']['teams']}
    live_points = {p['id']: p['stats']['total_points'] for p in fpl_data['live']['elements']}
    coordinates = calculate_player_coordinates(fpl_data['picks']['picks'], all_players)

    # Draw players (same as original team image but without captain/vice captain)
    for player_pick in fpl_data['picks']['picks']:
        player_id = player_pick['element']
        player_info = all_players[player_id]
        player_name = player_info['web_name']
        player_points = live_points.get(player_id, 0) * player_pick.get('multiplier', 1)
        asset_img = None
        asset_size = (88, 112)
        headshot_path = os.path.join(HEADSHOTS_DIR, f"{player_name.replace(' ', '_')}_{player_id}.png")
        try:
            asset_img = Image.open(headshot_path).convert("RGBA")
        except FileNotFoundError:
            team_id = player_info['team']
            team_name = all_teams[team_id]['name'].replace(' ', '_')
            kit = 'goalkeeper' if player_info['element_type'] == 1 else 'home'
            jersey_path = os.path.join(JERSEYS_DIR, f"{team_name}_{kit}.webp")
            asset_size = (110, 110)
            try:
                asset_img = Image.open(jersey_path).convert("RGBA")
            except FileNotFoundError:
                continue
        asset_img = asset_img.resize(asset_size, Image.LANCZOS)
        x, y = coordinates[player_id]
        paste_x, paste_y = x - asset_img.width // 2, y - asset_img.height // 2
        background.paste(asset_img, (paste_x, paste_y), asset_img)

        name_text, points_text = player_name, f"{player_points} pts"
        name_bbox = draw.textbbox((0, 0), name_text, font=name_font)
        points_bbox = draw.textbbox((0, 0), points_text, font=points_font)
        box_width = max(name_bbox[2], points_bbox[2]) + 10
        name_box_height = (name_bbox[3] - name_bbox[1]) + 4
        name_box_x = x - box_width // 2
        name_box_y = y + 55
        points_box_height = (points_bbox[3] - points_bbox[1]) + 4
        points_box_x = name_box_x
        points_box_y = name_box_y + name_box_height
        draw.rounded_rectangle([name_box_x, name_box_y, name_box_x + box_width, name_box_y + name_box_height], radius=5, fill=(0, 0, 0, 100))
        draw.rounded_rectangle([points_box_x, points_box_y, points_box_x + box_width, points_box_y + points_box_height], radius=5, fill=(0, 135, 81, 150))
        draw.text((x - name_bbox[2] / 2, name_box_y - 4), name_text, font=name_font, fill="white")
        draw.text((x - points_bbox[2] / 2, points_box_y), points_text, font=points_font, fill="white")

    # Draw summary info (modified for dream team)
    summary_strings = [f"Dream Team", f"Total: {summary_data['total_points']} pts", f"Gameweek {summary_data['gameweek']}"]
    for i, text in enumerate(summary_strings):
        y_pos = SUMMARY_Y_START + (i * SUMMARY_LINE_SPACING)
        text_bbox = draw.textbbox((0, 0), text, font=summary_font)
        text_width = text_bbox[2] - text_bbox[0]
        x_pos = SUMMARY_X - text_width
        draw.text((x_pos, y_pos), text, font=summary_font, fill="white", stroke_width=1, stroke_fill="black")

    # Draw Player of the Week section
    potw_data = summary_data['player_of_week']
    potw_player_info = potw_data['player_info']
    potw_name = potw_player_info['web_name']
    potw_points = potw_data['points']
    
    # Player of the Week positioning (top left, same Y as goalkeeper)
    potw_x = 140  # Left side of the image
    potw_y = GK_Y - 20  # Same Y axis as goalkeeper, slightly above
    
    # Try to load player headshot for POTW
    potw_headshot_path = os.path.join(HEADSHOTS_DIR, f"{potw_name.replace(' ', '_')}_{potw_data['id']}.png")
    potw_img = None
    try:
        potw_img = Image.open(potw_headshot_path).convert("RGBA")
        potw_img = potw_img.resize((60, 76), Image.LANCZOS)
    except FileNotFoundError:
        # Fallback to jersey if headshot not found
        team_id = potw_player_info['team']
        team_name = all_teams[team_id]['name'].replace(' ', '_')
        kit = 'goalkeeper' if potw_player_info['element_type'] == 1 else 'home'
        jersey_path = os.path.join(JERSEYS_DIR, f"{team_name}_{kit}.webp")
        try:
            potw_img = Image.open(jersey_path).convert("RGBA")
            potw_img = potw_img.resize((60, 60), Image.LANCZOS)
        except FileNotFoundError:
            pass
    
    # Draw POTW background box
    potw_box_width = 220  # Made wider to fit "Player of the Week" text
    potw_box_height = 110
    draw.rounded_rectangle([potw_x, potw_y, potw_x + potw_box_width, potw_y + potw_box_height], 
                          radius=10, fill=(255, 215, 0, 200))  # Gold background
    
    # Draw "Player of the Week" title
    title_text = "Player of the Week"
    title_bbox = draw.textbbox((0, 0), title_text, font=potw_font)
    title_x = potw_x + (potw_box_width - title_bbox[2]) // 2
    draw.text((title_x, potw_y + 5), title_text, font=potw_font, fill="black")
    
    # Draw player image if available
    if potw_img:
        img_x = potw_x + 15
        img_y = potw_y + 30
        background.paste(potw_img, (img_x, img_y), potw_img)
        
        # Draw name and points beside the image
        name_x = img_x + potw_img.width + 15
        draw.text((name_x, img_y), potw_name, font=potw_font, fill="black")
        draw.text((name_x, img_y + 25), f"{potw_points} pts", font=potw_font, fill="black")
        draw.text((name_x, img_y + 50), f"G: {potw_data['goals']} A: {potw_data['assists']}", font=potw_font, fill="black")
    else:
        # Draw text only if no image
        name_x = potw_x + 15
        draw.text((name_x, potw_y + 35), potw_name, font=potw_font, fill="black")
        draw.text((name_x, potw_y + 55), f"{potw_points} pts", font=potw_font, fill="black")
        draw.text((name_x, potw_y + 75), f"Goals: {potw_data['goals']}, Assists: {potw_data['assists']}", font=potw_font, fill="black")

    img_byte_arr = io.BytesIO()
    background.save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)
    return img_byte_arr

# --- DISCORD SLASH COMMANDS ---

@bot.tree.command(name="team", description="Generates an image of a manager's current FPL team.")
@app_commands.describe(manager="Select the manager's team to view.")
async def team(interaction: discord.Interaction, manager: str):
    await interaction.response.defer()
    manager_id = int(manager)

    async with aiohttp.ClientSession() as session:
        current_gw = await get_current_gameweek(session)
        if not current_gw:
            await interaction.followup.send("Could not determine the current gameweek.")
            return

        bootstrap_data = await fetch_fpl_api(session, f"{BASE_API_URL}bootstrap-static/")
        live_data = await fetch_fpl_api(session, f"{BASE_API_URL}event/{current_gw}/live/")
        league_data = await fetch_fpl_api(session, f"{BASE_API_URL}leagues-classic/{FPL_LEAGUE_ID}/standings/")

        if not all([bootstrap_data, live_data, league_data]):
            await interaction.followup.send("Failed to fetch essential FPL data. Please try again later.")
            return

        live_points_map = {p['id']: p['stats'] for p in live_data['elements']}
        
        tasks = [get_live_manager_details(session, mgr, current_gw, live_points_map) for mgr in league_data['standings']['results']]
        all_manager_data = await asyncio.gather(*tasks)
        
        manager_live_scores = [d for d in all_manager_data if d is not None]
        manager_live_scores.sort(key=lambda x: x['live_total_points'], reverse=True)
        
        live_rank = "N/A"
        selected_manager_details = None
        for i, mgr_data in enumerate(manager_live_scores):
            if mgr_data['id'] == manager_id:
                live_rank = i + 1
                selected_manager_details = mgr_data
                break
        
        if not selected_manager_details:
            await interaction.followup.send("Could not calculate live data for the selected manager.")
            return

        summary_data = {
            "rank": live_rank,
            "gw_points": selected_manager_details['final_gw_points'],
            "total_points": selected_manager_details['live_total_points']
        }

        fpl_data_for_image = {
            "bootstrap": bootstrap_data,
            "live": live_data,
            "picks": selected_manager_details['picks_data']
        }
        
        image_bytes = generate_team_image(fpl_data_for_image, summary_data)
        if image_bytes:
            file = discord.File(fp=image_bytes, filename="fpl_team.png")
            await interaction.followup.send(file=file)
        else:
            await interaction.followup.send("Sorry, there was an error creating the team image.")

@team.autocomplete('manager')
async def team_autocomplete(interaction: discord.Interaction, current: str):
    async with aiohttp.ClientSession() as session:
        managers = await get_league_managers(session)
        choices = [app_commands.Choice(name=name, value=str(id)) for name, id in managers.items() if current.lower() in name.lower()]
        return choices[:25]

@bot.tree.command(name="table", description="Displays the live FPL league table.")
async def table(interaction: discord.Interaction):
    await interaction.response.defer()

    async with aiohttp.ClientSession() as session:
        current_gw = await get_current_gameweek(session)
        if not current_gw:
            await interaction.followup.send("Could not determine the current gameweek.")
            return
            
        league_data = await fetch_fpl_api(session, f"{BASE_API_URL}leagues-classic/{FPL_LEAGUE_ID}/standings/")
        live_data = await fetch_fpl_api(session, f"{BASE_API_URL}event/{current_gw}/live/")

        if not league_data or not live_data:
            await interaction.followup.send("Failed to fetch FPL league data. Please try again later.")
            return

        live_points_map = {p['id']: p['stats'] for p in live_data['elements']}
        
        tasks = [get_live_manager_details(session, manager, current_gw, live_points_map) for manager in league_data['standings']['results']]
        manager_details = [res for res in await asyncio.gather(*tasks) if res]

        manager_details.sort(key=lambda x: x['live_total_points'], reverse=True)

        header = f"🏆 {league_data['league']['name']} - Live GW {current_gw} Table 🏆"
        table_content = "```"
        table_content += f"{'Rank':<5} {'Manager':<20} {'GW Pts':<8} {'Total':<8} {'Played':<8}\n"
        table_content += "-" * 52 + "\n"

        for i, manager in enumerate(manager_details):
            rank = i + 1
            table_content += f"{str(rank):<5} {manager['name']:<20.19} {manager['final_gw_points']:<8} {manager['live_total_points']:<8} {manager['players_played']}/11\n"
        
        table_content += "```"
        await interaction.followup.send(f"{header}\n{table_content}")

@bot.tree.command(name="player", description="Shows which managers in the league own a specific player.")
@app_commands.describe(player="Select the player to check ownership for.")
async def player(interaction: discord.Interaction, player: str):
    await interaction.response.defer()
    player_id = int(player)

    async with aiohttp.ClientSession() as session:
        current_gw = await get_current_gameweek(session)
        if not current_gw:
            await interaction.followup.send("Could not determine the current gameweek.")
            return

        bootstrap_data = await fetch_fpl_api(session, f"{BASE_API_URL}bootstrap-static/")
        league_data = await fetch_fpl_api(session, f"{BASE_API_URL}leagues-classic/{FPL_LEAGUE_ID}/standings/")

        if not bootstrap_data or not league_data:
            await interaction.followup.send("Failed to fetch FPL data. Please try again later.")
            return

        all_players = {p['id']: p for p in bootstrap_data['elements']}
        selected_player = all_players.get(player_id)
        
        if not selected_player:
            await interaction.followup.send("Player not found.")
            return

        player_name = f"{selected_player['first_name']} {selected_player['second_name']}"
        
        tasks = []
        for manager in league_data['standings']['results']:
            manager_id = manager['entry']
            tasks.append(fetch_fpl_api(session, f"{BASE_API_URL}entry/{manager_id}/event/{current_gw}/picks/"))
        
        all_picks_data = await asyncio.gather(*tasks)
        
        owners = []
        for i, picks_data in enumerate(all_picks_data):
            if picks_data and 'picks' in picks_data:
                manager_name = league_data['standings']['results'][i]['player_name']
                for pick in picks_data['picks']:
                    if pick['element'] == player_id:
                        # Check if player is on bench (positions 12-15) or starting XI (positions 1-11)
                        if pick['position'] > 11:
                            owners.append(f"{manager_name} **(B)**")  # On bench
                        else:
                            owners.append(manager_name)  # Starting XI
                        break
        
        if owners:
            owner_count = len(owners)
            response = f"**{player_name}** features in **{owner_count}** team{'s' if owner_count != 1 else ''}:\n"
            for owner in owners:
                response += f"• {owner}\n"
        else:
            response = f"**{player_name}** is not owned by any managers in the league."
        
        await interaction.followup.send(response)

@player.autocomplete('player')
async def player_autocomplete(interaction: discord.Interaction, current: str):
    async with aiohttp.ClientSession() as session:
        bootstrap_data = await fetch_fpl_api(session, f"{BASE_API_URL}bootstrap-static/")
        if not bootstrap_data:
            return []
        
        all_players = bootstrap_data['elements']
        choices = []
        
        for player in all_players:
            full_name = f"{player['first_name']} {player['second_name']}"
            web_name = player['web_name']
            if current.lower() in full_name.lower() or current.lower() in web_name.lower():
                display_name = f"{full_name} ({web_name})"
                choices.append(app_commands.Choice(name=display_name, value=str(player['id'])))
        
        return sorted(choices, key=lambda x: x.name)[:25]

def find_optimal_dreamteam(all_squad_players):
    """Find the optimal 11 players following FPL formation rules with tie-breaking."""
    # Separate players by position
    goalkeepers = []
    defenders = []
    midfielders = []
    forwards = []
    
    for player_id, player_data in all_squad_players.items():
        element_type = player_data['element_type']
        # Create sorting key: points (desc), goals (desc), assists (desc), minutes (desc)
        sort_key = (-player_data['points'], -player_data['goals'], -player_data['assists'], -player_data['minutes'])
        
        if element_type == 1:  # GK
            goalkeepers.append((player_id, sort_key))
        elif element_type == 2:  # DEF
            defenders.append((player_id, sort_key))
        elif element_type == 3:  # MID
            midfielders.append((player_id, sort_key))
        elif element_type == 4:  # FWD
            forwards.append((player_id, sort_key))
    
    # Sort each position by the tie-breaking criteria
    goalkeepers.sort(key=lambda x: x[1])
    defenders.sort(key=lambda x: x[1])
    midfielders.sort(key=lambda x: x[1])
    forwards.sort(key=lambda x: x[1])
    
    # Must have at least 1 GK, 3 DEF, 3 MID, 1 FWD
    if (len(goalkeepers) < 1 or len(defenders) < 3 or 
        len(midfielders) < 3 or len(forwards) < 1):
        return None, None
    
    # Try all valid formations and find the one with highest total points
    best_team = None
    best_points = -1
    best_formation = None
    
    # Valid formations: (def_count, mid_count, fwd_count)
    # Must sum to 10 (plus 1 GK = 11 total)
    valid_formations = [
        (3, 5, 2), (3, 4, 3), (4, 5, 1), (4, 4, 2), (4, 3, 3), (5, 4, 1), (5, 3, 2)
    ]
    
    for def_count, mid_count, fwd_count in valid_formations:
        # Check if we have enough players for this formation
        if (def_count <= len(defenders) and 
            mid_count <= len(midfielders) and 
            fwd_count <= len(forwards)):
            
            # Build team for this formation
            team = []
            team.append(goalkeepers[0][0])  # Best GK
            
            # Add best players for each position
            for i in range(def_count):
                team.append(defenders[i][0])
            for i in range(mid_count):
                team.append(midfielders[i][0])
            for i in range(fwd_count):
                team.append(forwards[i][0])
            
            # Calculate total points for this formation
            total_points = sum(all_squad_players[pid]['points'] for pid in team)
            
            if total_points > best_points:
                best_points = total_points
                best_team = team
                best_formation = f"{def_count}-{mid_count}-{fwd_count}"
    
    return best_team, best_formation

@bot.tree.command(name="dreamteam", description="Shows the optimal XI from the league for the most recent completed gameweek.")
async def dreamteam(interaction: discord.Interaction):
    await interaction.response.defer()
    
    async with aiohttp.ClientSession() as session:
        last_completed_gw = await get_last_completed_gameweek(session)
        if not last_completed_gw:
            await interaction.followup.send("Could not determine the last completed gameweek.")
            return

        # Fetch required data
        bootstrap_data = await fetch_fpl_api(session, f"{BASE_API_URL}bootstrap-static/")
        league_data = await fetch_fpl_api(session, f"{BASE_API_URL}leagues-classic/{FPL_LEAGUE_ID}/standings/")
        completed_gw_data = await fetch_fpl_api(session, f"{BASE_API_URL}event/{last_completed_gw}/live/")

        if not all([bootstrap_data, league_data, completed_gw_data]):
            await interaction.followup.send("Failed to fetch FPL data. Please try again later.")
            return

        all_players = {p['id']: p for p in bootstrap_data['elements']}
        completed_gw_stats = {p['id']: p['stats'] for p in completed_gw_data['elements']}
        
        # Get all unique players from all managers' squads for the completed gameweek
        all_squad_players = {}
        tasks = []
        for manager in league_data['standings']['results']:
            manager_id = manager['entry']
            tasks.append(fetch_fpl_api(session, f"{BASE_API_URL}entry/{manager_id}/event/{last_completed_gw}/picks/"))
        
        all_picks_data = await asyncio.gather(*tasks)
        
        for picks_data in all_picks_data:
            if picks_data and 'picks' in picks_data:
                for pick in picks_data['picks']:
                    player_id = pick['element']
                    if player_id not in all_squad_players:
                        player_stats = completed_gw_stats.get(player_id, {})
                        all_squad_players[player_id] = {
                            'id': player_id,
                            'element_type': all_players[player_id]['element_type'],
                            'points': player_stats.get('total_points', 0),
                            'goals': player_stats.get('goals_scored', 0),
                            'assists': player_stats.get('assists', 0),
                            'minutes': player_stats.get('minutes', 0),
                            'player_info': all_players[player_id]
                        }
        
        # Find optimal formation and team
        optimal_team, best_formation = find_optimal_dreamteam(all_squad_players)
        if not optimal_team:
            await interaction.followup.send("Could not create dream team - insufficient players in each position.")
            return
        
        # Calculate total points and find player of the week
        total_points = sum(all_squad_players[pid]['points'] for pid in optimal_team)
        player_of_week = max([all_squad_players[pid] for pid in optimal_team], 
                           key=lambda x: (x['points'], x['goals'], x['assists'], x['minutes']))
        
        # Create mock picks data for image generation
        dream_picks = []
        for i, player_id in enumerate(optimal_team):
            dream_picks.append({
                'element': player_id,
                'position': i + 1,
                'multiplier': 1,
                'is_captain': False,
                'is_vice_captain': False
            })
        
        # Prepare data for image generation
        summary_data = {
            "formation": best_formation,
            "total_points": total_points,
            "gameweek": last_completed_gw,
            "player_of_week": player_of_week
        }

        fpl_data_for_image = {
            "bootstrap": bootstrap_data,
            "live": completed_gw_data,
            "picks": {"picks": dream_picks}
        }
        
        # Generate image
        image_bytes = generate_dreamteam_image(fpl_data_for_image, summary_data)
        if image_bytes:
            file = discord.File(fp=image_bytes, filename="fpl_dreamteam.png")
            await interaction.followup.send(f"🌟 **Dream Team for GW {last_completed_gw}** 🌟", file=file)
        else:
            await interaction.followup.send("Sorry, there was an error creating the dream team image.")

@bot.tree.command(name="captains", description="Shows which player each manager captained for the current gameweek.")
async def captains(interaction: discord.Interaction):
    await interaction.response.defer()

    async with aiohttp.ClientSession() as session:
        current_gw = await get_current_gameweek(session)
        if not current_gw:
            await interaction.followup.send("Could not determine the current gameweek.")
            return

        bootstrap_data = await fetch_fpl_api(session, f"{BASE_API_URL}bootstrap-static/")
        league_data = await fetch_fpl_api(session, f"{BASE_API_URL}leagues-classic/{FPL_LEAGUE_ID}/standings/")

        if not bootstrap_data or not league_data:
            await interaction.followup.send("Failed to fetch FPL data. Please try again later.")
            return

        all_players = {p['id']: p for p in bootstrap_data['elements']}
        
        # Get all managers' picks for current gameweek
        tasks = []
        for manager in league_data['standings']['results']:
            manager_id = manager['entry']
            tasks.append(fetch_fpl_api(session, f"{BASE_API_URL}entry/{manager_id}/event/{current_gw}/picks/"))
        
        all_picks_data = await asyncio.gather(*tasks)
        
        captain_info = []
        for i, picks_data in enumerate(all_picks_data):
            if picks_data and 'picks' in picks_data:
                manager_name = league_data['standings']['results'][i]['player_name']
                
                # Find the captain (is_captain = True)
                captain_pick = None
                for pick in picks_data['picks']:
                    if pick['is_captain']:
                        captain_pick = pick
                        break
                
                if captain_pick:
                    captain_id = captain_pick['element']
                    captain_player_info = all_players[captain_id]
                    captain_name = f"{captain_player_info['first_name']} {captain_player_info['second_name']}"
                    captain_info.append((manager_name, captain_name))
        
        if captain_info:
            response = f"**Captain choices for GW {current_gw}:**\n\n"
            for manager_name, captain_name in captain_info:
                response += f"{manager_name} - **{captain_name}**\n"
        else:
            response = "No captain information found for the current gameweek."
        
        await interaction.followup.send(response)


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("!!! ERROR: DISCORD_BOT_TOKEN not found in .env file. Please create a .env file with your bot token.")
    elif not FPL_LEAGUE_ID:
        print("!!! ERROR: FPL_LEAGUE_ID not found in .env file. Please create a .env file with your league ID.")
    else:
        bot.run(DISCORD_BOT_TOKEN)