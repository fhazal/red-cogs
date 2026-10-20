import aiohttp
import asyncio
import logging
import re
import socket
from datetime import datetime, timedelta, timezone

import discord
from redbot.core import Config, commands
from discord.ext import tasks

log = logging.getLogger("red.enshrouded")

ROOT_ROLE_ID = 390291169260732427
CONTROL = "/usr/local/sbin/enshrouded-control"

# Stamped into every panel footer. If the panel ever shows a footer WITHOUT
# this tag, another cog/script with the same bot token has overwritten it.
PANEL_TAG = "Panel v2.0"

GAME_PORT = 15636
QUERY_PORT = 15637
MAX_PLAYERS = 10
QUERY_HOST = "127.0.0.1"  # the container's query port is published on the host

# Shown to players in the panel's "Server Info" section.
SERVER_ADDRESS = "enshrouded.bajubesi.org"
SERVER_NAME = "BajuBesi Enshrouded Server"
ENSHROUDED_ICON_URL = "https://bajubesi.org/enshrouded-icon.png"

# Status cards delete themselves after this many seconds.
STATUS_LIFETIME = 600  # 10 minutes

# "Peak today" resets at midnight in this timezone (Malaysia, UTC+8, no DST).
LOCAL_TZ = timezone(timedelta(hours=8))

# Shown in the panel's "Game Rules" section. Edit these to match your compose
# file (they are display-only; changing them here does not change the game).
GAME_RULES = [
    ("Enemy damage", "1.5×"),
    ("Boss damage", "1.5×"),
    ("XP gain", "1.5×"),
    ("Stamina", "1.5×"),
    ("Perk cost", "0.5×"),
    ("Factory speed", "2×"),
    ("Day / Night", "30 / 12 min"),
    ("Tombstones", "Off"),
]


# --------------------------------------------------- live player count (A2S)
# --- a2s begin
_A2S_HEADER = b"\xff\xff\xff\xff"
_A2S_INFO_REQUEST = _A2S_HEADER + b"TSource Engine Query\x00"


def _read_cstring(data, pos):
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("utf-8", errors="replace"), end + 1


def _a2s_info_sync(host, port, timeout=2.0):
    """Query Steam A2S_INFO. Returns (players, max_players) or None."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(_A2S_INFO_REQUEST, (host, port))
        data, _ = sock.recvfrom(4096)
        # Newer servers answer with a challenge (0x41) that must be echoed back.
        if data[:5] == _A2S_HEADER + b"\x41":
            sock.sendto(_A2S_INFO_REQUEST + data[5:9], (host, port))
            data, _ = sock.recvfrom(4096)

    if data[:5] != _A2S_HEADER + b"I":
        return None
    pos = 6  # header (4) + 'I' (1) + protocol (1)
    for _ in range(4):  # name, map, folder, game
        _, pos = _read_cstring(data, pos)
    pos += 2  # app id
    return data[pos], data[pos + 1]  # players, max players


async def get_players():
    """Return (players, max_players), or None if the query fails."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _a2s_info_sync, QUERY_HOST, QUERY_PORT
        )
    except Exception as exc:
        log.debug("A2S player query failed: %r", exc)
        return None
# --- a2s end


# ---------------------------------------------------------------- helpers

def has_root_role(member):
    return (
        isinstance(member, discord.Member)
        and any(role.id == ROOT_ROLE_ID for role in member.roles)
    )


async def root_admin_check(ctx):
    if ctx.guild is None or not has_root_role(ctx.author):
        raise commands.CheckFailure(
            "Only members with the @Root Administrator role can control Enshrouded."
        )
    return True


def parse_status(status):
    """Pull container fields out of the controller's status line."""
    running = "Status=running" in status

    restarts = None
    m = re.search(r"Restarts=(\d+)", status)
    if m:
        restarts = int(m.group(1))

    started_dt = None
    m = re.search(r"Started=(\S+)", status)
    if m:
        raw = m.group(1)
        # Docker gives nanoseconds; fromisoformat only handles microseconds.
        raw = re.sub(r"(\.\d{6})\d+", r"\1", raw).replace("Z", "+00:00")
        try:
            started_dt = datetime.fromisoformat(raw)
            if started_dt.tzinfo is None:
                started_dt = started_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            started_dt = None

    return running, restarts, started_dt


def rules_text():
    """GAME_RULES laid out two per line."""
    lines = []
    for i in range(0, len(GAME_RULES), 2):
        pair = GAME_RULES[i:i + 2]
        lines.append("  •  ".join(f"{name} `{value}`" for name, value in pair))
    return "\n".join(lines)


def next_peak(stored_date, stored_peak, today, players):
    """Return (date, peak) after folding in the latest player reading.

    The peak resets when the date changes; an unavailable reading
    (players is None) never changes it.
    """
    peak = stored_peak or 0
    if stored_date != today:
        peak = 0
    if players is not None and players[0] > peak:
        peak = players[0]
    return today, peak


BAR_SEGMENTS = 5  # the bar is always this wide, whatever the slot count


def capacity_text(players):
    """Player count plus a fixed-width bar: 🟩 normal, 🟨 filling up, 🟥 full."""
    if players is None:
        return f"`{MAX_PLAYERS} slots`\n*Live count unavailable*"
    online, slots = players
    slots = slots or MAX_PLAYERS

    if online >= slots:
        filled, square, label = BAR_SEGMENTS, "🟥", " — **FULL**"
    else:
        # Round half up, but only a full server shows every segment, and any
        # player at all shows at least one.
        filled = min(int(online / slots * BAR_SEGMENTS + 0.5), BAR_SEGMENTS - 1)
        if online > 0:
            filled = max(filled, 1)
        square = "🟨" if online * 10 >= slots * 7 else "🟩"  # 70% and up
        label = ""

    bar = square * filled + "⬛" * (BAR_SEGMENTS - filled)
    return f"**{online} / {slots}** online{label}\n{bar}"


def health_embed(status, health, players=None, activity=None, stats=None):
    running, restarts, started_dt = parse_status(status)
    healthy = health.startswith("HEALTHY:")

    if healthy:
        colour = discord.Colour.from_str("#2ECC71")
        headline = "🟢  **ONLINE**"
        sub = "The realm is open — the game process is up and healthy."
    elif running:
        colour = discord.Colour.from_str("#F39C12")
        headline = "🟠  **STARTING / DEGRADED**"
        sub = "The container is running but the game process hasn't been confirmed."
    else:
        colour = discord.Colour.from_str("#E74C3C")
        headline = "🔴  **OFFLINE**"
        sub = "The container is stopped or unreachable. An admin should investigate."

    embed = discord.Embed(
        title="🔥 Enshrouded Dedicated Server",
        description=f"{headline}\n{sub}",
        colour=colour,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(
        name="Bajubesi Gaming Network",
        icon_url=ENSHROUDED_ICON_URL,
    )

    embed.add_field(
        name="🖥️ Server",
        value="`RUNNING`" if running else "`STOPPED`",
        inline=True,
    )
    embed.add_field(
        name="⚔️ Game Process",
        value="`HEALTHY`" if healthy else "`NOT DETECTED`",
        inline=True,
    )
    embed.add_field(
        name="🔄 Restarts",
        value=f"`{restarts if restarts is not None else '—'}`",
        inline=True,
    )

    if started_dt and running:
        epoch = int(started_dt.timestamp())
        uptime = f"<t:{epoch}:R>\n<t:{epoch}:f>"
    elif started_dt:
        uptime = f"Last started <t:{int(started_dt.timestamp())}:R>"
    else:
        uptime = "`Unknown`"
    embed.add_field(name="⏱️ Up Since", value=uptime, inline=True)

    embed.add_field(
        name="👥 Capacity",
        value=capacity_text(players),
        inline=True,
    )
    embed.add_field(
        name="🌐 Connection",
        value=(
            f"Game `{GAME_PORT}`\n"
            f"Query `{QUERY_PORT}`"
        ),
        inline=True,
    )

    embed.add_field(
        name="📡 Server Info",
        value=(
            f"**Address:** `{SERVER_ADDRESS}`\n"
            f"**Server name:** `{SERVER_NAME}`\n"
            "**Chat:** Voice (Global) + Text"
        ),
        inline=False,
    )

    embed.add_field(name="⚙️ Game Rules", value=rules_text(), inline=False)

    lines = []
    if activity is not None:
        last_down = activity.get("last_down")
        down_text = f"<t:{last_down}:R>" if last_down else "None recorded"
        lines.append(f"**Peak today:** `{activity.get('peak', 0)}` players")
        lines.append(f"**Last downtime:** {down_text}")
    if stats is not None:
        lines.append(
            f"**Load:** CPU `{stats['cpu']}` • "
            f"RAM `{stats['mem']}` ({stats['mem_pct']})"
        )
    if lines:
        embed.add_field(name="📈 Activity", value="\n".join(lines), inline=False)

    embed.add_field(
        name="<:tipidea:1560389964301074612> Tip",
        value="Want to know more about the server? Use `!help enshrouded` for more commands.",
        inline=False,
    )
    embed.set_footer(
        text=f"BajuBesi Network • Enshrouded Manager • {PANEL_TAG} • Last checked"
    )
    return embed


def status_embed(status, health, players=None, activity=None):
    """Compact at-a-glance card for the status command."""
    running, restarts, started_dt = parse_status(status)
    healthy = health.startswith("HEALTHY:")

    if healthy:
        colour = discord.Colour.from_str("#2ECC71")
        state = "🟢 Online"
        if players is not None and players[0] >= (players[1] or MAX_PLAYERS):
            note = "The realm is full. Try again in a little while."
        elif players is not None and players[0] > 0:
            n = players[0]
            plural = "" if n == 1 else "s"
            note = f"The server is up with {n} player{plural} in the realm. Come join them!"
        else:
            note = "The server is up and accepting players."
    elif running:
        colour = discord.Colour.from_str("#F39C12")
        state = "🟠 Starting / Degraded"
        note = "The server is starting up or having issues. Give it a few minutes."
    else:
        colour = discord.Colour.from_str("#E74C3C")
        state = "🔴 Offline"
        note = "The server is down for maintenance. Please check back soon."

    embed = discord.Embed(
        description=f"**Enshrouded: {state}**\n{note}",
        colour=colour,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name="BajuBesi Gaming Network", icon_url=ENSHROUDED_ICON_URL)

    if players is None:
        player_text = "`Unavailable`"
    else:
        player_text = f"`{players[0]} / {players[1] or MAX_PLAYERS}`"
    embed.add_field(name="👥 Players", value=player_text, inline=True)

    peak = activity.get("peak", 0) if activity else 0
    embed.add_field(name="📈 Peak today", value=f"`{peak}`", inline=True)

    if started_dt and running:
        up_text = f"<t:{int(started_dt.timestamp())}:R>"
    else:
        up_text = "`—`"
    embed.add_field(name="⏱️ Up since", value=up_text, inline=True)

    embed.add_field(
        name="🌐 Join",
        value=f"`{SERVER_ADDRESS}:{GAME_PORT}`",
        inline=False,
    )
    embed.set_footer(text="BajuBesi Gaming Network • Quick status")
    return embed


# Nightly backup window (Malaysia time). Matches the 3:00 AM cron job.
MAINT_START = (2, 55)
MAINT_END = (3, 15)
MAINT_MSG_LIFETIME = 600  # seconds after recovery before maintenance posts vanish
MAINT_DOWN_MSG = (
    "🛠️ **Nightly maintenance started.**\n"
    "The Enshrouded server is down for its daily backup and restart. "
    "It should be back in a couple of minutes."
)
MAINT_UP_MSG = (
    "✅ **Maintenance complete.**\n"
    "The Enshrouded server is back online and accepting players."
)


def in_maintenance_window():
    now = datetime.now(LOCAL_TZ)
    return MAINT_START <= (now.hour, now.minute) < MAINT_END


# ------------------------------------------------------- Steam news / patch notes
NEWS_CHANNEL_ID = 1560646937223372960  # #enshrouded-updates
STEAM_APP_ID = 1203620
NEWS_URL = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
    f"?appid={STEAM_APP_ID}&count=10&maxlength=0&format=json&feeds=steam_community_announcements"
)
NEWS_POLL_MINUTES = 15
NEWS_MAX_CHARS = 1500
NEWS_PING_ROLE_ID = None  # e.g. 123456789012345678 to ping a role on new posts

# Shown at the bottom of every news card. Set to "" to hide it.
NEWS_SERVER_NOTE = (
    "The server needs `docker compose pull` and a restart to run this version. "
    "Until an admin updates it, players on the new version may not be able to join."
)

# First match wins. Add your own (regex, emoji) pairs if you like.
HEADING_EMOJIS = [
    (r"changelog|patch notes|release notes", "📝"),
    (r"known issue", "⚠️"),
    (r"bug|fix", "🐛"),
    (r"balanc", "⚖️"),
    (r"perform|optimi|stabil", "⚡"),
    (r"combat|enem|boss|weapon", "⚔️"),
    (r"quest|story", "📜"),
    (r"build|decor|craft", "🏗️"),
    (r"dedicated|server|multiplayer", "🖥️"),
    (r"\bui\b|interface|quality of life|\bqol\b", "🧭"),
    (r"\bnew\b|feature|added|addition", "✨"),
]


# Labels such as "Note:" or "IMPORTANT:" at the start of a line. First match wins.
LABEL_EMOJIS = [
    (r"important|warning|caution|attention", "⚠️"),
    (r"note|notice", "📌"),
]
LABEL_RE = re.compile(
    r"(?mi)^(\*\*|__)?(" + "|".join(p for p, _ in LABEL_EMOJIS) + r")(?=\s*:)"
)


def decorate_labels(text):
    def repl(m):
        marker = m.group(1) or ""
        label = m.group(2)
        for pattern, emoji in LABEL_EMOJIS:
            if re.fullmatch(pattern, label.lower()):
                return f"{emoji} {marker}{label}"
        return m.group(0)

    return LABEL_RE.sub(repl, text)


def decorate_heading(text):
    """Put an emoji in front of short bold headings that match a keyword."""
    if len(text) > 60 or "**" in text or not re.match(r"\w", text):
        return text
    low = text.lower()
    for pattern, emoji in HEADING_EMOJIS:
        if re.search(pattern, low):
            return f"{emoji} {text}"
    return text


async def fetch_news():
    """Developer announcements from Steam, newest first, or None on failure."""
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(NEWS_URL) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
    except Exception as exc:
        log.debug("Steam news fetch failed: %r", exc)
        return None
    items = data.get("appnews", {}).get("newsitems", [])
    items = [i for i in items if i.get("feedname") == "steam_community_announcements"]
    items.sort(key=lambda i: i.get("date", 0), reverse=True)
    return items


def drop_title_line(body, title):
    """Remove a leading heading that just repeats the card title."""
    def norm(t):
        return re.sub(r"[^\w]+", " ", t).strip().lower()

    lines = body.split("\n")
    if lines and norm(lines[0]) == norm(title):
        return "\n".join(lines[1:]).strip()
    return body


def steam_bbcode_to_markdown(text):
    """Convert Steam's BBCode into Discord markdown (best effort)."""
    text = text.replace("\r", "")
    flags = re.S | re.I
    text = re.sub(r"\[img\].*?\[/img\]", "", text, flags=flags)
    text = re.sub(r"\[previewyoutube=[^\]]*\]\[/previewyoutube\]", "", text, flags=flags)
    # Steam closes tags with a bare [/] and list items with [/*].
    text = text.replace("[/*]", "").replace("[/]", "")
    # Paragraphs, line breaks, rules.
    text = re.sub(r"\[/p\]", "\n\n", text, flags=flags)
    text = re.sub(r"\[p\]", "", text, flags=flags)
    text = re.sub(r"\[br\s*/?\]", "\n", text, flags=flags)
    text = re.sub(r"\[hr\](\[/hr\])?", "\n", text, flags=flags)
    # Every list item starts on its own line.
    text = text.replace("[*]", "\n• ")
    text = re.sub(r"\[/?o?list\]", "\n", text, flags=flags)
    text = re.sub(
        r"\[h[1-6]\](.*?)\[/h[1-6]\]",
        lambda m: "\n**" + m.group(1).strip() + "**\n",
        text,
        flags=flags,
    )
    text = re.sub(r"\[b\](.*?)\[/b\]", r"**\1**", text, flags=flags)
    text = re.sub(r"\[i\](.*?)\[/i\]", r"*\1*", text, flags=flags)
    text = re.sub(r"\[u\](.*?)\[/u\]", r"__\1__", text, flags=flags)
    text = re.sub(r"\[strike\](.*?)\[/strike\]", r"~~\1~~", text, flags=flags)
    text = re.sub(r"\[spoiler\](.*?)\[/spoiler\]", r"||\1||", text, flags=flags)
    text = re.sub(r"\[url=([^\]]+)\](.*?)\[/url\]", r"[\2](\1)", text, flags=flags)
    text = re.sub(r"\[/?[a-zA-Z0-9]+(?:=[^\]]*)?\]", "", text)
    # A space after labels such as "**Note:**Text".
    text = re.sub(r"(\*\*[^*\n]{1,40}:\*\*|__[^_\n]{1,40}:__)(?=\S)", r"\1 ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(
        r"(?m)^\*\*(.+?)\*\*[ \t]*$",
        lambda m: "**" + decorate_heading(m.group(1)) + "**",
        text,
    )
    text = decorate_labels(text)
    return text.strip()


def trim_news(text):
    if len(text) <= NEWS_MAX_CHARS:
        return text
    cut = text[:NEWS_MAX_CHARS]
    if "\n" in cut:
        cut = cut.rsplit("\n", 1)[0]
    return cut.rstrip() + "\n\n…"


def news_image(contents):
    m = re.search(r"\[img\]\{STEAM_CLAN_IMAGE\}(.+?)\[/img\]", contents, re.I)
    if m:
        return "https://clan.fastly.steamstatic.com/images/" + m.group(1).strip()
    m = re.search(r"\[img\](https?://.+?)\[/img\]", contents, re.I)
    return m.group(1).strip() if m else None


def news_embed(item):
    url = f"https://store.steampowered.com/news/app/{STEAM_APP_ID}/view/{item.get('gid')}"
    contents = item.get("contents", "")
    body = trim_news(
        drop_title_line(steam_bbcode_to_markdown(contents), item.get("title") or "")
    )
    embed = discord.Embed(
        title=(item.get("title") or "Enshrouded news")[:256],
        url=url,
        description=f"{body}\n\n[Read the full post on Steam]({url})",
        colour=discord.Colour.from_str("#C9A227"),
        timestamp=datetime.fromtimestamp(item.get("date", 0), tz=timezone.utc),
    )
    embed.set_author(
        name="BajuBesi Gaming Network • Enshrouded News",
        icon_url=ENSHROUDED_ICON_URL,
    )
    embed.set_thumbnail(url=ENSHROUDED_ICON_URL)
    if NEWS_SERVER_NOTE:
        embed.add_field(
            name="<:tipidea:1560389964301074612> Server note",
            value=NEWS_SERVER_NOTE,
            inline=False,
        )
    image = news_image(contents)
    if image:
        embed.set_image(url=image)
    embed.set_footer(text="Source: Steam • Keen Games")
    return embed


async def run_control(action, timeout=30):
    if action not in {"status", "health", "stats", "start", "stop", "restart", "logs"}:
        raise ValueError("Unsupported Enshrouded action")

    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", CONTROL, action,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "Controller timed out."

    return proc.returncode, output.decode(errors="replace").strip()


def parse_stats(output):
    """Parse the controller's `stats` line: 'cpu%|used / limit|mem%|cores'.

    Docker reports CPU as a percentage of ONE core (it can exceed 100%), so it
    is divided by the core count to give a share of the whole machine.
    Returns a dict of display strings, or None if the line can't be parsed.
    """
    try:
        line = output.strip().splitlines()[-1]
        cpu, mem, mem_pct, cores = [part.strip() for part in line.split("|")]
        host_cpu = float(cpu.rstrip("%")) / max(int(cores), 1)
        return {
            "cpu": f"{min(host_cpu, 100.0):.1f}%",
            "mem": mem,
            "mem_pct": mem_pct,
        }
    except (ValueError, IndexError):
        return None


async def get_stats():
    """Return CPU/RAM display strings, or None if unavailable."""
    try:
        code, output = await run_control("stats", timeout=20)
    except Exception as exc:
        log.debug("Stats query failed: %r", exc)
        return None
    return parse_stats(output) if code == 0 else None


async def get_status():
    """Return (status_line, health_line, players, stats)."""
    (_, status), (_, health) = await asyncio.gather(
        run_control("status"), run_control("health")
    )
    if health.startswith("HEALTHY:"):
        players, stats = await asyncio.gather(get_players(), get_stats())
    else:
        players = stats = None
    return status, health, players, stats


# ------------------------------------------------------------------ views

class ConfirmControl(discord.ui.View):
    def __init__(self, cog, action, requester_id):
        super().__init__(timeout=60)
        self.cog = cog
        self.action = action
        self.requester_id = requester_id

    async def interaction_check(self, interaction):
        if not has_root_role(interaction.user):
            await interaction.response.send_message(
                "Access denied. @Root Administrator is required.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass

        timeout = 60 if self.action == "stop" else 30
        code, output = await run_control(self.action, timeout=timeout)

        if code != 0:
            await interaction.followup.send(
                f"❌ `{self.action}` failed (exit {code}).\n"
                f"```text\n{output[-1500:] or 'No output'}\n```",
                ephemeral=True,
            )
            return

        if self.action in {"start", "restart"}:
            await interaction.followup.send(
                "⏳ Command accepted. Checking for the game process…",
                ephemeral=True,
            )
            for _ in range(60):
                await asyncio.sleep(5)
                health_code, health = await run_control("health", timeout=20)
                if health_code == 0 and health.startswith("HEALTHY:"):
                    await interaction.followup.send(
                        "✅ Enshrouded is running and the game process was detected.",
                        ephemeral=True,
                    )
                    await self.cog.refresh_panels(interaction.guild)
                    return
            await interaction.followup.send(
                "⚠️ The container command succeeded, but the game process "
                "was not confirmed healthy within five minutes. Check "
                "`/enshrouded logs`.",
                ephemeral=True,
            )
            await self.cog.refresh_panels(interaction.guild)
            return

        await interaction.followup.send(
            f"✅ Enshrouded `{self.action}` command completed.\n"
            f"```text\n{output[-1200:] or 'Completed.'}\n```",
            ephemeral=True,
        )
        await self.cog.refresh_panels(interaction.guild)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="Operation cancelled.", view=self
        )


# -------------------------------------------------------------------- cog

class EnshroudedManager(commands.Cog):
    """Manage the BajuBesi Enshrouded dedicated server."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=781246390512, force_registration=True
        )
        self.config.register_guild(
            alert_channel=None,
            panel_channel=None,
            panel_message=None,
            last_health=None,
            peak_date=None,
            peak_players=0,
            last_down=None,
            news_last_gid=None,
        )
        self.health_monitor.start()
        self.news_monitor.start()

    def cog_unload(self):
        self.health_monitor.cancel()
        self.news_monitor.cancel()

    @commands.hybrid_group(name="enshrouded", invoke_without_command=True)
    async def enshrouded(self, ctx):
        """Manage the BajuBesi Enshrouded server."""
        await self.status(ctx)

    @enshrouded.command(name="status")
    async def status(self, ctx):
        """Show container and game-process status."""
        if ctx.guild is None:
            await ctx.send("Use this command in a server.")
            return
        await ctx.defer()
        try:
            state, health, players, stats = await get_status()
            activity = await self.activity(ctx.guild)
            message = await ctx.send(
                embed=status_embed(state, health, players, activity),
                delete_after=STATUS_LIFETIME,
            )
            await self.replace_status(ctx, message)
        except Exception:
            log.exception("Status command failed")
            await ctx.send("Could not retrieve Enshrouded status.")

    @enshrouded.command(name="panel")
    @commands.check(root_admin_check)
    async def panel(self, ctx):
        """Create or replace the persistent status panel here."""
        if ctx.guild is None:
            return
        # Slash commands must answer within 3 seconds; the controller calls
        # can take longer, so acknowledge first.
        await ctx.defer()
        state, health, players, stats = await get_status()
        activity = await self.activity(ctx.guild)
        message = await ctx.send(
            embed=health_embed(state, health, players, activity, stats)
        )
        await self.delete_old_panel(ctx.guild, keep_id=message.id)
        await self.config.guild(ctx.guild).panel_channel.set(ctx.channel.id)
        await self.config.guild(ctx.guild).panel_message.set(message.id)
        await self.config.guild(ctx.guild).alert_channel.set(ctx.channel.id)
        await ctx.send(
            "✅ Dashboard created. Health alerts will be sent to this channel.",
            delete_after=10,
        )
        if ctx.interaction is None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

    async def delete_old_panel(self, guild, keep_id=None):
        """Delete the previously stored panel message, if there is one."""
        data = await self.config.guild(guild).all()
        channel_id = data.get("panel_channel")
        message_id = data.get("panel_message")
        if not channel_id or not message_id or message_id == keep_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            return
        try:
            old = await channel.fetch_message(message_id)
            await old.delete()
        except discord.HTTPException:
            pass

    async def replace_status(self, ctx, message):
        """Keep only the newest status card per channel."""
        last = self.__dict__.setdefault("_last_status", {})
        old = last.get(ctx.channel.id)
        last[ctx.channel.id] = message
        if old is not None and old.id != message.id:
            try:
                await old.delete()
            except discord.HTTPException:
                pass
        if ctx.interaction is None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

    async def activity(self, guild):
        """Peak players today and the last downtime, for the panel."""
        data = await self.config.guild(guild).all()
        today = datetime.now(LOCAL_TZ).date().isoformat()
        peak = data.get("peak_players") or 0
        if data.get("peak_date") != today:
            peak = 0  # stored peak is from an earlier day
        return {"peak": peak, "last_down": data.get("last_down")}

    async def refresh_panels(self, guild, snapshot=None):
        """Edit the panel message. `snapshot` = (status, health, players, stats) to avoid
        re-running the controller when the caller already has fresh data."""
        if guild is None:
            return
        data = await self.config.guild(guild).all()
        channel_id = data.get("panel_channel")
        message_id = data.get("panel_message")
        if not channel_id or not message_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(message_id)

            # Detect a second writer: our embeds always carry PANEL_TAG.
            if message.embeds:
                footer = message.embeds[0].footer
                footer_text = footer.text if footer else ""
                if PANEL_TAG not in (footer_text or ""):
                    log.warning(
                        "Panel %s was overwritten by something else "
                        "(title=%r footer=%r). Another cog/script using this "
                        "bot token is editing the same message.",
                        message.id, message.embeds[0].title, footer_text,
                    )

            state, health, players, stats = snapshot or await get_status()
            activity = await self.activity(guild)
            # view=None also strips the old Refresh button from existing panels.
            await message.edit(
                embed=health_embed(state, health, players, activity, stats),
                view=None,
            )
        except discord.NotFound:
            log.warning("Enshrouded dashboard message was deleted")
        except Exception:
            log.exception("Could not update Enshrouded dashboard")

    async def control_prompt(self, ctx, action):
        if ctx.guild is None:
            return
        view = ConfirmControl(self, action, ctx.author.id)
        await ctx.send(
            f"⚠️ Confirm Enshrouded **{action}**?\n"
            "This operation affects the dedicated server.",
            view=view,
            ephemeral=True,
        )

    @enshrouded.command(name="start")
    @commands.check(root_admin_check)
    async def start(self, ctx):
        """Start the dedicated server."""
        await self.control_prompt(ctx, "start")

    @enshrouded.command(name="stop")
    @commands.check(root_admin_check)
    async def stop(self, ctx):
        """Stop the dedicated server."""
        await self.control_prompt(ctx, "stop")

    @enshrouded.command(name="restart")
    @commands.check(root_admin_check)
    async def restart(self, ctx):
        """Restart the dedicated server."""
        await self.control_prompt(ctx, "restart")

    @enshrouded.command(name="logs")
    @commands.check(root_admin_check)
    async def logs(self, ctx):
        """Show recent container logs."""
        await ctx.defer()
        code, output = await run_control("logs", timeout=30)
        if not output:
            output = "No log output."
        output = output[-1700:]
        await ctx.send(
            f"{'✅' if code == 0 else '⚠️'} Recent Enshrouded logs:\n"
            f"```text\n{output}\n```"
        )

    async def post_news(self, channel, item):
        content = f"<@&{NEWS_PING_ROLE_ID}>" if NEWS_PING_ROLE_ID else None
        await channel.send(
            content=content,
            embed=news_embed(item),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )

    @enshrouded.command(name="latestnews")
    @commands.check(root_admin_check)
    async def latestnews(self, ctx):
        """Preview the newest developer post here (no ping)."""
        await ctx.defer()
        items = await fetch_news()
        if not items:
            await ctx.send("Could not fetch Steam news right now.")
            return
        await ctx.send(embed=news_embed(items[0]))

    @tasks.loop(minutes=NEWS_POLL_MINUTES)
    async def news_monitor(self):
        channel = self.bot.get_channel(NEWS_CHANNEL_ID)
        if channel is None:
            log.warning("Enshrouded news channel %s not found or not visible", NEWS_CHANNEL_ID)
            return
        items = await fetch_news()
        if not items:
            return
        try:
            guild = channel.guild
            gids = [i["gid"] for i in items]
            last = await self.config.guild(guild).news_last_gid()
            if last is None:
                # First run: remember what already exists, so old posts are not re-posted.
                await self.config.guild(guild).news_last_gid.set(gids[0])
                return
            new = items[: gids.index(last)] if last in gids else items[:1]
            for item in reversed(new):
                await self.post_news(channel, item)
            if new:
                await self.config.guild(guild).news_last_gid.set(new[0]["gid"])
        except Exception:
            log.exception("Enshrouded news check failed")

    @enshrouded.command(name="testnews")
    @commands.check(root_admin_check)
    async def testnews(self, ctx):
        """End-to-end test: re-post the newest announcement via the automatic path."""
        await ctx.defer()
        channel = self.bot.get_channel(NEWS_CHANNEL_ID)
        if channel is None:
            await ctx.send(
                "❌ I can't see the news channel. Check the channel ID and that "
                "the bot has View Channel there."
            )
            return
        perms = channel.permissions_for(channel.guild.me)
        missing = [
            name
            for name, ok in (
                ("View Channel", perms.view_channel),
                ("Send Messages", perms.send_messages),
                ("Embed Links", perms.embed_links),
            )
            if not ok
        ]
        if missing:
            await ctx.send(
                f"❌ Missing permissions in {channel.mention}: {', '.join(missing)}"
            )
            return
        items = await fetch_news()
        if not items or len(items) < 2:
            await ctx.send("❌ Could not fetch enough Steam announcements to run the test.")
            return
        guild = channel.guild
        # Pretend the newest post is unseen, then run the real automatic check once.
        await self.config.guild(guild).news_last_gid.set(items[1]["gid"])
        await self.news_monitor()
        done = await self.config.guild(guild).news_last_gid() == items[0]["gid"]
        if done:
            await ctx.send(
                f"✅ Test passed. The automatic path posted the newest announcement "
                f"in {channel.mention}."
            )
        else:
            await ctx.send(
                "❌ The automatic path did not post. Check the bot log for "
                "`Enshrouded news check failed`."
            )

    @news_monitor.before_loop
    async def before_news_monitor(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=60)
    async def health_monitor(self):
        for guild in self.bot.guilds:
            try:
                data = await self.config.guild(guild).all()
                channel_id = data.get("alert_channel")
                if not channel_id:
                    continue

                status, health, players, stats = await get_status()
                current = "healthy" if health.startswith("HEALTHY:") else "unhealthy"
                previous = data.get("last_health")
                await self.config.guild(guild).last_health.set(current)

                maint = self.__dict__.setdefault("_maint", set())
                msgs = self.__dict__.setdefault("_maint_msgs", {})
                handled = False
                channel = guild.get_channel(channel_id)
                if channel and previous and previous != current:
                    if current == "unhealthy" and in_maintenance_window():
                        maint.add(guild.id)
                        handled = True
                        msgs[guild.id] = [await channel.send(MAINT_DOWN_MSG)]
                    elif current == "healthy" and guild.id in maint:
                        maint.discard(guild.id)
                        handled = True
                        await channel.send(MAINT_UP_MSG, delete_after=MAINT_MSG_LIFETIME)
                        for old in msgs.pop(guild.id, []):
                            try:
                                await old.delete(delay=MAINT_MSG_LIFETIME)
                            except discord.HTTPException:
                                pass
                if (
                    channel
                    and current == "unhealthy"
                    and guild.id in maint
                    and not in_maintenance_window()
                ):
                    maint.discard(guild.id)
                    alert = await channel.send(
                        "🚨 **Enshrouded server health alert**\n"
                        "The server is still not back after the maintenance window. "
                        "An administrator should check `/enshrouded logs`."
                    )
                    msgs.setdefault(guild.id, []).append(alert)

                if current == "healthy" and guild.id not in maint and msgs.get(guild.id):
                    for old in msgs.pop(guild.id):
                        try:
                            await old.delete(delay=MAINT_MSG_LIFETIME)
                        except discord.HTTPException:
                            pass

                if previous and previous != current and not handled:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        if current == "unhealthy":
                            await channel.send(
                                "🚨 **Enshrouded server health alert**\n"
                                "The container or game process is not healthy. "
                                "An administrator should check `/enshrouded logs`."
                            )
                        else:
                            await channel.send(
                                "✅ **Enshrouded server recovered.** "
                                "The game process is responding to the health check."
                            )

                # Bookkeeping for the panel's Activity section.
                today = datetime.now(LOCAL_TZ).date().isoformat()
                peak_date, peak = next_peak(
                    data.get("peak_date"), data.get("peak_players"),
                    today, players,
                )
                await self.config.guild(guild).peak_date.set(peak_date)
                await self.config.guild(guild).peak_players.set(peak)
                if previous == "healthy" and current == "unhealthy" and guild.id not in maint:
                    await self.config.guild(guild).last_down.set(
                        int(datetime.now(timezone.utc).timestamp())
                    )

                await self.refresh_panels(
                    guild, snapshot=(status, health, players, stats)
                )
            except Exception:
                log.exception("Enshrouded health monitoring failed")

    @health_monitor.before_loop
    async def before_health_monitor(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(EnshroudedManager(bot))
