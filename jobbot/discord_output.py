"""Discord rendering, publication, and durable-marker reconciliation support."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import discord

from .config import Settings
from .models import Job

COLORS = {"SWE": 0x5865F2, "Quant": 0xF1C40F, "AI/ML": 0x1ABC9C}
ICONS = {"SWE": "💻", "Quant": "📈", "AI/ML": "🧠"}


def clean(text: str, limit: int) -> str:
    # Payloads are snapshotted at queue time, so normalising the model is not enough.
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(str(text).strip()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def marker(delivery_id: str) -> str:
    return f"jobbot:{delivery_id}"


def make_message(delivery: dict, settings: Settings) -> dict:
    """Render one persisted delivery without changing its durable state."""
    if delivery["kind"] == "job":
        job = Job(**json.loads(delivery["payload"]))
        kind, experience = delivery["route"].split("_", 1)
        details = [
            f"**{clean(job.company, 256)}**",
            f"📍 {clean(job.location, 600)}"
            + (f"  ·  {clean(job.workplace, 100)}" if job.workplace else ""),
        ]
        if job.compensation:
            details.append(f"💰 {clean(job.compensation, 800)}")
        details.append(f"**[Apply now ↗]({job.apply_url})**")
        embed = discord.Embed(
            title=f"{ICONS[kind]}  {clean(job.title, 252)}",
            url=job.url,
            color=COLORS[kind],
            description="\n".join(details),
            timestamp=(
                datetime.fromisoformat(job.published_at.replace("Z", "+00:00"))
                if job.published_at
                else None
            ),
        )
        footer = f"{kind} · {experience}  •  {job.provider.title()}  •  {marker(delivery['id'])}"
        intended = settings.channels[delivery["route"]]
        content = None
    else:
        payload = json.loads(delivery["payload"])
        counts = payload["counts"]
        total = sum(counts.values())
        embed = discord.Embed(
            title=f"{total} new job listing{'s' if total != 1 else ''}", color=0x5865F2
        )
        for route, count in sorted(counts.items()):
            kind, experience = route.split("_", 1)
            embed.add_field(
                name=f"{kind} · {experience}",
                value=f"{count} in <#{settings.channels[route]}>",
                inline=False,
            )
        if payload["backlog"]:
            embed.add_field(name="Still queued", value=str(payload["backlog"]))
        intended = settings.posting_channel
        footer = marker(delivery["id"])
        content = " ".join(f"<@&{role}>" for role in dict.fromkeys(settings.roles))
    if settings.debug:
        destination = f"*🧪 Debug preview · would post in <#{intended}>*"
        embed.description = (
            f"{embed.description}\n\n{destination}" if embed.description else destination
        )
        content = None
        footer = f"DEBUG • {footer}"
    embed.set_footer(text=footer)
    allowed = discord.AllowedMentions.none()
    if delivery["kind"] == "summary" and not settings.debug:
        allowed = discord.AllowedMentions(
            everyone=False,
            users=False,
            replied_user=False,
            roles=[discord.Object(r) for r in settings.roles],
        )
    return {"content": content, "embed": embed, "allowed_mentions": allowed}


class DiscordPublisher:
    """Publishes through Discord while maintaining the bot's online Gateway presence."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = discord.Client(intents=discord.Intents.none())
        self.channels = {}
        self.gateway_task: asyncio.Task | None = None

    async def start(self):
        await self.client.__aenter__()
        await self.client.login(self.settings.token)
        self.gateway_task = asyncio.create_task(
            self.client.connect(reconnect=True), name="discord-gateway"
        )
        try:
            async with asyncio.timeout(30):
                await self.client.wait_until_ready()
        except BaseException:
            await self.close()
            raise
        await self.client.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="early-career roles",
            ),
        )
        ids = (
            {self.settings.test_channel}
            if self.settings.debug
            else {*self.settings.channels.values(), self.settings.posting_channel}
        )
        guild_ids = set()
        guilds = {}
        for channel_id in ids:
            channel = await self.client.fetch_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                raise ValueError(f"Channel {channel_id} must be a server text channel")
            guild_ids.add(channel.guild.id)
            if channel.guild.id not in guilds:
                guild = await self.client.fetch_guild(channel.guild.id)
                guilds[guild.id] = {c.id: c for c in await guild.fetch_channels()}
            channel = guilds[channel.guild.id][channel_id]
            member = await channel.guild.fetch_member(self.client.user.id)
            permissions = channel.permissions_for(member)
            if not all(
                (
                    permissions.view_channel,
                    permissions.send_messages,
                    permissions.embed_links,
                    permissions.read_message_history,
                )
            ):
                raise ValueError(f"Missing send/embed/history permissions in channel {channel_id}")
            self.channels[channel_id] = channel
        if len(guild_ids) != 1:
            raise ValueError("All destinations must belong to one Discord server")
        if not self.settings.debug:
            summary = self.channels[self.settings.posting_channel]
            roles = {role.id: role for role in await summary.guild.fetch_roles()}
            member = await summary.guild.fetch_member(self.client.user.id)
            for role_id in self.settings.roles:
                if role_id not in roles:
                    raise ValueError(f"Notification role {role_id} does not exist in this server")
                if (
                    not roles[role_id].mentionable
                    and not summary.permissions_for(member).mention_everyone
                ):
                    raise ValueError(f"Notification role {role_id} must be mentionable")

    async def close(self):
        await self.client.close()
        if self.gateway_task is not None:
            await asyncio.gather(self.gateway_task, return_exceptions=True)
            self.gateway_task = None

    async def send(self, delivery: dict, channel_id: int) -> int:
        message = make_message(delivery, self.settings)
        # Discord's nonce prevents repeat sends during its short deduplication window.
        # The durable footer marker also supports recovery beyond that window.
        nonce = int.from_bytes(hashlib.sha256(delivery["id"].encode()).digest()[:8], "big") >> 1
        async with asyncio.timeout(120):
            result = await self.channels[channel_id].send(**message, nonce=nonce)
        return result.id

    async def find_sent(self, delivery: dict) -> int | None:
        channel_id = delivery["channel_id"]
        channel = self.channels.get(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)
        since = datetime.fromisoformat(delivery["attempted_at"]) - timedelta(seconds=30)
        # Wait for an ambiguous request to settle before concluding it was not accepted.
        if (datetime.now(UTC) - since).total_seconds() < 90:
            raise RuntimeError("Recent uncertain send; reconciliation deferred until next scan")
        async with asyncio.timeout(120):
            async for message in channel.history(limit=None, after=since, oldest_first=False):
                if message.author.id != self.client.user.id:
                    continue
                if any(
                    marker(delivery["id"]) in (embed.footer.text or "") for embed in message.embeds
                ):
                    return message.id
        return None


def definitive_failure(exc: Exception) -> bool:
    return isinstance(exc, discord.HTTPException) and 400 <= exc.status < 500 and exc.status != 429
