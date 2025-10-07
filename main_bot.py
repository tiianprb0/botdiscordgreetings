import os
import re
import io
import json
import asyncio
from typing import Optional, Tuple
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands

import firebase_admin
from firebase_admin import credentials, firestore

# =========================
# ENV & FIREBASE INIT
# =========================
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    print("❌ Env DISCORD_BOT_TOKEN tidak ditemukan.")

firebase_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
if not firebase_json:
    print("❌ Env FIREBASE_SERVICE_ACCOUNT_JSON tidak ditemukan.")
    raise SystemExit(1)

try:
    cred = credentials.Certificate(json.loads(firebase_json))
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("✅ Firestore terhubung.")
except Exception as e:
    print(f"❌ Gagal inisialisasi Firestore: {e}")
    raise

# =========================
# KONFIG DISCORD
# =========================
CHANNEL_ID_WELCOME       = 1423964756158447738
CHANNEL_ID_LOGS          = 1423969192389902339
CHANNEL_ID_MABAR         = 1424029336683679794
CHANNEL_ID_INTRO         = 1424033383339659334
RULES_CHANNEL_ID         = 1423969192389902336
ROLE_ID_LIGHT            = 1424026593143164958
CHANNEL_ID_PHOTO_MEDIA   = 1424033929874247802
CHANNEL_ID_DOWNLOADER    = 1425023771185774612
CHANNEL_ID_SPOTLIGHT     = 1425015637197066260
CHANNEL_ID_PUBLIC_CHAT   = 1424032583519567952

REACTION_EMOJI = "🔆"

# WIB timezone
TZ = ZoneInfo("Asia/Jakarta")

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)
KONTEN_LIMIT = 1000

# =========================
# UTIL TIME (WIB)
# =========================
def now_wib() -> datetime:
    return datetime.now(TZ)

def to_epoch(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(timezone.utc).timestamp()

def from_epoch_to_wib(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=TZ)

def parse_natural_time(text: str, ref: datetime):
    t = text.lower().strip()
    if t in {"now", "sekarang", "skrng"}:
        return ref, "sekarang (WIB)"

    is_tomorrow = "besok" in t
    m = re.search(r"(\d{1,2})(?:[:.](\d{1,2}))?", t)
    hour, minute = 0, 0
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)

    pagi  = "pagi" in t
    siang = "siang" in t
    sore  = "sore" in t
    malam = "malam" in t

    if pagi and hour == 12:
        hour = 0
    elif (sore or malam) and hour < 12:
        hour += 12

    target = ref.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    if is_tomorrow or target <= ref:
        target += timedelta(days=1)

    return target, target.strftime("%H:%M WIB")

# =========================
# FIRESTORE HELPERS
# =========================
WELCOME_COL  = "welcome_messages"
MABAR_COL    = "mabar_reminders"
ANNOUNCE_COL = "announcements"
DL_LOGS_COL  = "downloads"
# config doc untuk status downloader & id pesan panduan
DL_CONFIG_COL = "config"
DL_CONFIG_DOC = "downloader"

try:
    from google.cloud.firestore_v1.base_query import FieldFilter
    HAS_FIELD_FILTER = True
except Exception:
    HAS_FIELD_FILTER = False

async def fs_add(col: str, data: dict):
    db.collection(col).add(data)

def fs_get_downloader_config() -> dict:
    doc = db.collection(DL_CONFIG_COL).document(DL_CONFIG_DOC).get()
    return doc.to_dict() if doc.exists else {"status": "off", "info_msg": None, "updated": None}

def fs_set_downloader_config(status: str, info_msg: Optional[int]):
    db.collection(DL_CONFIG_COL).document(DL_CONFIG_DOC).set({
        "status": status,
        "info_msg": info_msg,
        "updated": now_wib().isoformat()
    }, merge=True)

# =========================
# STARTUP
# =========================
@bot.event
async def on_ready():
    print(f"✅ Bot login sebagai {bot.user}")
    try:
        await bot.change_presence(activity=discord.Game("menjaga server ✨"))
    except Exception:
        pass

    # Resume semua mabar reminder yg masih 'scheduled'
    pending = load_pending_mabar(to_epoch(now_wib()))
    if pending:
        print(f"🕰️ Menjadwalkan ulang {len(pending)} reminder mabar dari Firestore.")
    for doc_id, dat in pending:
        asyncio.create_task(schedule_mabar_tasks_from_doc(doc_id, dat))

# =========================
# GREETINGS (WELCOME + EMOJI)
# =========================
async def save_welcome_message(user_id: int, message_id: int):
    try:
        db.collection(WELCOME_COL).document(str(user_id)).set({
            "message_id": message_id,
            "created_at": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        print("[WARN] Gagal simpan welcome_messages:", e)

async def get_welcome_message(user_id: int) -> Optional[int]:
    try:
        doc = db.collection(WELCOME_COL).document(str(user_id)).get()
        if doc.exists:
            v = doc.to_dict().get("message_id")
            return int(v) if v is not None else None
    except Exception as e:
        print("[WARN] Gagal ambil welcome_messages:", e)
    return None

async def delete_welcome_message(user_id: int):
    try:
        db.collection(WELCOME_COL).document(str(user_id)).delete()
    except Exception as e:
        print("[WARN] Gagal hapus welcome_messages:", e)

async def _safe_get_member(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    m = guild.get_member(user_id)
    if m is None:
        try:
            m = await guild.fetch_member(user_id)
        except Exception:
            m = None
    return m

@bot.event
async def on_member_join(member: discord.Member):
    ch = bot.get_channel(CHANNEL_ID_WELCOME)
    if not isinstance(ch, discord.TextChannel):
        return

    rules_ch = member.guild.get_channel(RULES_CHANNEL_ID)
    rules_text = rules_ch.mention if isinstance(rules_ch, discord.TextChannel) else "#rules"

    role_light = member.guild.get_role(ROLE_ID_LIGHT)
    role_text = role_light.mention if role_light else "**Light**"

    desc = (
        f"Halo {member.mention}, selamat datang di **{member.guild.name}**!\n"
        f"• Baca aturan di {rules_text}\n"
        f"• Klik reaksi {REACTION_EMOJI} di pesan ini untuk **ambil role {role_text}**.\n"
        f"• Klik ulang untuk melepas role."
    )

    embed = discord.Embed(
        title="🎉 Selamat Datang!",
        description=desc,
        color=discord.Color.green()
    )
    embed.set_footer(text="Selamat bergabung & have fun! ✨")

    msg = await ch.send(embed=embed)
    try:
        await msg.add_reaction(REACTION_EMOJI)
    except Exception:
        pass

    await save_welcome_message(member.id, msg.id)

    async def autodelete_welcome():
        await asyncio.sleep(24 * 3600)
        stored_id = await get_welcome_message(member.id)
        if stored_id and stored_id == msg.id:
            try:
                await msg.delete()
            except Exception:
                pass
            await delete_welcome_message(member.id)

    asyncio.create_task(autodelete_welcome())

@bot.event
async def on_member_remove(member: discord.Member):
    await delete_welcome_message(member.id)
    ch = bot.get_channel(CHANNEL_ID_WELCOME)
    if isinstance(ch, discord.TextChannel):
        embed = discord.Embed(
            title="👋 Selamat Tinggal",
            description=f"{member.display_name} telah keluar dari server.",
            color=discord.Color.red()
        )
        await ch.send(embed=embed)

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None or str(payload.emoji) != REACTION_EMOJI:
        return
    target_msg_id = await get_welcome_message(payload.user_id)
    if not target_msg_id or payload.message_id != target_msg_id:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    member = await _safe_get_member(guild, payload.user_id)
    if not member or member.bot:
        return
    role = guild.get_role(ROLE_ID_LIGHT)
    if not role:
        return
    try:
        if role not in member.roles:
            await member.add_roles(role, reason="Welcome reaction role: Light")
            intro = guild.get_channel(CHANNEL_ID_INTRO)
            if isinstance(intro, discord.TextChannel):
                await intro.send(
                    f"Ekhem… {member.mention}! Sebutin umur kamu aja boleh kok. "
                    f"Kalau mau cerita lebih, juga boleh, ngga perlu terlalu detail, ya!"
                )
    except Exception as e:
        print("[ERROR] Gagal add role:", e)

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None or str(payload.emoji) != REACTION_EMOJI:
        return
    target_msg_id = await get_welcome_message(payload.user_id)
    if not target_msg_id or payload.message_id != target_msg_id:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    member = await _safe_get_member(guild, payload.user_id)
    if not member or member.bot:
        return
    role = guild.get_role(ROLE_ID_LIGHT)
    if not role:
        return
    try:
        if role in member.roles:
            await member.remove_roles(role, reason="Welcome reaction role: Light (remove)")
    except Exception as e:
        print("[ERROR] Gagal remove role:", e)

# =========================
# LOG PESAN DIHAPUS
# =========================
@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot:
        return
    log_channel = bot.get_channel(CHANNEL_ID_LOGS)
    if not isinstance(log_channel, discord.TextChannel):
        return

    raw = (message.content or "")
    konten = raw[:KONTEN_LIMIT] + ("..." if len(raw) > KONTEN_LIMIT else "")
    konten = konten.replace("```", "")

    embed = discord.Embed(title="🗑️ Pesan Dihapus", color=discord.Color.orange())
    embed.add_field(name="Pengirim", value=message.author.mention, inline=False)
    if isinstance(message.channel, (discord.TextChannel, discord.Thread)):
        embed.add_field(name="Channel", value=message.channel.mention, inline=False)
    if konten.strip():
        embed.add_field(name="Konten", value=f"```{konten}```", inline=False)
    await log_channel.send(embed=embed)

# =========================
# FORWARD GAMBAR DENGAN KONFIRMASI
# =========================
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

def _is_image_attachment(att: discord.Attachment) -> bool:
    ct = (att.content_type or "").lower()
    if ct.startswith("image/"):
        return True
    name = (att.filename or "").lower()
    return any(name.endswith(ext) for ext in IMAGE_EXTS)

def _jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

async def _confirm_and_forward_images(message: discord.Message):
    if not message.guild or not message.attachments:
        return
    images = [att for att in message.attachments if _is_image_attachment(att)]
    if not images:
        return

    prompt = await message.channel.send(
        f"hola {message.author.mention}, apakah kamu ingin foto nya aku forward ke **Channel Photo-Media**?"
    )

    async def prompt_timeout_cleanup():
        await asyncio.sleep(30)
        try:
            await prompt.delete()
        except Exception:
            pass

    timeout_task = asyncio.create_task(prompt_timeout_cleanup())
    for em in ("✅", "❌"):
        try:
            await prompt.add_reaction(em)
        except Exception:
            pass

    def check(reaction, user):
        return (
            user == message.author
            and str(reaction.emoji) in ["✅", "❌"]
            and reaction.message.id == prompt.id
        )

    decided = False
    try:
        reaction, _ = await bot.wait_for("reaction_add", timeout=30.0, check=check)
        decided = True
    except asyncio.TimeoutError:
        await message.channel.send("⏰ Konfirmasi habis. Forward dibatalkan.", delete_after=6)
        return

    if decided:
        try: timeout_task.cancel()
        except Exception: pass
        try: await prompt.delete()
        except Exception: pass

    if str(reaction.emoji) == "❌":
        await message.channel.send("❌ Oke, tidak di-forward.", delete_after=5)
        return

    try:
        dest = bot.get_channel(CHANNEL_ID_PHOTO_MEDIA)
        if not isinstance(dest, discord.TextChannel):
            return await message.channel.send("⚠️ Channel Photo-Media tidak ditemukan.", delete_after=6)

        caption = message.clean_content.strip()
        prefix = f"media dari {message.author.mention}"
        content = f"{prefix}\n{caption}" if caption else prefix

        files = []
        for att in images[:10]:
            try:
                files.append(await att.to_file())
            except Exception as e:
                print("[WARN] Gagal mengambil attachment:", e)

        sent = await dest.send(content=content, files=files) if files else await dest.send(content)
        jump = _jump_url(message.guild.id, dest.id, sent.id) if sent else ""
        await message.channel.send(
            f"Ekhem.. media {message.author.mention} udah aku forward ke [Media Photo]({jump}), cuss lihat~",
            suppress_embeds=True,
            delete_after=10
        )
    except Exception as e:
        print("[ERROR] Gagal forward foto:", e)
        await message.channel.send("⚠️ Terjadi kendala saat forward media.", delete_after=6)

# =========================
# MABAR SCHEDULER
# =========================
def save_mabar_schedule(doc_id: str, data: dict):
    try:
        db.collection(MABAR_COL).document(doc_id).set(data)
    except Exception as e:
        print("[WARN] Gagal simpan mabar_reminders:", e)

def update_mabar_status(doc_id: str, **fields):
    try:
        db.collection(MABAR_COL).document(doc_id).update(fields)
    except Exception as e:
        print("[WARN] Gagal update mabar_reminders:", e)

def load_pending_mabar(now_epoch: float):
    try:
        if HAS_FIELD_FILTER:
            q = db.collection(MABAR_COL).where(filter=FieldFilter("status", "==", "scheduled")).stream()
        else:
            q = db.collection(MABAR_COL).where("status", "==", "scheduled").stream()
        items = []
        for d in q:
            dat = d.to_dict()
            if "remind_at_epoch" in dat and "guild_id" in dat and "channel_id" in dat and "map_name" in dat:
                if dat["remind_at_epoch"] + 5400 > now_epoch:
                    items.append((d.id, dat))
        return items
    except Exception as e:
        print("[WARN] Gagal load pending mabar:", e)
        return []

async def schedule_mabar_tasks_from_doc(doc_id: str, dat: dict):
    try:
        remind_at_epoch = float(dat["remind_at_epoch"])
        channel_id      = int(dat["channel_id"])
        map_name        = str(dat["map_name"])
        role_id         = int(dat.get("role_id", ROLE_ID_LIGHT))
        announce_msg_id = int(dat.get("announce_message_id", 0))
    except Exception as e:
        print("[WARN] Dokumen mabar invalid:", e, dat)
        return

    remind_at_dt = from_epoch_to_wib(remind_at_epoch)
    ch = bot.get_channel(channel_id)
    if not isinstance(ch, discord.TextChannel):
        print("[WARN] Channel mabar tidak ditemukan untuk doc:", doc_id)
        return

    role_mention = f"<@&{role_id}>"

    async def remind_task():
        delay = max(0, (remind_at_dt - now_wib()).total_seconds())
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await ch.send(f"{role_mention}\n⏰ Waktunya mabar **{map_name.title()}**! Siap-siap yuk 🎮")
            update_mabar_status(doc_id, status="reminded")
        except Exception as e:
            print("[ERROR] Reminder gagal:", e)

    async def autodelete_task():
        total = max(0, (remind_at_dt - now_wib()).total_seconds()) + 3600
        await asyncio.sleep(total)
        if announce_msg_id:
            try:
                msg = await ch.fetch_message(announce_msg_id)
                await msg.delete()
            except Exception:
                pass
        update_mabar_status(doc_id, status="done")

    asyncio.create_task(remind_task())
    asyncio.create_task(autodelete_task())

@bot.command(aliases=["main"])
async def mabar(ctx: commands.Context, *, arg: str = None):
    """Contoh: !mabar Distrik Violence jam 8 malam (WIB)"""
    role_light = ctx.guild.get_role(ROLE_ID_LIGHT) if ctx.guild else None
    if not role_light:
        return await ctx.send("⚠️ Role Light belum diset di kode.")
    if role_light not in ctx.author.roles:
        return await ctx.send("❌ Kamu belum punya role Light untuk pakai perintah ini!")

    if not arg:
        return await ctx.send("Gunakan format: `!mabar [nama game/map] [jam]`")

    tokens = arg.strip()
    w_match = re.search(
        r"(?:\bjam\b|\bpukul\b|(?:\d{1,2}(?::|\.)?\d{0,2})|sekarang|now|besok)",
        tokens,
        flags=re.IGNORECASE
    )
    if w_match:
        split_idx = w_match.start()
        map_name = tokens[:split_idx].strip(" ,.-")
        waktu_text = tokens[split_idx:].strip()
    else:
        map_name = tokens.strip(" ,.-")
        waktu_text = "sekarang"

    ref = now_wib()
    remind_at, when_str = parse_natural_time(waktu_text, ref)

    embed = discord.Embed(
        title="🎮 Konfirmasi Mabar",
        description=(
            f"Game / Map: **{map_name.title()}**\n"
            f"Waktu: **{when_str}**\n\n"
            f"Kirim pengumuman ke <#{CHANNEL_ID_MABAR}>?"
        ),
        color=discord.Color.blurple()
    )
    msg = await ctx.send(embed=embed)
    for em in ("✅", "❌"):
        try:
            await msg.add_reaction(em)
        except Exception:
            pass

    def check(reaction, user):
        return (
            user == ctx.author
            and str(reaction.emoji) in ["✅", "❌"]
            and reaction.message.id == msg.id
        )

    try:
        reaction, _ = await bot.wait_for("reaction_add", timeout=60.0, check=check)
    except asyncio.TimeoutError:
        try:
            await msg.delete()
        except Exception:
            pass
        return await ctx.send("⏰ Waktu konfirmasi habis, mabar dibatalkan.", delete_after=5)

    if str(reaction.emoji) == "❌":
        try:
            await msg.delete()
        except Exception:
            pass
        return await ctx.send("❌ Mabar dibatalkan.", delete_after=5)

    try:
        await msg.delete()
    except Exception:
        pass

    mabar_channel = bot.get_channel(CHANNEL_ID_MABAR)
    if not isinstance(mabar_channel, discord.TextChannel):
        return await ctx.send("❌ Channel mabar tidak ditemukan.")

    announce_text = (
        f"{role_light.mention}\n"
        f"🎮 Kalau nggak sibuk **{when_str}**, join mabar **{map_name.title()}**, yuk!"
    )
    announce_msg = await mabar_channel.send(announce_text)

    await ctx.send(f"✅ Pengumuman mabar dikirim ke <#{CHANNEL_ID_MABAR}>", delete_after=5)

    # Simpan jadwal & jadwalkan task
    doc_id = f"{ctx.guild.id}-{announce_msg.id}"
    data = {
        "status": "scheduled",
        "guild_id": ctx.guild.id,
        "channel_id": CHANNEL_ID_MABAR,
        "role_id": ROLE_ID_LIGHT,
        "map_name": map_name,
        "announce_message_id": announce_msg.id,
        "created_by_id": ctx.author.id,
        "created_at": firestore.SERVER_TIMESTAMP,
        "remind_at_epoch": to_epoch(remind_at),
        "remind_at_wib": remind_at.strftime("%Y-%m-%d %H:%M:%S WIB"),
    }
    save_mabar_schedule(doc_id, data)
    await schedule_mabar_tasks_from_doc(doc_id, data)

# =========================
# ANNOUNCEMENT
# =========================
@bot.command()
@commands.has_permissions(manage_messages=True)
async def announce(ctx, *, text=None):
    if ctx.channel.id != CHANNEL_ID_LOGS:
        return await ctx.send("⚠️ Gunakan perintah ini di channel moderator/log.")
    if not text and not ctx.message.attachments:
        return await ctx.send("Kirim teks atau gambar untuk pengumuman.")

    spotlight = bot.get_channel(CHANNEL_ID_SPOTLIGHT)
    if not isinstance(spotlight, discord.TextChannel):
        return await ctx.send("⚠️ Channel spotlight tidak ditemukan.")

    files = []
    for att in ctx.message.attachments:
        try:
            files.append(await att.to_file())
        except Exception:
            pass

    sent = await spotlight.send(content=text or "", files=files)
    await ctx.send(f"✅ Pengumuman dikirim ke {spotlight.mention}.", delete_after=10)

    await fs_add(ANNOUNCE_COL, {
        "author_id": ctx.author.id,
        "message_id": sent.id if sent else None,
        "text": text or "",
        "file_count": len(files),
        "timestamp": now_wib().isoformat()
    })

# =========================
# DOWNLOADER (anti-duplikasi & !dw)
# =========================
def downloader_embed():
    e = discord.Embed(
        title="🎥 Media Downloader",
        description=(
            "Untuk menjaga privasi, **jangan kirim tautan langsung di sini.**\n"
            "Ketik **`!dw`** untuk memulai — bot akan membuat **thread pribadi** khusus untukmu 🤫\n"
            "*(hanya kamu dan bot yang dapat melihat percakapan tersebut)*.\n\n"
            "📦 **Maksimum ukuran media:** 25 MB — lebih dari itu, bot mengirimkan tautan unduhan.\n\n"
            "🟢 *Fitur ini aktif untuk member dengan role 🔆 Light.*"
        ),
        color=discord.Color.green()
    )
    e.set_footer(text="Powered by Ryzen Api")
    return e

async def ensure_downloader_guide_exists() -> int:
    """
    Pastikan pesan panduan ada di channel downloaderr.
    Mengembalikan message_id panduan yang aktif.
    """
    ch = bot.get_channel(CHANNEL_ID_DOWNLOADER)
    if not isinstance(ch, discord.TextChannel):
        return 0

    cfg = fs_get_downloader_config()
    msg_id = cfg.get("info_msg")

    # Jika ada ID, cek masih eksis
    if msg_id:
        try:
            msg = await ch.fetch_message(int(msg_id))
            if msg:
                return msg.id
        except Exception:
            pass  # pesan mungkin sudah dihapus

    # Buat baru
    guide = await ch.send(embed=downloader_embed())
    try:
        await guide.pin()
    except Exception:
        pass
    fs_set_downloader_config(cfg.get("status", "off"), guide.id)
    return guide.id

@bot.command()
@commands.has_permissions(administrator=True)
async def downloader(ctx, action: str):
    if ctx.channel.id != CHANNEL_ID_LOGS:
        return await ctx.send("⚠️ Jalankan di channel moderator/log.")
    if action.lower() not in {"on", "off"}:
        return await ctx.send("Gunakan: `!downloader on` atau `!downloader off`.")

    cfg = fs_get_downloader_config()

    if action.lower() == "on":
        # pastikan panduan ada, tapi jangan dobel
        msg_id = await ensure_downloader_guide_exists()
        fs_set_downloader_config("on", msg_id)
        await ctx.send("✅ Downloader **aktif**. Panduan tersedia di channel downloader.")
    else:
        fs_set_downloader_config("off", cfg.get("info_msg"))
        await ctx.send("🛑 Downloader **dimatikan**. (Panduan dibiarkan, tidak dikirim ulang).")

# ---- HTTP helpers (hindari 403) ----
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

async def http_get_json(url: str) -> dict | None:
    """GET JSON dengan header default; tangani 403."""
    try:
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout, headers=DEFAULT_HEADERS) as s:
            async with s.get(url) as r:
                if r.status == 403:
                    # Coba ulang sekali dengan header tambahan 'Referer'
                    async with s.get(url, headers={**DEFAULT_HEADERS, "Referer": "https://discord.com/"}) as r2:
                        if r2.status != 200:
                            return None
                        return await r2.json()
                if r.status != 200:
                    return None
                return await r.json()
    except Exception:
        return None

async def fetch_media_bytes(url: str, max_mb: float = 25.0) -> Tuple[Optional[bytes], float, Optional[str]]:
    """Download media dengan header yang mirip browser. Balik (data, sizeMB, ext)."""
    limit_bytes = int(max_mb * 1024 * 1024)
    try:
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout, headers=DEFAULT_HEADERS) as s:
            async with s.get(url, headers={**DEFAULT_HEADERS, "Referer": "https://discord.com/"}) as r:
                if r.status == 403:
                    # beberapa CDN butuh referer/ua khusus; sudah ditambah, kalau tetap 403 -> link-only
                    return None, 0.0, None
                if r.status != 200:
                    return None, 0.0, None

                cl = r.headers.get("Content-Length")
                ct = (r.headers.get("Content-Type") or "").lower()
                ext = None
                if "video" in ct:
                    ext = ".mp4"
                elif "image" in ct:
                    if "jpeg" in ct or "jpg" in ct: ext = ".jpg"
                    elif "png" in ct: ext = ".png"
                    elif "webp" in ct: ext = ".webp"
                    else: ext = ".jpg"

                if cl and cl.isdigit() and int(cl) > limit_bytes:
                    size_mb = int(cl) / 1024 / 1024
                    return None, size_mb, ext

                buf = io.BytesIO()
                async for chunk in r.content.iter_chunked(1 << 14):
                    buf.write(chunk)
                    if buf.tell() > limit_bytes:
                        size_mb = buf.tell()/1024/1024
                        return None, size_mb, ext
                data = buf.getvalue()
                size_mb = len(data)/1024/1024
                return data, size_mb, ext
    except Exception:
        return None, 0.0, None

# ---- Downloader Thread Logic ----
async def process_media_download_in_thread(author: discord.Member, link: str, thread: discord.Thread):
    platform = "instagram" if "instagram.com" in link else "tiktok"
    api_url = f"https://api.ryzumi.vip/api/downloader/{'igdl' if platform=='instagram' else 'ttdl'}?url={link}"

    await thread.send("⏳ Mengambil media…")

    data = await http_get_json(api_url)
    if data is None:
        await thread.send("🚫 Gagal memanggil API (403/timeout). Coba lagi beberapa menit nanti.")
        await fs_add(DL_LOGS_COL, {
            "user_id": author.id, "platform": platform, "url": link,
            "status": "api_fail", "ts": now_wib().isoformat()
        })
        return

    # Instagram (batch)
    if platform == "instagram":
        if not data.get("status"):
            await thread.send("⚠️ Gagal mengambil data Instagram.")
            await fs_add(DL_LOGS_COL, {
                "user_id": author.id, "platform": platform, "url": link,
                "status": "api_fail", "ts": now_wib().isoformat()
            })
            return

        media_list = data.get("data", [])
        total = len(media_list)
        sent_ok = 0
        for idx, media in enumerate(media_list, start=1):
            media_url = media.get("url")
            if not media_url:
                await thread.send(f"⚠️ Media {idx}/{total} tidak memiliki URL.")
                await fs_add(DL_LOGS_COL, {
                    "user_id": author.id, "platform": platform, "url": link,
                    "status": "no_url", "batch_index": idx, "batch_total": total, "ts": now_wib().isoformat()
                })
                continue

            data_bytes, size_mb, ext = await fetch_media_bytes(media_url, 25.0)
            if data_bytes is None:
                # bisa karena >25MB atau 403 CDN
                if size_mb > 0:
                    await thread.send(f"⚠️ Media {idx}/{total} terlalu besar ({size_mb:.1f} MB). [Lihat di sini]({media_url})")
                else:
                    await thread.send(f"⚠️ Media {idx}/{total} tidak bisa diunduh langsung. [Buka tautan]({media_url})")
                await fs_add(DL_LOGS_COL, {
                    "user_id": author.id, "platform": platform, "url": link,
                    "file_size_mb": float(size_mb), "status": "oversize_or_403",
                    "batch_index": idx, "batch_total": total, "ts": now_wib().isoformat()
                })
            else:
                filename = f"ig_{idx}{ext or '.mp4'}"
                await thread.send(file=discord.File(io.BytesIO(data_bytes), filename=filename))
                await fs_add(DL_LOGS_COL, {
                    "user_id": author.id, "platform": platform, "url": link,
                    "file_size_mb": float(size_mb), "status": "success",
                    "batch_index": idx, "batch_total": total, "ts": now_wib().isoformat()
                })
                sent_ok += 1
            await asyncio.sleep(1.2)
        await thread.send(f"✅ IG selesai: {sent_ok}/{total} terkirim.")

    # TikTok (single)
    else:
        if not data.get("success"):
            await thread.send("⚠️ Gagal mengambil data TikTok.")
            await fs_add(DL_LOGS_COL, {
                "user_id": author.id, "platform": platform, "url": link,
                "status": "api_fail", "ts": now_wib().isoformat()
            })
            return
        try:
            media_url = data["data"]["data"]["play"]
        except Exception:
            media_url = None

        if not media_url:
            await thread.send("⚠️ Respons API tidak berisi URL video.")
            await fs_add(DL_LOGS_COL, {
                "user_id": author.id, "platform": platform, "url": link,
                "status": "no_url", "ts": now_wib().isoformat()
            })
            return

        data_bytes, size_mb, ext = await fetch_media_bytes(media_url, 25.0)
        if data_bytes is None:
            if size_mb > 0:
                await thread.send(f"⚠️ File terlalu besar ({size_mb:.1f} MB). [Lihat di sini]({media_url})")
            else:
                await thread.send(f"⚠️ CDN menolak unduhan langsung (403). [Buka tautan]({media_url})")
            await fs_add(DL_LOGS_COL, {
                "user_id": author.id, "platform": platform, "url": link,
                "file_size_mb": float(size_mb), "status": "oversize_or_403",
                "ts": now_wib().isoformat()
            })
        else:
            await thread.send(file=discord.File(io.BytesIO(data_bytes), filename=f"tiktok{ext or '.mp4'}"))
            await thread.send("✅ Video TikTok selesai dikirim.")
            await fs_add(DL_LOGS_COL, {
                "user_id": author.id, "platform": platform, "url": link,
                "file_size_mb": float(size_mb), "status": "success",
                "ts": now_wib().isoformat()
            })

async def create_private_download_thread(message: discord.Message, initial_link: Optional[str] = None):
    thread = await message.channel.create_thread(
        name=f"📥 Downloader – {message.author.display_name}",
        type=discord.ChannelType.private_thread,
        invitable=False
    )
    try:
        await thread.add_user(message.author)
    except Exception:
        pass

    await thread.send(
        f"Halo {message.author.mention}!\n"
        f"Kirimkan tautan **Instagram** atau **TikTok** di sini ya.\n"
        f"📦 Maksimum 25MB — lebih dari itu akan dikirim sebagai tautan."
    )

    if initial_link and re.search(r"(instagram\.com|tiktok\.com)", initial_link):
        await process_media_download_in_thread(author=message.author, link=initial_link, thread=thread)

    while True:
        menu = await thread.send("Pilih aksi: 🔁 unduh lagi • ✅ tutup thread.")
        for em in ("🔁", "✅"):
            try: await menu.add_reaction(em)
            except Exception: pass

        def react_check(r: discord.Reaction, u: discord.User):
            return u.id == message.author.id and r.message.id == menu.id and str(r.emoji) in ["🔁", "✅"]

        try:
            reaction, _ = await bot.wait_for("reaction_add", timeout=90, check=react_check)
        except asyncio.TimeoutError:
            await thread.send("⏰ Tidak ada respon, thread ditutup dalam 10 detik.")
            await asyncio.sleep(10)
            try: await thread.delete()
            except Exception: pass
            return

        if str(reaction.emoji) == "✅":
            await thread.send("👋 Baik! Thread akan ditutup dalam 10 detik…")
            await asyncio.sleep(10)
            try: await thread.delete()
            except Exception: pass
            return
        else:
            await thread.send("Kirim tautan IG/TT berikutnya di bawah ini…")
            def link_check(m: discord.Message):
                return m.channel.id == thread.id and m.author.id == message.author.id and re.search(r"(instagram\.com|tiktok\.com)", m.content)
            try:
                msg = await bot.wait_for("message", timeout=120, check=link_check)
                await process_media_download_in_thread(author=message.author, link=msg.content.strip(), thread=thread)
            except asyncio.TimeoutError:
                await thread.send("⏰ Waktu habis menunggu tautan. Thread ditutup.")
                await asyncio.sleep(10)
                try: await thread.delete()
                except Exception: pass
                return

# Command untuk memulai downloader (privasi)
@bot.command(name="dw")
async def start_downloader(ctx: commands.Context):
    """Mulai downloader (buat private thread)."""
    if ctx.channel.id != CHANNEL_ID_DOWNLOADER:
        return await ctx.send(f"Fitur ini ada di <#{CHANNEL_ID_DOWNLOADER}> ya.")
    cfg = fs_get_downloader_config()
    if cfg.get("status") != "on":
        return await ctx.send("⚠️ Downloader sedang nonaktif oleh moderator.", delete_after=10)

    role_light = discord.utils.get(ctx.guild.roles, id=ROLE_ID_LIGHT)
    if role_light not in ctx.author.roles:
        return await ctx.send(f"⚠️ Fitur ini hanya untuk member dengan role {role_light.mention}.", delete_after=10)

    await create_private_download_thread(ctx.message)

# =========================
# SINGLE on_message HANDLER
# =========================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content_lower = message.content.lower()

    # 1) Deteksi !mabar / !main manual
    match_cmd = re.search(r'!(mabar|main)\s+(.+)', content_lower)
    if match_cmd:
        ctx = await bot.get_context(message)
        arg = match_cmd.group(2).strip()
        await mabar(ctx, arg=arg)
        return

    # 2) Deteksi gambar + konfirmasi forward (global)
    try:
        await _confirm_and_forward_images(message)
    except Exception as e:
        print("[WARN] Handler forward images error:", e)

    # 3) Redirect link IG/TT di channel publik
    if message.channel.id == CHANNEL_ID_PUBLIC_CHAT:
        if re.search(r"(instagram\.com|tiktok\.com)", message.content):
            reply = await message.reply(
                f"Hola {message.author.mention}, mau download medianya? Yuk ke <#{CHANNEL_ID_DOWNLOADER}> 🎥",
                delete_after=300
            )
            await asyncio.sleep(300)
            try: await reply.delete()
            except Exception: pass

    # 4) Perilaku di channel downloader
    if message.channel.id == CHANNEL_ID_DOWNLOADER:
        cfg = fs_get_downloader_config()
        if cfg.get("status") == "on":
            # Kalau user kirim link langsung, arahkan pakai !dw
            if re.search(r"(instagram\.com|tiktok\.com)", message.content):
                await message.reply("Demi privasi, ketik **`!dw`** dulu ya — linknya kirim di dalam thread pribadimu. 🙏", delete_after=15)
        else:
            # Saat OFF, pastikan panduan tidak dikirim ulang (cukup diam)
            pass

    # 5) Proses commands lain (!announce, !downloader, !dw, !ping, ...)
    await bot.process_commands(message)

# =========================
# RUN
# =========================
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        print("❌ Token invalid. Pastikan DISCORD_BOT_TOKEN benar.")
    except Exception as e:
        print(f"[FATAL] Error menjalankan bot: {e}")
