# main_bot.py
import os
import re
import json
import io
import math
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, List, Tuple

import discord
from discord.ext import commands
from discord import app_commands

import aiohttp
import requests

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
CHANNEL_ID_PHOTO_MEDIA   = 1424033929874247802  # forward foto
CHANNEL_ID_DOWNLOADER    = 1425023771185774612  # channel downloader target
CHANNEL_ID_LINK_DETECT   = 1424032583519567952  # channel lain: jika ada link IG/TT → arahkan ke downloader
REACTION_EMOJI           = "🔆"

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
# UTIL TIME
# =========================
def now_wib() -> datetime:
    return datetime.now(TZ)

def to_epoch(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(timezone.utc).timestamp()

def from_epoch_to_wib(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=TZ)

# =========================
# PARSER WAKTU (WIB)
# =========================
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
WELCOME_COL   = "welcome_messages"
MABAR_COL     = "mabar_reminders"
DL_SETTINGS   = "downloader_settings"  # per guild: { enabled: bool }
DL_NOTICE     = "downloader_notice"    # per guild: { message_id }

async def save_welcome_message(user_id: int, message_id: int):
    try:
        db.collection(WELCOME_COL).document(str(user_id)).set({
            "message_id": message_id,
            "created_at": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        print("[WARN] Gagal simpan welcome_messages:", e)

async def get_welcome_message(user_id: int) -> int | None:
    try:
        doc = db.collection(WELCOME_COL).document(str(user_id)).get()
        if doc.exists:
            return int(doc.to_dict().get("message_id"))
    except Exception as e:
        print("[WARN] Gagal ambil welcome_messages:", e)
    return None

async def delete_welcome_message(user_id: int):
    try:
        db.collection(WELCOME_COL).document(str(user_id)).delete()
    except Exception as e:
        print("[WARN] Gagal hapus welcome_messages:", e)

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

def get_downloader_enabled(guild_id: int) -> bool:
    try:
        doc = db.collection(DL_SETTINGS).document(str(guild_id)).get()
        if doc.exists:
            return bool(doc.to_dict().get("enabled", True))
    except Exception as e:
        print("[WARN] get_downloader_enabled:", e)
    return True

def set_downloader_enabled(guild_id: int, enabled: bool):
    try:
        db.collection(DL_SETTINGS).document(str(guild_id)).set(
            {"enabled": enabled, "updated_at": firestore.SERVER_TIMESTAMP},
            merge=True
        )
    except Exception as e:
        print("[WARN] set_downloader_enabled:", e)

def get_downloader_notice(guild_id: int) -> Optional[int]:
    try:
        doc = db.collection(DL_NOTICE).document(str(guild_id)).get()
        if doc.exists:
            return int(doc.to_dict().get("message_id", 0)) or None
    except Exception as e:
        print("[WARN] get_downloader_notice:", e)
    return None

def set_downloader_notice(guild_id: int, message_id: int):
    try:
        db.collection(DL_NOTICE).document(str(guild_id)).set(
            {"message_id": message_id, "created_at": firestore.SERVER_TIMESTAMP}
        )
    except Exception as e:
        print("[WARN] set_downloader_notice:", e)

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

    # Resume mabar reminders
    pending = load_pending_mabar(to_epoch(now_wib()))
    if pending:
        print(f"⏲️ Menjadwalkan ulang {len(pending)} reminder mabar dari Firestore.")
    for doc_id, dat in pending:
        asyncio.create_task(schedule_mabar_tasks_from_doc(doc_id, dat))

    # Pastikan pemberitahuan downloader (anti-duplikat)
    await ensure_downloader_notice()

async def ensure_downloader_notice():
    ch = bot.get_channel(CHANNEL_ID_DOWNLOADER)
    if not isinstance(ch, discord.TextChannel):
        return
    gid = ch.guild.id if ch.guild else 0
    exists_msg_id = get_downloader_notice(gid)
    if exists_msg_id:
        # Coba fetch—kalau sudah terhapus, kirim lagi
        try:
            _ = await ch.fetch_message(exists_msg_id)
            return
        except Exception:
            pass

    enabled = get_downloader_enabled(gid)
    status_bullet = "🟢" if enabled else "🔴"
    embed = discord.Embed(
        title="Downloader Center",
        description=(
            "Cukup kirimkan **tautan postingan** di sini — "
            "bot akan **otomatis membuat thread pribadi** khusus untukmu 🤫\n"
            "*(Hanya kamu dan bot yang dapat melihat percakapan tersebut.)*\n\n"
            "📦 **Maksimum ukuran media:** 25 MB\n"
            "Lebih dari itu, bot akan mengirimkan **tautan unduhan**.\n\n"
            f"| {status_bullet} Fitur ini aktif untuk member dengan role 🔆 Light."
        ),
        color=discord.Color.blurple()
    )
    msg = await ch.send(embed=embed)
    set_downloader_notice(gid, msg.id)

# =========================
# GREETINGS (WELCOME + EMOJI)
# =========================
@bot.event
async def on_member_join(member: discord.Member):
    ch = bot.get_channel(CHANNEL_ID_WELCOME)
    if not isinstance(ch, discord.TextChannel):
        return

    rules_ch = member.guild.get_channel(RULES_CHANNEL_ID) if member.guild else None
    rules_text = rules_ch.mention if isinstance(rules_ch, discord.TextChannel) else "#rules"

    role_light = member.guild.get_role(ROLE_ID_LIGHT) if member.guild else None
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
    if not isinstance(ch, discord.TextChannel):
        return
    embed = discord.Embed(
        title="👋 Selamat Tinggal",
        description=f"{member.display_name} telah keluar dari server.",
        color=discord.Color.red()
    )
    await ch.send(embed=embed)

# =========================
# REACTION ROLE (WELCOME MSG)
# =========================
async def _safe_get_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    m = guild.get_member(user_id)
    if m is None:
        try:
            m = await guild.fetch_member(user_id)
        except Exception:
            m = None
    return m

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

    channel = bot.get_channel(CHANNEL_ID_WELCOME)

    try:
        if role not in member.roles:
            await member.add_roles(role, reason="Welcome reaction role: Light")
            intro_channel = guild.get_channel(CHANNEL_ID_INTRO)
            if isinstance(intro_channel, discord.TextChannel):
                await intro_channel.send(
                    f"Ekhem… {member.mention}! Sebutin umur kamu aja boleh kok. "
                    f"Kalau mau cerita lebih, juga boleh, ngga perlu terlalu detail, ya!"
                )
        else:
            await member.remove_roles(role, reason="Welcome reaction role: Light (remove)")
    except Exception as e:
        print("[ERROR] Gagal toggle role:", e)
        return

    try:
        if isinstance(channel, discord.TextChannel):
            msg = await channel.fetch_message(target_msg_id)
            have_role = role in member.roles
            status = "✅ Role Light diberikan." if have_role else "❎ Role Light dilepas."
            new_embed = msg.embeds[0] if msg.embeds else discord.Embed(color=discord.Color.green())
            new_embed.set_footer(text=status + " (pesan akan dihapus sebentar lagi)")
            await msg.edit(embed=new_embed)
            await asyncio.sleep(8)
            await msg.delete()
    except Exception:
        pass
    finally:
        await delete_welcome_message(member.id)

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
# DETEKSI GAMBAR & FORWARD DGN KONFIRMASI
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
    """Konfirmasi  ✅/❌ lalu forward gambar; auto-hapus prompt 30 detik."""
    if not message.guild or not message.attachments:
        return

    images = [att for att in message.attachments if _is_image_attachment(att)]
    if not images:
        return

    prompt = await message.channel.send(
        f"hola {message.author.mention}, apakah kamu ingin foto nya aku forward ke **Channel Photo-Media**?"
    )
    # Auto-hapus prompt dalam 30 detik bila tidak ada aksi
    async def prompt_timeout_cleanup():
        await asyncio.sleep(30)
        try:
            await prompt.delete()
        except Exception:
            pass
    timeout_task = asyncio.create_task(prompt_timeout_cleanup())

    try:
        await prompt.add_reaction("✅")
        await prompt.add_reaction("❌")
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
        # timeout -> prompt akan dihapus oleh timeout_task
        await message.channel.send("⏰ Konfirmasi habis. Forward dibatalkan.", delete_after=6)
        return

    if decided:
        try:
            timeout_task.cancel()
        except Exception:
            pass
        try:
            await prompt.delete()
        except Exception:
            pass

    if str(reaction.emoji) == "❌":
        await message.channel.send("❌ Oke, tidak di-forward.", delete_after=5)
        return

    # ✅ Forward
    try:
        dest = bot.get_channel(CHANNEL_ID_PHOTO_MEDIA)
        if not isinstance(dest, discord.TextChannel):
            await message.channel.send("⚠️ Channel Photo-Media tidak ditemukan.", delete_after=6)
            return

        caption = message.clean_content.strip()
        prefix = f"media dari {message.author.mention}"
        content = f"{prefix}\n{caption}" if caption else prefix

        files = []
        for att in images[:10]:
            try:
                files.append(await att.to_file())
            except Exception as e:
                print("[WARN] Gagal mengambil attachment:", e)

        sent = None
        if files:
            sent = await dest.send(content=content, files=files)
        else:
            sent = await dest.send(content)

        jump = _jump_url(message.guild.id, dest.id, sent.id) if sent else ""
        await message.channel.send(
            f"Ekhem.. media {message.author.mention} udah aku forward ke "
            f"[Media Photo]({jump}), cuss lihat~",
            suppress_embeds=True,
            delete_after=10
        )

    except Exception as e:
        print("[ERROR] Gagal forward foto:", e)
        await message.channel.send("⚠️ Terjadi kendala saat forward media.", delete_after=6)

# =========================
# DOWNLOADER (IG/TT) — HELPERS
# =========================
IG_RE = re.compile(r"(https?://(www\.)?instagram\.com/[^ \n]+)", re.IGNORECASE)
TT_RE = re.compile(r"(https?://(www\.)?(vm\.|vt\.)?tiktok\.com/[^ \n]+)", re.IGNORECASE)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25MB

def _platform_from_text(text: str) -> Optional[str]:
    if IG_RE.search(text):
        return "ig"
    if TT_RE.search(text):
        return "tt"
    return None

def _extract_first_url(text: str) -> Optional[str]:
    m = IG_RE.search(text)
    if m:
        return m.group(1)
    m = TT_RE.search(text)
    if m:
        return m.group(1)
    return None

def _api_url(platform: str, post_url: str) -> str:
    # Pastikan pakai ?url= (bukan kolon)
    base = "https://api.ryzumi.vip/api/downloader/igdl" if platform == "ig" \
        else "https://api.ryzumi.vip/api/downloader/ttdl"
    from urllib.parse import urlencode
    qs = urlencode({"url": post_url})
    return f"{base}?{qs}"

def _platform_headers(platform: str) -> dict:
    # Header mirip browser + referer domain platform
    referer = "https://www.instagram.com/" if platform == "ig" else "https://www.tiktok.com/"
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/127.0.0.1 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
        "Connection": "keep-alive",
    }

def _pick_tt_media(payload: dict) -> Optional[str]:
    """Ambil hdplay → play → wmplay dari berbagai bentuk nested."""
    if not isinstance(payload, dict):
        return None
    candidate = payload
    if "data" in candidate and isinstance(candidate["data"], dict):
        inner = candidate["data"]
        if "data" in inner and isinstance(inner["data"], dict):
            candidate = inner["data"]
        else:
            candidate = inner
    for key in ("hdplay", "play", "wmplay"):
        url = candidate.get(key)
        if isinstance(url, str) and url.startswith("http"):
            return url
    music = payload.get("music") or {}
    play_url = music.get("play_url")
    if isinstance(play_url, dict):
        lst = play_url.get("url_list")
        if isinstance(lst, list) and lst and isinstance(lst[0], str):
            return lst[0]
    return None

async def fetch_api_fresh(session: aiohttp.ClientSession, platform: str, post_url: str) -> List[str]:
    api = _api_url(platform, post_url)
    headers = _platform_headers(platform)
    for attempt in range(3):
        try:
            async with session.get(api, headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as r:
                if r.status >= 500:
                    await asyncio.sleep(1.2 * (attempt + 1))
                    continue
                if r.status != 200:
                    raise RuntimeError(f"API {platform} status {r.status}")
                data = await r.json(content_type=None)

                urls: List[str] = []
                if platform == "ig":
                    arr = data.get("data") or []
                    if isinstance(arr, list):
                        for it in arr:
                            if not isinstance(it, dict):
                                continue
                            u = (it.get("url") or "").strip()
                            if not u:
                                thumb = (it.get("thumbnail") or "").strip()
                                if thumb.startswith("http"):
                                    u = thumb
                            if u.startswith("http"):
                                urls.append(u)
                    if not urls:
                        print("[IG] Parser tidak menemukan url. keys:", list(data.keys()))
                else:
                    media = _pick_tt_media(data)
                    if media and media.startswith("http"):
                        urls.append(media)
                    if not urls:
                        print("[TT] Parser tidak menemukan media. keys:", list(data.keys()))
                        if isinstance(data.get("data"), dict):
                            print("[TT] data.keys():", list(data["data"].keys()))
                return urls
        except Exception as e:
            if attempt == 2:
                print("[API] gagal fetch:", e)
                return []
            await asyncio.sleep(1.0 * (attempt + 1))
    return []

def fetch_api_requests(platform: str, post_url: str) -> List[str]:
    api = _api_url(platform, post_url)
    headers = _platform_headers(platform)
    try:
        resp = requests.get(api, headers=headers, timeout=25)
        if resp.status_code != 200:
            print(f"[fallback/{platform}] status:", resp.status_code, resp.text[:200])
            return []
        data = resp.json()
        urls: List[str] = []
        if platform == "ig":
            arr = data.get("data") or []
            if isinstance(arr, list):
                for it in arr:
                    if not isinstance(it, dict):
                        continue
                    u = (it.get("url") or "").strip()
                    if not u:
                        thumb = (it.get("thumbnail") or "").strip()
                        if thumb.startswith("http"):
                            u = thumb
                    if u.startswith("http"):
                        urls.append(u)
        else:
            media = _pick_tt_media(data)
            if media and media.startswith("http"):
                urls.append(media)
        return urls
    except Exception as e:
        print(f"[fallback/{platform}] exception:", e)
        return []

async def download_to_bytes(session: aiohttp.ClientSession, url: str, max_bytes: int) -> Tuple[Optional[bytes], bool]:
    """Return (content, is_link_fallback). is_link_fallback=True jika > max_bytes atau gagal."""
    headers = _platform_headers("ig" if "instagram" in url else "tt")
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                print("[download] status:", r.status)
                return None, True
            total = 0
            buff = io.BytesIO()
            async for chunk in r.content.iter_chunked(1024 * 64):
                total += len(chunk)
                if total > max_bytes:
                    return None, True
                buff.write(chunk)
            return buff.getvalue(), False
    except asyncio.TimeoutError:
        print("[download] timeout")
        return None, True
    except Exception as e:
        print("[download] exception:", e)
        return None, True

class DlActionView(discord.ui.View):
    def __init__(self, thread: discord.Thread, author_id: int):
        super().__init__(timeout=300)
        self.thread = thread
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Ini tombol untuk pembuat thread saja.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Download lagi", style=discord.ButtonStyle.primary)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Silakan kirim tautan Instagram/TikTok pada thread ini. Aku akan memprosesnya 🤝",
            ephemeral=True
        )

    @discord.ui.button(label="Tutup thread", style=discord.ButtonStyle.secondary)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.thread.edit(archived=True, locked=True)
            await interaction.response.send_message("Thread ditutup ✅", ephemeral=True)
        except Exception:
            await interaction.response.send_message("Gagal menutup thread.", ephemeral=True)

async def ensure_private_thread(channel: discord.TextChannel, user: discord.Member) -> discord.Thread:
    name = f"DL-{user.display_name}".strip()[:80]
    th = await channel.create_thread(
        name=name,
        type=discord.ChannelType.private_thread,
        invitable=False
    )
    try:
        await th.add_user(user)
    except Exception:
        pass
    return th

async def process_download_in_thread(thread: discord.Thread, author: discord.Member, link: str):
    platform = _platform_from_text(link)
    if not platform:
        await thread.send("Maaf, aku hanya mendukung tautan Instagram/TikTok untuk saat ini.")
        return

    await thread.send("⏳ Tunggu sebentar, aku ambil medianya…")

    urls: List[str] = []
    # fetch daftar media url
    async with aiohttp.ClientSession() as session:
        urls = await fetch_api_fresh(session, platform, link)
        if not urls:
            # fallback sync (kadang lebih 'pas' ke gateway tertentu)
            urls = fetch_api_requests(platform, link)

    if not urls:
        await thread.send("❌ Tidak menemukan media pada tautan tersebut. Coba tautan lain ya.")
        return

    # Ambil 1 saja (IG bisa banyak). Bisa di-improve jadi semua, tapi jaga rate.
    media_url = urls[0]

    # Download atau kirim link jika >25MB
    async with aiohttp.ClientSession() as session:
        content, fallback = await download_to_bytes(session, media_url, MAX_UPLOAD_BYTES)

    if fallback or not content:
        # kirim link saja
        await thread.send(
            f"⚠️ Ukuran file besar / unduh langsung gagal. Ini tautannya:\n{media_url}",
            view=DlActionView(thread, author.id)
        )
        return

    # determine filename
    ext = ".mp4" if "tiktok" in media_url or "video" in media_url else ".mp4"
    filename = f"media_{author.id}{ext}"

    file = discord.File(io.BytesIO(content), filename=filename)
    await thread.send(
        content=f"Media untuk {author.mention}",
        file=file,
        view=DlActionView(thread, author.id)
    )

# =========================
# COMMAND ROUTING + HOOKS
# =========================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # 0) Deteksi link IG/TT di CHANNEL_ID_LINK_DETECT → arahkan ke downloader (hapus 5 menit)
    if message.channel.id == CHANNEL_ID_LINK_DETECT:
        if _platform_from_text(message.content):
            ch = bot.get_channel(CHANNEL_ID_DOWNLOADER)
            if isinstance(ch, discord.TextChannel):
                tip = await message.reply(
                    f"hola {message.author.mention}, mau download medianya? ke {ch.mention} yuk!",
                    mention_author=True
                )
                try:
                    await tip.delete(delay=300)
                except Exception:
                    pass

    # 1) Deteksi !mabar / !main manual
    content_low = message.content.lower()
    match_cmd = re.search(r'!(mabar|main)\s+(.+)', content_low)
    if match_cmd:
        ctx = await bot.get_context(message)
        arg = match_cmd.group(2).strip()
        await mabar(ctx, arg=arg)
        return

    # 2) Deteksi gambar + konfirmasi forward
    try:
        await _confirm_and_forward_images(message)
    except Exception as e:
        print("[WARN] Handler forward images error:", e)

    # 3) Downloader UX:
    # - Di channel downloader: jika user kirim link langsung, ingatkan untuk pakai !dw (privasi)
    # - Pesan reminder ini dihapus setelah 30 detik
    if message.channel.id == CHANNEL_ID_DOWNLOADER and not isinstance(message.channel, discord.Thread):
        if _platform_from_text(message.content):
            warn = await message.reply(
                "Demi privasi, jalankan perintah `!dw` dulu ya. Nanti aku bikinkan **thread privat** khusus buat kamu.",
                mention_author=True
            )
            try:
                await warn.delete(delay=30)
            except Exception:
                pass

    # 4) Proses commands biasa
    await bot.process_commands(message)

# =========================
# COMMANDS UMUM
# =========================
@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send(f"Pong! {round(bot.latency * 1000)}ms")

# ---------- MABAR ----------
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
        if delay > 60:
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

# ---------- DOWNLOADER COMMANDS ----------
@bot.command(name="dl_on")
@commands.has_permissions(administrator=True)
async def dl_on(ctx: commands.Context):
    set_downloader_enabled(ctx.guild.id, True)
    await ctx.send("✅ Downloader di-aktifkan.")
    await ensure_downloader_notice()

@bot.command(name="dl_off")
@commands.has_permissions(administrator=True)
async def dl_off(ctx: commands.Context):
    set_downloader_enabled(ctx.guild.id, False)
    await ctx.send("⛔ Downloader di-nonaktifkan.")
    await ensure_downloader_notice()

@bot.command(name="dw")
async def dw(ctx: commands.Context):
    """Mulai sesi download (buat thread privat). Pesan perintah dihapus setelah 30 detik."""
    if ctx.channel.id != CHANNEL_ID_DOWNLOADER:
        return await ctx.send(f"Fitur ini hanya di <#{CHANNEL_ID_DOWNLOADER}> ya.", delete_after=7)

    role_light = ctx.guild.get_role(ROLE_ID_LIGHT)
    if not role_light or role_light not in ctx.author.roles:
        return await ctx.send("❌ Hanya member dengan role 🔆 Light yang bisa memakai fitur ini.", delete_after=7)

    if not get_downloader_enabled(ctx.guild.id):
        return await ctx.send("⛔ Fitur downloader sedang non-aktif oleh admin.", delete_after=7)

    # Buat thread privat
    thread = await ensure_private_thread(ctx.channel, ctx.author)

    guide = (
        f"Hai {ctx.author.mention}! Kirim **tautan Instagram/TikTok** di thread ini ya.\n"
        "Aku akan mengunduh dan mengirimkan media (maks 25 MB). Jika lebih besar, akan kukirim tautan unduhnya. 👍"
    )
    await thread.send(guide, view=DlActionView(thread, ctx.author.id))

    # Hapus pesan perintah user setelah 30 detik
    try:
        await ctx.message.delete(delay=30)
    except Exception:
        pass

# Tangani link di thread privat
@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    # jika user mengedit pesan di thread dengan menambahkan link—proses juga
    await maybe_process_thread_message(after)

@bot.event
async def on_message_delete_bulk(messages):
    # no-op
    pass

@bot.event
async def on_thread_update(before: discord.Thread, after: discord.Thread):
    # no-op
    pass

@bot.event
async def on_message(message: discord.Message):
    # NOTE: fungsi ini sudah dideklarasikan di atas.
    # Python hanya menjaga yang terakhir. Agar tidak bentrok, gabungkan logika ke satu on_message (sudah dilakukan di atas).
    # (Section ini dibiarkan kosong supaya tidak override)
    return

async def maybe_process_thread_message(message: discord.Message):
    # dipanggil oleh on_message_edit — tapi kita juga panggil manual dari handler utama kalau mau.
    pass  # tidak diperlukan karena kita proses di handler di bawah

# Handler untuk pesan baru (thread privat downloader)
@bot.event
async def on_message_listener(message: discord.Message):
    # (Tidak dipakai—discord.py tidak punya on_message_listener default.)
    pass

# Gunakan on_message yang sudah ada untuk juga memeriksa thread:
_original_on_message = bot.on_message

async def _merged_on_message(message: discord.Message):
    if message.author.bot:
        return

    # panggil logika yang sudah ada
    await _original_on_message(message)

    # Jika pesan berada di thread privat di dalam channel downloader → proses jika berisi link
    if isinstance(message.channel, discord.Thread):
        parent = message.channel.parent
        if parent and parent.id == CHANNEL_ID_DOWNLOADER and not message.author.bot:
            url = _extract_first_url(message.content)
            if not url:
                return
            # cek role & enabled
            role_light = message.guild.get_role(ROLE_ID_LIGHT) if message.guild else None
            if not role_light or role_light not in message.author.roles:
                await message.channel.send("❌ Hanya member dengan role 🔆 Light yang bisa memakai fitur ini.")
                return
            if not get_downloader_enabled(message.guild.id):
                await message.channel.send("⛔ Fitur downloader sedang non-aktif oleh admin.")
                return
            await process_download_in_thread(message.channel, message.author, url)

# ganti handler on_message menjadi merged
bot.on_message = _merged_on_message

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
