"""
🎯 Knockdown Shield — Intelligent Channel Reviver
Apex Legends themed, but revival content stays general.
Revives dead channels using multiple techniques powered by Claude AI.

Commands:
  !revive now        - AI-crafted message based on channel history
  !revive poll       - Drop a conversation poll
  !revive debate     - Spark a hot take debate
  !revive versus     - Pit two things against each other
  !revive memory     - Resurface a past message
  !revive question   - Ask a random active member something
  !revive challenge  - Post a fun daily challenge
  !revive set <hrs>  - Watch this channel, revive after N hours
  !revive auto       - Toggle smart auto-revival (picks best technique)
  !revive quiet <s> <e> - Set quiet hours
  !revive timezone <tz> - Set server timezone
  !revive personality <txt> - Set channel personality
  !revive ignore     - Stop watching this channel
  !revive status     - Show all watched channels
"""

import discord
from discord.ext import commands, tasks
from groq import Groq
import os
import asyncio
import re
import random
from datetime import datetime, timezone
from collections import defaultdict, Counter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ── Clients ────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
ai  = Groq(api_key=os.environ["GROQ_API_KEY"])

# ── State ──────────────────────────────────────────────────────────────────────
guild_config: dict[int, dict] = {}
revival_history: dict[int, list] = defaultdict(list)
last_message: dict[int, datetime] = {}
pending_revival: set[int] = set()
last_revival: dict[int, datetime] = {}
REVIVAL_COOLDOWN_HOURS = 1

# ── Apex flavor ────────────────────────────────────────────────────────────────
APEX_INTROS = [
    "🛡️ Knockdown Shield deployed —",
    "🎯 Dropping in —",
    "💜 Revive beacon active —",
    "🔋 Shield cells charged —",
    "📡 Pinging the squad —",
]

def apex_intro(): return random.choice(APEX_INTROS)


def get_guild_cfg(guild_id: int) -> dict:
    if guild_id not in guild_config:
        guild_config[guild_id] = {
            "timezone": "UTC",
            "quiet_start": 23,
            "quiet_end": 8,
            "channels": {},
        }
    return guild_config[guild_id]


def is_quiet_hours(guild_id: int) -> bool:
    cfg = get_guild_cfg(guild_id)
    try:
        tz = ZoneInfo(cfg["timezone"])
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    now = datetime.now(tz)
    hour = now.hour
    qs, qe = cfg["quiet_start"], cfg["quiet_end"]
    if qs > qe:
        return hour >= qs or hour < qe
    return qs <= hour < qe


# ── Events ─────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"🛡️ Knockdown Shield online as {bot.user}")
    await bot.change_presence(activity=discord.Game("!revive • Knockdown Shield"))
    check_dead_channels.start()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        await bot.process_commands(message)
        return
    last_message[message.channel.id] = datetime.now(timezone.utc)
    ch_history = revival_history[message.channel.id]
    if ch_history and not ch_history[-1]["got_response"]:
        ch_history[-1]["got_response"] = True
    await bot.process_commands(message)


# ── Auto checker ───────────────────────────────────────────────────────────────
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
            if not ch_cfg.get("auto", True):
                continue
            channel = guild.get_channel(ch_id)
            if not channel:
                continue
            last = last_message.get(ch_id)
            if last is None:
                try:
                    async for msg in channel.history(limit=1):
                        last_message[ch_id] = msg.created_at
                        last = msg.created_at
                except Exception:
                    continue
            if last is None:
                continue
            silence = (now - last).total_seconds() / 3600
            if silence < ch_cfg.get("threshold_hours", 6):
                continue
            if ch_id in pending_revival:
                continue
            last_rev = last_revival.get(ch_id)
            if last_rev and (now - last_rev).total_seconds() / 3600 < REVIVAL_COOLDOWN_HOURS:
                continue
            pending_revival.add(ch_id)
            technique = _pick_auto_technique(ch_id)
            bot.loop.create_task(_run_technique(channel, guild.id, ch_cfg, technique))


def _pick_auto_technique(channel_id: int) -> str:
    history = revival_history[channel_id]
    if not history:
        return "now"
    rates = defaultdict(lambda: {"success": 0, "total": 0})
    for h in history:
        t = h.get("technique", "now")
        rates[t]["total"] += 1
        if h["got_response"]:
            rates[t]["success"] += 1
    best = max(rates, key=lambda t: rates[t]["success"] / max(rates[t]["total"], 1), default=None)
    if best and rates[best]["success"] > 0:
        return best
    return random.choice(["now", "poll", "debate", "versus", "question", "challenge"])


async def _run_technique(channel: discord.TextChannel, guild_id: int, ch_cfg: dict, technique: str):
    try:
        fn = {
            "poll":      _revive_poll,
            "debate":    _revive_debate,
            "versus":    _revive_versus,
            "memory":    _revive_memory,
            "question":  _revive_question,
            "challenge": _revive_challenge,
        }.get(technique, _revive_now)
        await fn(channel, guild_id, ch_cfg)
    except Exception as e:
        print(f"[_run_technique] {technique} failed in #{channel.name}: {e}")
    finally:
        pending_revival.discard(channel.id)
        last_revival[channel.id] = datetime.now(timezone.utc)


def _log_revival(channel_id: int, technique: str, content: str):
    revival_history[channel_id].append({
        "technique": technique,
        "prompt": content,
        "got_response": False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def _get_history(channel: discord.TextChannel, limit: int = 60) -> list:
    messages = []
    async for msg in channel.history(limit=limit):
        if not msg.author.bot:
            messages.append(msg)
    messages.reverse()
    return messages


async def _call_claude(system: str, user: str) -> str:
    response = await asyncio.to_thread(
        ai.chat.completions.create,
        model="llama-3.1-8b-instant",
        max_tokens=300,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    return response.choices[0].message.content.strip()


# ── Techniques ─────────────────────────────────────────────────────────────────
async def _revive_now(channel: discord.TextChannel, guild_id: int, ch_cfg: dict):
    messages = await _get_history(channel)
    if not messages:
        await channel.send("⚠️ *No message history found — send some messages first!*")
        return
    stop_words = {"the","a","an","is","it","in","on","at","to","for","of","and","or",
                  "but","i","you","we","they","he","she","my","your","this","that",
                  "was","are","be","have","has","do","did","not","with","just","so",
                  "what","how","like","go","get","ok","okay","yeah","yes","no","lol","im"}
    word_freq: Counter = Counter()
    user_activity: Counter = Counter()
    for msg in messages:
        user_activity[msg.author.id] += 1
        for word in re.findall(r"\b[a-z]{4,}\b", msg.content.lower()):
            if word not in stop_words:
                word_freq[word] += 1
    top_topics = [w for w, _ in word_freq.most_common(8)]
    top_user_ids = [uid for uid, _ in user_activity.most_common(3)]
    top_members = [channel.guild.get_member(uid) for uid in top_user_ids if channel.guild.get_member(uid)]
    history_text = "\n".join(f"{m.author.display_name}: {m.content[:120]}" for m in messages[-25:])
    personality = ch_cfg.get("personality", "casual, warm, direct")
    result = await _call_claude(
        system=(
            f"You are a Discord community member. Craft ONE short revival message to restart conversation.\n"
            f"Channel personality: {personality}\n"
            f"Rules: sound human, reference real past topics naturally, under 180 chars, "
            f"end with a question, no 'Hey everyone' openers, never mention silence or quiet, 1 emoji max.\n"
            f"Output: first line = message, second line = one word topic"
        ),
        user=f"History:\n{history_text}\n\nTop topics: {', '.join(top_topics[:5]) or 'general'}"
    )
    lines = result.splitlines()
    revival_msg = lines[0].strip()
    topic = lines[1].strip() if len(lines) > 1 else (top_topics[0] if top_topics else "general")
    pings = " ".join(m.mention for m in top_members[:2]) if top_members else ""
    final = f"{pings} {revival_msg}".strip() if pings else revival_msg
    await channel.send(final)
    _log_revival(channel.id, "now", revival_msg)


async def _revive_poll(channel: discord.TextChannel, guild_id: int, ch_cfg: dict):
    messages = await _get_history(channel, 40)
    history_text = "\n".join(f"{m.author.display_name}: {m.content[:80]}" for m in messages[-15:]) or "general chat"
    result = await _call_claude(
        system=(
            "Generate a fun engaging poll for a Discord community based on their recent chat. "
            "Keep it general and relatable. Output ONLY:\n"
            "QUESTION: <question>\nA: <option>\nB: <option>\nC: <option>\nD: <option>"
        ),
        user=f"Recent chat:\n{history_text}"
    )
    lines = [l.strip() for l in result.splitlines() if l.strip()]
    question = next((l.replace("QUESTION:", "").strip() for l in lines if l.startswith("QUESTION:")), "What's your take?")
    options = [l for l in lines if re.match(r"^[A-D]:", l)]
    embed = discord.Embed(title=f"📊 {question}", color=0xDA292A)
    emojis = ["🇦", "🇧", "🇨", "🇩"]
    for i, opt in enumerate(options[:4]):
        embed.add_field(name=emojis[i], value=opt[2:].strip(), inline=False)
    embed.set_footer(text=f"{apex_intro()} React to vote! • Knockdown Shield")
    msg = await channel.send(embed=embed)
    for i in range(len(options[:4])):
        try:
            await msg.add_reaction(emojis[i])
        except Exception:
            pass
    _log_revival(channel.id, "poll", question)


async def _revive_debate(channel: discord.TextChannel, guild_id: int, ch_cfg: dict):
    messages = await _get_history(channel, 40)
    history_text = "\n".join(f"{m.author.display_name}: {m.content[:80]}" for m in messages[-15:]) or "general chat"
    result = await _call_claude(
        system=(
            "Generate ONE spicy but friendly hot take for a Discord community. "
            "Opinionated enough to spark debate but not offensive. "
            "Base it loosely on chat history. Output ONLY the statement, under 120 chars."
        ),
        user=f"Recent chat:\n{history_text}"
    )
    embed = discord.Embed(title="🔥 Hot Take", description=f"*{result.strip()}*", color=0xFF6B00)
    embed.set_footer(text=f"{apex_intro()} Agree or disagree? • Knockdown Shield")
    msg = await channel.send(embed=embed)
    try:
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")
    except Exception:
        pass
    _log_revival(channel.id, "debate", result.strip())


async def _revive_versus(channel: discord.TextChannel, guild_id: int, ch_cfg: dict):
    messages = await _get_history(channel, 40)
    history_text = "\n".join(f"{m.author.display_name}: {m.content[:80]}" for m in messages[-15:]) or "general chat"
    result = await _call_claude(
        system=(
            "Generate a fun 'This vs That' matchup for a Discord community. "
            "Broadly relatable, loosely inspired by chat. Output ONLY:\n"
            "OPTION_A: <thing>\nOPTION_B: <thing>\nCONTEXT: <why this matters, max 80 chars>"
        ),
        user=f"Recent chat:\n{history_text}"
    )
    lines = [l.strip() for l in result.splitlines() if l.strip()]
    option_a = next((l.replace("OPTION_A:", "").strip() for l in lines if l.startswith("OPTION_A:")), "Option A")
    option_b = next((l.replace("OPTION_B:", "").strip() for l in lines if l.startswith("OPTION_B:")), "Option B")
    context  = next((l.replace("CONTEXT:", "").strip() for l in lines if l.startswith("CONTEXT:")), "Which side are you on?")
    embed = discord.Embed(title="⚔️ Versus", description=f"*{context}*", color=0x9B59B6)
    embed.add_field(name="🅰️", value=f"**{option_a}**", inline=True)
    embed.add_field(name="🅱️", value=f"**{option_b}**", inline=True)
    embed.set_footer(text=f"{apex_intro()} React to pick your side! • Knockdown Shield")
    msg = await channel.send(embed=embed)
    try:
        await msg.add_reaction("🅰️")
        await msg.add_reaction("🅱️")
    except Exception:
        pass
    _log_revival(channel.id, "versus", f"{option_a} vs {option_b}")


async def _revive_memory(channel: discord.TextChannel, guild_id: int, ch_cfg: dict):
    messages = await _get_history(channel, 80)
    if len(messages) < 5:
        await _revive_now(channel, guild_id, ch_cfg)
        return
    picked = random.choice(messages[:len(messages)//2])
    result = await _call_claude(
        system=(
            "Someone said something interesting in a Discord chat a while back. "
            "Write a short casual follow-up that resurfaces it and invites others in. "
            "Sound natural. Under 140 chars. End with a question."
        ),
        user=f"{picked.author.display_name} once said: \"{picked.content[:200]}\""
    )
    embed = discord.Embed(description=f"*{result.strip()}*", color=0x3498DB)
    embed.set_footer(text=f"{apex_intro()} Throwback • Knockdown Shield")
    await channel.send(embed=embed)
    _log_revival(channel.id, "memory", result.strip())


async def _revive_question(channel: discord.TextChannel, guild_id: int, ch_cfg: dict):
    messages = await _get_history(channel, 40)
    if not messages:
        await _revive_now(channel, guild_id, ch_cfg)
        return
    seen_ids = []
    for msg in reversed(messages):
        if msg.author.id not in seen_ids:
            seen_ids.append(msg.author.id)
        if len(seen_ids) >= 5:
            break
    target = channel.guild.get_member(random.choice(seen_ids))
    if not target:
        await _revive_now(channel, guild_id, ch_cfg)
        return
    history_text = "\n".join(f"{m.author.display_name}: {m.content[:80]}" for m in messages[-15:])
    result = await _call_claude(
        system=(
            "Generate a fun friendly question for a Discord member based on recent chat. "
            "Light and engaging, not invasive. Under 120 chars. "
            "Do NOT include their name — just the question."
        ),
        user=f"Target: {target.display_name}\nChat:\n{history_text}"
    )
    await channel.send(f"🎯 {target.mention} {result.strip()}")
    _log_revival(channel.id, "question", result.strip())


async def _revive_challenge(channel: discord.TextChannel, guild_id: int, ch_cfg: dict):
    messages = await _get_history(channel, 40)
    history_text = "\n".join(f"{m.author.display_name}: {m.content[:80]}" for m in messages[-15:]) or "general chat"
    result = await _call_claude(
        system=(
            "Generate a fun light-hearted challenge for a Discord community. "
            "Something people can do or discuss today. General and engaging. Output ONLY:\n"
            "CHALLENGE: <challenge, max 100 chars>\nREWARD: <fun fake reward, max 60 chars>"
        ),
        user=f"Recent chat:\n{history_text}"
    )
    lines = [l.strip() for l in result.splitlines() if l.strip()]
    challenge = next((l.replace("CHALLENGE:", "").strip() for l in lines if l.startswith("CHALLENGE:")), result.strip())
    reward    = next((l.replace("REWARD:", "").strip() for l in lines if l.startswith("REWARD:")), "Eternal glory 🏆")
    embed = discord.Embed(title="⚡ Daily Challenge", description=f"**{challenge}**", color=0xF1C40F)
    embed.add_field(name="Reward", value=reward, inline=False)
    embed.set_footer(text=f"{apex_intro()} Can you do it? • Knockdown Shield")
    await channel.send(embed=embed)
    _log_revival(channel.id, "challenge", challenge)


# ── Commands ───────────────────────────────────────────────────────────────────
async def _manual_revive(ctx, technique: str, status_text: str, fn):
    if ctx.channel.id in pending_revival:
        await ctx.send("⏳ A revival is already in progress.")
        return
    cfg = get_guild_cfg(ctx.guild.id)
    ch_cfg = cfg["channels"].get(ctx.channel.id, {"personality": "casual and friendly"})
    status = await ctx.send(status_text)
    pending_revival.add(ctx.channel.id)
    try:
        await fn(ctx.channel, ctx.guild.id, ch_cfg)
        await status.delete()
    except Exception as e:
        await status.edit(content=f"❌ Revival failed: `{e}`")
    finally:
        pending_revival.discard(ctx.channel.id)
        last_revival[ctx.channel.id] = datetime.now(timezone.utc)


@bot.group(name="revive", invoke_without_command=True)
@commands.has_permissions(manage_channels=True)
async def revive(ctx):
    embed = discord.Embed(
        title="🛡️ Knockdown Shield",
        description="Multi-technique channel reviver. Drop in and get the squad talking.",
        color=0xDA292A,
    )
    for name, val in [
        ("`!revive now`",               "AI message based on channel history"),
        ("`!revive poll`",              "Drop a conversation poll"),
        ("`!revive debate`",            "Spark a hot take debate"),
        ("`!revive versus`",            "Pit two things against each other"),
        ("`!revive memory`",            "Resurface a past message"),
        ("`!revive question`",          "Ask a random active member something"),
        ("`!revive challenge`",         "Post a fun daily challenge"),
        ("`!revive set <hours>`",       "Watch this channel, auto-revive after N hours"),
        ("`!revive auto`",              "Toggle smart technique selection on/off"),
        ("`!revive quiet <s> <e>`",     "Set quiet hours (e.g. `!revive quiet 23 8`)"),
        ("`!revive timezone <tz>`",     "Set timezone (e.g. `America/New_York`)"),
        ("`!revive personality <txt>`", "Set channel personality hint"),
        ("`!revive ignore`",            "Stop watching this channel"),
        ("`!revive status`",            "Show all watched channels"),
    ]:
        embed.add_field(name=name, value=val, inline=False)
    embed.set_footer(text="Requires Manage Channels • Knockdown Shield")
    await ctx.send(embed=embed)


@revive.command(name="now")
@commands.has_permissions(manage_channels=True)
async def revive_now(ctx):
    await _manual_revive(ctx, "now", "🛡️ *Knockdown Shield deploying…*", _revive_now)


@revive.command(name="poll")
@commands.has_permissions(manage_channels=True)
async def revive_poll(ctx):
    await _manual_revive(ctx, "poll", "📊 *Crafting a poll…*", _revive_poll)


@revive.command(name="debate")
@commands.has_permissions(manage_channels=True)
async def revive_debate(ctx):
    await _manual_revive(ctx, "debate", "🔥 *Loading a hot take…*", _revive_debate)


@revive.command(name="versus")
@commands.has_permissions(manage_channels=True)
async def revive_versus(ctx):
    await _manual_revive(ctx, "versus", "⚔️ *Setting up the matchup…*", _revive_versus)


@revive.command(name="memory")
@commands.has_permissions(manage_channels=True)
async def revive_memory(ctx):
    await _manual_revive(ctx, "memory", "🔍 *Digging through the archives…*", _revive_memory)


@revive.command(name="question")
@commands.has_permissions(manage_channels=True)
async def revive_question(ctx):
    await _manual_revive(ctx, "question", "🎯 *Picking a target…*", _revive_question)


@revive.command(name="challenge")
@commands.has_permissions(manage_channels=True)
async def revive_challenge(ctx):
    await _manual_revive(ctx, "challenge", "⚡ *Generating today's challenge…*", _revive_challenge)


@revive.command(name="set")
@commands.has_permissions(manage_channels=True)
async def revive_set(ctx, hours: float = 6.0):
    if not (0.5 <= hours <= 168):
        await ctx.send("❌ Threshold must be between 0.5 and 168 hours.")
        return
    cfg = get_guild_cfg(ctx.guild.id)
    cfg["channels"].setdefault(ctx.channel.id, {})
    cfg["channels"][ctx.channel.id].update({"threshold_hours": hours, "watched": True, "auto": True})
    embed = discord.Embed(
        title="🛡️ Channel Watched",
        description=f"Knockdown Shield will deploy in **#{ctx.channel.name}** after **{hours}h** of silence.",
        color=0xDA292A,
    )
    embed.set_footer(text="Use !revive auto to toggle smart technique selection.")
    await ctx.send(embed=embed)


@revive.command(name="auto")
@commands.has_permissions(manage_channels=True)
async def revive_auto(ctx):
    cfg = get_guild_cfg(ctx.guild.id)
    cfg["channels"].setdefault(ctx.channel.id, {})
    current = cfg["channels"][ctx.channel.id].get("auto", True)
    cfg["channels"][ctx.channel.id]["auto"] = not current
    state = "ON ✅" if not current else "OFF ❌"
    await ctx.send(
        f"🤖 Smart auto-revival is now **{state}** for **#{ctx.channel.name}**.\n"
        + ("Bot picks the best technique based on what worked before." if not current
           else "Auto-revival disabled — use manual commands only.")
    )


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
    cfg["quiet_end"] = end
    await ctx.send(f"🌙 Quiet hours: **{start:02d}:00 → {end:02d}:00** — no revivals during this window.")


@revive.command(name="timezone")
@commands.has_permissions(manage_channels=True)
async def revive_timezone(ctx, tz: str = "UTC"):
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        await ctx.send(f"❌ Unknown timezone `{tz}`. Use a TZ name like `America/New_York`.")
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
    await ctx.send(f"🎭 Personality for **#{ctx.channel.name}**:\n> {personality}")


@revive.command(name="status")
@commands.has_permissions(manage_channels=True)
async def revive_status(ctx):
    cfg = get_guild_cfg(ctx.guild.id)
    channels = cfg["channels"]
    if not channels:
        await ctx.send("📭 No channels watched yet. Use `!revive set <hours>`.")
        return
    embed = discord.Embed(title="🛡️ Knockdown Shield Status", color=0xDA292A)
    embed.add_field(
        name="Server settings",
        value=f"Timezone: `{cfg['timezone']}` • Quiet: `{cfg['quiet_start']:02d}:00 → {cfg['quiet_end']:02d}:00`",
        inline=False,
    )
    now = datetime.now(timezone.utc)
    for ch_id, ch_cfg in channels.items():
        channel = ctx.guild.get_channel(ch_id)
        if not channel:
            continue
        last = last_message.get(ch_id)
        silence = f"{(now - last).total_seconds() / 3600:.1f}h ago" if last else "unknown"
        history = revival_history[ch_id]
        successes = sum(1 for h in history if h["got_response"])
        techniques_used = list({h.get("technique", "now") for h in history})
        embed.add_field(
            name=f"{'👁️' if ch_cfg.get('watched', True) else '🔕'} #{channel.name}",
            value=(
                f"Threshold: `{ch_cfg.get('threshold_hours', 6)}h` • Last msg: `{silence}`\n"
                f"Auto: {'✅' if ch_cfg.get('auto', True) else '❌'} • "
                f"Techniques used: {', '.join(techniques_used) or 'none yet'}\n"
                f"Revivals: {len(history)} • Responses: {successes}"
            ),
            inline=False,
        )
    await ctx.send(embed=embed)


@revive.error
async def revive_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need **Manage Channels** permission.")


# ── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise ValueError("Set the DISCORD_TOKEN environment variable.")
    if not os.environ.get("GROQ_API_KEY"):
        raise ValueError("Set the GROQ_API_KEY environment variable.")
    bot.run(token)
