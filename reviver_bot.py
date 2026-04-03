"""
🎯 Knockdown Shield — Intelligent Channel Reviver
Apex Legends themed, but revival content stays general.

Commands:
  !revive now        - AI message based on channel history
  !revive poll       - Drop a conversation poll
  !revive debate     - Spark a hot take debate
  !revive versus     - Pit two things against each other
  !revive memory     - Resurface a past message
  !revive question   - Ask someone about what they said
  !revive challenge  - Post a fun daily challenge
  !revive set <hrs>  - Watch this channel, revive after N hours
  !revive auto       - Toggle smart auto-revival
  !revive mood <mood>- Set channel mood: witty / chaotic / sharp
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
REVIVAL_COOLDOWN_HOURS = 2

# Trends cache — refreshed every 30 min, shared across all channels
_trends_cache: dict = {"topics": [], "fetched_at": None}
TRENDS_TTL_MINUTES = 30

# Mood definitions per channel
MOODS = {
    "witty": (
        "You are witty and sarcastic. You find the joke in everything, roast bad takes lightly, "
        "and deliver punchlines naturally. Think sharp Twitter energy."
    ),
    "chaotic": (
        "You are chaotic and unfiltered. You say what no one else will, make bold claims, "
        "stir the pot, and keep people on their toes. Unpredictable but never mean."
    ),
    "sharp": (
        "You are sharp and observational. You notice things others miss, call out contradictions, "
        "and ask the questions that make people stop and think."
    ),
}

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
            print(f"[auto] reviving #{channel.name} in {guild.name} with technique: {technique} (silence: {silence:.1f}h)")
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


def _resolve_mentions(text: str, channel: discord.TextChannel) -> str:
    """Replace any plain name mentions in text with proper Discord mentions."""
    for member in channel.guild.members:
        for name in [member.display_name, member.name]:
            if not name or len(name) < 3:
                continue
            pattern = re.compile(rf'\b{re.escape(name)}\b', re.IGNORECASE)
            if pattern.search(text):
                text = pattern.sub(member.mention, text, count=1)
    return text


async def _get_history(channel: discord.TextChannel, limit: int = 60) -> list:
    messages = []
    async for msg in channel.history(limit=limit):
        if not msg.author.bot:
            messages.append(msg)
    messages.reverse()
    return messages


async def _get_trends() -> str:
    """Fetch trending topics, cached for 30 minutes."""
    now = datetime.now(timezone.utc)
    if (
        _trends_cache["fetched_at"] and
        (now - _trends_cache["fetched_at"]).total_seconds() < TRENDS_TTL_MINUTES * 60 and
        _trends_cache["topics"]
    ):
        return _trends_cache["topics"]

    try:
        response = await asyncio.to_thread(
            ai.chat.completions.create,
            model="llama-3.1-8b-instant",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": (
                    "List 6 things currently trending or being talked about on social media right now — "
                    "could be memes, news, pop culture, drama, anything viral. "
                    "Be specific and current. Output as a simple comma-separated list, nothing else."
                )
            }],
            tools=[{"type": "web_search"}],
        )
        topics = response.choices[0].message.content.strip()
        _trends_cache["topics"] = topics
        _trends_cache["fetched_at"] = now
        return topics
    except Exception as e:
        print(f"[_get_trends] failed: {e}")
        return ""


async def _call_ai(system: str, user: str) -> str:
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


def _get_mood_prompt(ch_cfg: dict) -> str:
    mood = ch_cfg.get("mood", "witty")
    return MOODS.get(mood, MOODS["witty"])


# ── Techniques ─────────────────────────────────────────────────────────────────
async def _revive_now(channel: discord.TextChannel, guild_id: int, ch_cfg: dict):
    messages = await _get_history(channel, 60)
    if not messages:
        await channel.send("⚠️ *No message history — send some messages first!*")
        return

    stop_words = {"the","a","an","is","it","in","on","at","to","for","of","and","or",
                  "but","i","you","we","they","he","she","my","your","this","that",
                  "was","are","be","have","has","do","did","not","with","just","so",
                  "what","how","like","go","get","ok","okay","yeah","yes","no","lol","im"}

    # Build topic → users map
    topic_users: dict[str, set[int]] = defaultdict(set)
    for msg in messages:
        for word in re.findall(r"\b[a-z]{4,}\b", msg.content.lower()):
            if word not in stop_words:
                topic_users[word].add(msg.author.id)

    top_topics = sorted(topic_users, key=lambda w: len(topic_users[w]), reverse=True)[:6]

    # Find the most relevant person to tag — most recent substantive message
    target_member = None
    for msg in reversed(messages[-15:]):
        if len(msg.content) > 20 and not msg.content.startswith("!"):
            m = channel.guild.get_member(msg.author.id)
            if m and not m.bot:
                target_member = m
                break

    # Clean history for AI — replace all Discord mentions with plain "Name said:"
    def clean(content: str) -> str:
        def swap(match):
            uid = int(match.group(1))
            mem = channel.guild.get_member(uid)
            return mem.display_name if mem else "someone"
        return re.sub(r'<@!?(\d+)>', swap, content)

    history_text = "\n".join(
        f"{m.author.display_name}: {clean(m.content[:120])}"
        for m in messages[-35:]
    )
    trends = await _get_trends()
    mood = _get_mood_prompt(ch_cfg)

    # Tell AI to write PING as a placeholder — we replace it ourselves
    ping_instruction = (
        f"Use the placeholder PING when addressing the person directly. "
        f"The person you're talking to is: {target_member.display_name}\n"
    ) if target_member else ""

    result = await _call_ai(
        system=(
            f"{mood}\n\n"
            f"You're a member of a Discord server jumping back into a dead conversation. "
            f"Write ONE message that restarts things. Be specific to what they talked about.\n\n"
            f"You can: reference something funny, connect to a trend, roast a take, "
            f"ask something divisive, or drop an observation.\n\n"
            f"{ping_instruction}"
            f"Rules: under 180 chars, no greetings, never mention silence, "
            f"sound like a real person texting. 0-1 emoji. Output ONLY the message."
        ),
        user=(
            f"Chat history:\n{history_text}\n\n"
            f"Hot topics: {', '.join(top_topics) or 'general'}\n"
            + (f"Trending: {trends}\n" if trends else "")
        )
    )

    revival_msg = result.splitlines()[0].strip()

    # Replace PING placeholder with actual Discord mention
    if target_member and "PING" in revival_msg:
        revival_msg = revival_msg.replace("PING", target_member.mention)
    elif target_member and random.random() < 0.5:
        # Prepend mention if AI didn't use PING but we have a target
        revival_msg = f"{target_member.mention} {revival_msg}"

    await channel.send(revival_msg)
    _log_revival(channel.id, "now", revival_msg)
    _log_revival(channel.id, "now", revival_msg)


async def _revive_poll(channel: discord.TextChannel, guild_id: int, ch_cfg: dict):
    messages = await _get_history(channel, 40)
    history_text = "\n".join(f"{m.author.display_name}: {m.content[:80]}" for m in messages[-15:]) or "general"
    trends = await _get_trends()
    mood = _get_mood_prompt(ch_cfg)

    result = await _call_ai(
        system=(
            f"{mood}\n\n"
            "Write a poll that will actually divide people. "
            "Base it on what they talked about or connect it to something trending. "
            "Make the options genuinely hard to choose between — no obvious right answer. "
            "Output ONLY:\nQUESTION: <question>\nA: <option>\nB: <option>\nC: <option>\nD: <option>"
        ),
        user=(
            f"Chat:\n{history_text}\n"
            + (f"Trending: {trends}" if trends else "")
        )
    )
    lines = [l.strip() for l in result.splitlines() if l.strip()]
    question = next((l.replace("QUESTION:", "").strip() for l in lines if l.startswith("QUESTION:")), "What's your take?")
    options = [l for l in lines if re.match(r"^[A-D]:", l)]
    embed = discord.Embed(title=f"📊 {question}", color=0xDA292A)
    emojis = ["🇦", "🇧", "🇨", "🇩"]
    for i, opt in enumerate(options[:4]):
        embed.add_field(name=emojis[i], value=opt[2:].strip(), inline=False)
    embed.set_footer(text="React to vote!")
    msg = await channel.send(embed=embed)
    for i in range(len(options[:4])):
        try:
            await msg.add_reaction(emojis[i])
        except Exception:
            pass
    _log_revival(channel.id, "poll", question)


async def _revive_debate(channel: discord.TextChannel, guild_id: int, ch_cfg: dict):
    messages = await _get_history(channel, 40)
    history_text = "\n".join(f"{m.author.display_name}: {m.content[:80]}" for m in messages[-15:]) or "general"
    trends = await _get_trends()
    mood = _get_mood_prompt(ch_cfg)

    member_map: dict[str, discord.Member] = {}
    for msg in messages[-15:]:
        m = channel.guild.get_member(msg.author.id)
        if m:
            member_map[m.display_name] = m
    names_available = ", ".join(member_map.keys()) if member_map else "no specific members"

    result = await _call_ai(
        system=(
            f"{mood}\n\n"
            "Drop a hot take in a Discord server. Make it bold — something people will push back on. "
            "Tie it to what they talked about or something trending. "
            f"IMPORTANT: If you call someone out, write their name as @TheirName. "
            f"Only these people: {names_available}. Don't invent names.\n"
            "Sound like a real person, not a prompt. Under 140 chars. Output ONLY the take."
        ),
        user=(
            f"Chat:\n{history_text}\n"
            + (f"Trending: {trends}" if trends else "")
        )
    )
    msg = result.strip()
    for name, member in member_map.items():
        msg = re.sub(rf'@{re.escape(name)}\b', member.mention, msg, flags=re.IGNORECASE)
        msg = re.sub(rf'\b{re.escape(name)}\b(?=\s+(think|said|you|your|is|was|has|did|do|are|were))', member.mention, msg, flags=re.IGNORECASE)
    msg = re.sub(r'<(\d{15,20})>', r'<@\1>', msg)
    await channel.send(f"🔥 {msg}")
    _log_revival(channel.id, "debate", msg)


async def _revive_versus(channel: discord.TextChannel, guild_id: int, ch_cfg: dict):
    messages = await _get_history(channel, 40)
    history_text = "\n".join(f"{m.author.display_name}: {m.content[:80]}" for m in messages[-15:]) or "general"
    trends = await _get_trends()
    mood = _get_mood_prompt(ch_cfg)

    result = await _call_ai(
        system=(
            f"{mood}\n\n"
            "Create a 'this vs that' that's actually hard to pick. "
            "Relevant to their chat or a current trend. No obvious winner. "
            "Output ONLY:\nOPTION_A: <thing>\nOPTION_B: <thing>\nCONTEXT: <one punchy line>"
        ),
        user=(
            f"Chat:\n{history_text}\n"
            + (f"Trending: {trends}" if trends else "")
        )
    )
    lines = [l.strip() for l in result.splitlines() if l.strip()]
    option_a = next((l.replace("OPTION_A:", "").strip() for l in lines if l.startswith("OPTION_A:")), "Option A")
    option_b = next((l.replace("OPTION_B:", "").strip() for l in lines if l.startswith("OPTION_B:")), "Option B")
    context  = next((l.replace("CONTEXT:", "").strip() for l in lines if l.startswith("CONTEXT:")), "Pick a side.")
    embed = discord.Embed(
        description=f"**{option_a}** vs **{option_b}**\n*{context}*",
        color=0x9B59B6
    )
    embed.set_footer(text="🅰️ or 🅱️ — no fence sitting")
    msg = await channel.send(embed=embed)
    try:
        await msg.add_reaction("🅰️")
        await msg.add_reaction("🅱️")
    except Exception:
        pass
    _log_revival(channel.id, "versus", f"{option_a} vs {option_b}")


async def _revive_memory(channel: discord.TextChannel, guild_id: int, ch_cfg: dict):
    messages = await _get_history(channel, 80)
    old_msgs = [m for m in messages[:len(messages)//2] if len(m.content) > 20]
    if not old_msgs:
        await _revive_now(channel, guild_id, ch_cfg)
        return
    picked = random.choice(old_msgs)
    mood = _get_mood_prompt(ch_cfg)

    result = await _call_ai(
        system=(
            f"{mood}\n\n"
            "Someone said something in a Discord chat earlier. React to it — "
            "could be agreement, disagreement, roasting it, building on it, or calling it out. "
            "Sound like a real person who just remembered this. Under 100 chars. No quotes. "
            "Output ONLY your reaction, nothing else."
        ),
        user=f"{picked.author.display_name} said: {picked.content[:200]}"
    )
    reaction = result.strip()
    preview = picked.content[:60] + ('...' if len(picked.content) > 60 else '')
    # Use the actual member mention directly — no name guessing
    author = channel.guild.get_member(picked.author.id)
    mention = author.mention if author else picked.author.display_name
    await channel.send(f"wait {mention} said \"{preview}\" — {reaction}")
    _log_revival(channel.id, "memory", result.strip())


async def _revive_question(channel: discord.TextChannel, guild_id: int, ch_cfg: dict):
    messages = await _get_history(channel, 40)
    substantial = [m for m in messages[-15:] if len(m.content) > 20 and not m.content.startswith("!")]
    if not substantial:
        await _revive_now(channel, guild_id, ch_cfg)
        return

    picked_msg = random.choice(substantial)
    target = channel.guild.get_member(picked_msg.author.id)
    if not target:
        await _revive_now(channel, guild_id, ch_cfg)
        return

    mood = _get_mood_prompt(ch_cfg)
    result = await _call_ai(
        system=(
            f"{mood}\n\n"
            "Someone said something in a Discord server. "
            "Ask them a follow-up — genuine curiosity, playfully challenging, or calling them out. "
            "Under 90 chars. Do NOT include any name. Output ONLY the question or comment."
        ),
        user=f"They said: \"{picked_msg.content[:200]}\""
    )
    # We handle the mention ourselves directly — no name guessing
    await channel.send(f"{target.mention} {result.strip()}")
    _log_revival(channel.id, "question", result.strip())


async def _revive_challenge(channel: discord.TextChannel, guild_id: int, ch_cfg: dict):
    messages = await _get_history(channel, 40)
    history_text = "\n".join(f"{m.author.display_name}: {m.content[:80]}" for m in messages[-15:]) or "general"
    trends = await _get_trends()
    mood = _get_mood_prompt(ch_cfg)

    result = await _call_ai(
        system=(
            f"{mood}\n\n"
            "Drop a challenge or dare in a Discord server. Make it specific, fun, and something "
            "people would actually want to do or argue about. "
            "Output ONLY:\nCHALLENGE: <challenge, punchy, under 100 chars>\nREWARD: <ridiculous fake reward, under 50 chars>"
        ),
        user=(
            f"Chat:\n{history_text}\n"
            + (f"Trending: {trends}" if trends else "")
        )
    )
    lines = [l.strip() for l in result.splitlines() if l.strip()]
    challenge = next((l.replace("CHALLENGE:", "").strip() for l in lines if l.startswith("CHALLENGE:")), result.strip())
    reward    = next((l.replace("REWARD:", "").strip() for l in lines if l.startswith("REWARD:")), "bragging rights forever")
    embed = discord.Embed(
        description=f"**{challenge}**\n\n*Prize: {reward}*",
        color=0xF1C40F
    )
    embed.set_footer(text="⚡ first one wins")
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
        ("`!revive now`",               "AI message based on channel history + trends"),
        ("`!revive poll`",              "Drop a divisive poll"),
        ("`!revive debate`",            "Spark a hot take debate"),
        ("`!revive versus`",            "Pit two things against each other"),
        ("`!revive memory`",            "Resurface & react to a past message"),
        ("`!revive question`",          "Call someone out on what they said"),
        ("`!revive challenge`",         "Drop a challenge or dare"),
        ("`!revive mood <mood>`",       "Set mood: `witty` / `chaotic` / `sharp`"),
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
    # Seed last_message to now so it doesn't immediately trigger
    last_message[ctx.channel.id] = datetime.now(timezone.utc)
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


@revive.command(name="mood")
@commands.has_permissions(manage_channels=True)
async def revive_mood(ctx, mood: str = ""):
    mood = mood.lower()
    if mood not in MOODS:
        await ctx.send(
            f"❌ Unknown mood. Choose one of: `witty`, `chaotic`, `sharp`\n\n"
            f"**witty** — sarcastic, finds the joke, roasts bad takes\n"
            f"**chaotic** — unfiltered, stirs the pot, bold claims\n"
            f"**sharp** — observational, calls out contradictions, asks hard questions"
        )
        return
    cfg = get_guild_cfg(ctx.guild.id)
    cfg["channels"].setdefault(ctx.channel.id, {})
    cfg["channels"][ctx.channel.id]["mood"] = mood
    descriptions = {
        "witty": "Sarcastic and sharp — finds the joke in everything 😏",
        "chaotic": "Unfiltered and bold — says what no one else will 🔥",
        "sharp": "Observational and incisive — notices what others miss 🎯",
    }
    await ctx.send(f"🎭 Mood for **#{ctx.channel.name}** set to **{mood}**\n{descriptions[mood]}")


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
