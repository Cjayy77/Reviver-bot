"""
🌱 Reviver Bot — Intelligent Dead Channel Reviver
Analyzes channel history, learns what works, and uses Claude AI to craft
contextual revival messages that feel natural and get responses.

Commands:
  !revive set <hours>       - Set silence threshold for current channel
  !revive quiet <HH> <HH>  - Set quiet hours (e.g. !revive quiet 23 8)
  !revive status            - Show config for all watched channels
  !revive now               - Manually trigger a revival in this channel
  !revive ignore            - Stop watching this channel
  !revive timezone <tz>     - Set server timezone (e.g. America/New_York)
  !revive personality <txt> - Set channel personality hint
"""

import discord
from discord.ext import commands, tasks
import anthropic
import os
import json
import asyncio
import re
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ── Clients ───────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
ai  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ── State (swap for a DB in production) ───────────────────────────────────────
# Per-guild config
# {guild_id: {
#   "timezone": "UTC",
#   "quiet_start": 23,   # hour (0-23)
#   "quiet_end":   8,
#   "channels": {
#     channel_id: {
#       "threshold_hours": 6,
#       "personality": "casual and friendly",
#       "watched": True,
#     }
#   }
# }}
guild_config: dict[int, dict] = {}

# Per-channel learning: track which revival messages got responses
# {channel_id: [{"prompt": str, "got_response": bool, "topic": str}]}
revival_history: dict[int, list] = defaultdict(list)

# Track last message time per channel
last_message: dict[int, datetime] = {}

# Track if a revival is already pending for a channel (avoid double-fires)
pending_revival: set[int] = set()

# Track last revival time per channel (avoid spam)
last_revival: dict[int, datetime] = {}

REVIVAL_COOLDOWN_HOURS = 1  # don't revive same channel more than once per hour


def get_guild_cfg(guild_id: int) -> dict:
    if guild_id not in guild_config:
        guild_config[guild_id] = {
            "timezone": "UTC",
            "quiet_start": 23,
            "quiet_end": 8,
            "channels": {},
        }
    return guild_config[guild_id]


def get_channel_cfg(guild_id: int, channel_id: int) -> dict | None:
    return get_guild_cfg(guild_id)["channels"].get(channel_id)


def is_quiet_hours(guild_id: int) -> bool:
    cfg = get_guild_cfg(guild_id)
    try:
        tz = ZoneInfo(cfg["timezone"])
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    now = datetime.now(tz)
    hour = now.hour
    qs, qe = cfg["quiet_start"], cfg["quiet_end"]
    if qs > qe:  # spans midnight e.g. 23 → 8
        return hour >= qs or hour < qe
    return qs <= hour < qe


# ── Message tracking ──────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"🌱 Reviver Bot online as {bot.user}")
    await bot.change_presence(activity=discord.Game("!revive set 6 • keeping servers alive"))
    check_dead_channels.start()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        await bot.process_commands(message)
        return

    # Update last seen
    last_message[message.channel.id] = datetime.now(timezone.utc)

    # If a revival just fired and this is a response, mark it as successful
    ch_history = revival_history[message.channel.id]
    if ch_history and not ch_history[-1]["got_response"]:
        ch_history[-1]["got_response"] = True

    await bot.process_commands(message)


# ── Background checker ────────────────────────────────────────────────────────
@tasks.loop(minutes=5)
async def check_dead_channels():
    now = datetime.now(timezone.utc)
    for guild in bot.guilds:
        cfg = get_guild_cfg(guild.id)
        if is_quiet_hours(guild.id):
            continue
        for ch_id, ch_cfg in cfg["channels"].items():
            if not ch_cfg.get("watched", True):
                continue
            channel = guild.get_channel(ch_id)
            if not channel:
                continue

            # Check silence duration
            last = last_message.get(ch_id)
            if last is None:
                # Seed from channel history
                try:
                    async for msg in channel.history(limit=1):
                        last_message[ch_id] = msg.created_at
                        last = msg.created_at
                except Exception:
                    continue

            if last is None:
                continue

            silence = (now - last).total_seconds() / 3600  # hours
            threshold = ch_cfg.get("threshold_hours", 6)

            if silence < threshold:
                continue
            if ch_id in pending_revival:
                continue

            # Cooldown check
            last_rev = last_revival.get(ch_id)
            if last_rev and (now - last_rev).total_seconds() / 3600 < REVIVAL_COOLDOWN_HOURS:
                continue

            pending_revival.add(ch_id)
            bot.loop.create_task(revive_channel(channel, guild.id, ch_cfg))


# ── Core revival logic ────────────────────────────────────────────────────────
async def revive_channel(channel: discord.TextChannel, guild_id: int, ch_cfg: dict):
    try:
        # Gather recent history
        messages = []
        async for msg in channel.history(limit=80):
            if not msg.author.bot:
                messages.append(msg)

        if not messages:
            pending_revival.discard(channel.id)
            return

        messages.reverse()  # oldest first

        # Analyze: top topics (naive keyword frequency), top active users
        word_freq: Counter = Counter()
        user_activity: Counter = Counter()
        recent_users: list[discord.Member] = []

        stop_words = {"the","a","an","is","it","in","on","at","to","for","of",
                      "and","or","but","i","you","we","they","he","she","my",
                      "your","this","that","was","are","be","have","has","do",
                      "did","not","with","just","so","what","how","like","go",
                      "get","ok","okay","yeah","yes","no","lol","haha","im"}

        for msg in messages:
            user_activity[msg.author.id] += 1
            if msg.author not in recent_users:
                recent_users.append(msg.author)
            for word in re.findall(r"\b[a-z]{4,}\b", msg.content.lower()):
                if word not in stop_words:
                    word_freq[word] += 1

        top_topics = [w for w, _ in word_freq.most_common(10)]
        top_user_ids = [uid for uid, _ in user_activity.most_common(3)]
        top_members = [channel.guild.get_member(uid) for uid in top_user_ids if channel.guild.get_member(uid)]

        # Check revival history — prefer topics that got responses
        successful_topics = [
            h["topic"] for h in revival_history[channel.id] if h["got_response"]
        ]

        # Build context for Claude
        history_text = "\n".join(
            f"{msg.author.display_name}: {msg.content[:120]}"
            for msg in messages[-30:]
        )

        personality = ch_cfg.get("personality", "casual, warm, and conversational")
        successful_hint = (
            f"Topics that previously got engagement: {', '.join(successful_topics[-5:])}. Lean towards these."
            if successful_topics else ""
        )

        system_prompt = f"""You are a Discord community manager for a server channel. Your job is to craft a single revival message that restarts conversation in a channel that has gone quiet.

Channel personality: {personality}
{successful_hint}

Rules:
- Write ONLY the message itself — no preamble, no explanation, no quotes around it
- Sound like a real community member, not a bot
- Reference actual topics from the chat history naturally
- Keep it under 180 characters
- End with an open question or poll to invite responses
- Do NOT use generic openers like "Hey everyone!" or "It's been quiet…"
- Do NOT mention that you're a bot or that the channel is dead
- Use 1 emoji max, only if it fits naturally
- Output format: first line = the message, second line = one word topic label"""

        user_prompt = f"Recent chat history:\n{history_text}\n\nTop discussed topics: {', '.join(top_topics[:6]) or 'general chat'}\n\nWrite the revival message now."

        # Call Claude
        response = await asyncio.to_thread(
            ai.messages.create,
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw = response.content[0].text.strip()
        lines = raw.strip().splitlines()
        revival_msg = lines[0].strip()
        topic_label = lines[1].strip() if len(lines) > 1 else (top_topics[0] if top_topics else "general")

        # Build ping string for top members (max 2)
        pings = " ".join(m.mention for m in top_members[:2]) if top_members else ""
        final_msg = f"{pings} {revival_msg}".strip() if pings else revival_msg

        await channel.send(final_msg)

        # Log revival
        revival_history[channel.id].append({
            "prompt": revival_msg,
            "got_response": False,
            "topic": topic_label,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        last_revival[channel.id] = datetime.now(timezone.utc)

    except Exception as e:
        print(f"Revival error in #{channel.name}: {e}")
    finally:
        pending_revival.discard(channel.id)


# ── Commands ──────────────────────────────────────────────────────────────────
@bot.group(name="revive", invoke_without_command=True)
@commands.has_permissions(manage_channels=True)
async def revive(ctx):
    embed = discord.Embed(
        title="🌱 Reviver Bot",
        description="Keeps your server alive with AI-powered conversation starters.",
        color=0x57F287,
    )
    cmds = [
        ("`!revive set <hours>`",       "Watch this channel, revive after N hours of silence"),
        ("`!revive quiet <start> <end>`","Set quiet hours (24h format, e.g. `!revive quiet 23 8`)"),
        ("`!revive timezone <tz>`",      "Set timezone (e.g. `America/New_York`)"),
        ("`!revive personality <text>`", "Set this channel's personality hint"),
        ("`!revive ignore`",             "Stop watching this channel"),
        ("`!revive now`",                "Manually trigger a revival right now"),
        ("`!revive status`",             "Show all watched channels and their config"),
    ]
    for name, val in cmds:
        embed.add_field(name=name, value=val, inline=False)
    embed.set_footer(text="Requires Manage Channels permission.")
    await ctx.send(embed=embed)


@revive.command(name="set")
@commands.has_permissions(manage_channels=True)
async def revive_set(ctx, hours: float = 6.0):
    if hours < 0.5 or hours > 168:
        await ctx.send("❌ Threshold must be between 0.5 and 168 hours.")
        return
    cfg = get_guild_cfg(ctx.guild.id)
    cfg["channels"].setdefault(ctx.channel.id, {})
    cfg["channels"][ctx.channel.id].update({"threshold_hours": hours, "watched": True})
    embed = discord.Embed(
        title="🌱 Channel Watched",
        description=f"I'll revive **#{ctx.channel.name}** after **{hours}h** of silence.",
        color=0x57F287,
    )
    embed.set_footer(text="Use !revive personality to customize the tone.")
    await ctx.send(embed=embed)


@revive.command(name="ignore")
@commands.has_permissions(manage_channels=True)
async def revive_ignore(ctx):
    cfg = get_guild_cfg(ctx.guild.id)
    if ctx.channel.id in cfg["channels"]:
        cfg["channels"][ctx.channel.id]["watched"] = False
    await ctx.send(f"🔕 No longer watching **#{ctx.channel.name}**.")


@revive.command(name="quiet")
@commands.has_permissions(manage_channels=True)
async def revive_quiet(ctx, start: int = 23, end: int = 8):
    if not (0 <= start <= 23 and 0 <= end <= 23):
        await ctx.send("❌ Hours must be 0–23. Example: `!revive quiet 23 8`")
        return
    cfg = get_guild_cfg(ctx.guild.id)
    cfg["quiet_start"] = start
    cfg["quiet_end"]   = end
    await ctx.send(
        f"🌙 Quiet hours set: **{start:02d}:00 → {end:02d}:00** "
        f"(timezone: {cfg['timezone']}). No revivals during this window."
    )


@revive.command(name="timezone")
@commands.has_permissions(manage_channels=True)
async def revive_timezone(ctx, tz: str = "UTC"):
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        await ctx.send(f"❌ Unknown timezone `{tz}`. Use a TZ name like `America/New_York` or `Europe/London`.")
        return
    cfg = get_guild_cfg(ctx.guild.id)
    cfg["timezone"] = tz
    await ctx.send(f"🕐 Server timezone set to **{tz}**.")


@revive.command(name="personality")
@commands.has_permissions(manage_channels=True)
async def revive_personality(ctx, *, personality: str):
    cfg = get_guild_cfg(ctx.guild.id)
    cfg["channels"].setdefault(ctx.channel.id, {})
    cfg["channels"][ctx.channel.id]["personality"] = personality
    await ctx.send(
        f"🎭 Personality for **#{ctx.channel.name}** set to:\n> {personality}"
    )


@revive.command(name="now")
@commands.has_permissions(manage_channels=True)
async def revive_now(ctx):
    cfg = get_guild_cfg(ctx.guild.id)
    ch_cfg = cfg["channels"].get(ctx.channel.id, {"threshold_hours": 6, "personality": "casual and friendly", "watched": True})
    if ctx.channel.id in pending_revival:
        await ctx.send("⏳ A revival is already in progress for this channel.")
        return
    await ctx.send("🌱 *Crafting a revival message…*")
    pending_revival.add(ctx.channel.id)
    await revive_channel(ctx.channel, ctx.guild.id, ch_cfg)


@revive.command(name="status")
@commands.has_permissions(manage_channels=True)
async def revive_status(ctx):
    cfg = get_guild_cfg(ctx.guild.id)
    channels = cfg["channels"]
    if not channels:
        await ctx.send("📭 No channels are being watched yet. Use `!revive set <hours>` in a channel.")
        return

    embed = discord.Embed(title="🌱 Reviver Status", color=0x57F287)
    embed.add_field(
        name="Server settings",
        value=(
            f"Timezone: `{cfg['timezone']}`\n"
            f"Quiet hours: `{cfg['quiet_start']:02d}:00 → {cfg['quiet_end']:02d}:00`"
        ),
        inline=False,
    )

    now = datetime.now(timezone.utc)
    for ch_id, ch_cfg in channels.items():
        channel = ctx.guild.get_channel(ch_id)
        if not channel:
            continue
        watched = ch_cfg.get("watched", True)
        threshold = ch_cfg.get("threshold_hours", 6)
        last = last_message.get(ch_id)
        silence = f"{(now - last).total_seconds() / 3600:.1f}h ago" if last else "unknown"
        history = revival_history[ch_id]
        success_rate = (
            f"{sum(1 for h in history if h['got_response'])}/{len(history)} revivals got responses"
            if history else "no revivals yet"
        )
        embed.add_field(
            name=f"{'👁️' if watched else '🔕'} #{channel.name}",
            value=(
                f"Threshold: `{threshold}h` • Last message: `{silence}`\n"
                f"Personality: _{ch_cfg.get('personality', 'default')}_\n"
                f"{success_rate}"
            ),
            inline=False,
        )
    await ctx.send(embed=embed)


@revive.error
async def revive_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need **Manage Channels** permission to configure the Reviver.")


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    discord_token = os.environ.get("DISCORD_TOKEN")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not discord_token:
        raise ValueError("Set the DISCORD_TOKEN environment variable.")
    if not anthropic_key:
        raise ValueError("Set the ANTHROPIC_API_KEY environment variable.")
    bot.run(discord_token)
