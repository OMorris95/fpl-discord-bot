"""Database operations for the FPL Discord bot."""

import sqlite3
from pathlib import Path

DB_PATH = Path("config/fpl_bot.db")


def init_database():
    """Initializes the database and creates/migrates tables if they don't exist."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()

        # Check if league_teams has the old discord_user_id column
        cur.execute("PRAGMA table_info(league_teams)")
        columns = [row[1] for row in cur.fetchall()]
        if 'discord_user_id' in columns:
            print("Old schema detected. Migrating league_teams table...")
            cur.execute("CREATE TABLE IF NOT EXISTS league_teams_new (fpl_team_id INTEGER PRIMARY KEY, league_id INTEGER NOT NULL, team_name TEXT NOT NULL, manager_name TEXT NOT NULL)")
            cur.execute("INSERT INTO league_teams_new (fpl_team_id, league_id, team_name, manager_name) SELECT fpl_team_id, league_id, team_name, manager_name FROM league_teams")
            cur.execute("DROP TABLE league_teams")
            cur.execute("ALTER TABLE league_teams_new RENAME TO league_teams")
            print("Migration complete.")

        # Ensure league_teams table exists (for fresh setups)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS league_teams (
                fpl_team_id INTEGER PRIMARY KEY,
                league_id INTEGER NOT NULL,
                team_name TEXT NOT NULL,
                manager_name TEXT NOT NULL
            )
        """)

        # Create the new user_links table for per-server linking
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_links (
                guild_id TEXT NOT NULL,
                discord_user_id TEXT NOT NULL,
                fpl_team_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, discord_user_id),
                UNIQUE (guild_id, fpl_team_id)
            )
        """)

        # Create and migrate goal_subscriptions table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS goal_subscriptions (
                channel_id TEXT PRIMARY KEY,
                league_id INTEGER NOT NULL,
                transfer_alerts_enabled BOOLEAN NOT NULL DEFAULT 0
            )
        """)
        # This handles migration for older versions that didn't have the new column
        cur.execute("PRAGMA table_info(goal_subscriptions)")
        columns = [row[1] for row in cur.fetchall()]
        if 'transfer_alerts_enabled' not in columns:
            print("Migrating goal_subscriptions table for transfer alerts...")
            cur.execute("ALTER TABLE goal_subscriptions ADD COLUMN transfer_alerts_enabled BOOLEAN NOT NULL DEFAULT 0")
            print("Migration complete.")

        con.commit()


def upsert_league_teams(league_id, teams):
    """Inserts or updates team information in the database."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        for team in teams:
            cur.execute("""
                INSERT INTO league_teams (fpl_team_id, league_id, team_name, manager_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(fpl_team_id) DO UPDATE SET
                    team_name = excluded.team_name,
                    manager_name = excluded.manager_name
            """, (team['entry'], league_id, team['entry_name'], team['player_name']))
        con.commit()


def get_fpl_id_for_user(guild_id: int, user_id: int):
    """Gets the FPL team ID linked to a Discord user in a specific guild."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("SELECT fpl_team_id FROM user_links WHERE guild_id = ? AND discord_user_id = ?", (str(guild_id), str(user_id)))
        result = cur.fetchone()
        return result[0] if result else None


def get_linked_user_for_team(guild_id: int, fpl_team_id: int):
    """Gets the Discord user ID linked to an FPL team in a specific guild."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("SELECT discord_user_id FROM user_links WHERE guild_id = ? AND fpl_team_id = ?", (str(guild_id), fpl_team_id))
        result = cur.fetchone()
        return result[0] if result else None


def link_user_to_team(guild_id: int, user_id: int, fpl_team_id: int):
    """Links a Discord user to an FPL team in a specific guild, overwriting any previous link for that user in that guild."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("INSERT OR REPLACE INTO user_links (guild_id, discord_user_id, fpl_team_id) VALUES (?, ?, ?)", (str(guild_id), str(user_id), fpl_team_id))
        con.commit()


def get_unclaimed_teams(league_id: int, guild_id: int, search_term: str):
    """Gets a list of teams in a league that are not claimed in the specific guild."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        # Find all teams in the league that are NOT in the user_links table for the current guild
        cur.execute("""
            SELECT fpl_team_id, team_name, manager_name
            FROM league_teams
            WHERE league_id = ?
              AND (team_name LIKE ? OR manager_name LIKE ?)
              AND fpl_team_id NOT IN (
                SELECT fpl_team_id FROM user_links WHERE guild_id = ?
              )
            LIMIT 25
        """, (league_id, f"%{search_term}%", f"%{search_term}%", str(guild_id)))
        return cur.fetchall()


def get_all_teams_for_autocomplete(league_id: int, search_term: str):
    """Gets a list of all teams for autocomplete."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("""
            SELECT fpl_team_id, team_name, manager_name FROM league_teams
            WHERE league_id = ? AND (team_name LIKE ? OR manager_name LIKE ?)
            LIMIT 25
        """, (league_id, f"%{search_term}%", f"%{search_term}%"))
        return cur.fetchall()


def get_team_by_fpl_id(fpl_team_id: int):
    """Gets all details for a specific FPL team."""
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT * FROM league_teams WHERE fpl_team_id = ?", (fpl_team_id,))
        return cur.fetchone()


def get_linked_users(guild_id: int, league_id: int):
    """Gets a list of all FPL teams that are linked to a Discord user in a specific guild."""
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("""
            SELECT T.fpl_team_id, L.discord_user_id, T.manager_name
            FROM league_teams T
            INNER JOIN user_links L ON T.fpl_team_id = L.fpl_team_id
            WHERE T.league_id = ? AND L.guild_id = ?
        """, (league_id, str(guild_id)))
        return cur.fetchall()


def get_all_league_teams(guild_id: int, league_id: int):
    """Gets a list of all teams for a league, including the linked discord user if one exists for the guild."""
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("""
            SELECT T.fpl_team_id, L.discord_user_id, T.manager_name
            FROM league_teams T
            LEFT JOIN user_links L ON T.fpl_team_id = L.fpl_team_id AND L.guild_id = ?
            WHERE T.league_id = ?
        """, (str(guild_id), league_id))
        return cur.fetchall()


def is_goal_subscribed(channel_id: int):
    """Checks if a channel is subscribed to goal alerts."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM goal_subscriptions WHERE channel_id = ?", (str(channel_id),))
        return cur.fetchone() is not None


def add_goal_subscription(channel_id: int, league_id: int):
    """Adds a channel to the goal alert subscription list."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("INSERT INTO goal_subscriptions (channel_id, league_id) VALUES (?, ?)", (str(channel_id), league_id))
        con.commit()


def remove_goal_subscription(channel_id: int):
    """Removes a channel from the goal alert subscription list."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("DELETE FROM goal_subscriptions WHERE channel_id = ?", (str(channel_id),))
        con.commit()


def get_all_goal_subscriptions():
    """Gets all channel IDs and their league IDs subscribed to goal alerts."""
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT channel_id, league_id, transfer_alerts_enabled FROM goal_subscriptions")
        return cur.fetchall()


def is_transfer_alert_subscribed(channel_id: int):
    """Checks if a channel is subscribed to transfer flop alerts."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("SELECT transfer_alerts_enabled FROM goal_subscriptions WHERE channel_id = ?", (str(channel_id),))
        result = cur.fetchone()
        return result[0] if result and result[0] else False


def set_transfer_alert_subscription(channel_id: int, status: bool):
    """Sets the transfer alert subscription status for a channel."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("UPDATE goal_subscriptions SET transfer_alerts_enabled = ? WHERE channel_id = ?", (status, str(channel_id)))
        con.commit()
