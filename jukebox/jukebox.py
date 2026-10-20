from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from typing import Optional

import discord
from discord.ext import tasks
from redbot.core import commands, Config

try:
    import lavalink
except ImportError:
    lavalink = None

try:
    from redbot.cogs.audio.audio_dataclasses import Query
except ImportError as exc:
    Query = None
    _QUERY_IMPORT_ERROR = exc
else:
    _QUERY_IMPORT_ERROR = None

log = logging.getLogger("red.jukebox")

JUKEBOX_TIP = '<:tipidea:1560389964301074612> Want to add a song? Type `!play song name` in chat and it joins the queue after the current track.'


def _icon(bot, names, fallback):
    """Return a custom emoji by name (first match), else a unicode fallback."""
    for name in names:
        emoji = discord.utils.get(bot.emojis, name=name)
        if emoji is not None:
            return str(emoji)
    return fallback


class InteractionContext:
    """Small Context-compatible adapter for Red Audio's enqueue helper."""
    def __init__(self, interaction: discord.Interaction):
        self.interaction = interaction
        self.bot = interaction.client
        self.guild = interaction.guild
        self.author = interaction.user
        self.channel = interaction.channel
        self.message = interaction.message

    async def send(self, *args, **kwargs):
        # Modal/button callbacks defer before this adapter is used.
        kwargs.setdefault("ephemeral", True)
        return await self.interaction.followup.send(*args, **kwargs)


class SongRequestModal(discord.ui.Modal, title="Request a song"):
    song = discord.ui.TextInput(
        label="Song name or YouTube URL",
        placeholder="Search a song or paste a YouTube link",
        required=True,
        max_length=500,
    )

    def __init__(self, cog: "Jukebox"):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.request_song(interaction, str(self.song.value))


class JukeboxView(discord.ui.LayoutView):
    """Persistent Components V2 music panel with controls inside one container."""

    def __init__(self, cog: "Jukebox", track=None, requester=None, status=None):
        super().__init__(timeout=None)
        self.cog = cog
        status = status or {}
        accent = discord.Colour(0x00D9FF)

        if track is None:
            song_request = self._buttons([
                ("Song Request", "🎵", "jukebox:idle_request",
                 discord.ButtonStyle.primary, "open_song_request_modal"),
            ])[0]

            website = discord.ui.Button(
                label="Website", emoji="🌐",
                style=discord.ButtonStyle.link,
                url="https://bajubesi.org",
            )
            support = discord.ui.Button(
                label="Support", emoji="🎧",
                style=discord.ButtonStyle.link,
                url="https://www.bajubesi.org/v3/misc/contact",
            )
            forum = discord.ui.Button(
                label="Forum", emoji="💬",
                style=discord.ButtonStyle.link,
                url="https://bajubesi.org/v3/",
            )

            self.add_item(discord.ui.Container(
                discord.ui.TextDisplay(
                    "## <:music_np:1559716000654557204> Playback Finished"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay(
                    "All queued songs have been played!\n\n"
                    "Want to keep listening? Add another song using the "
                    "**Song Request** button below, or type **!play song name** "
                    "in chat.\n\n"
                    "**Example:** `!play Linkin Park Numb`"
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(
                    song_request, website, support, forum
                ),
                accent_colour=accent,
            ))
            return

        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## <:music_np:1559716000654557204> Now Playing"),
            discord.ui.Separator(),
            self._track_section(track, requester),
            discord.ui.Separator(),
            discord.ui.TextDisplay(status.get(
                "line",
                "-# Volume: — | Loop: Off | Autoplay: Off | Queue: None | Filters: None"
            )),
            discord.ui.Separator(),
            discord.ui.ActionRow(*self._buttons([
                ("Previous", discord.PartialEmoji(name="previous", id=1559702310266085437), "jukebox:previous", discord.ButtonStyle.secondary, "previous_track"),
                ("Loop: " + status.get("loop_label", "Off").replace("Loop: ", ""), discord.PartialEmoji(name="repeat", id=1559702459876646982), "jukebox:loop", discord.ButtonStyle.secondary, "toggle_loop"),
                (status.get("pause_label", "Pause"), discord.PartialEmoji(name="pause", id=1559702281295888475), "jukebox:pause", discord.ButtonStyle.secondary, "toggle_pause"),
                ("Shuffle", discord.PartialEmoji(name="shuffle", id=1559702498716160100), "jukebox:shuffle", discord.ButtonStyle.secondary, "shuffle_queue"),
                ("Skip", discord.PartialEmoji(name="next", id=1559702252136960140), "jukebox:skip", discord.ButtonStyle.secondary, "skip_track"),
            ])),
            discord.ui.ActionRow(*self._buttons([
                ("-10s", discord.PartialEmoji(name="back10", id=1559702093831344279), "jukebox:back10", discord.ButtonStyle.secondary, "seek_back"),
                ("Queue", discord.PartialEmoji(name="queue", id=1559702377953755186), "jukebox:queue", discord.ButtonStyle.secondary, "show_queue"),
                ("Stop", discord.PartialEmoji(name="stop", id=1559702529883775076), "jukebox:stop", discord.ButtonStyle.danger, "stop_track"),
                ("Filters", discord.PartialEmoji(name="filters", id=1559702121027477545), "jukebox:filters", discord.ButtonStyle.secondary, "show_filters"),
                ("+10s", discord.PartialEmoji(name="forward10", id=1559702156108632095), "jukebox:forward10", discord.ButtonStyle.secondary, "seek_forward"),
            ])),
            discord.ui.ActionRow(*self._buttons([
                ("Vol -", discord.PartialEmoji(name="volume_down", id=1559702558057046066), "jukebox:volume_down", discord.ButtonStyle.secondary, "volume_down"),
                ("Grab", discord.PartialEmoji(name="grab", id=1559702193886462024), "jukebox:grab", discord.ButtonStyle.secondary, "grab_track"),
                ("Like", discord.PartialEmoji(name="like", id=1559702223548850287), "jukebox:like", discord.ButtonStyle.secondary, "like_track"),
                (status.get("autoplay_label", "Autoplay: Off"), discord.PartialEmoji(name="autoplay", id=1559702064550903969), "jukebox:autoplay", discord.ButtonStyle.secondary, "toggle_autoplay"),
                ("Vol +", discord.PartialEmoji(name="volume_up", id=1559702582937526292), "jukebox:volume_up", discord.ButtonStyle.secondary, "volume_up"),
            ])),
            accent_colour=accent,
        ))
        self.add_item(discord.ui.TextDisplay(JUKEBOX_TIP))

    @staticmethod
    def _safe_title(title: str, limit: int = 50) -> str:
        title = str(title).replace("||", " ")
        title = "".join(
            ch for ch in title
            if ch not in ("\ufe0f", "\ufe0e")
            and unicodedata.category(ch) not in ("So", "Sk", "Cs", "Cf", "Co", "Cn")
        )
        title = title.replace("[", "(").replace("]", ")")
        title = re.sub(r"\s+", " ", title).strip(" -|")
        if len(title) > limit:
            title = title[:limit].rstrip() + "…"
        return discord.utils.escape_markdown(title) or "Unknown track"

    @staticmethod
    def _track_section(track, requester):
        if track is None:
            return discord.ui.TextDisplay(
                "Nothing is playing right now. Use `!play song name` to start listening."
            )
        title = getattr(track, "title", None) or "Unknown track"
        uri = getattr(track, "uri", None)
        artist = getattr(track, "author", None) or "Unknown artist"
        length = getattr(track, "length", None)
        if length and not getattr(track, "is_stream", False):
            seconds = max(0, int(length // 1000))
            h, rem = divmod(seconds, 3600)
            m, sec = divmod(rem, 60)
            duration = f"{h}h {m:02d}m" if h else f"{m}m {sec:02d}s"
        else:
            duration = "Live / Unknown"
        who = f"<@{requester.id}>" if requester and getattr(requester, "id", None) else (getattr(requester, "display_name", None) or getattr(requester, "name", "Unknown") if requester else "Unknown")
        safe_title = JukeboxView._safe_title(title)
        safe_uri = uri.replace(")", "%29").replace(" ", "%20") if uri else None

        content = f"### [{safe_title}]({safe_uri})\n" if safe_uri else f"### {safe_title}\n"
        content += (
            "\n"
            f"<:artist:1559711514011766936> **Artist:** {artist}\n"
            f"<:duration:1559711400002191410> **Duration:** {duration}\n"
            f"<:requester:1559711219710033990> **Requester:** {who}"
        )
        thumbnail = getattr(track, "thumbnail", None)
        if thumbnail and str(thumbnail).startswith(("https://", "http://")):
            return discord.ui.Section(
                discord.ui.TextDisplay(content),
                accessory=discord.ui.Thumbnail(media=thumbnail),
            )
        return discord.ui.TextDisplay(content)

    def _buttons(self, definitions):
        buttons = []
        for label, emoji, custom_id, style, method_name in definitions:
            button = discord.ui.Button(
                label=label, emoji=emoji, style=style, custom_id=custom_id
            )

            async def callback(interaction, name=method_name):
                if name == "seek_back":
                    await self.cog.seek_by(interaction, -10)
                elif name == "seek_forward":
                    await self.cog.seek_by(interaction, 10)
                elif name == "volume_down":
                    await self.cog.adjust_volume(interaction, -10)
                elif name == "volume_up":
                    await self.cog.adjust_volume(interaction, 10)
                else:
                    await getattr(self.cog, name)(interaction)

            button.callback = callback
            buttons.append(button)
        return buttons



class Jukebox(commands.Cog):
    """Persistent button panel for the existing Red Audio cog."""

    @commands.command(name="jukeboxemojiids")
    @commands.guild_only()
    async def jukebox_emoji_ids(self, ctx):
        wanted = {
            "previous", "repeat", "pause", "shuffle", "next",
            "back10", "queue", "stop", "filters", "forward10",
            "volume_down", "grab", "like", "autoplay", "volume_up",
        }
        found = {
            emoji.name: emoji.id
            for emoji in ctx.guild.emojis
            if emoji.name in wanted
        }

        lines = [
            f"{name}: {found[name]}"
            for name in sorted(wanted)
            if name in found
        ]
        missing = sorted(wanted - found.keys())

        message = "**Jukebox emoji IDs**\n"
        message += "\n".join(lines) if lines else "No matching emoji found."
        if missing:
            message += "\n\n**Missing:** " + ", ".join(missing)

        await ctx.send(
            message,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=834726195018, force_registration=True)
        self.config.register_guild(panel_channel_id=None, panel_message_id=None)
        self.config.register_user(liked_tracks=[])
        self._requesters = {}
        self.view = JukeboxView(self)
        self.bot.add_view(self.view)
        self._refresh_tasks = {}
        self._last_state = {}
        self.panel_watchdog.start()

    async def cog_load(self):
        # Persistent custom IDs are registered in __init__. Existing legacy
        # embed panels cannot be converted in-place to Components V2; run
        # [p]jukebox setup once to create the new panel safely.
        for guild in self.bot.guilds:
            data = await self.config.guild(guild).all()
            if data.get("panel_channel_id") and data.get("panel_message_id"):
                channel = guild.get_channel(data["panel_channel_id"])
                if channel:
                    try:
                        await channel.fetch_message(data["panel_message_id"])
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        log.warning("Could not find saved Jukebox panel in guild %s; run jukebox setup", guild.id)

    def audio_cog(self):
        audio = self.bot.get_cog("Audio")
        if audio is None:
            audio = self.bot.get_cog("audio")
        return audio

    async def player_for(self, interaction: discord.Interaction, connect: bool = False):
        if interaction.guild is None:
            raise RuntimeError("Use this panel in a server.")
        if lavalink is None:
            raise RuntimeError("The Lavalink Python library could not be imported.")
        if connect:
            voice = getattr(getattr(interaction.user, "voice", None), "channel", None)
            if voice is None:
                raise RuntimeError("Join a voice channel first, then request a song.")
        try:
            player = lavalink.get_player(interaction.guild.id)
        except Exception:
            player = None
        if player is None and connect:
            await lavalink.connect(voice, self_deaf=True)
            player = lavalink.get_player(interaction.guild.id)
        if player is None:
            raise RuntimeError("Nothing is playing yet. Request a song first.")
        if connect:
            voice = interaction.user.voice.channel
            if player.channel and player.channel.id != voice.id:
                raise RuntimeError(f"The bot is already playing in **{player.channel.name}**. Join that voice channel to control it.")
        return player

    async def open_song_request_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SongRequestModal(self))

    async def request_song(self, interaction: discord.Interaction, text: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            audio = self.audio_cog()
            if audio is None:
                raise RuntimeError("Red Audio is not loaded. Load the Audio cog first.")
            if Query is None:
                raise RuntimeError(f"Could not import Red Audio's Query class: {_QUERY_IMPORT_ERROR}")
            player = await self.player_for(interaction, connect=True)
            # Ensure the player is attached to the requesting voice channel.
            query = Query.process_input(text, audio.local_folder_current_path)
            ctx = InteractionContext(interaction)
            ctx.author = interaction.user
            ctx.guild = interaction.guild
            ctx.channel = interaction.channel
            # Reuse Red Audio's own restrictions, metadata, queue insertion and playback logic.
            result = await audio._enqueue_tracks(ctx, query)
            self.schedule_refresh(interaction.guild)
            if result is None:
                await interaction.followup.send("Request processed.", ephemeral=True)
        except Exception as exc:
            log.exception("Jukebox song request failed")
            await interaction.followup.send(f"Couldn't queue that song: {exc}", ephemeral=True)

    async def show_queue(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            player = await self.player_for(interaction)
            lines = []
            current = getattr(player, "current", None)
            if current:
                title_for_link = JukeboxView._safe_title(current.title, 60)
                lines.append(f"**Now playing:** [{title_for_link}]({current.uri})")

            queue = list(getattr(player, "queue", []) or [])
            for index, track in enumerate(queue[:15], start=1):
                title_for_link = JukeboxView._safe_title(track.title, 60)
                lines.append(f"**{index}.** [{title_for_link}]({track.uri})")

            if len(queue) > 15:
                lines.append(f"…and {len(queue) - 15} more queued tracks.")
            if not lines:
                lines = ["The queue is empty."]
            embed = discord.Embed(title="🎵 Jukebox Queue", description="\n".join(lines), color=discord.Color.blurple())
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    async def toggle_pause(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            player = await self.player_for(interaction, connect=True)
            paused = bool(getattr(player, "paused", False))
            await player.pause(not paused)
            self.schedule_refresh(interaction.guild)
            await interaction.followup.send("Playback resumed." if paused else "Playback paused.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    async def skip_track(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            player = await self.player_for(interaction, connect=True)
            if not getattr(player, "current", None):
                raise RuntimeError("Nothing is currently playing.")
            await player.skip()
            await interaction.followup.send("Skipped the current track.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    async def stop_track(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            if not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.manage_messages):
                raise RuntimeError("Only moderators with Manage Server or Manage Messages can stop playback.")
            player = await self.player_for(interaction, connect=True)
            player.queue.clear()
            await player.stop()
            self.schedule_refresh(interaction.guild)
            await interaction.followup.send("Playback stopped and the queue cleared.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)


    async def previous_track(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            player = await self._control_player(interaction)
            if not getattr(player, "current", None):
                raise RuntimeError("Nothing is playing.")
            await player.seek(0)
            await interaction.followup.send("Restarted the current track. Previous-track history is not available in this controller yet.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    async def show_filters(self, interaction):
        await interaction.response.send_message("Filters are not configured yet; I need to verify the installed Lavalink filter API before enabling audio effects.", ephemeral=True)

    async def _control_player(self, interaction):
        player = await self.player_for(interaction, connect=True)
        voice = getattr(getattr(interaction.user, "voice", None), "channel", None)
        if voice is None or player.channel is None or voice.id != player.channel.id:
            raise RuntimeError("Join the bot's voice channel to use this control.")
        return player

    async def toggle_loop(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            audio = self.audio_cog()
            if audio is None:
                raise RuntimeError("Red Audio is not loaded.")
            cfg = audio.config.guild(interaction.guild)
            enabled = not await cfg.repeat()
            await cfg.repeat.set(enabled)
            try:
                lavalink.get_player(interaction.guild.id).repeat = enabled
            except Exception:
                pass
            await self._refresh_panel(interaction.guild)
            await interaction.followup.send(f"Repeat {'enabled' if enabled else 'disabled'}.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    async def shuffle_queue(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            player = await self._control_player(interaction)
            if not player.queue:
                raise RuntimeError("There are no queued tracks to shuffle.")
            player.force_shuffle(0)
            self.schedule_refresh(interaction.guild)
            await interaction.followup.send("Queue shuffled.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    async def seek_by(self, interaction, seconds):
        await interaction.response.defer(ephemeral=True)
        try:
            player = await self._control_player(interaction)
            current = getattr(player, "current", None)
            if current is None:
                raise RuntimeError("Nothing is playing.")
            if getattr(current, "is_stream", False):
                raise RuntimeError("Seeking is unavailable for live streams.")
            length = int(getattr(current, "length", 0) or 0)
            position = max(0, int(getattr(player, "position", 0) or 0) + seconds * 1000)
            if length:
                position = min(position, max(0, length - 1000))
            await player.seek(position)
            await interaction.followup.send(f"Seeked to {position // 60000}:{(position // 1000) % 60:02d}.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    async def adjust_volume(self, interaction, amount):
        await interaction.response.defer(ephemeral=True)
        try:
            player = await self._control_player(interaction)
            volume = max(0, min(150, int(getattr(player, "volume", 100) or 0) + amount))
            await player.set_volume(volume)
            self.schedule_refresh(interaction.guild)
            await interaction.followup.send(f"Volume set to {volume}%.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    async def toggle_autoplay(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            audio = self.audio_cog()
            if audio is None:
                raise RuntimeError("Red Audio is not loaded.")
            cfg = audio.config.guild(interaction.guild)
            enabled = not await cfg.auto_play()
            await cfg.auto_play.set(enabled)
            await self._refresh_panel(interaction.guild)
            await interaction.followup.send(f"Autoplay {'enabled' if enabled else 'disabled'}.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    async def grab_track(self, interaction):
        try:
            player = await self._control_player(interaction)
            track = getattr(player, "current", None)
            uri = getattr(track, "uri", None) if track else None
            if not uri:
                raise RuntimeError("The current track has no shareable URL.")
            await interaction.response.send_message(f"Current track: {uri}", ephemeral=True)
        except Exception as exc:
            if interaction.response.is_done():
                await interaction.followup.send(str(exc), ephemeral=True)
            else:
                await interaction.response.send_message(str(exc), ephemeral=True)

    async def like_track(self, interaction):
        try:
            player = await self._control_player(interaction)
            track = getattr(player, "current", None)
            uri = getattr(track, "uri", None) if track else None
            if not uri:
                raise RuntimeError("Nothing with a shareable URL is playing.")
            liked = await self.config.user(interaction.user).liked_tracks()
            entry = {"title": str(getattr(track, "title", "Unknown track"))[:200], "uri": uri}
            if any(item.get("uri") == uri for item in liked if isinstance(item, dict)):
                await interaction.response.send_message("You've already liked this track.", ephemeral=True)
                return
            liked.append(entry)
            await self.config.user(interaction.user).liked_tracks.set(liked[-100:])
            await interaction.response.send_message(f"❤️ Saved **{entry['title']}** to your Jukebox likes.", ephemeral=True)
        except Exception as exc:
            if interaction.response.is_done():
                await interaction.followup.send(str(exc), ephemeral=True)
            else:
                await interaction.response.send_message(str(exc), ephemeral=True)

    def _jukebox_duration(self, milliseconds):
        if not milliseconds:
            return "Live / Unknown"
        seconds = max(0, int(milliseconds // 1000))
        return f"{seconds // 60}:{seconds % 60:02d}"

    async def cog_unload(self):
        self.panel_watchdog.cancel()
        for task in self._refresh_tasks.values():
            task.cancel()

    @staticmethod
    def _state_sig(player):
        cur = getattr(player, "current", None)
        return (
            getattr(cur, "uri", None),
            len(getattr(player, "queue", []) or []),
            getattr(player, "volume", None),
            bool(getattr(player, "paused", False)),
            bool(getattr(player, "repeat", False)),
        )

    def schedule_refresh(self, guild, delay: float = 1.5):
        # Debounced: a burst of changes (e.g. a playlist) becomes one edit.
        old = self._refresh_tasks.get(guild.id)
        if old and not old.done():
            old.cancel()

        async def runner():
            try:
                await asyncio.sleep(delay)
                await self._refresh_panel(guild)
                if lavalink is not None:
                    try:
                        self._last_state[guild.id] = self._state_sig(lavalink.get_player(guild.id))
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        self._refresh_tasks[guild.id] = asyncio.create_task(runner())

    @tasks.loop(seconds=10)
    async def panel_watchdog(self):
        if lavalink is None:
            return
        for guild in self.bot.guilds:
            try:
                player = lavalink.get_player(guild.id)
            except Exception:
                continue
            sig = self._state_sig(player)
            if self._last_state.get(guild.id) != sig:
                if await self.config.guild(guild).panel_message_id():
                    self._last_state[guild.id] = sig
                    self.schedule_refresh(guild, delay=0)

    @panel_watchdog.before_loop
    async def _before_watchdog(self):
        await self.bot.wait_until_red_ready()

    @commands.Cog.listener()
    async def on_red_audio_track_enqueue(self, guild, track, requester):
        self.schedule_refresh(guild)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx):
        # Catches !play, !volume, !pause, !remove, !shuffle, !repeat, etc.
        if ctx.guild and ctx.command and ctx.command.cog_name == "Audio":
            self.schedule_refresh(ctx.guild)

    async def _refresh_panel(self, guild, track=None, requester=None, repost=False):
        data = await self.config.guild(guild).all()
        channel_id = data.get("panel_channel_id")
        message_id = data.get("panel_message_id")

        if not channel_id or not message_id:
            log.warning("Jukebox panel IDs missing for guild %s", guild.id)
            return

        channel = guild.get_channel(channel_id)
        if channel is None:
            log.warning("Jukebox panel channel %s not found in guild %s", channel_id, guild.id)
            return

        try:
            player = None
            if lavalink is not None:
                try:
                    player = lavalink.get_player(guild.id)
                except Exception:
                    player = None

            if track is None and player is not None:
                track = getattr(player, "current", None)
            if requester is None:
                requester = self._requesters.get(guild.id) or getattr(track, "requester", None)

            audio = self.audio_cog()
            loop_enabled = False
            autoplay_enabled = False

            if audio is not None:
                cfg = audio.config.guild(guild)
                try:
                    loop_enabled = bool(await cfg.repeat())
                except Exception:
                    pass
                try:
                    autoplay_enabled = bool(await cfg.auto_play())
                except Exception:
                    pass

            queue_count = len(getattr(player, "queue", []) or []) if player else 0
            volume = getattr(player, "volume", None) if player else None
            paused = bool(getattr(player, "paused", False)) if player else False

            status = {
                "loop_label": f"Loop: {'On' if loop_enabled else 'Off'}",
                "autoplay_label": f"Autoplay: {'On' if autoplay_enabled else 'Off'}",
                "pause_label": "Resume" if paused else "Pause",
                "pause_emoji": "▶️" if paused else "⏸️",
                "line": f"-# {_icon(self.bot, ('status_volume', 'volume_up'), '🔊')} Volume: {volume if volume is not None else '—'}% | {_icon(self.bot, ('status_loop', 'repeat'), '🔁')} Loop: {'On' if loop_enabled else 'Off'} | {_icon(self.bot, ('status_autoplay', 'autoplay'), '♾️')} Autoplay: {'On' if autoplay_enabled else 'Off'} | {_icon(self.bot, ('status_queue', 'queue'), '📋')} Queue: {queue_count or 'None'} | {_icon(self.bot, ('status_filters', 'filters'), '🎚️')} Filters: None",
            }

            view = JukeboxView(self, track=track, requester=requester, status=status)

            if repost:
                old_message = None
                try:
                    old_message = await channel.fetch_message(message_id)
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    log.warning(
                        "Could not fetch old Jukebox panel %s in guild %s",
                        message_id, guild.id
                    )

                # Post the replacement first so the panel is not lost
                # if sending the new message fails.
                new_message = await channel.send(view=view, silent=True, allowed_mentions=discord.AllowedMentions.none())

                await self.config.guild(guild).panel_message_id.set(new_message.id)
                self.view = view

                if old_message is not None:
                    try:
                        await old_message.delete()
                    except discord.NotFound:
                        pass
                    except discord.HTTPException:
                        log.warning(
                            "New Jukebox panel posted, but old panel %s could not be deleted",
                            message_id
                        )

                log.info(
                    "Jukebox panel reposted at the bottom of channel %s in guild %s",
                    channel_id, guild.id
                )
            else:
                message = await channel.fetch_message(message_id)
                await message.edit(view=view)
                self.view = view

        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.exception("Could not update Jukebox panel in guild %s", guild.id)
        except Exception:
            log.exception("Unexpected Jukebox panel update error in guild %s", guild.id)

    @commands.Cog.listener()
    async def on_red_audio_track_start(self, guild, track, requester):
        log.info(
            "Jukebox DEBUG: track-start event received for guild %s (track=%s)",
            getattr(guild, "id", None), getattr(track, "title", None)
        )
        self._requesters[guild.id] = requester
        pending = self._refresh_tasks.pop(guild.id, None)
        if pending and not pending.done():
            pending.cancel()
        await self._refresh_panel(guild, track, requester, repost=True)

    @commands.Cog.listener()
    async def on_red_audio_track_end(self, guild, track, requester):
        # Avoid replacing the panel if another track has already started.
        if lavalink is not None:
            try:
                player = lavalink.get_player(guild.id)
                if getattr(player, "current", None) is not None:
                    return
            except Exception:
                pass
        await self._refresh_panel(guild)

    @commands.group(name="jukebox", invoke_without_command=True)
    @commands.guild_only()
    async def jukebox(self, ctx):
        await ctx.send_help()

    @jukebox.command(name="setup")
    @commands.admin_or_permissions(manage_guild=True)
    async def jukebox_setup(self, ctx):
        """Create or replace the persistent Components V2 Jukebox panel."""
        # Create the replacement before touching the old panel.
        view = await self._make_setup_view(ctx.guild)
        message = await ctx.send(view=view, silent=True, allowed_mentions=discord.AllowedMentions.none())

        data = await self.config.guild(ctx.guild).all()
        old_channel_id = data.get("panel_channel_id")
        old_message_id = data.get("panel_message_id")

        await self.config.guild(ctx.guild).panel_channel_id.set(ctx.channel.id)
        await self.config.guild(ctx.guild).panel_message_id.set(message.id)
        self.view = view

        if old_channel_id and old_message_id:
            old_channel = ctx.guild.get_channel(old_channel_id)
            if old_channel:
                try:
                    old_message = await old_channel.fetch_message(old_message_id)
                    if old_message.id != message.id:
                        await old_message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    log.warning("New Jukebox panel created, but the old panel could not be deleted.")

        await ctx.send("Jukebox Components V2 panel created.", delete_after=10)

    async def _make_setup_view(self, guild):
        player = None
        track = None
        if lavalink is not None:
            try:
                player = lavalink.get_player(guild.id)
                track = getattr(player, "current", None)
            except Exception:
                pass
        audio = self.audio_cog()
        loop_enabled = False
        autoplay_enabled = False
        if audio is not None:
            cfg = audio.config.guild(guild)
            try:
                loop_enabled = bool(await cfg.repeat())
            except Exception:
                pass
            try:
                autoplay_enabled = bool(await cfg.auto_play())
            except Exception:
                pass
        queue_count = len(getattr(player, "queue", []) or []) if player else 0
        volume = getattr(player, "volume", None) if player else None
        paused = bool(getattr(player, "paused", False)) if player else False
        status = {
            "loop_label": f"Loop: {'On' if loop_enabled else 'Off'}",
            "autoplay_label": f"Autoplay: {'On' if autoplay_enabled else 'Off'}",
            "pause_label": "Resume" if paused else "Pause",
            "pause_emoji": "▶️" if paused else "⏸️",
            "line": f"-# {_icon(self.bot, ('status_volume', 'volume_up'), '🔊')} Volume: {volume if volume is not None else '—'}% | {_icon(self.bot, ('status_loop', 'repeat'), '🔁')} Loop: {'On' if loop_enabled else 'Off'} | {_icon(self.bot, ('status_autoplay', 'autoplay'), '♾️')} Autoplay: {'On' if autoplay_enabled else 'Off'} | {_icon(self.bot, ('status_queue', 'queue'), '📋')} Queue: {queue_count or 'None'} | {_icon(self.bot, ('status_filters', 'filters'), '🎚️')} Filters: None",
        }
        return JukeboxView(self, track=track, requester=self._requesters.get(guild.id), status=status)
