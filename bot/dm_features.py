"""DM notification queue and embed builders for premium+ users."""

import asyncio
import time
from collections import deque
from datetime import datetime

import discord

from bot.logging_config import get_logger
from bot.database import mark_dm_failed, update_dm_channel_id

logger = get_logger('dm_features')

# Status emoji mapping (matches FPL API status codes)
STATUS_EMOJI = {
    'd': '\u2753',   # Doubtful (question mark)
    'i': '\U0001f3e5',   # Injured (hospital)
    's': '\U0001f6ab',   # Suspended (prohibited)
    'u': '\u274c',   # Unavailable (cross)
    'n': '\U0001f4e4',   # Not in squad / on loan
}

# Position labels
POS_LABELS = {1: 'GKP', 2: 'DEF', 3: 'MID', 4: 'FWD'}


class DMQueue:
    """Async queue for sending DM notifications with rate limiting."""

    # Configurable parameters (easy to tune post-launch)
    DELAY_EXISTING = 1.5    # seconds between DMs to existing channels
    DELAY_NEW = 10          # seconds between DMs to NEW channels (conservative)
    BATCH_SIZE = 25         # pause after this many
    BATCH_PAUSE = 10        # seconds to pause between batches
    MAX_NEW_CHANNELS = 9    # per 10 minutes (Discord hard limit is ~10)

    def __init__(self, bot):
        self.bot = bot
        self._queue = deque()
        self._processing = False
        self._new_channel_count = 0
        self._new_channel_window_start = 0

    def enqueue(self, user_id: int, embed: discord.Embed, dm_channel_id: str = None,
                guild_id: str = None, on_failure=None):
        """Add a DM to the queue. Starts processing if idle."""
        self._queue.append({
            'user_id': user_id,
            'embed': embed,
            'dm_channel_id': dm_channel_id,
            'guild_id': guild_id,
            'on_failure': on_failure,
        })
        if not self._processing:
            asyncio.create_task(self._process())

    async def _process(self):
        """Process the queue with rate limiting."""
        self._processing = True
        sent_count = 0

        try:
            while self._queue:
                item = self._queue.popleft()
                user_id = item['user_id']
                embed = item['embed']
                dm_channel_id = item['dm_channel_id']

                try:
                    is_new_channel = dm_channel_id is None

                    # Rate limit new channel creation
                    if is_new_channel:
                        now = time.time()
                        if now - self._new_channel_window_start > 600:
                            self._new_channel_count = 0
                            self._new_channel_window_start = now

                        if self._new_channel_count >= self.MAX_NEW_CHANNELS:
                            wait_time = 600 - (now - self._new_channel_window_start)
                            if wait_time > 0:
                                logger.info(f"New channel limit reached, waiting {wait_time:.0f}s")
                                await asyncio.sleep(wait_time)
                                self._new_channel_count = 0
                                self._new_channel_window_start = time.time()

                    # Send the DM
                    if dm_channel_id:
                        # Use cached DM channel (fast path)
                        channel = self.bot.get_channel(int(dm_channel_id))
                        if not channel:
                            channel = await self.bot.fetch_channel(int(dm_channel_id))
                        await channel.send(embed=embed)
                    else:
                        # Open new DM channel
                        user = await self.bot.fetch_user(user_id)
                        dm_channel = await user.create_dm()
                        await dm_channel.send(embed=embed)

                        # Cache the channel ID
                        if item.get('guild_id'):
                            update_dm_channel_id(str(user_id), item['guild_id'], str(dm_channel.id))
                        self._new_channel_count += 1

                    sent_count += 1

                    # Batch pause
                    if sent_count % self.BATCH_SIZE == 0:
                        logger.info(f"Batch pause after {sent_count} DMs ({len(self._queue)} remaining)")
                        await asyncio.sleep(self.BATCH_PAUSE)
                    else:
                        delay = self.DELAY_NEW if is_new_channel else self.DELAY_EXISTING
                        await asyncio.sleep(delay)

                except discord.Forbidden:
                    # User has DMs disabled
                    logger.warning(f"DMs disabled for user {user_id}, marking as failed")
                    if item.get('guild_id'):
                        mark_dm_failed(str(user_id), item['guild_id'])
                    if item.get('on_failure'):
                        item['on_failure']()

                except discord.HTTPException as e:
                    if e.status == 429:
                        # Rate limited — respect retry_after and re-queue
                        retry_after = getattr(e, 'retry_after', 5)
                        logger.warning(f"Rate limited, retrying in {retry_after}s")
                        await asyncio.sleep(retry_after)
                        self._queue.appendleft(item)
                    else:
                        logger.error(f"HTTP error sending DM to {user_id}: {e}")

                except Exception as e:
                    logger.error(f"Unexpected error sending DM to {user_id}: {e}", exc_info=True)

        finally:
            self._processing = False
            if sent_count > 0:
                logger.info(f"DM queue finished: {sent_count} sent")


# =====================================================
# EMBED BUILDERS
# =====================================================

def build_confirmation_embed():
    """Green embed sent on /notify enable to confirm subscription."""
    embed = discord.Embed(
        title="DM Notifications Enabled",
        description=(
            "You'll receive personal FPL notifications via DM:\n\n"
            "\u23f0 **Deadline Reminders** — 3h and 1h before deadline\n"
            "\U0001f3af **Captain Suggestions** — Top 3 picks with the 3h reminder\n"
            "\U0001f3e5 **Injury Alerts** — When your squad players get flagged\n\n"
            "Use `/notify status` to check your settings.\n"
            "Use `/notify disable` to opt out."
        ),
        color=0x2ecc71,  # Green
    )
    embed.set_footer(text="LiveFPLStats Premium+")
    return embed


def build_deadline_embed(deadline_info, captain_suggestions=None, transfer_suggestions=None):
    """Orange embed with deadline countdown and optional captain/transfer picks."""
    next_gw = deadline_info.get('next') or {}
    gw_num = next_gw.get('gameweek', '?')
    deadline_str = next_gw.get('deadline')

    embed = discord.Embed(
        title=f"\u23f0 GW{gw_num} Deadline Reminder",
        color=0xe67e22,  # Orange
    )

    if deadline_str:
        try:
            dt = datetime.fromisoformat(deadline_str.replace('Z', '+00:00'))
            unix_ts = int(dt.timestamp())
            embed.description = f"Deadline: <t:{unix_ts}:F> (<t:{unix_ts}:R>)"
        except (ValueError, TypeError):
            embed.description = f"Deadline: {deadline_str}"

    # Captain suggestions
    if captain_suggestions and captain_suggestions.get('suggestions'):
        lines = []
        for i, s in enumerate(captain_suggestions['suggestions'][:3], 1):
            medal = ['\U0001f947', '\U0001f948', '\U0001f949'][i - 1]
            fixtures = ', '.join(s.get('fixtures', []))
            lines.append(f"{medal} **{s['webName']}** ({s['teamShortName']}) vs {fixtures}")
            if s.get('reasoning'):
                lines.append(f"  \u2514 {s['reasoning']}")
        embed.add_field(name="\U0001f3af Captain Suggestions", value='\n'.join(lines), inline=False)

    # Transfer suggestions
    if transfer_suggestions and transfer_suggestions.get('suggestions'):
        lines = []
        for s in transfer_suggestions['suggestions'][:3]:
            out_name = s['out']['webName']
            in_name = s['in']['webName']
            gain = s['scoreGain']
            lines.append(f"\U0001f534 {out_name} ({s['out']['teamShortName']}) \u2192 \U0001f7e2 {in_name} ({s['in']['teamShortName']}) (+{gain:.2f})")
        ft = transfer_suggestions.get('freeTransfers', 1)
        embed.add_field(
            name=f"\U0001f504 Transfer Suggestions ({ft} FT)",
            value='\n'.join(lines),
            inline=False,
        )

    embed.set_footer(text="LiveFPLStats Premium+")
    return embed


def build_injury_embed(alerts, gameweek):
    """Red embed with flagged squad players."""
    embed = discord.Embed(
        title=f"\U0001f3e5 Squad Injury Alert — GW{gameweek}",
        color=0xe74c3c,  # Red
    )

    if not alerts:
        embed.description = "All your players are available!"
        return embed

    lines = []
    for a in alerts:
        emoji = STATUS_EMOJI.get(a['status'], '\u2753')
        chance = a.get('chanceNextRound')
        chance_str = f" ({chance}%)" if chance is not None else ""
        tag = "**START**" if a.get('isStarter') else "bench"
        news = a.get('news') or 'No news'
        lines.append(f"{emoji} **{a['webName']}** ({a['teamShortName']}) [{tag}]{chance_str}")
        lines.append(f"  \u2514 {news}")

    embed.description = '\n'.join(lines)
    embed.set_footer(text="LiveFPLStats Premium+")
    return embed


def build_transfer_embed(suggestions, gameweek, free_transfers):
    """Blue embed with transfer out/in pairs and score gain."""
    embed = discord.Embed(
        title=f"\U0001f504 Transfer Suggestions — GW{gameweek}",
        color=0x3498db,  # Blue
    )

    if not suggestions:
        embed.description = "No strong transfer suggestions this week."
        return embed

    lines = [f"Free transfers: **{free_transfers}**\n"]
    for i, s in enumerate(suggestions[:3], 1):
        out = s['out']
        inp = s['in']
        cost_str = f"\u00a3{inp['cost'] / 10:.1f}m" if inp.get('cost') else ""
        lines.append(
            f"**{i}.** \U0001f534 {out['webName']} ({out['teamShortName']}) "
            f"\u2192 \U0001f7e2 {inp['webName']} ({inp['teamShortName']}) {cost_str}"
        )
        lines.append(f"  \u2514 Score gain: **+{s['scoreGain']:.2f}**")

    embed.description = '\n'.join(lines)
    embed.set_footer(text="LiveFPLStats Premium+")
    return embed
