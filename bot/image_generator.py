"""Image generation functions for the FPL Discord bot."""

import os
import io
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from bot.logging_config import get_logger

logger = get_logger('image')

# --- File Paths ---
BACKGROUND_IMAGE_PATH = "mobile-pitch-graphic.png"
DREAMTEAM_BACKGROUND_PATH = "dream-pitch-graphic.png"
FONT_PATH = "Geist-Medium.otf"
JERSEYS_DIR = "team_jerseys"
JERSEY_SIZE = (85, 113)  # ~18% smaller than original 104x146, maintains aspect ratio

# --- Layout & Styling ---
NAME_FONT_SIZE = 18
POINTS_FONT_SIZE = 18
CAPTAIN_FONT_SIZE = 22
SUMMARY_FONT_SIZE = 24
POINTS_BOX_EXTRA_PADDING = 4
PLAYER_BOX_WIDTH = 110  # Fixed width to fit "XXXXXXXX..." at font size 20

# --- Shared Layout Settings ---
NAME_BOX_Y_OFFSET = 66      # Offset from player Y to name box
JERSEY_Y_OFFSET = 16        # Offset for jersey paste position
NAME_TEXT_Y_OFFSET = -1     # Name text Y offset within box
POINTS_BOX_COLOR = "#2E0F68"  # Purple
NAME_BOX_COLOR = (0, 0, 0, 200)  # Black, mostly opaque

# --- Glassmorphism Settings ---
GLASS_PADDING_TOP = 8       # Padding above jersey inside glass card
GLASS_BLUR_RADIUS = 10      # Gaussian blur radius
GLASS_TINT = (0, 0, 0, 102) # ~40% opacity black (dark mode style)
GLASS_CORNER_RADIUS = 10    # Rounded top corners


def format_player_price(player):
    """Return player's price as £X.Xm string."""
    return f"£{player.get('now_cost', 0) / 10:.1f}m"


def build_manager_url(entry_id, gameweek=None, base_url=None):
    """Return the URL for a manager's team on our site, or FPL as fallback."""
    if base_url:
        return f"{base_url}/manager/{entry_id}/stats"
    if gameweek:
        return f"https://fantasy.premierleague.com/entry/{entry_id}/event/{gameweek}"
    return f"https://fantasy.premierleague.com/entry/{entry_id}/history/"


def format_manager_link(label, entry_id, gameweek=None, base_url=None):
    """Wrap a label in Markdown linking to the manager's team."""
    url = build_manager_url(entry_id, gameweek, base_url)
    return f"[{label}]({url})"


def get_jersey_filename(team_name: str, is_goalkeeper: bool = False) -> str:
    """Convert FPL API team name to jersey filename."""
    # Handle special cases where API name differs from file name
    name_mapping = {
        "Spurs": "Tottenham",
        "Nott'm Forest": "Nott-Forest",
    }
    mapped_name = name_mapping.get(team_name, team_name)
    # Replace spaces with hyphens
    base_name = mapped_name.replace(" ", "-")

    # Support goalkeeper jerseys (fall back to outfield if not found)
    if is_goalkeeper:
        gk_path = f"{base_name}-GK.png"
        if os.path.exists(os.path.join(JERSEYS_DIR, gk_path)):
            return gk_path

    return f"{base_name}.png"


def load_jersey_image(team_name: str, is_goalkeeper: bool = False, target_height: int = None):
    """
    Load and resize a jersey image for a team.

    Args:
        team_name: The FPL API team name
        is_goalkeeper: Whether to try loading GK jersey first
        target_height: Target height to resize to (maintains aspect ratio)

    Returns:
        PIL Image object or None if not found
    """
    jersey_filename = get_jersey_filename(team_name, is_goalkeeper)
    jersey_path = os.path.join(JERSEYS_DIR, jersey_filename)

    try:
        jersey = Image.open(jersey_path).convert("RGBA")
        if target_height:
            scale = target_height / jersey.height
            new_width = int(jersey.width * scale)
            jersey = jersey.resize((new_width, target_height), Image.LANCZOS)
        return jersey
    except FileNotFoundError:
        logger.warning(f"Jersey not found: {jersey_path}")
        return None


def draw_captain_indicator(background, draw, font, text: str, paste_x: int, paste_y: int,
                          circle_size: int = 28, x_offset: int = 63, y_offset: int = -5):
    """
    Draw a captain/vice-captain indicator circle with text.

    Args:
        background: PIL Image to paste the circle onto
        draw: ImageDraw object for text drawing
        font: Font to use for the indicator text
        text: Text to display ("C", "TC", or "V")
        paste_x: X position of the jersey
        paste_y: Y position of the jersey
        circle_size: Size of the indicator circle (default 28)
        x_offset: X offset from paste_x (default 75)
        y_offset: Y offset from paste_y (default -5)
    """
    # Create antialiased circle using supersampling
    scale = 4
    circle_img = Image.new("RGBA", (circle_size * scale, circle_size * scale), (0, 0, 0, 0))
    circle_draw = ImageDraw.Draw(circle_img)
    circle_draw.ellipse([0, 0, circle_size * scale - 1, circle_size * scale - 1], fill="black")
    circle_img = circle_img.resize((circle_size, circle_size), Image.LANCZOS)

    # Position and paste circle
    circle_x = paste_x + x_offset
    circle_y = paste_y + y_offset
    background.paste(circle_img, (circle_x, circle_y), circle_img)

    # Use smaller font for "TC" so it fits in the circle
    text_font = font
    if text == "TC":
        text_font = ImageFont.truetype(FONT_PATH, CAPTAIN_FONT_SIZE - 6)

    # Draw centered text (with stroke for bold effect)
    text_x = circle_x + circle_size // 2
    text_y = circle_y + circle_size // 2
    # Adjust Y position slightly for "V" to center better
    if text == "V":
        text_y += 1
    draw.text((text_x, text_y), text, font=text_font, fill="white", anchor="mm", stroke_width=1, stroke_fill="white")


def draw_glass_card(background, card_x, card_y, card_w, card_h,
                    blur_radius=GLASS_BLUR_RADIUS, tint=GLASS_TINT,
                    corner_radius=GLASS_CORNER_RADIUS):
    """Draw a glassmorphism card by blurring the background region and applying a dark tint.

    Only top corners are rounded (matching website PlayerPill style).
    """
    # Clamp to image bounds
    x1 = max(0, card_x)
    y1 = max(0, card_y)
    x2 = min(background.width, card_x + card_w)
    y2 = min(background.height, card_y + card_h)

    if x2 <= x1 or y2 <= y1:
        return

    w, h = x2 - x1, y2 - y1

    # Crop the region from the background and blur it
    region = background.crop((x1, y1, x2, y2)).convert("RGBA")
    blurred = region.filter(ImageFilter.GaussianBlur(radius=blur_radius))

    # Apply dark tint overlay
    tint_overlay = Image.new("RGBA", (w, h), tint)
    blurred = Image.alpha_composite(blurred, tint_overlay)

    # Create mask with rounded top corners only
    mask = Image.new("L", (w, h), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=corner_radius, fill=255)
    # Square off bottom corners
    if corner_radius > 0:
        mask_draw.rectangle([0, h - corner_radius, corner_radius, h], fill=255)
        mask_draw.rectangle([w - corner_radius, h - corner_radius, w, h], fill=255)

    # Paste blurred+tinted region back using the mask
    background.paste(blurred, (x1, y1), mask)


def calculate_player_coordinates(picks, all_players, width, height):
    """Calculate x,y coordinates for each player based on their position."""
    starters = [p for p in picks if p['position'] <= 11]
    bench = [p for p in picks if p['position'] > 11]
    bench.sort(key=lambda x: x['position'])  # Ensure bench is ordered

    positions = {1: [], 2: [], 3: [], 4: []}
    for p in starters:
        player_type = all_players[p['element']]['element_type']
        positions[player_type].append(p)

    coords = {}

    # Vertical Layout Y-coordinates (approximate ratios for mobile pitch)
    y_ratios = {1: 0.13, 2: 0.31, 3: 0.50, 4: 0.69}

    # Calculate coordinates for starters
    for pos_type, y_ratio in y_ratios.items():
        players = positions[pos_type]
        if not players:
            continue
        y = int(height * y_ratio)
        for i, p in enumerate(players):
            x = int(width * (i + 0.5) / len(players))
            coords[p['element']] = (x, y)

    # Calculate coordinates for bench (Horizontal row at bottom)
    bench_y = int(height * 0.89)
    for i, p in enumerate(bench):
        x = int(width * (i + 0.5) / len(bench))
        coords[p['element']] = (x, bench_y)
    return coords


def generate_team_image(fpl_data, summary_data, is_finished=False):
    """Generate a team image showing all players with their points."""
    try:
        pitch = Image.open(BACKGROUND_IMAGE_PATH).convert("RGBA")
        base_layer = Image.new("RGBA", pitch.size, "#1A1A1E")
        background = Image.alpha_composite(base_layer, pitch)
        draw = ImageDraw.Draw(background)
        name_font = ImageFont.truetype(FONT_PATH, NAME_FONT_SIZE)
        points_font = ImageFont.truetype(FONT_PATH, POINTS_FONT_SIZE)
        captain_font = ImageFont.truetype(FONT_PATH, CAPTAIN_FONT_SIZE)
        summary_font = ImageFont.truetype(FONT_PATH, SUMMARY_FONT_SIZE)
        name_sample_bbox = draw.textbbox((0, 0), "Agjpqy", font=name_font)
        points_sample_bbox = draw.textbbox((0, 0), "Agjpqy 0123456789", font=points_font)
        fixed_name_box_height = (name_sample_bbox[3] - name_sample_bbox[1]) + 4
        fixed_points_box_height = (points_sample_bbox[3] - points_sample_bbox[1]) + 4
    except Exception as e:
        logger.error(f"Error loading image resources: {e}")
        return None

    all_players = {p['id']: p for p in fpl_data['bootstrap']['elements']}
    all_teams = {t['id']: t for t in fpl_data['bootstrap']['teams']}
    live_points = {p['id']: p['stats'] for p in fpl_data['live']['elements']}
    width, height = background.size
    coordinates = calculate_player_coordinates(fpl_data['picks']['picks'], all_players, width, height)

    # Build fixture lookup per team for unstarted games
    live_fixtures = fpl_data['live'].get('fixtures', [])
    team_fixture_map = {}  # team_id -> fixture
    for fix in live_fixtures:
        team_fixture_map[fix['team_h']] = fix
        team_fixture_map[fix['team_a']] = fix

    def get_fixture_text(team_id):
        """Return fixture text like 'ARS (H)' if the player's game hasn't started, else None."""
        fix = team_fixture_map.get(team_id)
        if not fix or fix.get('started', False):
            return None
        is_home = fix['team_h'] == team_id
        opp_id = fix['team_a'] if is_home else fix['team_h']
        opp_team = all_teams.get(opp_id, {})
        opp_name = opp_team.get('short_name', '???')
        return f"{opp_name} ({'H' if is_home else 'A'})"

    # Determine the final set of scoring players from the provided data
    scoring_picks_data = fpl_data['picks'].get('scoring_picks', [])
    scoring_player_ids = {p['element'] for p in scoring_picks_data}

    for player_pick in fpl_data['picks']['picks']:
        player_id = player_pick['element']
        player_info = all_players[player_id]
        player_name = player_info['web_name']
        base_points = live_points.get(player_id, {}).get('total_points', 0)

        is_starter = player_pick['position'] <= 11
        was_subbed_out = is_starter and player_id not in scoring_player_ids and is_finished
        was_subbed_in = not is_starter and player_id in scoring_player_ids and is_finished

        # Check if this player's game hasn't started yet
        fixture_text = get_fixture_text(player_info['team'])

        # Determine points to display
        display_points = base_points
        scoring_pick_details = next((p for p in scoring_picks_data if p['element'] == player_id), None)

        if scoring_pick_details:
            final_multiplier = scoring_pick_details.get('final_multiplier', 1)
            display_points = base_points * final_multiplier

        if fixture_text:
            points_text = fixture_text
        else:
            points_text = f"{display_points} pts"

        # For subbed out starters, show their base points but they won't be summed
        if was_subbed_out:
            points_text = f"({base_points}) pts"

        # --- Drawing logic ---
        # Get team jersey using utility function
        team_id = player_info['team']
        team_name = all_teams[team_id]['name']
        is_goalkeeper = player_info['element_type'] == 1
        asset_img = load_jersey_image(team_name, is_goalkeeper, target_height=JERSEY_SIZE[1])
        if asset_img is None:
            continue

        x, y = coordinates[player_id]
        paste_x, paste_y = x - asset_img.width // 2, y - asset_img.height // 2 + JERSEY_Y_OFFSET

        # Pre-calculate box positions for glass card
        box_width = PLAYER_BOX_WIDTH
        name_box_x = x - box_width // 2
        name_box_y = y + NAME_BOX_Y_OFFSET
        name_box_height = fixed_name_box_height + POINTS_BOX_EXTRA_PADDING

        # Draw glass card behind jersey + name area
        glass_x = name_box_x
        glass_y = paste_y - GLASS_PADDING_TOP
        glass_h = (name_box_y + name_box_height) - glass_y
        draw_glass_card(background, glass_x, glass_y, box_width, glass_h)

        # Add visual indicators for subs
        if was_subbed_out:
            red_overlay = Image.new("RGBA", asset_img.size, (255, 0, 0, 80))
            asset_img = Image.alpha_composite(asset_img, red_overlay)
        if was_subbed_in:
            green_overlay = Image.new("RGBA", asset_img.size, (0, 255, 0, 80))
            asset_img = Image.alpha_composite(asset_img, green_overlay)

        # Paste jersey on top of glass card
        background.paste(asset_img, (paste_x, paste_y), asset_img)

        name_text = player_name
        if len(name_text) > 9:
            name_text = name_text[:8] + "..."
        name_bbox = draw.textbbox((0, 0), name_text, font=name_font)
        points_bbox = draw.textbbox((0, 0), points_text, font=points_font)
        points_box_height = fixed_points_box_height + POINTS_BOX_EXTRA_PADDING
        points_box_x = name_box_x
        points_box_y = name_box_y + name_box_height

        # Name box (black, rounded top corners, flat bottom)
        r = 5
        draw.rounded_rectangle([name_box_x, name_box_y, name_box_x + box_width, name_box_y + name_box_height], radius=r, fill=NAME_BOX_COLOR)
        draw.rectangle([name_box_x, name_box_y + name_box_height - r, name_box_x + box_width, name_box_y + name_box_height], fill=NAME_BOX_COLOR)
        # Points box (purple for points, grey for upcoming fixture)
        pts_color = "#374151" if fixture_text else POINTS_BOX_COLOR
        draw.rounded_rectangle([points_box_x, points_box_y, points_box_x + box_width, points_box_y + points_box_height], radius=r, fill=pts_color)
        draw.rectangle([points_box_x, points_box_y, points_box_x + box_width, points_box_y + r], fill=pts_color)
        draw.text((x - name_bbox[2] / 2, name_box_y + NAME_TEXT_Y_OFFSET), name_text, font=name_font, fill="white")
        draw.text((x - points_bbox[2] / 2, points_box_y), points_text, font=points_font, fill="white")

        if player_pick['is_captain']:
            active_chip = fpl_data['picks'].get('active_chip')
            captain_text = "TC" if active_chip == '3xc' else "C"
            draw_captain_indicator(background, draw, captain_font, captain_text, paste_x, paste_y)
        elif player_pick['is_vice_captain']:
            draw_captain_indicator(background, draw, captain_font, "V", paste_x, paste_y)

    # Draw Header Info (Team Name, GW Points, Total Points)
    header_y = 20
    left_margin = 20

    # Team Name
    team_name_text = summary_data.get('team_name', 'My Team')
    team_font = ImageFont.truetype(FONT_PATH, 28)
    draw.text((left_margin - 10, header_y - 6), team_name_text, font=team_font, fill="white")

    # Position points info from right side of canvas
    right_margin = 20
    gw_text = f"GW{fpl_data['live'].get('gw', '')} PTS: {summary_data['gw_points']}"
    total_text = f"Total PTS: {summary_data['total_points']}"
    gw_bbox = draw.textbbox((0, 0), gw_text, font=summary_font)
    total_bbox = draw.textbbox((0, 0), total_text, font=summary_font)
    max_text_width = max(gw_bbox[2], total_bbox[2])
    points_x = background.width - max_text_width - right_margin

    # GW Points
    draw.text((points_x, header_y - 14), gw_text, font=summary_font, fill="white")

    # Total Points (below GW points)
    draw.text((points_x, header_y + 12), total_text, font=summary_font, fill="white")

    img_byte_arr = io.BytesIO()
    background.convert("RGB").save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)
    return img_byte_arr


def generate_dreamteam_image(fpl_data, summary_data):
    """Generate dream team image with Player of the Week graphic."""
    try:
        pitch = Image.open(DREAMTEAM_BACKGROUND_PATH).convert("RGBA")
        base_layer = Image.new("RGBA", pitch.size, "#1A1A1E")
        background = Image.alpha_composite(base_layer, pitch)
        draw = ImageDraw.Draw(background)
        name_font = ImageFont.truetype(FONT_PATH, NAME_FONT_SIZE)
        points_font = ImageFont.truetype(FONT_PATH, POINTS_FONT_SIZE)
        summary_font = ImageFont.truetype(FONT_PATH, SUMMARY_FONT_SIZE)
        potw_font = ImageFont.truetype(FONT_PATH, 20)  # Player of the Week font
        name_sample_bbox = draw.textbbox((0, 0), "Agjpqy", font=name_font)
        points_sample_bbox = draw.textbbox((0, 0), "Agjpqy 0123456789", font=points_font)
        fixed_name_box_height = (name_sample_bbox[3] - name_sample_bbox[1]) + 4
        fixed_points_box_height = (points_sample_bbox[3] - points_sample_bbox[1]) + 4
    except Exception as e:
        logger.error(f"Error loading image resources: {e}")
        return None

    all_players = {p['id']: p for p in fpl_data['bootstrap']['elements']}
    all_teams = {t['id']: t for t in fpl_data['bootstrap']['teams']}
    live_points = {p['id']: p['stats']['total_points'] for p in fpl_data['live']['elements']}
    width, height = background.size
    coordinates = calculate_player_coordinates(fpl_data['picks']['picks'], all_players, width, height)

    # Draw players (same as original team image but without captain/vice captain)
    for player_pick in fpl_data['picks']['picks']:
        player_id = player_pick['element']
        player_info = all_players[player_id]
        player_name = player_info['web_name']
        base_points = live_points.get(player_id, 0)
        multiplier = player_pick.get('multiplier', 1)
        is_bench_player = player_pick['position'] > 11 and multiplier == 0
        display_points = base_points if is_bench_player else base_points * multiplier

        # Get team jersey using utility function
        team_id = player_info['team']
        team_name = all_teams[team_id]['name']
        is_goalkeeper = player_info['element_type'] == 1
        asset_img = load_jersey_image(team_name, is_goalkeeper, target_height=JERSEY_SIZE[1])
        if asset_img is None:
            continue

        x, y = coordinates[player_id]
        paste_x, paste_y = x - asset_img.width // 2, y - asset_img.height // 2 + JERSEY_Y_OFFSET

        # Pre-calculate box positions for glass card
        box_width = PLAYER_BOX_WIDTH
        name_box_x = x - box_width // 2
        name_box_y = y + NAME_BOX_Y_OFFSET
        name_box_height = fixed_name_box_height + POINTS_BOX_EXTRA_PADDING

        # Draw glass card behind jersey + name area
        glass_x = name_box_x
        glass_y = paste_y - GLASS_PADDING_TOP
        glass_h = (name_box_y + name_box_height) - glass_y
        draw_glass_card(background, glass_x, glass_y, box_width, glass_h)

        # Paste jersey on top of glass card
        background.paste(asset_img, (paste_x, paste_y), asset_img)

        name_text = player_name
        if len(name_text) > 9:
            name_text = name_text[:8] + "..."
        points_text = f"{display_points} pts"
        name_bbox = draw.textbbox((0, 0), name_text, font=name_font)
        points_bbox = draw.textbbox((0, 0), points_text, font=points_font)
        points_box_height = fixed_points_box_height + POINTS_BOX_EXTRA_PADDING
        points_box_x = name_box_x
        points_box_y = name_box_y + name_box_height

        # Name box (black, rounded top corners, flat bottom)
        r = 5
        draw.rounded_rectangle([name_box_x, name_box_y, name_box_x + box_width, name_box_y + name_box_height], radius=r, fill=NAME_BOX_COLOR)
        draw.rectangle([name_box_x, name_box_y + name_box_height - r, name_box_x + box_width, name_box_y + name_box_height], fill=NAME_BOX_COLOR)
        # Points box (purple, flat top, rounded bottom corners)
        draw.rounded_rectangle([points_box_x, points_box_y, points_box_x + box_width, points_box_y + points_box_height], radius=r, fill=POINTS_BOX_COLOR)
        draw.rectangle([points_box_x, points_box_y, points_box_x + box_width, points_box_y + r], fill=POINTS_BOX_COLOR)
        draw.text((x - name_bbox[2] / 2, name_box_y + NAME_TEXT_Y_OFFSET), name_text, font=name_font, fill="white")
        draw.text((x - points_bbox[2] / 2, points_box_y), points_text, font=points_font, fill="white")

    # Draw Header Info for Dream Team
    header_y = 20
    left_margin = 20

    # League Name (truncate if >= 20 chars)
    league_name = summary_data.get('league_name', 'Dream Team')
    if len(league_name) >= 20:
        league_name = league_name[:18] + "..."
    team_font = ImageFont.truetype(FONT_PATH, 28)
    draw.text((left_margin - 10, header_y - 6), league_name, font=team_font, fill="white")

    # Position points info from right side of canvas
    right_margin = 20
    gw = summary_data.get('gameweek', '')
    dream_text = f"Dream Team GW{gw}"
    gw_points_text = f"GW PTS: {summary_data['total_points']}"
    dream_bbox = draw.textbbox((0, 0), dream_text, font=summary_font)
    gw_bbox = draw.textbbox((0, 0), gw_points_text, font=summary_font)
    max_text_width = max(dream_bbox[2], gw_bbox[2])
    points_x = background.width - max_text_width - right_margin

    draw.text((points_x, header_y - 14), dream_text, font=summary_font, fill="white")
    draw.text((points_x, header_y + 12), gw_points_text, font=summary_font, fill="white")

    # Draw Player of the Week section (centered at bottom)
    potw_data = summary_data['player_of_week']
    potw_player_info = potw_data['player_info']
    potw_name = potw_player_info['web_name']
    potw_points = potw_data['points']

    # POTW fonts
    potw_title_font = ImageFont.truetype(FONT_PATH, 36)  # Larger title
    potw_details_font = ImageFont.truetype(FONT_PATH, 28)  # Smaller details

    # Load jersey for POTW using utility function
    team_id = potw_player_info['team']
    team_name = all_teams[team_id]['name']
    is_goalkeeper = potw_player_info['element_type'] == 1
    potw_img = load_jersey_image(team_name, is_goalkeeper, target_height=JERSEY_SIZE[1])

    # Draw "Player of the Week" title - centered horizontally, higher up
    title_text = "Player of the Week"
    title_bbox = draw.textbbox((0, 0), title_text, font=potw_title_font)
    title_width = title_bbox[2]
    title_x = (width - title_width) // 2
    title_y = int(height * 0.85) - 15  # Adjust this to move title up/down
    draw.text((title_x, title_y), title_text, font=potw_title_font, fill="black")

    # Calculate positioning for details section (name, pts, stats + jersey)
    details_y = title_y + 70  # Adjust this to move details up/down (was 40, now 50 = 10px lower)
    jersey_width = potw_img.width if potw_img else 0

    # Get max width of detail texts
    name_bbox = draw.textbbox((0, 0), potw_name, font=potw_details_font)
    pts_bbox = draw.textbbox((0, 0), f"{potw_points} pts", font=potw_details_font)
    stats_bbox = draw.textbbox((0, 0), f"G: {potw_data['goals']} A: {potw_data['assists']}", font=potw_details_font)
    max_text_width = max(name_bbox[2], pts_bbox[2], stats_bbox[2])

    total_width = max_text_width + 20 + jersey_width
    start_x = (width - total_width) // 2

    # Draw player details (left-aligned within centered section)
    draw.text((start_x, details_y), potw_name, font=potw_details_font, fill="black")
    draw.text((start_x, details_y + 28), f"{potw_points} pts", font=potw_details_font, fill="black")
    draw.text((start_x, details_y + 56), f"G: {potw_data['goals']} A: {potw_data['assists']}", font=potw_details_font, fill="black")

    # Draw jersey to the right of text
    if potw_img:
        jersey_x = start_x + max_text_width + 20
        jersey_y = details_y - 10  # Adjust this to move jersey up/down
        background.paste(potw_img, (jersey_x, jersey_y), potw_img)

    img_byte_arr = io.BytesIO()
    background.convert("RGB").save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)
    return img_byte_arr
