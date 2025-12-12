FPL Discord Bot
===============

A Discord bot that brings Fantasy Premier League data straight into your server. It generates rich team images, tracks live league tables, highlights transfers, and more – all powered by the official FPL API.

Key Features
------------
- Multi-league support: configure a league per server or per channel via `/setleague`.
- Cached data per gameweek to minimise API calls while keeping live data fresh where needed.
- Auto-generated graphics for `/team` and `/dreamteam`, complete with player headshots, kit fallbacks, and Player of the Week cards.
- Live league table, transfers, captain lists, and ownership lookups for any manager or player.

Requirements
------------
- Python 3.10+
- A Discord bot token with the necessary application commands enabled
- An FPL league you administer or have access to
- Pillow and other dependencies listed in `requirements.txt`

Setup
-----
1. Clone the repo and install dependencies:
   ```bash
   git clone https://github.com/<you>/fpl-discord-bot.git
   cd fpl-discord-bot
   python -m venv .venv
   .venv\Scripts\activate  # or source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Create a `.env` file (copy `.env.example`) and set `DISCORD_BOT_TOKEN=<your token>`.
3. Download assets once:
   ```bash
   python download_fpl_assets.py
   ```
   This fetches player headshots and kit images into `player_headshots/` and `team_jerseys/`.
4. Ensure `config/` and `cache/` directories exist (they will be created automatically when the bot runs, but you can create them manually if deploying to another host).
5. Run the bot:
   ```bash
   python fpl_discord_bot.py
   ```

First-Time Configuration
------------------------
After inviting the bot to a server, an admin must run `/setleague`:
- `league_id`: numeric FPL league id (from the official site URL).
- `scope`: `server` (default) applies to all channels; `channel` scopes it to the current channel.

Slash Commands
--------------
- `/setleague` – Assign an FPL league to the server or current channel. Required before other commands will work.
- `/team` – Generates an image of the selected manager’s current team, with live points and summary info.
- `/table` – Displays the live league table (top 25) with current GW points and players played. Shows a link if more managers exist.
- `/dreamteam` – Builds an “optimal XI” for the last completed gameweek, including a Player of the Week highlight.
- `/transfers` – Lists every manager’s transfers for the current gameweek, including chip usage and point hits.
- `/captains` – Shows each manager’s captain choice for the current GW.
- `/player` – Lists which managers own a specific player, noting bench vs. starting XI.
- Autocomplete: `/team` manager selection and `/player` names both support autocomplete pulled from the configured league/players.

Maintenance Notes
-----------------
- Cached API responses are stored under `cache/` per league/gameweek and automatically refreshed when a new GW starts. Live-only views (e.g. `/table`) bypass the cache for fresh data.
- League mappings live in `config/league_config.json`. Copy this file when deploying so the bot remembers each server/channel’s league.
- Asset folders (`player_headshots/`, `team_jerseys/`) must be present on whatever machine runs the bot.

Screenshots
-----------
Place screenshots of `/team`, `/dreamteam`, `/table`, etc. here to show the outputs once you have them.
