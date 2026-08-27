import os
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)

MEDIA_ROLE_ID = int(os.getenv("MEDIA_ROLE_ID", "0") or 0)
MEDIA_INFO_CATEGORY_ID = int(os.getenv("MEDIA_INFO_CATEGORY_ID", "0") or 0)
MEDIA_TEAM_CATEGORY_ID = int(os.getenv("MEDIA_TEAM_CATEGORY_ID", "0") or 0)
STAFF_CATEGORY_ID = int(os.getenv("STAFF_CATEGORY_ID", "0") or 0)
APPLY_CHANNEL_ID = int(os.getenv("APPLY_CHANNEL_ID", "0") or 0)
SUBMIT_CHANNEL_ID = int(os.getenv("SUBMIT_CHANNEL_ID", "0") or 0)
GET_KEY_CHANNEL_ID = int(os.getenv("GET_KEY_CHANNEL_ID", "0") or 0)

DB_PATH = "rose_media.sqlite3"
THUMBNAIL_DIR = "thumbnails"

GAME_CHOICES = [
    "Rust",
    "Fortnite",
    "Apex Legends",
    "HWID Swoofer",
]

DURATION_COSTS = {
    "1 day": 3,
    "3 days": 6,
    "1 week": 9,
    "1 month": 15,
}

DURATION_DAYS = {
    "1 day": 1,
    "3 days": 3,
    "1 week": 7,
    "1 month": 30,
}

INITIAL_KEYS = [
    ("Rust", "3 days", "ihTEE-6Q1VY-OZ5bm-0JbdS"),
    ("Rust", "3 days", "9deOd-IVvJp-fY8H9-sMIp1"),
    ("Rust", "3 days", "9w7yg-YGouc-iGfhS-nXzoM"),
    ("Fortnite", "3 days", "W25K6-Cep9r-czE0n-j4pMy"),
    ("Fortnite", "3 days", "FVpqz-Pqnnj-FaHdC-AAGEV"),
    ("Fortnite", "3 days", "jOaK1-vNlDV-cDquK-Yfjma"),
    ("Apex Legends", "3 days", "61GOB-OcK7A-IVjjj-9XMZI"),
    ("Apex Legends", "3 days", "nsRTb-wu8Ds-sz8so-0kljW"),
    ("Apex Legends", "3 days", "6evlu-EyV9s-Qa5tM-I5GLX"),
]

# -----------------------------
# Database
# -----------------------------

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        accepted_at TEXT,
        application_cooldown_until TEXT,
        free_key_used INTEGER NOT NULL DEFAULT 0,
        free_thumbnail_used INTEGER NOT NULL DEFAULT 0,
        balance INTEGER NOT NULL DEFAULT 0,
        thumbnail_balance INTEGER NOT NULL DEFAULT 0,
        tiktok_approved INTEGER NOT NULL DEFAULT 0,
        youtube_approved INTEGER NOT NULL DEFAULT 0,
        warned_for_48h INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        youtube TEXT,
        tiktok TEXT,
        proof TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        feedback TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        platform TEXT NOT NULL,
        url TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        feedback TEXT,
        created_at TEXT NOT NULL,
        reviewed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game TEXT NOT NULL,
        duration TEXT NOT NULL,
        key_value TEXT NOT NULL UNIQUE,
        claimed_by INTEGER,
        claimed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS active_keys (
        user_id INTEGER PRIMARY KEY,
        game TEXT NOT NULL,
        duration TEXT NOT NULL,
        key_id INTEGER NOT NULL,
        issued_at TEXT NOT NULL,
        expires_at TEXT,
        requirement_platform TEXT,
        requirement_target INTEGER NOT NULL DEFAULT 0,
        requirement_completed INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS setup (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)
    count = con.execute("SELECT COUNT(*) AS c FROM keys").fetchone()["c"]
    if count == 0:
        con.executemany(
            "INSERT OR IGNORE INTO keys(game,duration,key_value) VALUES(?,?,?)",
            INITIAL_KEYS
        )
    con.commit()
    con.close()

def now():
    return datetime.now(timezone.utc)

def iso(dt):
    return dt.isoformat()

def parse_dt(value):
    return datetime.fromisoformat(value) if value else None

def get_user(user_id):
    con = db()
    row = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        con.execute("INSERT INTO users(user_id) VALUES(?)", (user_id,))
        con.commit()
        row = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    return row

def update_user(user_id, **values):
    if not values:
        return
    con = db()
    get_user(user_id)
    fields = ", ".join(f"{k}=?" for k in values)
    con.execute(f"UPDATE users SET {fields} WHERE user_id=?", (*values.values(), user_id))
    con.commit()
    con.close()

def get_active_key(user_id):
    con = db()
    row = con.execute("SELECT * FROM active_keys WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    return row

def requirement_done(user_id):
    active = get_active_key(user_id)
    if not active:
        return True
    return bool(active["requirement_completed"])

def claim_key(user_id, game, duration, platform, is_free=False):
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        active = con.execute(
            "SELECT * FROM active_keys WHERE user_id=?", (user_id,)
        ).fetchone()

        if active:
            if active["requirement_completed"]:
                con.execute("DELETE FROM active_keys WHERE user_id=?", (user_id,))
            else:
                raise RuntimeError("You still have an active content requirement.")

        row = con.execute(
            """SELECT * FROM keys
               WHERE game=? AND duration=? AND claimed_by IS NULL
               ORDER BY id LIMIT 1""",
            (game, duration)
        ).fetchone()

        if not row:
            raise RuntimeError(f"No unused {duration} {game} keys are currently in stock.")

        issued = now()
        expires = issued + timedelta(days=DURATION_DAYS[duration]) if not is_free else None
        target = 3 if platform == "TikTok" else 1

        con.execute(
            """UPDATE keys SET claimed_by=?, claimed_at=? WHERE id=? AND claimed_by IS NULL""",
            (user_id, iso(issued), row["id"])
        )

        con.execute(
            """INSERT INTO active_keys(
                user_id,game,duration,key_id,issued_at,expires_at,
                requirement_platform,requirement_target,requirement_completed
            ) VALUES(?,?,?,?,?,?,?,?,0)""",
            (
                user_id, game, duration, row["id"], iso(issued),
                iso(issued + timedelta(hours=48)),
                platform, target
            )
        )

        if is_free:
            con.execute(
                "UPDATE users SET free_key_used=1 WHERE user_id=?",
                (user_id,)
            )

        con.commit()
        return row["key_value"], issued
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def add_approved_credit(user_id, platform):
    credit = 1 if platform == "TikTok" else 3
    thumbnail_credit = 1
    
    con = db()
    con.execute(
        """UPDATE users SET
           balance=balance+?,
           thumbnail_balance=thumbnail_balance+?,
           tiktok_approved=tiktok_approved+?,
           youtube_approved=youtube_approved+?
           WHERE user_id=?""",
        (
            credit,
            thumbnail_credit,
            1 if platform == "TikTok" else 0,
            1 if platform == "YouTube" else 0,
            user_id,
        )
    )

    active = con.execute(
        "SELECT * FROM active_keys WHERE user_id=?", (user_id,)
    ).fetchone()

    if active and not active["requirement_completed"]:
        if active["requirement_platform"] == platform:
            cutoff = active["issued_at"]
            approved_after = con.execute(
                """SELECT COUNT(*) AS c FROM submissions
                   WHERE user_id=? AND platform=? AND status='approved' AND reviewed_at>=?""",
                (user_id, platform, cutoff)
            ).fetchone()["c"]
            if approved_after >= active["requirement_target"]:
                con.execute(
                    "UPDATE active_keys SET requirement_completed=1 WHERE user_id=?",
                    (user_id,)
                )
    con.commit()
    con.close()

def spend_balance(user_id, cost):
    con = db()
    con.execute(
        "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
        (cost, user_id, cost)
    )
    changed = con.total_changes
    con.commit()
    con.close()
    return changed > 0

def spend_thumbnail(user_id):
    con = db()
    con.execute(
        "UPDATE users SET thumbnail_balance=thumbnail_balance-1 WHERE user_id=? AND thumbnail_balance>0",
        (user_id,)
    )
    changed = con.total_changes
    con.commit()
    con.close()
    return changed > 0

def get_key_by_id(key_id):
    con = db()
    row = con.execute("SELECT * FROM keys WHERE id=?", (key_id,)).fetchone()
    con.close()
    return row

def delete_key_by_id(key_id):
    con = db()
    con.execute("DELETE FROM keys WHERE id=? AND claimed_by IS NULL", (key_id,))
    changed = con.total_changes
    con.commit()
    con.close()
    return changed > 0

# -----------------------------
# Free Thumbnail System
# -----------------------------

THUMBNAIL_GAMES = {
    "Rust": "rust",
    "Fortnite": "fortnite",
    "Apex Legends": "apex",
    "HWID Swoofer": "hwid-swoofer",
}

def get_thumbnail_files(game):
    folder = Path(THUMBNAIL_DIR) / THUMBNAIL_GAMES[game]
    if not folder.exists():
        return []
    return sorted(
        [x for x in folder.iterdir()
         if x.is_file() and x.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")],
        key=lambda x: x.name.lower()
    )

# -----------------------------
# Logging Helper
# -----------------------------

async def log_action(title: str, description: str, color: discord.Color = discord.Color.blue(), user: discord.Member = None):
    """Send a log message to the configured log channel."""
    if not LOG_CHANNEL_ID:
        return
    
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if not channel:
        return
    
    embed = discord.Embed(
        title=f"📋 {title}",
        description=description,
        color=color,
        timestamp=datetime.now()
    )
    embed.set_footer(text=f"User: {user.display_name if user else 'System'}")
    
    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"Failed to send log: {e}")

# -----------------------------
# Discord helpers
# -----------------------------

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

class RoseBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        self.add_view(ApplyView())
        self.add_view(GetKeyView())
        self.add_view(SubmitStatsView())
        self.add_view(ThumbnailPanelView())

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Synced commands to guild {GUILD_ID}")

bot = RoseBot()

async def owner_user():
    try:
        return await bot.fetch_user(OWNER_ID)
    except Exception:
        return None

async def send_owner_dm(embed, view=None, content=None):
    user = await owner_user()
    if not user:
        return False
    try:
        await user.send(content=content, embed=embed, view=view)
        return True
    except discord.Forbidden:
        return False

def base_embed(title, description, color=discord.Color.from_rgb(190, 70, 150)):
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text="Rose Media")
    return e

def is_media(member: discord.Member):
    role_id = MEDIA_ROLE_ID
    role = member.guild.get_role(role_id) if role_id else None
    return bool(role and role in member.roles)

async def require_media(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This can only be used in the server.", ephemeral=True)
        return False
    if not is_media(interaction.user):
        await interaction.response.send_message("You need the Media Creator role first.", ephemeral=True)
        return False
    return True

# -----------------------------
# Application UI
# -----------------------------

class ApplicationModal(discord.ui.Modal, title="Media Creator Application"):
    youtube = discord.ui.TextInput(
        label="YouTube channel (optional)",
        placeholder="https://youtube.com/@yourchannel",
        required=False,
        max_length=300,
    )
    tiktok = discord.ui.TextInput(
        label="TikTok account (optional)",
        placeholder="https://tiktok.com/@yourusername",
        required=False,
        max_length=300,
    )
    proof = discord.ui.TextInput(
        label="Proof of ownership",
        placeholder="Direct image URL (Imgur/Discord CDN/etc.)",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        youtube = self.youtube.value.strip()
        tiktok = self.tiktok.value.strip()
        proof = self.proof.value.strip()

        if not youtube and not tiktok:
            await interaction.response.send_message(
                "Please provide at least a YouTube or TikTok link.",
                ephemeral=True,
            )
            return

        user = get_user(interaction.user.id)
        cooldown = parse_dt(user["application_cooldown_until"])
        if cooldown and cooldown > now():
            remaining = cooldown - now()
            hours = max(1, int(remaining.total_seconds() // 3600))
            await interaction.response.send_message(
                f"You cannot submit another application yet. Try again in about {hours} hour(s).",
                ephemeral=True,
            )
            return

        con = db()
        pending = con.execute(
            "SELECT id FROM applications WHERE user_id=? AND status='pending'",
            (interaction.user.id,)
        ).fetchone()
        if pending:
            con.close()
            await interaction.response.send_message(
                "You already have a pending application.",
                ephemeral=True,
            )
            return

        cur = con.execute(
            """INSERT INTO applications(user_id,youtube,tiktok,proof,created_at)
               VALUES(?,?,?,?,?)""",
            (interaction.user.id, youtube, tiktok, proof, iso(now()))
        )
        app_id = cur.lastrowid
        con.commit()
        con.close()

        embed = base_embed(
            "📋 New Media Creator Application",
            f"**Applicant:** {interaction.user.mention} (`{interaction.user.id}`)\n"
            f"**Application ID:** `{app_id}`\n\n"
            f"**YouTube:** {youtube or 'Not provided'}\n"
            f"**TikTok:** {tiktok or 'Not provided'}\n\n"
            f"**Proof of ownership:**\n{proof}",
            discord.Color.blurple(),
        )
        view = ApplicationReviewView(app_id, interaction.user.id)
        ok = await send_owner_dm(embed, view=view)

        if not ok:
            con = db()
            con.execute("DELETE FROM applications WHERE id=?", (app_id,))
            con.commit()
            con.close()
            await interaction.response.send_message(
                "I could not send the application to the owner. Please contact staff.",
                ephemeral=True,
            )
            return

        await log_action(
            "📝 New Application Submitted",
            f"**User:** {interaction.user.mention}\n**ID:** {app_id}\n**YouTube:** {youtube or 'None'}\n**TikTok:** {tiktok or 'None'}",
            discord.Color.blurple(),
            interaction.user
        )

        await interaction.response.send_message(
            "✅ Application submitted. It has been sent to the media team for review.",
            ephemeral=True,
        )

class ApplyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="📝 Apply Now",
            style=discord.ButtonStyle.success,
            custom_id="rose_apply",
        )

    async def callback(self, interaction):
        await interaction.response.send_modal(ApplicationModal())

class ApplyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ApplyButton())

class ApplicationReviewView(discord.ui.View):
    def __init__(self, app_id, user_id):
        super().__init__(timeout=None)
        self.app_id = app_id
        self.user_id = user_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction, button):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Only the configured owner can review applications.", ephemeral=True)
            return

        guild = bot.get_guild(GUILD_ID)
        member = guild.get_member(self.user_id) if guild else None
        if not member:
            await interaction.response.send_message("The applicant is no longer in the server.", ephemeral=True)
            return

        role = guild.get_role(MEDIA_ROLE_ID)
        if not role:
            await interaction.response.send_message("Media Creator role is not configured.", ephemeral=True)
            return

        await member.add_roles(role, reason="Rose Media application accepted")

        con = db()
        con.execute(
            "UPDATE applications SET status='accepted' WHERE id=?",
            (self.app_id,)
        )
        con.execute(
            """INSERT INTO users(user_id,accepted_at)
               VALUES(?,?)
               ON CONFLICT(user_id) DO UPDATE SET accepted_at=excluded.accepted_at""",
            (self.user_id, iso(now()))
        )
        con.commit()
        con.close()

        await log_action(
            "✅ Application Accepted",
            f"**User:** {member.mention}\n**Application ID:** {self.app_id}",
            discord.Color.green(),
            member
        )

        try:
            await member.send(
                embed=base_embed(
                    "🎉 Media Application Accepted",
                    "You have been accepted into the **Rose Media Creator Program**!\n\n"
                    "**What you get:**\n"
                    "• 1 Free game key (with content requirement)\n"
                    "• 1 Free thumbnail\n"
                    "• Earn more keys and thumbnails through approved content\n\n"
                    "Use **Get Key** to request your first free key or **Get Thumbnail** for your free thumbnail.",
                    discord.Color.green(),
                )
            )
        except discord.Forbidden:
            pass

        await interaction.response.edit_message(
            content=f"✅ Accepted <@{self.user_id}> and assigned the Media Creator role.",
            view=None,
        )

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction, button):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Only the configured owner can review applications.", ephemeral=True)
            return
        await interaction.response.send_modal(DeclineApplicationModal(self.app_id, self.user_id))

class DeclineApplicationModal(discord.ui.Modal, title="Decline Application"):
    feedback = discord.ui.TextInput(
        label="Feedback",
        placeholder="Explain what the applicant should improve...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500,
    )

    def __init__(self, app_id, user_id):
        super().__init__()
        self.app_id = app_id
        self.user_id = user_id

    async def on_submit(self, interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Only the owner can do this.", ephemeral=True)
            return

        cooldown = now() + timedelta(days=3)
        con = db()
        con.execute(
            """UPDATE applications
               SET status='declined', feedback=?
               WHERE id=?""",
            (self.feedback.value, self.app_id)
        )
        con.execute(
            """INSERT INTO users(user_id,application_cooldown_until)
               VALUES(?,?)
               ON CONFLICT(user_id) DO UPDATE SET application_cooldown_until=excluded.application_cooldown_until""",
            (self.user_id, iso(cooldown))
        )
        con.commit()
        con.close()

        guild = bot.get_guild(GUILD_ID)
        member = guild.get_member(self.user_id) if guild else None
        
        await log_action(
            "❌ Application Declined",
            f"**User:** <@{self.user_id}>\n**Feedback:** {self.feedback.value}",
            discord.Color.red(),
            member
        )

        if member:
            try:
                await member.send(
                    embed=base_embed(
                        "❌ Media Application Declined",
                        f"Your application was declined.\n\n**Feedback:**\n{self.feedback.value}\n\n"
                        "You may submit another application after **3 days**.",
                        discord.Color.red(),
                    )
                )
            except discord.Forbidden:
                pass

        await interaction.response.edit_message(
            content=f"❌ Declined <@{self.user_id}>. Cooldown: 3 days.",
            view=None,
        )

# -----------------------------
# Thumbnail UI
# -----------------------------

class ThumbnailGameSelect(discord.ui.Select):
    def __init__(self):
        options = []
        for game in THUMBNAIL_GAMES:
            files = get_thumbnail_files(game)
            if files:
                options.append(discord.SelectOption(
                    label=game, 
                    value=game,
                    description=f"{len(files)} thumbnails available"
                ))
            else:
                options.append(discord.SelectOption(
                    label=game, 
                    value=game,
                    description="No thumbnails available",
                    emoji="❌"
                ))
        
        super().__init__(
            placeholder="Choose a game...",
            options=options,
            custom_id="rose_thumbnail_game",
        )

    async def callback(self, interaction):
        game = self.values[0]
        files = get_thumbnail_files(game)
        
        if not files:
            await interaction.response.send_message(
                f"❌ There are currently no {game} thumbnails uploaded.",
                ephemeral=True,
            )
            return

        user = get_user(interaction.user.id)
        
        if user["thumbnail_balance"] <= 0 and user["free_thumbnail_used"]:
            await interaction.response.send_message(
                f"❌ You have no thumbnail credits remaining.\n\n"
                f"**How to earn more thumbnails:**\n"
                f"• Submit and get approved videos (1 credit per approval)\n"
                f"• Each approved video = 1 thumbnail credit\n"
                f"• Your current balance: **{user['thumbnail_balance']}** credits\n\n"
                f"*You got 1 free thumbnail when you joined!*",
                ephemeral=True,
            )
            return

        if not user["free_thumbnail_used"]:
            con = db()
            con.execute(
                "UPDATE users SET free_thumbnail_used=1 WHERE user_id=?",
                (interaction.user.id,)
            )
            con.commit()
            con.close()
            
            selected_file = random.choice(files)
            
            await log_action(
                "🖼️ Free Thumbnail Claimed",
                f"**User:** {interaction.user.mention}\n**Game:** {game}\n**Type:** First free thumbnail",
                discord.Color.green(),
                interaction.user
            )
            
            try:
                await interaction.user.send(
                    content=f"🖼️ **Free Thumbnail - {game}**\n\nHere's your free thumbnail! You get 1 free thumbnail as a new member.",
                    file=discord.File(str(selected_file), filename=selected_file.name)
                )
                await interaction.response.send_message(
                    f"✅ Thumbnail sent to your DMs! Check your messages. (1 free thumbnail used)",
                    ephemeral=True
                )
            except discord.Forbidden:
                await interaction.response.send_message(
                    f"🖼️ **Free Thumbnail - {game}**",
                    file=discord.File(str(selected_file), filename=selected_file.name),
                    ephemeral=True
                )
            return

        if not spend_thumbnail(interaction.user.id):
            await interaction.response.send_message(
                f"❌ Failed to use thumbnail credit. Please try again.",
                ephemeral=True,
            )
            return

        selected_file = random.choice(files)
        user = get_user(interaction.user.id)
        
        await log_action(
            "🖼️ Thumbnail Claimed",
            f"**User:** {interaction.user.mention}\n**Game:** {game}\n**Credits remaining:** {user['thumbnail_balance']}",
            discord.Color.blue(),
            interaction.user
        )
        
        try:
            await interaction.user.send(
                content=f"🖼️ **Thumbnail - {game}**\n\nHere's your thumbnail! You have **{user['thumbnail_balance']}** thumbnail credits remaining.",
                file=discord.File(str(selected_file), filename=selected_file.name)
            )
            await interaction.response.send_message(
                f"✅ Thumbnail sent to your DMs! You have **{user['thumbnail_balance']}** credits remaining.",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"🖼️ **Thumbnail - {game}**\n\nYou have **{user['thumbnail_balance']}** credits remaining.",
                file=discord.File(str(selected_file), filename=selected_file.name),
                ephemeral=True
            )

class ThumbnailGameView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(ThumbnailGameSelect())

class GetThumbnailButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="🖼️ Get Thumbnail",
            style=discord.ButtonStyle.primary,
            custom_id="rose_get_thumbnail",
        )

    async def callback(self, interaction):
        if not await require_media(interaction):
            return
        
        user = get_user(interaction.user.id)
        
        embed = base_embed(
            "🖼️ Free Thumbnails",
            f"Choose a game to receive a random thumbnail.\n\n"
            f"**Your Thumbnail Credits:** {user['thumbnail_balance']}\n"
            f"**Free thumbnail used:** {'✅ Yes' if user['free_thumbnail_used'] else '❌ No (1 free available)'}\n\n"
            f"**How to earn more:**\n"
            f"• Submit videos for review\n"
            f"• Get approved = 1 thumbnail credit\n"
            f"• Unlimited earning potential!",
        )
        await interaction.response.send_message(
            embed=embed,
            view=ThumbnailGameView(),
            ephemeral=True,
        )

class ThumbnailPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(GetThumbnailButton())

# -----------------------------
# Key UI
# -----------------------------

class PlatformSelect(discord.ui.Select):
    def __init__(self, game):
        self.game = game
        options = [
            discord.SelectOption(label="TikTok", emoji="🎵", value="TikTok",
                                 description="3 TikToks required for the first free key."),
            discord.SelectOption(label="YouTube", emoji="▶️", value="YouTube",
                                 description="1 YouTube video required for the first free key."),
        ]
        super().__init__(
            placeholder="Choose your content platform...",
            options=options,
            custom_id=f"platform_{game.replace(' ', '_')}",
        )

    async def callback(self, interaction):
        await interaction.response.send_message(
            embed=base_embed(
                f"🔑 {self.game} — {self.values[0]}",
                "Choose the key duration below.\n\n"
                "**First key:** Free, with a 48-hour content requirement.\n"
                "**Later keys:** Your approved-video balance determines which duration you can afford.",
            ),
            view=DurationView(self.game, self.values[0]),
            ephemeral=True,
        )

class PlatformView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=180)
        self.add_item(PlatformSelect(game))

class GameSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Choose a game...",
            options=[discord.SelectOption(label=g, value=g) for g in GAME_CHOICES],
            custom_id="game_select",
        )

    async def callback(self, interaction):
        await interaction.response.send_message(
            embed=base_embed(
                f"🎮 {self.values[0]}",
                "Choose TikTok or YouTube for this key's content requirement.",
            ),
            view=PlatformView(self.values[0]),
            ephemeral=True,
        )

class GameView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(GameSelect())

class DurationSelect(discord.ui.Select):
    def __init__(self, game, platform):
        self.game = game
        self.platform = platform
        options = [
            discord.SelectOption(label="1 day", value="1 day", description="3 TikToks or 1 YouTube"),
            discord.SelectOption(label="3 days", value="3 days", description="6 TikToks or 2 YouTube videos"),
            discord.SelectOption(label="1 week", value="1 week", description="9 TikToks or 3 YouTube videos"),
            discord.SelectOption(label="1 month", value="1 month", description="15 TikToks or 5 YouTube videos"),
        ]
        super().__init__(
            placeholder="Choose key duration...",
            options=options,
            custom_id=f"duration_{game.replace(' ', '_')}_{platform.lower()}",
        )

    async def callback(self, interaction):
        user = get_user(interaction.user.id)
        active = get_active_key(interaction.user.id)

        if active and not active["requirement_completed"]:
            await interaction.response.send_message(
                "❌ You cannot request another key until your current content requirement is completed and approved.",
                ephemeral=True,
            )
            return

        if not user["free_key_used"]:
            try:
                key, issued = claim_key(
                    interaction.user.id,
                    self.game,
                    "3 days",
                    self.platform,
                    is_free=True,
                )
            except RuntimeError as e:
                await interaction.response.send_message(f"❌ {e}", ephemeral=True)
                return

            await log_action(
                "🔑 Free Key Claimed",
                f"**User:** {interaction.user.mention}\n**Game:** {self.game}\n**Duration:** 3 days\n**Platform:** {self.platform}\n**Key:** ||{key}||",
                discord.Color.green(),
                interaction.user
            )

            await interaction.response.send_message(
                embed=base_embed(
                    "🎉 Free Media Key Issued",
                    f"**Game:** {self.game}\n"
                    f"**Key:** `{key}`\n"
                    f"**Platform:** {self.platform}\n\n"
                    f"**Requirement:** {'3 TikToks' if self.platform == 'TikTok' else '1 YouTube video'}\n"
                    "**Deadline:** 48 hours\n\n"
                    "Your submissions must be approved before you can request another key.",
                    discord.Color.green(),
                ),
                ephemeral=True,
            )
            return

        duration = self.values[0]
        cost = DURATION_COSTS[duration]

        if user["balance"] < cost:
            await interaction.response.send_message(
                f"❌ You need **{cost} credits** for a {duration} key. You currently have **{user['balance']}**.",
                ephemeral=True,
            )
            return

        if not spend_balance(interaction.user.id, cost):
            await interaction.response.send_message("❌ Your balance changed. Please try again.", ephemeral=True)
            return

        try:
            key, issued = claim_key(
                interaction.user.id,
                self.game,
                duration,
                self.platform,
                is_free=False,
            )
        except RuntimeError as e:
            con = db()
            con.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (cost, interaction.user.id))
            con.commit()
            con.close()
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        await log_action(
            "🔑 Key Claimed",
            f"**User:** {interaction.user.mention}\n**Game:** {self.game}\n**Duration:** {duration}\n**Platform:** {self.platform}\n**Cost:** {cost} credits\n**Key:** ||{key}||",
            discord.Color.blue(),
            interaction.user
        )

        await interaction.response.send_message(
            embed=base_embed(
                "🔑 Key Issued",
                f"**Game:** {self.game}\n"
                f"**Duration:** {duration}\n"
                f"**Key:** `{key}`\n"
                f"**Credits spent:** {cost}\n"
                f"**Remaining balance:** {get_user(interaction.user.id)['balance']}",
                discord.Color.green(),
            ),
            ephemeral=True,
        )

class DurationView(discord.ui.View):
    def __init__(self, game, platform):
        super().__init__(timeout=180)
        self.add_item(DurationSelect(game, platform))

class GetKeyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="🔑 Get Key",
            style=discord.ButtonStyle.primary,
            custom_id="rose_get_key",
        )

    async def callback(self, interaction):
        if not await require_media(interaction):
            return
        await interaction.response.send_message(
            embed=base_embed(
                "🔑 Request Game Key",
                "Choose a game to continue.",
            ),
            view=GameView(),
            ephemeral=True,
        )

class GetKeyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(GetKeyButton())

# -----------------------------
# Video UI
# -----------------------------

class VideoSubmissionModal(discord.ui.Modal, title="Submit Video"):
    url = discord.ui.TextInput(
        label="Video URL",
        placeholder="Paste your TikTok or YouTube video link",
        required=True,
        max_length=500,
    )

    platform = discord.ui.TextInput(
        label="Platform",
        placeholder="Type exactly: TikTok or YouTube",
        required=True,
        max_length=20,
    )

    async def on_submit(self, interaction):
        if not await require_media(interaction):
            return

        platform = self.platform.value.strip().title()
        url = self.url.value.strip()

        if platform not in ("TikTok", "Youtube"):
            await interaction.response.send_message(
                "Platform must be `TikTok` or `YouTube`.",
                ephemeral=True,
            )
            return
        if platform == "Youtube":
            platform = "YouTube"

        con = db()
        cur = con.execute(
            """INSERT INTO submissions(user_id,platform,url,created_at)
               VALUES(?,?,?,?)""",
            (interaction.user.id, platform, url, iso(now()))
        )
        sub_id = cur.lastrowid
        con.commit()
        con.close()

        embed = base_embed(
            "📹 New Video Submission",
            f"**Creator:** {interaction.user.mention} (`{interaction.user.id}`)\n"
            f"**Submission ID:** `{sub_id}`\n"
            f"**Platform:** {platform}\n"
            f"**Video:** {url}",
            discord.Color.green(),
        )
        ok = await send_owner_dm(embed, view=VideoReviewView(sub_id, interaction.user.id))
        if not ok:
            con = db()
            con.execute("UPDATE submissions SET status='failed' WHERE id=?", (sub_id,))
            con.commit()
            con.close()
            await interaction.response.send_message(
                "Could not send the submission to the owner.",
                ephemeral=True,
            )
            return

        await log_action(
            "📹 Video Submitted",
            f"**User:** {interaction.user.mention}\n**Platform:** {platform}\n**URL:** [Link]({url})\n**Submission ID:** {sub_id}",
            discord.Color.blue(),
            interaction.user
        )

        await interaction.response.send_message(
            "✅ Video submitted for review.",
            ephemeral=True,
        )

class SubmitVideoButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="📤 Submit Video",
            style=discord.ButtonStyle.success,
            custom_id="rose_submit_video",
        )

    async def callback(self, interaction):
        await interaction.response.send_modal(VideoSubmissionModal())

class StatsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="📊 My Stats",
            style=discord.ButtonStyle.primary,
            custom_id="rose_my_stats",
        )

    async def callback(self, interaction):
        if not await require_media(interaction):
            return
        user = get_user(interaction.user.id)
        active = get_active_key(interaction.user.id)

        pending = db()
        pending_count = pending.execute(
            "SELECT COUNT(*) AS c FROM submissions WHERE user_id=? AND status='pending'",
            (interaction.user.id,)
        ).fetchone()["c"]
        pending.close()

        active_text = "None"
        if active:
            deadline = parse_dt(active["expires_at"])
            if active["requirement_completed"]:
                active_text = "Requirement completed — another key may be requested."
            else:
                active_text = (
                    f"{active['game']} {active['duration']} | "
                    f"{active['requirement_platform']} requirement: "
                    f"{active['requirement_target']} approval(s) | "
                    f"deadline: {discord.utils.format_dt(deadline, 'R')}"
                )

        await interaction.response.send_message(
            embed=base_embed(
                "📊 Your Media Stats",
                f"**Balance:** {user['balance']} credits\n"
                f"**Thumbnail Credits:** {user['thumbnail_balance']}\n"
                f"**Free Thumbnail Used:** {'✅ Yes' if user['free_thumbnail_used'] else '❌ No'}\n"
                f"**Approved TikToks:** {user['tiktok_approved']}\n"
                f"**Approved YouTube videos:** {user['youtube_approved']}\n"
                f"**Pending submissions:** {pending_count}\n\n"
                f"**Current key requirement:** {active_text}\n\n"
                "**Credit values:** TikTok = 1, YouTube = 3\n"
                "**Key costs:** 1d = 3, 3d = 6, 1w = 9, 1mo = 15\n"
                "**Thumbnails:** 1 per approved video",
            ),
            ephemeral=True,
        )

class SubmitStatsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(SubmitVideoButton())
        self.add_item(StatsButton())

class VideoReviewView(discord.ui.View):
    def __init__(self, submission_id, user_id):
        super().__init__(timeout=None)
        self.submission_id = submission_id
        self.user_id = user_id

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction, button):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Only the owner can review videos.", ephemeral=True)
            return
        await interaction.response.send_modal(VideoFeedbackModal(self.submission_id, self.user_id, True))

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction, button):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Only the owner can review videos.", ephemeral=True)
            return
        await interaction.response.send_modal(VideoFeedbackModal(self.submission_id, self.user_id, False))

class VideoFeedbackModal(discord.ui.Modal, title="Video Review Feedback"):
    feedback = discord.ui.TextInput(
        label="Feedback",
        placeholder="Tell the creator what they did well or what to improve...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500,
    )

    def __init__(self, submission_id, user_id, approved):
        super().__init__(title="Approve Video" if approved else "Decline Video")
        self.submission_id = submission_id
        self.user_id = user_id
        self.approved = approved

    async def on_submit(self, interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Only the owner can do this.", ephemeral=True)
            return

        status = "approved" if self.approved else "declined"
        con = db()
        row = con.execute(
            "SELECT * FROM submissions WHERE id=?", (self.submission_id,)
        ).fetchone()

        if not row or row["status"] != "pending":
            con.close()
            await interaction.response.send_message("This submission was already reviewed.", ephemeral=True)
            return

        con.execute(
            """UPDATE submissions SET status=?, feedback=?, reviewed_at=? WHERE id=?""",
            (status, self.feedback.value, iso(now()), self.submission_id)
        )
        con.commit()
        con.close()

        guild = bot.get_guild(GUILD_ID)
        member = guild.get_member(self.user_id) if guild else None

        if self.approved:
            add_approved_credit(self.user_id, row["platform"])
            
            await log_action(
                "✅ Video Approved",
                f"**User:** <@{self.user_id}>\n**Platform:** {row['platform']}\n**URL:** [Link]({row['url']})\n**Feedback:** {self.feedback.value}",
                discord.Color.green(),
                member
            )
        else:
            await log_action(
                "❌ Video Declined",
                f"**User:** <@{self.user_id}>\n**Platform:** {row['platform']}\n**Feedback:** {self.feedback.value}",
                discord.Color.red(),
                member
            )

        if member:
            try:
                title = "✅ Video Approved" if self.approved else "❌ Video Declined"
                color = discord.Color.green() if self.approved else discord.Color.red()
                user = get_user(self.user_id)
                description = (
                    f"Your **{row['platform']}** submission has been approved.\n\n"
                    f"**Feedback:**\n{self.feedback.value}\n\n"
                    f"**Credits earned:**\n"
                    f"• Key credits: **{3 if row['platform']=='YouTube' else 1}**\n"
                    f"• Thumbnail credits: **1**\n\n"
                    f"**Your balances:**\n"
                    f"• Key credits: {user['balance']}\n"
                    f"• Thumbnail credits: {user['thumbnail_balance']}"
                    if self.approved else
                    f"Your **{row['platform']}** submission was declined.\n\n"
                    f"**Feedback:**\n{self.feedback.value}"
                )
                await member.send(embed=base_embed(title, description, color))
            except discord.Forbidden:
                pass

        await interaction.response.edit_message(
            content=f"{'✅ Approved' if self.approved else '❌ Declined'} submission `{self.submission_id}`.",
            view=None,
        )

# -----------------------------
# Setup / admin commands
# -----------------------------

async def ensure_channel(guild, name, category=None, topic=None):
    existing = discord.utils.get(guild.text_channels, name=name)
    if existing:
        return existing
    return await guild.create_text_channel(name, category=category, topic=topic)

@bot.tree.command(name="setup", description="Create the Rose Media channels, role, and panels.")
async def setup(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return

    global MEDIA_ROLE_ID, MEDIA_INFO_CATEGORY_ID, MEDIA_TEAM_CATEGORY_ID
    global APPLY_CHANNEL_ID, SUBMIT_CHANNEL_ID, GET_KEY_CHANNEL_ID, LOG_CHANNEL_ID

    guild = interaction.guild
    await interaction.response.defer(ephemeral=True)

    role = guild.get_role(MEDIA_ROLE_ID) if MEDIA_ROLE_ID else None
    if not role:
        role = await guild.create_role(
            name="Media Creator",
            color=discord.Color.from_rgb(190, 70, 150),
            reason="Rose Media setup",
        )

    media_info = discord.utils.get(guild.categories, name="Media Information")
    if not media_info:
        media_info = await guild.create_category("Media Information")

    media_team = discord.utils.get(guild.categories, name="Media Team")
    if not media_team:
        media_team = await guild.create_category("Media Team")

    apply_ch = await ensure_channel(guild, "apply-for-media", media_info)
    announcements = await ensure_channel(guild, "announcements", media_info)
    rules = await ensure_channel(guild, "rules", media_info)
    faq = await ensure_channel(guild, "faq", media_info)

    news = await ensure_channel(guild, "news", media_team)
    submit = await ensure_channel(guild, "submit-video", media_team)
    get_key = await ensure_channel(guild, "get-key", media_team)
    thumbnails = await ensure_channel(guild, "free-thumbnails", media_team)
    log_channel = await ensure_channel(guild, "bot-logs", media_team)
    chat = await ensure_channel(guild, "chat", media_team)
    media_help = await ensure_channel(guild, "media-help", media_team)
    bugs = await ensure_channel(guild, "bugs", media_team)

    # Set LOG_CHANNEL_ID globally
    LOG_CHANNEL_ID = log_channel.id

    apply_embed = base_embed(
        "📝 Media Creator Applications",
        "**Join Our Exclusive Creator Program**\n\n"
        "We're looking for dedicated content creators who want to grow with us.\n\n"
        "**Requirements**\n"
        "• Proof of previous content (TikTok/YouTube)\n"
        "• Proof that you own the account\n"
        "• Quality content that represents our brand\n\n"
        "**Benefits**\n"
        "• Free game keys\n"
        "• Free thumbnails\n"
        "• Longer keys earned through approved content\n"
        "• Direct support from our team\n\n"
        "Click the button below to apply.\n"
        "Applications are reviewed by the media team.",
    )
    await apply_ch.send(embed=apply_embed, view=ApplyView())

    get_key_embed = base_embed(
        "🔑 Request Game Keys",
        "**Get Keys for Content Creation**\n\n"
        "Choose a game and platform. Your first approved application unlocks a free key.\n\n"
        "**Games:**\n"
        "• Rust\n• Fortnite\n• Apex Legends\n• HWID Swoofer\n\n"
        "**First key requirement:**\n"
        "• TikTok: 3 approved videos\n"
        "• YouTube: 1 approved video\n"
        "• Deadline: 48 hours\n\n"
        "Complete and get the requirement approved before requesting another key.",
    )
    await get_key.send(embed=get_key_embed, view=GetKeyView())

    submit_embed = base_embed(
        "📤 Submit Videos & View Stats",
        "**Submit Your Content**\n\n"
        "After creating your video, submit the link for review.\n\n"
        "**Supported platforms:**\n"
        "• TikTok\n"
        "• YouTube\n\n"
        "Approved videos increase your balance. Use **My Stats** to check your balance and requirements.",
    )
    await submit.send(embed=submit_embed, view=SubmitStatsView())
    
    thumbnail_embed = base_embed(
        "🖼️ Free Thumbnails",
        "**Free YouTube Thumbnails for Media Creators**\n\n"
        "Choose a game below to receive a random thumbnail.\n\n"
        "**How it works:**\n"
        "• New members get **1 free thumbnail**\n"
        "• Submit videos to earn more thumbnail credits\n"
        "• Each approved video = **1 thumbnail credit**\n"
        "• Unlimited earning potential!\n\n"
        "**Available Games:**\n"
        "• 🦀 **Rust**\n"
        "• 🔫 **Fortnite**\n"
        "• 🎯 **Apex Legends**\n"
        "• 💻 **HWID Swoofer**\n\n"
        "📁 *Each thumbnail is sent to your DMs for maximum quality.*",
    )
    await thumbnails.send(embed=thumbnail_embed, view=ThumbnailPanelView())

    log_embed = discord.Embed(
        title="📋 Bot Logs",
        description="All important bot actions will be logged here.",
        color=discord.Color.blue()
    )
    await log_channel.send(embed=log_embed)

    con = db()
    for k, v in {
        "media_role_id": role.id,
        "media_info_category_id": media_info.id,
        "media_team_category_id": media_team.id,
        "apply_channel_id": apply_ch.id,
        "submit_channel_id": submit.id,
        "get_key_channel_id": get_key.id,
        "thumbnails_channel_id": thumbnails.id,
        "log_channel_id": log_channel.id,
    }.items():
        con.execute(
            "INSERT INTO setup(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (k, str(v))
        )
    con.commit()
    con.close()

    MEDIA_ROLE_ID = role.id
    MEDIA_INFO_CATEGORY_ID = media_info.id
    MEDIA_TEAM_CATEGORY_ID = media_team.id
    APPLY_CHANNEL_ID = apply_ch.id
    SUBMIT_CHANNEL_ID = submit.id
    GET_KEY_CHANNEL_ID = get_key.id

    await interaction.followup.send(
        f"✅ Setup complete.\n"
        f"Media Creator role: `{role.id}`\n"
        f"Apply: {apply_ch.mention}\n"
        f"Submit: {submit.mention}\n"
        f"Get Key: {get_key.mention}\n"
        f"Thumbnails: {thumbnails.mention}\n"
        f"Logs: {log_channel.mention}",
        ephemeral=True,
    )

@bot.tree.command(name="setup_thumbnails", description="Set up or refresh the free thumbnails channel.")
async def setup_thumbnails(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return

    guild = interaction.guild
    await interaction.response.defer(ephemeral=True)

    media_team = discord.utils.get(guild.categories, name="Media Team")
    if not media_team:
        media_team = await guild.create_category("Media Team")

    thumbnails_channel = discord.utils.get(guild.text_channels, name="free-thumbnails")
    if not thumbnails_channel:
        thumbnails_channel = await guild.create_text_channel(
            "free-thumbnails",
            category=media_team,
            topic="Free thumbnails for content creators - select a game to download"
        )

    await thumbnails_channel.purge(limit=100)

    thumbnail_embed = base_embed(
        "🖼️ Free Thumbnails",
        "**Free YouTube Thumbnails for Media Creators**\n\n"
        "Choose a game below to receive a random thumbnail.\n\n"
        "**How it works:**\n"
        "• New members get **1 free thumbnail**\n"
        "• Submit videos to earn more thumbnail credits\n"
        "• Each approved video = **1 thumbnail credit**\n"
        "• Unlimited earning potential!\n\n"
        "**Available Games:**\n"
        "• 🦀 **Rust**\n"
        "• 🔫 **Fortnite**\n"
        "• 🎯 **Apex Legends**\n"
        "• 💻 **HWID Swoofer**\n\n"
        "📁 *Each thumbnail is sent to your DMs for maximum quality.*",
    )
    
    await thumbnails_channel.send(embed=thumbnail_embed, view=ThumbnailPanelView())

    con = db()
    con.execute(
        "INSERT INTO setup(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("thumbnails_channel_id", str(thumbnails_channel.id))
    )
    con.commit()
    con.close()

    await interaction.followup.send(
        f"✅ Thumbnail channel set up successfully!\n"
        f"Channel: {thumbnails_channel.mention}",
        ephemeral=True
    )

@bot.tree.command(name="check_thumbnails", description="Check if thumbnails are properly set up.")
async def check_thumbnails(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    
    thumbnail_path = Path(THUMBNAIL_DIR)
    if not thumbnail_path.exists():
        await interaction.followup.send(
            "❌ Thumbnail directory doesn't exist.",
            ephemeral=True
        )
        return

    games_found = []
    games_missing = []
    
    for game, folder_name in THUMBNAIL_GAMES.items():
        game_folder = thumbnail_path / folder_name
        if game_folder.exists():
            files = [f for f in game_folder.iterdir() if f.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp')]
            if files:
                games_found.append(f"✅ {game}: {len(files)} thumbnails found")
            else:
                games_missing.append(f"⚠️ {game}: Folder exists but no images found")
        else:
            games_missing.append(f"❌ {game}: Folder missing")

    guild = interaction.guild
    channel = discord.utils.get(guild.text_channels, name="free-thumbnails")
    channel_status = f"✅ free-thumbnails exists" if channel else "❌ free-thumbnails channel missing"

    response = (
        f"**Thumbnail System Status**\n\n"
        f"**Channel:** {channel_status}\n\n"
        f"**Game Folders:**\n"
        + "\n".join(games_found) + "\n" + "\n".join(games_missing)
    )
    
    await interaction.followup.send(response, ephemeral=True)

@bot.tree.command(name="addkey", description="Add one unused game key to inventory.")
@app_commands.describe(game="Game name", duration="Key duration", key="The unused key")
async def addkey(interaction, game: str, duration: str, key: str):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    if game not in GAME_CHOICES:
        await interaction.response.send_message("Invalid game.", ephemeral=True)
        return
    if duration not in ("1 day", "3 days", "1 week", "1 month"):
        await interaction.response.send_message("Invalid duration.", ephemeral=True)
        return

    con = db()
    try:
        con.execute(
            "INSERT INTO keys(game,duration,key_value) VALUES(?,?,?)",
            (game, duration, key.strip())
        )
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        await interaction.response.send_message("That key already exists in the inventory.", ephemeral=True)
        return
    con.close()

    await log_action(
        "➕ Key Added",
        f"**Game:** {game}\n**Duration:** {duration}\n**Key:** ||{key}||",
        discord.Color.green()
    )

    await interaction.response.send_message("✅ Key added to inventory.", ephemeral=True)

@bot.tree.command(name="keys", description="Show all unused keys in inventory.")
async def keys(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    
    con = db()
    rows = con.execute(
        """SELECT id, game, duration, key_value FROM keys WHERE claimed_by IS NULL ORDER BY game, duration"""
    ).fetchall()
    con.close()

    if not rows:
        await interaction.response.send_message(
            embed=base_embed("🔐 Key Inventory", "No unused keys available."),
            ephemeral=True,
        )
        return

    # Group by game
    grouped = {}
    for row in rows:
        key = f"{row['game']} - {row['duration']}"
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(row["key_value"])

    description = ""
    for key_group, keys_list in grouped.items():
        description += f"**{key_group}** ({len(keys_list)} keys):\n"
        for k in keys_list:
            description += f"• `{k}`\n"
        description += "\n"

    await interaction.response.send_message(
        embed=base_embed("🔐 Key Inventory", description[:4000]),
        ephemeral=True,
    )

@bot.tree.command(name="removekey", description="Remove a specific key from inventory by its value.")
@app_commands.describe(key_value="The exact key value to remove")
async def removekey(interaction: discord.Interaction, key_value: str):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return

    con = db()
    row = con.execute(
        "SELECT * FROM keys WHERE key_value=? AND claimed_by IS NULL", 
        (key_value.strip(),)
    ).fetchone()
    
    if not row:
        con.close()
        await interaction.response.send_message("❌ Key not found or already claimed.", ephemeral=True)
        return

    con.execute("DELETE FROM keys WHERE id=?", (row["id"],))
    con.commit()
    con.close()

    await log_action(
        "🗑️ Key Removed",
        f"**Game:** {row['game']}\n**Duration:** {row['duration']}\n**Key:** ||{key_value}||",
        discord.Color.red()
    )

    await interaction.response.send_message(f"✅ Removed key: `{key_value}`", ephemeral=True)

@bot.tree.command(name="addkeys_bulk", description="Add multiple keys at once.")
@app_commands.describe(keys="Format: game|duration|key (one per line)")
async def addkeys_bulk(interaction: discord.Interaction, keys: str):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return

    lines = keys.strip().split("\n")
    added = 0
    failed = 0

    con = db()
    for line in lines:
        parts = line.split("|")
        if len(parts) != 3:
            failed += 1
            continue
        game, duration, key = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if game not in GAME_CHOICES or duration not in ("1 day", "3 days", "1 week", "1 month"):
            failed += 1
            continue
        try:
            con.execute(
                "INSERT INTO keys(game,duration,key_value) VALUES(?,?,?)",
                (game, duration, key)
            )
            added += 1
        except sqlite3.IntegrityError:
            failed += 1
    
    con.commit()
    con.close()

    await log_action(
        "📦 Bulk Keys Added",
        f"**Added:** {added}\n**Failed:** {failed}",
        discord.Color.green()
    )

    await interaction.response.send_message(
        f"✅ Added {added} keys. Failed: {failed}",
        ephemeral=True
    )

@bot.tree.command(name="reset_user", description="Reset a user's progress and obligations.")
@app_commands.describe(user="User to reset")
async def reset_user(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return

    con = db()
    con.execute("DELETE FROM active_keys WHERE user_id=?", (user.id,))
    con.execute(
        "UPDATE users SET free_key_used=0, free_thumbnail_used=0 WHERE user_id=?",
        (user.id,)
    )
    con.commit()
    con.close()

    await log_action(
        "🔄 User Reset",
        f"**User:** {user.mention}\n**Progress reset**",
        discord.Color.orange(),
        user
    )

    await interaction.response.send_message(f"✅ Reset progress for {user.mention}.", ephemeral=True)

@bot.tree.command(name="reset_application", description="Let a user apply again immediately.")
@app_commands.describe(user="User to reset")
async def reset_application(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    update_user(user.id, application_cooldown_until=None)

    await log_action(
        "🔄 Application Reset",
        f"**User:** {user.mention}\n**Application cooldown removed**",
        discord.Color.orange(),
        user
    )

    await interaction.response.send_message(f"✅ Application cooldown reset for {user.mention}.", ephemeral=True)

@bot.tree.command(name="cancel_key", description="Manually release a user's active requirement/key lock.")
@app_commands.describe(user="User whose active key requirement should be released")
async def cancel_key(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    con = db()
    con.execute("DELETE FROM active_keys WHERE user_id=?", (user.id,))
    con.commit()
    con.close()

    await log_action(
        "🔓 Key Requirement Released",
        f"**User:** {user.mention}\n**Active key requirement removed**",
        discord.Color.orange(),
        user
    )

    await interaction.response.send_message(f"✅ Released the active key requirement for {user.mention}.", ephemeral=True)

@bot.tree.command(name="add_thumbnails", description="Add thumbnail credits to a user.")
@app_commands.describe(user="User to add credits to", amount="Number of credits to add")
async def add_thumbnails(interaction: discord.Interaction, user: discord.Member, amount: int):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return
    
    con = db()
    con.execute(
        "UPDATE users SET thumbnail_balance=thumbnail_balance+? WHERE user_id=?",
        (amount, user.id)
    )
    con.commit()
    con.close()
    
    updated_user = get_user(user.id)

    await log_action(
        "➕ Thumbnail Credits Added",
        f"**User:** {user.mention}\n**Amount:** {amount}\n**New Balance:** {updated_user['thumbnail_balance']}",
        discord.Color.green(),
        user
    )

    await interaction.response.send_message(
        f"✅ Added {amount} thumbnail credit(s) to {user.mention}.\n"
        f"New balance: **{updated_user['thumbnail_balance']}** credits",
        ephemeral=True
    )

@bot.tree.command(name="add_keys", description="Add key credits to a user.")
@app_commands.describe(user="User to add credits to", amount="Number of credits to add")
async def add_keys(interaction: discord.Interaction, user: discord.Member, amount: int):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    
    if amount <= 0:
        await interaction.response.send_message("Amount must be positive.", ephemeral=True)
        return
    
    con = db()
    con.execute(
        "UPDATE users SET balance=balance+? WHERE user_id=?",
        (amount, user.id)
    )
    con.commit()
    con.close()
    
    updated_user = get_user(user.id)

    await log_action(
        "➕ Key Credits Added",
        f"**User:** {user.mention}\n**Amount:** {amount}\n**New Balance:** {updated_user['balance']}",
        discord.Color.green(),
        user
    )

    await interaction.response.send_message(
        f"✅ Added {amount} key credit(s) to {user.mention}.\n"
        f"New balance: **{updated_user['balance']}** credits",
        ephemeral=True
    )

@bot.tree.command(name="check_expired_keys", description="Check for users who haven't submitted videos within 48 hours.")
async def check_expired_keys(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    con = db()
    active_rows = con.execute(
        """SELECT * FROM active_keys 
           WHERE requirement_completed=0 
           AND datetime(issued_at) < datetime(?, '-48 hours')""",
        (iso(now()),)
    ).fetchall()
    con.close()

    if not active_rows:
        await interaction.followup.send("✅ No users have expired key requirements.", ephemeral=True)
        return

    message = "**⚠️ Users with Expired Key Requirements (48h+):**\n\n"
    for row in active_rows:
        user = await bot.fetch_user(row["user_id"])
        message += f"• {user.mention if user else f'<@{row["user_id"]}>'} - {row['game']} ({row['duration']})\n"

    await interaction.followup.send(message[:4000], ephemeral=True)

@bot.tree.command(name="unrole_expired", description="Remove Media Creator role from users who didn't submit videos in 48 hours.")
async def unrole_expired(interaction: discord.Interaction):
    """Automatically remove Media Creator role from users with expired key requirements."""
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    role = guild.get_role(MEDIA_ROLE_ID)
    if not role:
        await interaction.followup.send("❌ Media Creator role not configured.", ephemeral=True)
        return

    con = db()
    expired_users = con.execute(
        """SELECT user_id FROM active_keys 
           WHERE requirement_completed=0 
           AND datetime(issued_at) < datetime(?, '-48 hours')""",
        (iso(now()),)
    ).fetchall()
    con.close()

    if not expired_users:
        await interaction.followup.send("✅ No users to unrole.", ephemeral=True)
        return

    removed = []
    failed = []
    
    for row in expired_users:
        member = guild.get_member(row["user_id"])
        if member and role in member.roles:
            try:
                await member.remove_roles(role, reason="Failed to submit required video within 48 hours")
                removed.append(member.mention)
                
                await log_action(
                    "🚫 Auto-Unrole",
                    f"**User:** {member.mention}\n**Reason:** Failed to submit video within 48 hours",
                    discord.Color.red(),
                    member
                )
            except:
                failed.append(member.mention)

    # Clean up their active key record
    con = db()
    for row in expired_users:
        con.execute("DELETE FROM active_keys WHERE user_id=?", (row["user_id"],))
    con.commit()
    con.close()

    response = f"✅ Removed Media Creator role from {len(removed)} users."
    if failed:
        response += f"\n❌ Failed to remove from: {', '.join(failed)}"
    
    await interaction.followup.send(response, ephemeral=True)

@bot.tree.command(name="stats_user", description="View detailed stats for a specific user.")
@app_commands.describe(user="User to view stats for")
async def stats_user(interaction: discord.Interaction, user: discord.Member):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return

    user_data = get_user(user.id)
    active = get_active_key(user.id)

    con = db()
    submissions = con.execute(
        """SELECT platform, status, created_at FROM submissions 
           WHERE user_id=? ORDER BY created_at DESC LIMIT 5""",
        (user.id,)
    ).fetchall()
    con.close()

    description = (
        f"**User:** {user.mention}\n"
        f"**Balance:** {user_data['balance']} credits\n"
        f"**Thumbnail Credits:** {user_data['thumbnail_balance']}\n"
        f"**Free Key Used:** {'✅' if user_data['free_key_used'] else '❌'}\n"
        f"**Free Thumbnail Used:** {'✅' if user_data['free_thumbnail_used'] else '❌'}\n"
        f"**Approved TikToks:** {user_data['tiktok_approved']}\n"
        f"**Approved YouTube:** {user_data['youtube_approved']}\n\n"
    )

    if active:
        description += f"**Active Key:**\n"
        description += f"• Game: {active['game']}\n"
        description += f"• Duration: {active['duration']}\n"
        description += f"• Requirement: {active['requirement_target']} {active['requirement_platform']}(s)\n"
        description += f"• Completed: {'✅' if active['requirement_completed'] else '❌'}\n"
        if not active['requirement_completed']:
            deadline = parse_dt(active['expires_at'])
            if deadline:
                description += f"• Deadline: {discord.utils.format_dt(deadline, 'R')}\n"
    else:
        description += f"**Active Key:** None\n"

    if submissions:
        description += f"\n**Recent Submissions:**\n"
        for sub in submissions:
            status_emoji = "✅" if sub["status"] == "approved" else "❌" if sub["status"] == "declined" else "⏳"
            description += f"• {status_emoji} {sub['platform']} - {sub['status']} ({discord.utils.format_dt(parse_dt(sub['created_at']), 'R')})\n"

    await interaction.response.send_message(
        embed=base_embed(f"📊 Stats for {user.display_name}", description[:4000]),
        ephemeral=True
    )

# -----------------------------
# Startup / Render health server
# -----------------------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health", "/healthz"):
            self.send_response(404)
            self.end_headers()
            return

        ready = bot.is_ready()
        body = (
            '{"status":"ok","discord_ready":'
            + ("true" if ready else "false")
            + '}\n'
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return

health_server = None

def start_health_server():
    global health_server
    port = int(os.getenv("PORT", "10000"))
    health_server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health server listening on 0.0.0.0:{port}")
    health_server.serve_forever(poll_interval=0.5)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    
    # Force sync commands to guild
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        try:
            # Clear old commands first
            self.tree.clear_commands(guild=guild)
            await self.tree.sync(guild=guild)
            print("Cleared old guild commands")
        except:
            pass
            
        # Copy global commands to guild
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"Synced commands to guild {GUILD_ID}")
        
        # List all synced commands
        commands = await self.tree.fetch_commands(guild=guild)
        print(f"Available commands: {[cmd.name for cmd in commands]}")
    
    await log_action(
        "🟢 Bot Online",
        f"Bot is now online!\n**Bot:** {bot.user.mention}\n**Guilds:** {len(bot.guilds)}",
        discord.Color.green()
    )

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing from the environment")
    if not OWNER_ID or not GUILD_ID:
        raise SystemExit("OWNER_ID and GUILD_ID must be set in the environment")

    init_db()

    threading.Thread(
        target=start_health_server,
        name="render-health-server",
        daemon=True,
    ).start()

    bot.run(TOKEN, log_handler=None)
