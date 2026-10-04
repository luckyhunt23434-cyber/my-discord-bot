import os
import time
import io
import html
import uuid
import asyncio
import json
import urllib.request
from datetime import timedelta

import discord  # type: ignore[reportMissingImports]
from discord import app_commands  # type: ignore[reportMissingImports]
from discord.ext import commands  # type: ignore[reportMissingImports]

def load_discord_token():
    for key in ("DISCORD_TOKEN", "TOKEN"):
        value = os.getenv(key)
        if value and value.strip():
            return value.strip()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    for dotenv_name in (".env", ".env.local", "token.env", "discord.env"):
        dotenv_path = os.path.join(base_dir, dotenv_name)
        if not os.path.exists(dotenv_path):
            continue
        try:
            with open(dotenv_path, "r", encoding="utf-8") as dotenv_file:
                for line in dotenv_file:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    key, value = stripped.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key in {"DISCORD_TOKEN", "TOKEN"} and value:
                        os.environ.setdefault(key, value)
                        return value
        except OSError:
            pass
    return None


intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
TARGET_GUILD_ID = 1367009613337202728
WELCOME_CHANNEL_ID = 1368309509512237137
WELCOME_DASHBOARD_URL = "https://discord.com/channels/1367009613337202728/1368071106308866109"
FEEDBACK_CHANNEL_ID = 1370949939739496488
OWNER_ID = 961042823614967809
OWNER_USERNAMES = {"luckyhunt2"}

# Replace these zeros with your Discord IDs when you are ready.
SESSION_CHANNEL_ID = 1368312865387450368
SESSION_ROLE_ID = 1368395805257371658
STAFF_ROLE_ID = 0
SESSION_BRAND = "Florida State Roleplay Systems"
SESSION_BANNER_URL = ""
SESSION_EMBED_COLOR = discord.Color.from_rgb(255, 255, 255)
SESSION_COMMAND_ROLE_IDS = (
    1368384197856591902,
    1551445514765992038,
    1394687656394227833,
    1368387884557537291,
    1551447768831234080,
)
TICKET_TRANSCRIPT_CHANNEL_ID = 1449266304350425150
TICKET_PANEL_CHANNEL_ID = 1370971665890279534
TICKET_CATEGORIES = {
    "general": (1370972192342409340, "General"),
    "department": (1552904153431285873, "Department"),
    "high_rank": (1449260851553898557, "High Rank"),
    "staff_report": (1370974134959476869, "Staff Report"),
    "directorship": (1552905793920045158, "Directorship"),
}

active_session = None
persistent_ticket_views_added = False
ticket_stats = {}


def record_ticket_stat(user_id, action):
    stats = ticket_stats.setdefault(str(user_id), {"claimed": 0, "closed": 0})
    if action == "claimed":
        stats["claimed"] += 1
    elif action == "closed":
        stats["closed"] += 1


def session_channel(guild):
    if SESSION_CHANNEL_ID:
        return guild.get_channel(SESSION_CHANNEL_ID)
    return None


def session_role_mention(guild):
    role = guild.get_role(SESSION_ROLE_ID) if SESSION_ROLE_ID else None
    return role.mention if role else "@Session Notification"


def style_session_embed(embed, guild):
    if guild.icon:
        embed.set_author(name=SESSION_BRAND, icon_url=guild.icon.url)
    else:
        embed.set_author(name=SESSION_BRAND)
    embed.timestamp = discord.utils.utcnow()
    embed.set_footer(text="Florida State Roleplay  •  System")
    if SESSION_BANNER_URL:
        embed.set_image(url=SESSION_BANNER_URL)
    return embed


class SessionVoteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.voters = set()

    @discord.ui.button(label="Vote to attend (0)", style=discord.ButtonStyle.primary, custom_id="session_vote")
    async def vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in self.voters:
            self.voters.remove(interaction.user.id)
            message = "Your vote was removed."
        else:
            self.voters.add(interaction.user.id)
            message = "Your vote was recorded."

        button.label = f"Vote to attend ({len(self.voters)})"
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(message, ephemeral=True)


def ticket_type_for_channel(channel):
    for ticket_type, (category_id, _) in TICKET_CATEGORIES.items():
        if channel.category_id == category_id:
            return ticket_type
    return None


def ticket_owner_id(channel):
    if not channel.topic or not channel.topic.startswith("ticket_owner="):
        return None
    try:
        return int(channel.topic.split(";", 1)[0].split("=", 1)[1])
    except ValueError:
        return None


def ticket_overwrites(guild, owner):
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        owner: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if bot.user:
        overwrites[guild.me or bot.user] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)
    for role_id in SESSION_COMMAND_ROLE_IDS:
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
    return overwrites


class TicketControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, custom_id="ticket_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not ticket_type_for_channel(interaction.channel):
            await interaction.response.send_message("This is not a ticket channel.", ephemeral=True)
            return
        record_ticket_stat(interaction.user.id, "claimed")
        await interaction.channel.send(f"✅ This ticket has been claimed by {interaction.user.mention}.")
        await interaction.response.send_message("Ticket claimed.", ephemeral=True)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        result = await close_ticket(interaction.channel, interaction.user)
        await interaction.response.send_message(result, ephemeral=True)


async def send_session_message(guild, embed, view=None, content=None):
    channel = session_channel(guild)
    if channel is None:
        return None
    return await channel.send(content=content, embed=embed, view=view)


def ticket_staff(member):
    return member.guild_permissions.manage_channels or any(role.id in SESSION_COMMAND_ROLE_IDS for role in member.roles)


async def bloxlink_profile(guild_id, discord_id):
    api_key = os.getenv("BLOXLINK_API_KEY")
    if not api_key:
        return None

    lookup_url = f"https://api.blox.link/v4/public/guilds/{guild_id}/discord-to-roblox/{discord_id}"

    def request_json(url, headers=None):
        request = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        link_data = await asyncio.to_thread(
            request_json,
            lookup_url,
            {"Authorization": api_key},
        )
        roblox_id = link_data.get("robloxID") or link_data.get("robloxId")
        if not roblox_id:
            return None
        user_data = await asyncio.to_thread(request_json, f"https://users.roblox.com/v1/users/{roblox_id}")
        return {"username": user_data.get("name", "Unknown"), "id": str(roblox_id)}
    except Exception as error:
        print(f"Bloxlink profile lookup failed: {error}")
        return None


async def open_ticket(interaction, ticket_type, reason):
    if interaction.guild is None:
        await interaction.response.send_message("Tickets can only be opened in a server.", ephemeral=True)
        return

    category_id, display_name = TICKET_CATEGORIES[ticket_type]
    category = interaction.guild.get_channel(category_id)
    if not isinstance(category, discord.CategoryChannel):
        await interaction.response.send_message("This ticket category is not configured correctly.", ephemeral=True)
        return

    for channel in interaction.guild.text_channels:
        if ticket_owner_id(channel) == interaction.user.id:
            await interaction.response.send_message(f"You already have an open ticket: {channel.mention}", ephemeral=True)
            return

    profile = await bloxlink_profile(interaction.guild.id, interaction.user.id)
    channel_name = f"{ticket_type}-{interaction.user.name}".lower().replace(" ", "-")[:90]
    channel = await interaction.guild.create_text_channel(
        channel_name,
        category=category,
        topic=f"ticket_owner={interaction.user.id};ticket_type={ticket_type}",
        overwrites=ticket_overwrites(interaction.guild, interaction.user),
        reason=f"{display_name} ticket opened by {interaction.user}",
    )
    embed = style_session_embed(discord.Embed(
        title=f"🗂️ {display_name} Support Ticket",
        description=(
            f"{interaction.user.mention} Welcome to **Florida State Roleplay**. "
            "Thank you for contacting support. We will be with you shortly.\n\n"
            "Please provide all relevant details so our team can help you quickly."
        ),
        color=SESSION_EMBED_COLOR,
    ), interaction.guild)
    if profile:
        profile_value = f"[{profile['username']} ({profile['id']})](https://www.roblox.com/users/{profile['id']}/profile)"
    else:
        profile_value = "Bloxlink profile could not be found."
    embed.add_field(name="Roblox Profile", value=profile_value, inline=False)
    embed.add_field(name="Inquiry", value=reason[:1024], inline=False)
    embed.add_field(name="Ticket Type", value=display_name, inline=True)
    embed.add_field(name="Status", value="🟢 Open", inline=True)
    await channel.send(
        content=f"{interaction.user.mention} @here",
        embed=embed,
        view=TicketControlsView(),
        allowed_mentions=discord.AllowedMentions(users=True, everyone=True),
    )
    await interaction.response.send_message(f"Your ticket has been opened: {channel.mention}", ephemeral=True)


class TicketReasonModal(discord.ui.Modal):
    def __init__(self, ticket_type):
        _, display_name = TICKET_CATEGORIES[ticket_type]
        super().__init__(title=f"Open {display_name} Ticket")
        self.ticket_type = ticket_type
        self.reason = discord.ui.TextInput(
            label="What do you need help with?",
            placeholder="Describe your question or issue...",
            style=discord.TextStyle.paragraph,
            min_length=3,
            max_length=1000,
            required=True,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await open_ticket(interaction, self.ticket_type, self.reason.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"Ticket form error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "I could not open that ticket. Please try again or contact staff.",
                ephemeral=True,
            )


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"Ticket button error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "I could not open that ticket form. Please try again.",
                ephemeral=True,
            )

    @discord.ui.button(label="General", style=discord.ButtonStyle.secondary, custom_id="ticket_general", row=0)
    async def general(self, interaction, button):
        await interaction.response.send_modal(TicketReasonModal("general"))

    @discord.ui.button(label="High Rank", style=discord.ButtonStyle.secondary, custom_id="ticket_high_rank", row=0)
    async def high_rank(self, interaction, button):
        await interaction.response.send_modal(TicketReasonModal("high_rank"))

    @discord.ui.button(label="Department", style=discord.ButtonStyle.secondary, custom_id="ticket_department", row=0)
    async def department(self, interaction, button):
        await interaction.response.send_modal(TicketReasonModal("department"))

    @discord.ui.button(label="Directorship", style=discord.ButtonStyle.secondary, custom_id="ticket_directorship", row=1)
    async def directorship(self, interaction, button):
        await interaction.response.send_modal(TicketReasonModal("directorship"))

    @discord.ui.button(label="Staff Report", style=discord.ButtonStyle.secondary, custom_id="ticket_staff_report", row=1)
    async def staff_report(self, interaction, button):
        await interaction.response.send_modal(TicketReasonModal("staff_report"))


def is_owner(user):
    if user is None:
        return False
    if user.id == OWNER_ID:
        return True

    usernames = {name.lower() for name in OWNER_USERNAMES}
    names = {
        (user.name or "").lower(),
        (getattr(user, "global_name", None) or "").lower(),
        (getattr(user, "display_name", None) or "").lower(),
    }
    return bool(names & usernames)


async def send_private_message(user, content):
    try:
        await user.send(content)
    except (discord.Forbidden, discord.HTTPException):
        return False
    return True


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("🚫 You do not have permission to use this command.")
    elif isinstance(error, commands.CommandNotFound):
        return
    else:
        await ctx.send(f"❌ An error occurred: {error}")


@bot.event
async def on_ready():
    global persistent_ticket_views_added
    print(f"Logged in as {bot.user}")
    if not persistent_ticket_views_added:
        bot.add_view(TicketPanelView())
        bot.add_view(TicketControlsView())
        persistent_ticket_views_added = True
    target_guild = discord.Object(id=TARGET_GUILD_ID)
    try:
        synced_commands = await bot.tree.sync(guild=target_guild)
        command_names = ", ".join(command.name for command in synced_commands)
        print(f"Synced {len(synced_commands)} guild slash command(s): {command_names}")
    except discord.HTTPException as error:
        print(f"Slash-command sync failed: {error}")


def welcome_view(guild):
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label=f"{guild.member_count:,}",
        style=discord.ButtonStyle.secondary,
        emoji="👥",
        disabled=True,
    ))
    view.add_item(discord.ui.Button(
        label="Dashboard",
        style=discord.ButtonStyle.primary,
        emoji="↗",
        url=WELCOME_DASHBOARD_URL,
    ))
    return view


@bot.event
async def on_member_join(member):
    if member.guild.id != TARGET_GUILD_ID:
        return

    channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
    if channel is not None:
        welcome_message = (
            f"**Welcome** {member.mention} to **{member.guild.name}** — "
            f"we hope you enjoy your stay!"
        )
        await channel.send(welcome_message, view=welcome_view(member.guild))


@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    if is_owner(member):
        await ctx.send("You can not kick this member.")
        return
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        await ctx.send("You cannot kick someone with an equal or higher role than you.")
        return

    await send_private_message(
        member,
        f"You were kicked from **{ctx.guild.name}** by {ctx.author.mention}.\nReason: {reason}",
    )
    await member.kick(reason=reason)
    await ctx.send(f"✅ {member.mention} was kicked by {ctx.author.mention}. Reason: {reason}")


@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    if is_owner(member):
        await ctx.send("You can not ban this member.")
        return
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        await ctx.send("You cannot ban someone with an equal or higher role than you.")
        return

    await send_private_message(
        member,
        f"You were banned from **{ctx.guild.name}** by {ctx.author.mention}.\nReason: {reason}",
    )
    await member.ban(reason=reason)
    await ctx.send(f"✅ {member.mention} was banned by {ctx.author.mention}. Reason: {reason}")


@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(ctx, *, user_name):
    banned_users = await ctx.guild.bans()
    member_name, member_discriminator = user_name.split('#')

    for ban_entry in banned_users:
        user = ban_entry.user
        if (user.name, user.discriminator) == (member_name, member_discriminator):
            await ctx.guild.unban(user)
            await ctx.send(f"✅ {user.mention} has been unbanned.")
            return

    await ctx.send("❌ That user is not banned.")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def say(ctx, *, message):
    await ctx.channel.send(message)
    await ctx.message.delete()


@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount: int = 5):
    if amount < 1:
        await ctx.send("Please enter a number greater than 0.")
        return

    await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f"✅ Cleared {amount} messages.", delete_after=3)


@bot.command()
async def ping(ctx):
    await ctx.send(f"🏓 Pong! Latency: {round(bot.latency * 1000)}ms")


@bot.command()
async def userinfo(ctx, member: discord.Member = None):
    member = ctx.author if member is None else member
    embed = discord.Embed(title=f"User Info: {member}", color=discord.Color.blue())
    embed.add_field(name="ID", value=member.id, inline=True)
    embed.add_field(name="Joined", value=member.joined_at.strftime("%Y-%m-%d %H:%M:%S"), inline=True)
    embed.add_field(name="Created", value=member.created_at.strftime("%Y-%m-%d %H:%M:%S"), inline=True)
    embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
    await ctx.send(embed=embed)


@bot.command()
async def hello(ctx):
    await ctx.send(f"Hello {ctx.author.mention}!")


async def close_ticket(channel, member):
    owner_id = ticket_owner_id(channel)
    if owner_id is None:
        return "This command can only be used inside a ticket."
    if member.id != owner_id and not ticket_staff(member):
        return "Only the ticket owner or support staff can close this ticket."

    closed_at = discord.utils.utcnow()
    reference_id = f"FSRP-{closed_at.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    owner = channel.guild.get_member(owner_id)
    owner_name = str(owner) if owner else f"User {owner_id}"
    owner_mention = owner.mention if owner else f"<@{owner_id}>"
    lines = [f"Transcript: {channel.name}", f"Closed by: {member} ({member.id})", ""]
    messages = []
    async for message in channel.history(limit=None, oldest_first=True):
        messages.append(message)
        timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append(f"[{timestamp}] {message.author}: {message.content}")

    if owner is None:
        try:
            owner = await bot.fetch_user(owner_id)
        except (discord.NotFound, discord.HTTPException):
            owner = None

    html_messages = "\n".join(
        f"<p><strong>[{html.escape(message.created_at.strftime('%Y-%m-%d %H:%M:%S UTC'))}] "
        f"{html.escape(str(message.author))}:</strong> {html.escape(message.content)}</p>"
        for message in messages
    )
    html_document = (
        "<!doctype html><html><head><meta charset='utf-8'><title>Ticket Transcript</title>"
        "<style>body{font-family:Arial,sans-serif;background:#2f3036;color:#f2f3f5;padding:24px}"
        "main{max-width:900px;margin:auto;background:#36393f;padding:24px;border-radius:8px}"
        "p{border-bottom:1px solid #4f5158;padding:8px 0}</style></head><body><main>"
        f"<h1>Ticket Transcript</h1><h2>#{html.escape(channel.name)}</h2>{html_messages}"
        "</main></body></html>"
    )
    embed = discord.Embed(title="✈ Ticket Transcript.", color=SESSION_EMBED_COLOR)
    if channel.guild.icon:
        embed.set_author(name=SESSION_BRAND, icon_url=channel.guild.icon.url)
    embed.add_field(name="# Channel Name", value=f"{channel.name}\nChannel ID: `{channel.id}`", inline=False)
    embed.add_field(name="👤 Owner", value=f"{owner_name}\n@ Mention: {owner_mention}\nUser ID: `{owner_id}`", inline=False)
    embed.add_field(name="🛡 Closed By", value=f"{member}\nClosed On: {closed_at.strftime('%A, %B %d, %Y %I:%M %p UTC')}", inline=False)
    embed.add_field(name="# Reference ID", value=f"`{reference_id}`", inline=False)
    embed.set_footer(text="Florida State Roleplay  •  Ticket System")
    if SESSION_BANNER_URL:
        embed.set_image(url=SESSION_BANNER_URL)

    transcript_channel = channel.guild.get_channel(TICKET_TRANSCRIPT_CHANNEL_ID)
    if transcript_channel:
        await transcript_channel.send(
            embed=embed,
            file=discord.File(io.BytesIO(html_document.encode("utf-8")), filename=f"{reference_id}.html"),
        )
    if owner:
        try:
            await owner.send(
                embed=embed,
                file=discord.File(io.BytesIO(html_document.encode("utf-8")), filename=f"{reference_id}.html"),
            )
            await send_private_message(
                owner,
                "If you still need assistance, you can open another ticket in our "
                "<#1370971665890279534> channel.",
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
    record_ticket_stat(member.id, "closed")
    await channel.delete(reason=f"Ticket closed by {member}")
    return "Ticket closed and transcript saved."


@bot.group(name="ticket")
async def ticket_group(ctx):
    if ctx.invoked_subcommand is None:
        await ctx.send("Use `!ticket leaderboard` to view the ticket leaderboard.")


@ticket_group.command(name="leaderboard")
async def ticket_leaderboard(ctx):
    if not ticket_stats:
        await ctx.send("No ticket stats have been recorded yet.")
        return

    rankings = []
    for user_id, stats in ticket_stats.items():
        member = ctx.guild.get_member(int(user_id))
        display = member.mention if member else f"<@{user_id}>"
        total = stats["claimed"] + stats["closed"]
        rankings.append((total, stats["claimed"], stats["closed"], display))

    rankings.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    top = rankings[:10]

    embed = discord.Embed(
        title="🏆 Ticket Leaderboard",
        description="Top ticket claimers and closers this session.",
        color=SESSION_EMBED_COLOR,
    )
    lines = []
    for index, (total, claimed, closed, display) in enumerate(top, start=1):
        lines.append(f"{index}. {display} — **{claimed}** claimed • **{closed}** closed • **{total}** total")

    embed.add_field(name="Top performers", value="\n".join(lines), inline=False)
    await ctx.send(embed=embed)


@bot.command(name="ticket-panel")
@commands.has_permissions(manage_channels=True)
async def ticket_panel(ctx):
    target = ctx.guild.get_channel(TICKET_PANEL_CHANNEL_ID)
    if target is None:
        await ctx.send("Ticket panel channel not found.")
        return
    embed = style_session_embed(discord.Embed(
        title="🗂️ Contact Us — Florida State Roleplay",
        description=(
            "Need assistance? Open a ticket and our team will be ready to help. "
            "Choose the category that matches your issue below and a private channel will be opened for you.\n\n"
            "**Choose your category:**\n"
            "• **General:** Questions, support, and assistance from staff.\n"
            "• **High Rank:** Escalated concerns requiring senior staff attention.\n"
            "• **Department:** Department-related matters.\n"
            "• **Directorship:** Leadership and directorship matters.\n"
            "• **Staff Report:** Reports involving staff conduct."
        ),
        color=SESSION_EMBED_COLOR,
    ), ctx.guild)
    await target.send(embed=embed, view=TicketPanelView())
    await ctx.send(f"Ticket panel posted in {target.mention}.", delete_after=5)


@bot.command(name="close")
async def close(ctx):
    result = await close_ticket(ctx.channel, ctx.author)
    if result != "Ticket closed and transcript saved.":
        await ctx.send(result, delete_after=8)


@bot.command(name="rename")
async def rename_ticket(ctx, *, new_name):
    if ticket_owner_id(ctx.channel) is None:
        await ctx.send("This command can only be used inside a ticket.")
        return
    if not ticket_staff(ctx.author):
        await ctx.send("Only support staff can rename tickets.")
        return
    channel_name = "-".join(new_name.lower().split())[:90]
    await ctx.channel.edit(name=channel_name, reason=f"Ticket renamed by {ctx.author}")
    await ctx.send(f"✅ Ticket renamed to `{channel_name}`.")


@bot.command(name="add")
async def add_to_ticket(ctx, member: discord.Member):
    if ticket_owner_id(ctx.channel) is None or not ticket_staff(ctx.author):
        await ctx.send("Only support staff can add users to tickets.")
        return
    await ctx.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
    await ctx.send(f"✅ Added {member.mention} to this ticket.")


@bot.command(name="remove")
async def remove_from_ticket(ctx, member: discord.Member):
    if ticket_owner_id(ctx.channel) is None or not ticket_staff(ctx.author):
        await ctx.send("Only support staff can remove users from tickets.")
        return
    if member.id == ticket_owner_id(ctx.channel):
        await ctx.send("The ticket owner cannot be removed from their ticket.")
        return
    await ctx.channel.set_permissions(
        member,
        view_channel=False,
        send_messages=False,
        read_message_history=False,
    )
    await ctx.send(f"✅ Removed {member.mention} from this ticket.")


@bot.command(name="escalate")
async def escalate_ticket(ctx, destination):
    if ticket_owner_id(ctx.channel) is None or not ticket_staff(ctx.author):
        await ctx.send("Only support staff can escalate tickets.")
        return
    destination = destination.lower().replace("-", "_")
    destination = {
        "ia": "staff_report",
        "hr": "high_rank",
        "highrank": "high_rank",
        "derpartment": "department",
    }.get(destination, destination)
    if destination not in TICKET_CATEGORIES:
        await ctx.send("Use `general`, `department`, `hr`, `directorship`, or `ia`.")
        return
    category_id, display_name = TICKET_CATEGORIES[destination]
    try:
        category = await bot.fetch_channel(category_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        category = None
    owner = ctx.guild.get_member(ticket_owner_id(ctx.channel))
    if not isinstance(category, discord.CategoryChannel) or owner is None:
        await ctx.send(f"The {display_name} category could not be found or accessed. Check category ID `{category_id}` and my permissions.")
        return
    await ctx.channel.edit(category=category, topic=f"ticket_owner={owner.id};ticket_type={destination}")
    await ctx.channel.edit(overwrites=ticket_overwrites(ctx.guild, owner))
    await ctx.send(f"✅ Ticket escalated to **{display_name}**.")


@bot.tree.command(name="session-start", description="Start an ER:LC roleplay session", guild=discord.Object(id=TARGET_GUILD_ID))
@app_commands.checks.has_any_role(*SESSION_COMMAND_ROLE_IDS)
async def session_start(interaction: discord.Interaction, server_name: str, join_code: str, player_count: int = 0, staff_count: int = 0, queue: int = 0):
    global active_session

    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    active_session = {
        "server_name": server_name,
        "join_code": join_code,
        "player_count": player_count,
        "staff_count": staff_count,
        "queue": queue,
    }
    embed = style_session_embed(discord.Embed(
        title="Session Start-up",
        description=(
            "The server is now open and the session is officially active.\n"
            "You may now join and begin roleplay. Please remain in VC as required "
            "and maintain realistic interactions."
        ),
        color=SESSION_EMBED_COLOR,
    ), interaction.guild)
    embed.add_field(
        name="▰ Server Details",
        value=f"**Server Name:** {server_name}\n**Server Join Code:** `{join_code}`",
        inline=False,
    )
    embed.add_field(
        name="Server Statistics",
        value=f"**Player Count**  `{player_count}`\n**Staff In-Game**  `{staff_count}`\n**In Queue**  `{queue}`",
        inline=True,
    )
    embed.add_field(
        name="Session Status",
        value="🟢 **Online**\n\nUse the join code above to enter the server.",
        inline=True,
    )
    target = session_channel(interaction.guild) or interaction.channel
    await target.send(content=session_role_mention(interaction.guild), embed=embed)
    await interaction.response.send_message("Session start-up posted.", ephemeral=True)


@bot.tree.command(name="help", description="Get help and support information", guild=discord.Object(id=TARGET_GUILD_ID))
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Hello! If you need assistance, please check out our support channel for help:\n"
        "https://discord.com/channels/1367009613337202728/1370971665890279534",
        ephemeral=True,
    )


@bot.tree.command(name="bug-report", description="Report a problem with the bot", guild=discord.Object(id=TARGET_GUILD_ID))
async def bug_report(interaction: discord.Interaction, details: str):
    try:
        owner = bot.get_user(OWNER_ID) or await bot.fetch_user(OWNER_ID)
        embed = discord.Embed(
            title="🐞 New Bot Bug Report",
            description=details[:4000],
            color=SESSION_EMBED_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Reported By", value=f"{interaction.user.mention}\n`{interaction.user.id}`", inline=True)
        embed.add_field(name="Server", value=f"{interaction.guild.name if interaction.guild else 'Direct Message'}\n`{interaction.guild_id or 'N/A'}`", inline=True)
        embed.add_field(name="Channel", value=f"{interaction.channel.mention if interaction.channel else 'N/A'}\n`{interaction.channel_id}`", inline=True)
        embed.set_footer(text="Florida State Roleplay Systems")
        await owner.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
        await interaction.response.send_message(
            "I could not deliver the bug report. Please contact an administrator directly.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "✅ Your bug report was sent privately to the bot owner. Thank you!",
        ephemeral=True,
    )


@bot.command(name="add-role")
@commands.has_permissions(manage_roles=True)
async def add_role(ctx, member: discord.Member, role: discord.Role):
    if role.is_default() or role.managed:
        await ctx.send("That role cannot be assigned manually.")
        return
    if ctx.author != ctx.guild.owner and role >= ctx.author.top_role:
        await ctx.send("You can only assign roles below your highest role.")
        return
    bot_member = ctx.guild.me
    if bot_member is None or role >= bot_member.top_role:
        await ctx.send("That role is higher than the bot's highest role.")
        return
    if role in member.roles:
        await ctx.send(f"{member.mention} already has {role.mention}.")
        return

    try:
        await member.add_roles(role, reason=f"Added by {ctx.author}")
    except discord.Forbidden:
        await ctx.send("I do not have permission to assign that role.")
        return
    await ctx.send(f"✅ Added {role.mention} to {member.mention}.")


@bot.tree.command(name="session-shutdown", description="End the active ER:LC roleplay session", guild=discord.Object(id=TARGET_GUILD_ID))
@app_commands.checks.has_any_role(*SESSION_COMMAND_ROLE_IDS)
async def session_shutdown(interaction: discord.Interaction):
    global active_session

    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    embed = style_session_embed(discord.Embed(
        title="Session Shutdown",
        description="The session has officially ended. Thank you for attending.",
        color=SESSION_EMBED_COLOR,
    ), interaction.guild)
    embed.add_field(name="Session Status", value="🔴 **Offline**", inline=False)
    target = session_channel(interaction.guild) or interaction.channel
    await target.send(embed=embed)
    active_session = None
    await interaction.response.send_message("Session shutdown posted.", ephemeral=True)


@bot.tree.command(name="session-vote", description="Create a vote for an ER:LC session", guild=discord.Object(id=TARGET_GUILD_ID))
@app_commands.checks.has_any_role(*SESSION_COMMAND_ROLE_IDS)
async def session_vote(interaction: discord.Interaction, notes: str = "Please vote if you can attend."):
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    view = SessionVoteView()
    embed = style_session_embed(discord.Embed(
        title="Session Vote",
        description=(
            "Please vote below if you can attend the next roleplay session.\n\n"
            f"{notes}"
        ),
        color=SESSION_EMBED_COLOR,
    ), interaction.guild)
    target = session_channel(interaction.guild) or interaction.channel
    await target.send(content=session_role_mention(interaction.guild), embed=embed, view=view)
    await interaction.response.send_message("Session vote posted.", ephemeral=True)


@bot.tree.command(name="session-boost", description="Post a reminder to boost session attendance", guild=discord.Object(id=TARGET_GUILD_ID))
@app_commands.checks.has_any_role(*SESSION_COMMAND_ROLE_IDS)
async def session_boost(interaction: discord.Interaction, message: str = "Please join and help boost the session!"):
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    embed = style_session_embed(discord.Embed(
        title="Session Boost",
        description=f"🚨 **Session reminder**\n\n{message}",
        color=SESSION_EMBED_COLOR,
    ), interaction.guild)
    target = session_channel(interaction.guild) or interaction.channel
    await target.send(content=session_role_mention(interaction.guild), embed=embed)
    await interaction.response.send_message("Session boost posted.", ephemeral=True)


@bot.tree.command(name="feedback", description="Submit staff feedback", guild=discord.Object(id=TARGET_GUILD_ID))
async def feedback(interaction: discord.Interaction, staff: str, rating: int, feedback: str):
    if interaction.guild is None or interaction.guild.id != TARGET_GUILD_ID:
        await interaction.response.send_message("This command is only available in the configured server.", ephemeral=True)
        return

    if rating < 1 or rating > 5:
        await interaction.response.send_message("Please choose a rating between 1 and 5 stars.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(FEEDBACK_CHANNEL_ID)
    if channel is None:
        await interaction.response.send_message("Feedback channel not found.", ephemeral=True)
        return

    stars = "★" * rating + "☆" * (5 - rating)
    embed = discord.Embed(
        title="Support Review",
        description=(
            f"• **Support member:** @{staff}\n"
            f"• **Rating:** {rating}/5 {stars}\n"
            f"• **Feedback:** {feedback}"
        ),
        color=SESSION_EMBED_COLOR,
    )
    embed.set_author(name="FEEDBACK", icon_url=interaction.client.user.display_avatar.url)
    embed.set_footer(text=f"Reviewer: @{interaction.user.display_name}")

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I cannot send messages in the feedback channel. Check my View Channel and Send Messages permissions.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message("✅ Your feedback has been submitted.", ephemeral=True)


@bot.command()
@commands.has_permissions(manage_channels=True)
async def lock(ctx, channel: discord.TextChannel = None):
    target = channel or ctx.channel
    overwrite = target.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await target.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send(f"🔒 Locked {target.mention}.")


@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx, channel: discord.TextChannel = None):
    target = channel or ctx.channel
    overwrite = target.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = True
    await target.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send(f"🔓 Unlocked {target.mention}.")


@bot.command()
@commands.has_permissions(moderate_members=True)
async def mute(ctx, member: discord.Member, duration: int = 10, *, reason="No reason provided"):
    if is_owner(member):
        await ctx.send("You can not mute this member.")
        return
    if duration <= 0:
        await ctx.send("Please enter a valid duration in minutes greater than 0.")
        return

    timeout_until = discord.utils.utcnow() + timedelta(minutes=duration)
    await member.timeout(timeout_until, reason=reason)
    await ctx.send(f"🔇 {member.mention} was muted for {duration} minutes. Reason: {reason}")


@bot.command()
@commands.has_permissions(moderate_members=True)
async def unmute(ctx, member: discord.Member):
    if is_owner(member):
        await ctx.send("🚫 The bot owner cannot be unmuted by this command.")
        return
    await member.timeout(None)
    await ctx.send(f"🔊 {member.mention} has been unmuted.")


warns = {}


@bot.command()
@commands.has_permissions(moderate_members=True)
async def warn(ctx, member: discord.Member, *, reason="No reason provided"):
    if is_owner(member):
        await ctx.send("You can not warn this member.")
        return
    user_id = str(member.id)
    member_warns = warns.setdefault(user_id, [])
    warning_id = len(member_warns) + 1
    member_warns.append({
        "id": warning_id,
        "reason": reason,
        "moderator": ctx.author.id,
    })

    if len(member_warns) >= 10:
        await send_private_message(
            member,
            f"You received warning **#{warning_id}** in **{ctx.guild.name}** and reached 10 warnings.\n"
            f"You have been automatically banned by {ctx.author.mention}.\nReason: {reason}",
        )
        await member.ban(reason=f"Reached 10 warnings. Last reason: {reason}")
        warns.pop(user_id, None)
        await ctx.send(f"🚫 {member.mention} has reached 10 warnings and was automatically banned by {ctx.author.mention}.")
        return

    await send_private_message(
        member,
        f"You received warning **#{warning_id}** in **{ctx.guild.name}** from {ctx.author.mention}.\nReason: {reason}",
    )
    embed = discord.Embed(
        title="⚠️ Warning issued",
        description=f"{member.mention} has been warned.",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Warning ID", value=str(warning_id), inline=True)
    embed.add_field(name="Total warnings", value=str(len(member_warns)), inline=True)
    embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(moderate_members=True)
async def warnings(ctx, member: discord.Member):
    member_warns = warns.get(str(member.id), [])
    if not member_warns:
        await ctx.send(f"{member.mention} has no warnings.")
        return

    embed = discord.Embed(
        title=f"📋 Warnings for {member}",
        color=discord.Color.blue(),
    )
    for warning in member_warns:
        embed.add_field(
            name=f"Warning #{warning['id']}",
            value=f"Reason: {warning['reason']}",
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(moderate_members=True)
async def void(ctx, member: discord.Member, warning_id: int):
    if is_owner(member):
        await ctx.send("You can not void this member's warning.")
        return
    user_id = str(member.id)
    member_warns = warns.get(user_id, [])

    for index, warning in enumerate(member_warns):
        if warning["id"] == warning_id:
            del member_warns[index]
            if not member_warns:
                warns.pop(user_id, None)
            await ctx.send(f"⚪ Removed warning #{warning_id} from {member.mention}.")
            return

    await ctx.send(f"⚠️ No warning with ID #{warning_id} was found for {member.mention}.")


SLUR_WORDS = {
    "nigger",
    "nigga",
    "chink",
    "coon",
    "spic",
    "kike",
    "beaner",
    "gook",
    "wetback",
    "tranny",
}


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    lower_message = message.content.lower()
    if any(word in lower_message for word in SLUR_WORDS):
        try:
            await message.delete()
            timeout_until = discord.utils.utcnow() + timedelta(minutes=10)
            await message.author.timeout(timeout_until, reason="Used a racial slur")
            await message.channel.send(
                f"{message.author.mention} was timed out for 10 minutes for using a racial slur.",
                delete_after=10,
            )
        except discord.Forbidden:
            await message.channel.send(
                f"{message.author.mention} used a racial slur and the bot could not apply the timeout.",
                delete_after=10,
            )
        return

    await bot.process_commands(message)


token = load_discord_token()
if not token:
    raise RuntimeError(
        "Set the DISCORD_TOKEN or TOKEN environment variable before starting the bot, or create a .env file with DISCORD_TOKEN=your_token_here."
    )

while True:
    try:
        bot.run(token, reconnect=True)
    except KeyboardInterrupt:
        print("Bot stopped by user.")
        break
    except Exception as error:
        print(f"Bot stopped unexpectedly: {error}")
        print("Restarting in 5 seconds...")
        time.sleep(5)