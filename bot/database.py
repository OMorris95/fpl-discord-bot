"""Database operations for the FPL Discord bot."""

import sqlite3
from pathlib import Path

from bot.logging_config import get_logger

logger = get_logger('database')

DB_PATH = Path("config/fpl_bot.db")


def init_database():
    """Initializes the database and creates/migrates tables if they don't exist."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()

        # Check if league_teams has the old discord_user_id column
        cur.execute("PRAGMA table_info(league_teams)")
        columns = [row[1] for row in cur.fetchall()]
        if 'discord_user_id' in columns:
            logger.info("Old schema detected. Migrating league_teams table...")
            cur.execute("CREATE TABLE IF NOT EXISTS league_teams_new (fpl_team_id INTEGER PRIMARY KEY, league_id INTEGER NOT NULL, team_name TEXT NOT NULL, manager_name TEXT NOT NULL)")
            cur.execute("INSERT INTO league_teams_new (fpl_team_id, league_id, team_name, manager_name) SELECT fpl_team_id, league_id, team_name, manager_name FROM league_teams")
            cur.execute("DROP TABLE league_teams")
            cur.execute("ALTER TABLE league_teams_new RENAME TO league_teams")
            logger.info("Migration complete.")

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
        # Migrations for goal_subscriptions columns
        cur.execute("PRAGMA table_info(goal_subscriptions)")
        columns = [row[1] for row in cur.fetchall()]
        if 'transfer_alerts_enabled' not in columns:
            logger.info("Migrating goal_subscriptions table for transfer alerts...")
            cur.execute("ALTER TABLE goal_subscriptions ADD COLUMN transfer_alerts_enabled BOOLEAN NOT NULL DEFAULT 0")
            logger.info("Migration complete.")
        if 'auto_post_gw' not in columns:
            logger.info("Migrating goal_subscriptions table for auto-post support...")
            cur.execute("ALTER TABLE goal_subscriptions ADD COLUMN auto_post_gw BOOLEAN NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE goal_subscriptions ADD COLUMN auto_post_recap BOOLEAN NOT NULL DEFAULT 0")
            logger.info("Migration complete.")

        # Bot state table (persists auto-post tracking across restarts)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create indexes for frequently queried columns
        cur.execute("CREATE INDEX IF NOT EXISTS idx_league_teams_league_id ON league_teams(league_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_links_guild_id ON user_links(guild_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_links_fpl_team_id ON user_links(fpl_team_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_goal_subscriptions_league_id ON goal_subscriptions(league_id)")

        con.commit()


def upsert_league_teams(league_id, teams):
    """Inserts or updates team information in the database."""
    try:
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
    except sqlite3.Error as e:
        logger.error(f"Database error in upsert_league_teams: {e}")
        raise


def get_fpl_id_for_user(guild_id: int, user_id: int):
    """Gets the FPL team ID linked to a Discord user in a specific guild."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("SELECT fpl_team_id FROM user_links WHERE guild_id = ? AND discord_user_id = ?", (str(guild_id), str(user_id)))
            result = cur.fetchone()
            return result[0] if result else None
    except sqlite3.Error as e:
        logger.error(f"Database error in get_fpl_id_for_user: {e}")
        return None


def get_linked_user_for_team(guild_id: int, fpl_team_id: int):
    """Gets the Discord user ID linked to an FPL team in a specific guild."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("SELECT discord_user_id FROM user_links WHERE guild_id = ? AND fpl_team_id = ?", (str(guild_id), fpl_team_id))
            result = cur.fetchone()
            return result[0] if result else None
    except sqlite3.Error as e:
        logger.error(f"Database error in get_linked_user_for_team: {e}")
        return None


def link_user_to_team(guild_id: int, user_id: int, fpl_team_id: int):
    """Links a Discord user to an FPL team in a specific guild, overwriting any previous link for that user in that guild."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("INSERT OR REPLACE INTO user_links (guild_id, discord_user_id, fpl_team_id) VALUES (?, ?, ?)", (str(guild_id), str(user_id), fpl_team_id))
            con.commit()
    except sqlite3.Error as e:
        logger.error(f"Database error in link_user_to_team: {e}")
        raise


def get_unclaimed_teams(league_id: int, guild_id: int, search_term: str):
    """Gets a list of teams in a league that are not claimed in the specific guild."""
    try:
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
    except sqlite3.Error as e:
        logger.error(f"Database error in get_unclaimed_teams: {e}")
        return []


def get_all_teams_for_autocomplete(league_id: int, search_term: str):
    """Gets a list of all teams for autocomplete."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("""
                SELECT fpl_team_id, team_name, manager_name FROM league_teams
                WHERE league_id = ? AND (team_name LIKE ? OR manager_name LIKE ?)
                LIMIT 25
            """, (league_id, f"%{search_term}%", f"%{search_term}%"))
            return cur.fetchall()
    except sqlite3.Error as e:
        logger.error(f"Database error in get_all_teams_for_autocomplete: {e}")
        return []


def get_team_by_fpl_id(fpl_team_id: int):
    """Gets all details for a specific FPL team."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute("SELECT * FROM league_teams WHERE fpl_team_id = ?", (fpl_team_id,))
            return cur.fetchone()
    except sqlite3.Error as e:
        logger.error(f"Database error in get_team_by_fpl_id: {e}")
        return None


def get_linked_users(guild_id: int, league_id: int):
    """Gets a list of all FPL teams that are linked to a Discord user in a specific guild."""
    try:
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
    except sqlite3.Error as e:
        logger.error(f"Database error in get_linked_users: {e}")
        return []


def get_all_league_teams(guild_id: int, league_id: int):
    """Gets a list of all teams for a league, including the linked discord user if one exists for the guild."""
    try:
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
    except sqlite3.Error as e:
        logger.error(f"Database error in get_all_league_teams: {e}")
        return []


def is_goal_subscribed(channel_id: int):
    """Checks if a channel is subscribed to goal alerts."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("SELECT 1 FROM goal_subscriptions WHERE channel_id = ?", (str(channel_id),))
            return cur.fetchone() is not None
    except sqlite3.Error as e:
        logger.error(f"Database error in is_goal_subscribed: {e}")
        return False


def add_goal_subscription(channel_id: int, league_id: int):
    """Adds a channel to the goal alert subscription list."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("INSERT INTO goal_subscriptions (channel_id, league_id) VALUES (?, ?)", (str(channel_id), league_id))
            con.commit()
    except sqlite3.Error as e:
        logger.error(f"Database error in add_goal_subscription: {e}")
        raise


def remove_goal_subscription(channel_id: int):
    """Removes a channel from the goal alert subscription list."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("DELETE FROM goal_subscriptions WHERE channel_id = ?", (str(channel_id),))
            con.commit()
    except sqlite3.Error as e:
        logger.error(f"Database error in remove_goal_subscription: {e}")
        raise


def get_all_goal_subscriptions():
    """Gets all channel IDs and their league IDs subscribed to goal alerts."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute("SELECT channel_id, league_id, transfer_alerts_enabled FROM goal_subscriptions")
            return cur.fetchall()
    except sqlite3.Error as e:
        logger.error(f"Database error in get_all_goal_subscriptions: {e}")
        return []


def is_transfer_alert_subscribed(channel_id: int):
    """Checks if a channel is subscribed to transfer flop alerts."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("SELECT transfer_alerts_enabled FROM goal_subscriptions WHERE channel_id = ?", (str(channel_id),))
            result = cur.fetchone()
            return result[0] if result and result[0] else False
    except sqlite3.Error as e:
        logger.error(f"Database error in is_transfer_alert_subscribed: {e}")
        return False


def set_transfer_alert_subscription(channel_id: int, status: bool):
    """Sets the transfer alert subscription status for a channel."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("UPDATE goal_subscriptions SET transfer_alerts_enabled = ? WHERE channel_id = ?", (status, str(channel_id)))
            con.commit()
    except sqlite3.Error as e:
        logger.error(f"Database error in set_transfer_alert_subscription: {e}")
        raise


def get_auto_post_subscriptions(post_type='gw'):
    """Gets channels with auto-posting enabled for the given type ('gw' or 'recap')."""
    column = 'auto_post_gw' if post_type == 'gw' else 'auto_post_recap'
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute(f"SELECT channel_id, league_id FROM goal_subscriptions WHERE {column} = 1")
            return cur.fetchall()
    except sqlite3.Error as e:
        logger.error(f"Database error in get_auto_post_subscriptions: {e}")
        return []


def is_auto_post_enabled(channel_id: int, post_type: str):
    """Checks if auto-posting is enabled for a channel."""
    column = 'auto_post_gw' if post_type == 'gw' else 'auto_post_recap'
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute(f"SELECT {column} FROM goal_subscriptions WHERE channel_id = ?", (str(channel_id),))
            result = cur.fetchone()
            return result[0] if result and result[0] else False
    except sqlite3.Error as e:
        logger.error(f"Database error in is_auto_post_enabled: {e}")
        return False


def set_auto_post_subscription(channel_id: int, post_type: str, enabled: bool):
    """Enable/disable auto-posting for a channel."""
    column = 'auto_post_gw' if post_type == 'gw' else 'auto_post_recap'
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute(f"UPDATE goal_subscriptions SET {column} = ? WHERE channel_id = ?", (enabled, str(channel_id)))
            con.commit()
    except sqlite3.Error as e:
        logger.error(f"Database error in set_auto_post_subscription: {e}")
        raise


def get_bot_state(key: str):
    """Gets a bot state value by key."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
            result = cur.fetchone()
            return result[0] if result else None
    except sqlite3.Error as e:
        logger.error(f"Database error in get_bot_state: {e}")
        return None


def set_bot_state(key: str, value: str):
    """Sets a bot state value (upsert)."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("INSERT OR REPLACE INTO bot_state (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (key, value))
            con.commit()
    except sqlite3.Error as e:
        logger.error(f"Database error in set_bot_state: {e}")
        raise


def get_all_bot_state_keys(prefix: str):
    """Gets all state keys starting with a prefix."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("SELECT key FROM bot_state WHERE key LIKE ?", (f"{prefix}%",))
            return [row[0] for row in cur.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"Database error in get_all_bot_state_keys: {e}")
        return []
