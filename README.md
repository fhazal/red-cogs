<div align="center">

# 🧩 BajuBesi Labs · Red Cogs

**Cogs for [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot), made for the BajuBesi Discord.**<br>
An Enshrouded server panel · a music jukebox with buttons

![Red 3.5](https://img.shields.io/badge/Red--DiscordBot-3.5-ed4245?logo=discord&logoColor=white)
![Python 3.11](https://img.shields.io/badge/python-3.11-3776ab?logo=python&logoColor=white)
[![License: GPL v3](https://img.shields.io/badge/license-GPL--3.0-e8b04b)](LICENSE)

[![Join our Discord](https://img.shields.io/badge/Discord-Join%20BajuBesi-5865f2?logo=discord&logoColor=white&style=for-the-badge)](https://discord.gg/TCmm8cH3GE)
[![Ko-fi](https://img.shields.io/badge/Ko--fi-Support%20us-ff5e5b?logo=ko-fi&logoColor=white&style=for-the-badge)](https://ko-fi.com/bajubesi)

</div>

---

## ✨ The cogs

<table>
<tr>
<td width="50%" valign="top">

### 🎵 Jukebox
A **Now Playing** panel that stays in your music channel, with buttons for everything: previous, pause, skip, ±10s, loop, shuffle, volume, filters, queue, grab, like and autoplay. Built on Red's own **Audio** cog, so `[p]play` keeps working as usual.

</td>
<td width="50%" valign="top">

### 🏔️ Enshrouded
A live panel for an **Enshrouded dedicated server**: online or down, players right now and today's peak, an alert when it goes down, and the developers' latest **Steam news** posted to your channel. Admins can start, stop and restart the server from Discord.

</td>
</tr>
</table>

---

## 🚀 Install

```
[p]load downloader
[p]repo add bajubesi https://github.com/fhazal/red-cogs
[p]cog install bajubesi jukebox
[p]load jukebox
```

`[p]` is your bot's prefix. Install **enshrouded** the same way.

> [!IMPORTANT]
> Both cogs were written for the BajuBesi server. Some role, channel and emoji ids, and the Enshrouded server's address and control script, are set near the top of each file: change them for your own server before you load the cog.

---

## 🎛️ Commands

<details>
<summary><b>🎵 Jukebox</b></summary>

| Command | Who | What it does |
| --- | --- | --- |
| `[p]jukebox setup` | Admins / Manage Server | Posts the jukebox panel in this channel (replaces the old one) |
| `[p]jukeboxemojiids` | Everyone | Finds your server's emoji ids for the panel buttons (previous, pause, like…), to use your own emojis |

Needs Red's Audio cog: `[p]load audio`. Songs are added with Red's own `[p]play`.

</details>

<details>
<summary><b>🏔️ Enshrouded</b></summary>

| Command | Who | What it does |
| --- | --- | --- |
| `[p]enshrouded` · `[p]enshrouded status` | Everyone | Server and game status |
| `[p]enshrouded panel` | Server admins | Posts the live status panel here |
| `[p]enshrouded start` · `stop` · `restart` | Server admins | Controls the dedicated server |
| `[p]enshrouded logs` | Server admins | Recent server logs |
| `[p]enshrouded latestnews` | Server admins | Previews the newest developer post (no ping) |
| `[p]enshrouded testnews` | Server admins | Tests the automatic news post |

Also works as slash commands (`/enshrouded …`).

</details>

---

## 🔒 Your data

| Cog | What it stores |
| --- | --- |
| **Jukebox** | The panel's channel and message per server, and the songs each member likes (the ❤️ Like button) |
| **Enshrouded** | Per-server settings and server stats only (channels, panel message, peak players, last news post). Nothing about members |

---

## 🙏 Credits

- **[Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot)** by Cog Creators, the bot these cogs run on.
- Made by **BajuBesi Labs**. Not affiliated with Cog Creators, Keen Games, Valve or Discord.
- Released under the [GNU GPL v3](LICENSE), the same licence as Red.

<div align="center">

<sub>Made with ☕ by <b>BajuBesi Labs</b></sub>

</div>
