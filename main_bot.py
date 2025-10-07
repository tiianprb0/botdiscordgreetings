# main_bot.py

import os
import re
import io
import json
import asyncio
from typing import Optional, Tuple, List, Dict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import discord
from discord.ext import commands
from discord.ui import View, button, Button

import aiohttp
import requests  # fallback sync → dipanggil via asyncio.to_thread agar non-blocking

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
# KONFIG DISCORD (ID SERVER)
# =========================
CHANNEL_ID_WELCOME       = 1423964756158447738
CHANNEL_ID_LOGS          = 1423969192389902339
CHANNEL_ID_MABAR         = 1424029336683679794
CHANNEL_ID_INTRO         = 1424033383339659334
RULES_CHANNEL_ID         = 1423969192389902336
ROLE_ID_LIGHT            = 1424026593143164958
CHANNEL_ID_PHOTO_MEDIA   = 1424033929874247802  # tujuan forward foto

# Downloader
CHANNEL_ID_DOWNLOADER    = 1425023771185774612  # channel "downloader"
CHANNEL_ID_CHAT_GENERAL  = 1424032583519567952  # channel umum untuk pendeteksi link

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
WELCOME_COL      = "welcome_messages"
MABAR_COL        = "mabar_reminders"
DOWNLOADER_META  = "downloader_meta"      # doc: f"guild_{guild_id}"
DOWNLOADER_LOGS  = "downloader_logs"

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

def get_downloader_doc(guild_id: int):
    return db.collection(DOWNLOADER_META).document(f"guild_{guild_id}")

def ensure_downloader_state(guild_id: int) -> dict:
    docref = get_downloader_doc(guild_id)
    doc = docref.get()
    if not doc.exists:
        state = {"enabled": True, "notice_sent": False, "updated_at": firestore.SERVER_TIMESTAMP}
        docref.set(state)
        return state
    return doc.to_dict() or {"enabled": True, "notice_sent": False}

def set_downloader_state(guild_id: int, **fields):
    try:
        get_downloader_doc(guild_id).set({**fields, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
    except Exception as e:
        print("[WARN] gagal set_downloader_state:", e)

def log_downloader_event(guild_id: int, data: dict):
    try:
        key = f"{guild_id}-{int(datetime.utcnow().timestamp()*1000)}"
        db.collection(DOWNLOADER_LOGS).document(key).set({**data, "ts": firestore.SERVER_TIMESTAMP})
    except Exception as e:
        print("[WARN] gagal log_downloader_event:", e)

# =========================
# NETWORK / DOWNLOADER HELPERS
# =========================
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.1 Safari/537.36"
)

def is_ig_url(text: str) -> bool:
    t = (text or "").lower()
    return "instagram.com" in t

def is_tt_url(text: str) -> bool:
    t = (text or "").lower()
    return "tiktok.com" in t or "vt.tiktok.com" in t

def extract_first_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r'(https?://[^\s>]+)', text)
    return m.group(1) if m else None

def _platform_headers(platform: str) -> dict:
    if platform == "ig":
        ref = "https://www.instagram.com/"
        origin = "https://www.instagram.com"
    else:
        ref = "https://www.tiktok.com/"
        origin = "https://www.tiktok.com"
    return {
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": ref,
        "Origin": origin,
        "Connection": "keep-alive",
    }

def _api_url(platform: str, post_url: str) -> str:
    q = urlencode({"url": post_url})  # => url=https%3A%2F%2F...
    if platform == "ig":
        return f"https://api.ryzumi.vip/api/downloader/igdl?{q}"
    else:
        return f"https://api.ryzumi.vip/api/downloader/ttdl?{q}"

def _pick_tt_media(u: dict) -> Optional[str]:
    data = u.get("data") or {}
    inner = data.get("data") or {}
    for key in ("hdplay", "play", "wmplay"):
        val = inner.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    # dokumentasi varian
    music = u.get("music") or {}
    play_url = music.get("play_url") or {}
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
                    for it in arr:
                        u = (it.get("url") or "").strip()
                        if not u and it.get("thumbnail"):
                            u = it["thumbnail"]
                        if u.startswith("http"):
                            urls.append(u)
                else:
                    u = _pick_tt_media(data)
                    if u and u.startswith("http"):
                        urls.append(u)
                return urls
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(1.0 * (attempt + 1))
    return []

# ---- Fallback pakai requests (sesuai snippet kamu) ----
def fetch_api_requests(platform: str, post_url: str) -> List[str]:
    api = _api_url(platform, post_url)
    headers = _platform_headers(platform)
    try:
        resp = requests.get(api, headers=headers, timeout=25)
        if resp.status_code != 200:
            return []
        data = resp.json()
        urls: List[str] = []
        if platform == "ig":
            arr = data.get("data") or []
            for it in arr:
                u = (it.get("url") or "").strip()
                if not u and it.get("thumbnail"):
                    u = it["thumbnail"]
                if u.startswith("http"):
                    urls.append(u)
        else:
            u = _pick_tt_media(data)
            if u and u.startswith("http"):
                urls.append(u)
        return urls
    except Exception:
        return []

async def fetch_api_fresh_with_fallback(platform: str, post_url: str) -> List[str]:
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=8)) as session:
        try:
            return await fetch_api_fresh(session, platform, post_url)
        except Exception:
            # panggil requests di thread pool agar non-blocking
            return await asyncio.to_thread(fetch_api_requests, platform, post_url)

async def stream_send_or_link(
    session: aiohttp.ClientSession,
    thread: discord.Thread,
    media_url: str,
    platform: str,
    max_bytes: int = 25 * 1024 * 1024,
) -> bool:
    headers = _platform_headers(platform)
    timeout = aiohttp.ClientTimeout(total=180, sock_connect=15, sock_read=45)

    async def _try_download() -> Tuple[Optional[bytes], Optional[str]]:
        # HEAD size
        try:
            async with session.head(media_url, headers=headers, timeout=timeout, allow_redirects=True) as hr:
                if 200 <= hr.status < 400:
                    cl = hr.headers.get("Content-Length")
                    if cl and cl.isdigit() and int(cl) > max_bytes:
                        return None, "big"
        except Exception:
            pass
        # GET streaming
        try:
            async with session.get(media_url, headers=headers, timeout=timeout, allow_redirects=True) as gr:
                if gr.status in (403, 429) or gr.status >= 500:
                    return None, f"retry:{gr.status}"
                buf = bytearray()
                async for chunk in gr.content.iter_chunked(64 * 1024):
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        return None, "big"
                return bytes(buf), None
        except asyncio.TimeoutError:
            return None, "timeout"
        except Exception:
            return None, "err"

    # retry backoff
    for attempt in range(3):
        data, reason = await _try_download()
        if data is not None:
            filename = "media.mp4" if (".mp4" in media_url or "video" in media_url) else "media"
            try:
                await thread.send(file=discord.File(fp=io.BytesIO(data), filename=filename))
                return True
            except Exception as e:
                print("[WARN] gagal upload file:", e)
                await thread.send(f"🔗 Gagal upload file, ini tautannya:\n{media_url}")
                return True

        if reason == "big":
            await thread.send(f"📦 >25MB, aku kirim tautan unduhan saja:\n{media_url}")
            return True

        await asyncio.sleep(1.2 * (attempt + 1))

    await thread.send(f"⚠️ Gagal mengunduh media (403/timeout). Ini tautannya saja:\n{media_url}")
    return True

# =========================
# UI BUTTONS (Downloader)
# =========================
class DownloaderView(View):
    def __init__(self, thread: discord.Thread, requester: discord.Member, timeout: int = 600):
        super().__init__(timeout=timeout)
        self.thread = thread
        self.requester = requester

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.requester.id

    @button(label="Download Lagi", style=discord.ButtonStyle.primary)
    async def more_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "Kirim tautan Instagram/TikTok berikutnya di thread ini ya…",
            ephemeral=True
        )
        def check_msg(m: discord.Message):
            return (
                m.author.id == self.requester.id
                and m.channel.id == self.thread.id
                and (is_ig_url(m.content) or is_tt_url(m.content) or extract_first_url(m.content))
            )
        try:
            msg = await bot.wait_for("message", timeout=180, check=check_msg)
            link = extract_first_url(msg.content) or msg.content.strip()
            await handle_download_flow(self.thread, self.requester, link)
        except asyncio.TimeoutError:
            try:
                await self.thread.send("⌛ Waktu habis menunggu link baru. Kamu bisa klik **Download Lagi** lagi kapan pun.")
            except Exception:
                pass

    @button(label="Tutup Thread", style=discord.ButtonStyle.danger)
    async def close_btn(self, interaction: discord.Interaction, button: Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        try:
            await self.thread.send("👋 Oke, thread akan diarsip. Makasih sudah pakai downloader!")
            await self.thread.edit(archived=True, locked=True)
        except Exception as e:
            print("[WARN] gagal tutup thread:", e)
        try:
            await interaction.followup.send("Thread ditutup.", ephemeral=True)
        except Exception:
            pass

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

    # Resume mabar reminder
    pending = load_pending_mabar(to_epoch(now_wib()))
    if pending:
        print(f"🕰️ Menjadwalkan ulang {len(pending)} reminder mabar dari Firestore.")
    for doc_id, dat in pending:
        asyncio.create_task(schedule_mabar_tasks_from_doc(doc_id, dat))

    # Kirim notice downloader sekali (anti-duplikat)
    try:
        await maybe_send_downloader_notice()
    except Exception as e:
        print("[WARN] notice downloader:", e)

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
async def _safe_get_member(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
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

        sent = await dest.send(content=content, files=files) if files else await dest.send(content)

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
# DOWNLOADER LOGIC
# =========================
async def maybe_send_downloader_notice():
    guild = bot.guilds[0] if bot.guilds else None
    if not guild:
        return
    state = ensure_downloader_state(guild.id)
    if state.get("notice_sent"):
        return
    ch = bot.get_channel(CHANNEL_ID_DOWNLOADER)
    if not isinstance(ch, discord.TextChannel):
        return
    text = (
        "Untuk menjaga privasi, gunakan perintah **`!dw`** di sini —\n"
        "bot akan membuat **thread pribadi** khusus untukmu 🤫\n"
        "(hanya kamu dan bot yang dapat melihat percakapan tersebut).\n\n"
        "📦 **Maksimum ukuran media: 25 MB**\n"
        "Lebih dari itu, bot akan mengirimkan tautan unduhan.\n"
        "| 🟢 Fitur ini aktif untuk member dengan role 🔆 Light."
    )
    try:
        msg = await ch.send(text)
        set_downloader_state(guild.id, notice_sent=True, notice_message_id=msg.id)
    except Exception as e:
        print("[WARN] gagal kirim notice downloader:", e)

async def create_private_thread_for_user(base_channel: discord.TextChannel, user: discord.Member) -> Optional[discord.Thread]:
    try:
        name = f"dw-{user.name}-{user.discriminator}".lower()
    except Exception:
        name = f"dw-{user.id}"
    try:
        thread = await base_channel.create_thread(name=name, auto_archive_duration=60, reason="Downloader private thread")
        try:
            await thread.add_user(user)
        except Exception:
            pass
        return thread
    except Exception as e:
        print("[ERROR] create thread:", e)
        return None

async def handle_download_flow(thread: discord.Thread, requester: discord.Member, link: str):
    platform = "ig" if is_ig_url(link) else ("tt" if is_tt_url(link) else None)
    if not platform:
        await thread.send("⚠️ Tautan tidak dikenali. Kirim link Instagram atau TikTok ya.")
        return

    await thread.send("⏳ Tunggu sebentar, aku cek & ambil media…")

    # Panggil API tepat sebelum download (token CDN fresh) dgn fallback requests
    try:
        media_urls = await fetch_api_fresh_with_fallback(platform, link)
    except Exception as e:
        print("[ERROR] API fetch:", e)
        await thread.send("⚠️ Tidak bisa menghubungi API downloader. Coba lagi nanti.")
        return

    if not media_urls:
        await thread.send("⚠️ Tidak menemukan media pada tautan tersebut.")
        return

    # Stream tiap URL & kirim file/link sesuai batas
    sent_any = False
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=8)) as session:
        for u in media_urls[:3]:  # batasi 3 per link
            ok = await stream_send_or_link(session, thread, u, platform)
            sent_any = sent_any or ok

    # Tampilkan tombol aksi
    view = DownloaderView(thread, requester)
    if sent_any:
        await thread.send("✅ Selesai. Mau unduh lagi atau tutup thread?", view=view)
    else:
        await thread.send("⚠️ Tidak ada file yang bisa dikirim. Coba **Download Lagi** atau tutup thread.", view=view)

# =========================
# COMMAND ROUTING + HOOKS
# =========================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Deteksi perintah mabar
    content_low = message.content.lower()
    match_cmd = re.search(r'!(mabar|main)\s+(.+)', content_low)
    if match_cmd:
        ctx = await bot.get_context(message)
        arg = match_cmd.group(2).strip()
        await mabar(ctx, arg=arg)
        return

    # Deteksi gambar (konfirmasi forward)
    try:
        await _confirm_and_forward_images(message)
    except Exception as e:
        print("[WARN] Handler forward images error:", e)

    # Deteksi tautan IG/TT di channel umum → arahkan ke channel downloader
    if message.channel.id == CHANNEL_ID_CHAT_GENERAL:
        url_in = extract_first_url(message.content)
        if url_in and (is_ig_url(url_in) or is_tt_url(url_in)):
            try:
                downloader_ch = bot.get_channel(CHANNEL_ID_DOWNLOADER)
                if isinstance(downloader_ch, discord.TextChannel):
                    await message.reply(
                        f"hola {message.author.mention}, mau download medianya? "
                        f"ke {downloader_ch.mention} dan ketik `!dw` ya! ✨",
                        mention_author=True,
                        delete_after=300
                    )
            except Exception:
                pass

    await bot.process_commands(message)

# =========================
# COMMANDS: PING
# =========================
@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send(f"Pong! {round(bot.latency * 1000)}ms")

# =========================
# COMMANDS: DOWNLOADER
# =========================
@bot.command(name="dw-on")
@commands.has_permissions(manage_guild=True)
async def dw_on(ctx: commands.Context):
    set_downloader_state(ctx.guild.id, enabled=True)
    await ctx.reply("🟢 Downloader diaktifkan.")
    try:
        await maybe_send_downloader_notice()
    except Exception:
        pass

@bot.command(name="dw-off")
@commands.has_permissions(manage_guild=True)
async def dw_off(ctx: commands.Context):
    set_downloader_state(ctx.guild.id, enabled=False)
    await ctx.reply("🔴 Downloader dimatikan.")

@bot.command(name="dw")
async def dw(ctx: commands.Context):
    """Mulai sesi unduhan privat (hanya di channel downloader)."""
    if ctx.channel.id != CHANNEL_ID_DOWNLOADER:
        return await ctx.reply("Gunakan perintah ini di channel downloader ya.", delete_after=8)

    role_light = ctx.guild.get_role(ROLE_ID_LIGHT)
    if not role_light or role_light not in ctx.author.roles:
        return await ctx.reply("❌ Fitur ini khusus member dengan role 🔆 **Light**.", delete_after=8)

    state = ensure_downloader_state(ctx.guild.id)
    if not state.get("enabled", True):
        return await ctx.reply("⚠️ Downloader sedang dimatikan oleh admin.", delete_after=8)

    base_ch = ctx.channel
    if not isinstance(base_ch, discord.TextChannel):
        return await ctx.reply("⚠️ Channel tidak valid.", delete_after=6)

    thread = await create_private_thread_for_user(base_ch, ctx.author)
    if not isinstance(thread, discord.Thread):
        return await ctx.reply("⚠️ Gagal membuat thread.", delete_after=6)

    await ctx.reply(f"✅ Thread dibuat: {thread.mention} — lanjut di sana ya!", delete_after=10)

    # Hapus pesan perintah !dw setelah 30 detik (rapi)
    async def _del_cmd():
        await asyncio.sleep(30)
        try:
            await ctx.message.delete()
        except Exception:
            pass
    asyncio.create_task(_del_cmd())

    guide = (
        f"Hola {ctx.author.mention}! Kirim **tautan Instagram/TikTok** di sini.\n"
        "Aku akan ambil media dan mengirimkan file jika ≤ 25MB. Jika lebih besar, akan kukirim tautan unduhan.\n\n"
        "Contoh:\n"
        "- https://www.instagram.com/reel/DPOExAWkrVL/?igsh=MWptN2hmc3RocTF2dA==\n"
        "- https://vt.tiktok.com/ZSUFxSW72\n\n"
        "Setelah selesai proses pertama, akan muncul tombol **Download Lagi** dan **Tutup Thread**."
    )
    guide_msg = await thread.send(guide)

    # Tunggu link pertama dari user (5 menit)
    def check_msg(m: discord.Message):
        return (
            m.author.id == ctx.author.id
            and m.channel.id == thread.id
            and (is_ig_url(m.content) or is_tt_url(m.content) or extract_first_url(m.content))
        )
    try:
        first = await bot.wait_for("message", timeout=300, check=check_msg)
        link = extract_first_url(first.content) or first.content.strip()
        await handle_download_flow(thread, ctx.author, link)
    except asyncio.TimeoutError:
        await thread.send("⌛ Timeout. Kalau masih mau lanjut, kirim link kapan saja ya.")

# =========================
# MABAR (WIB + Firestore)
# =========================
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
        try: await msg.add_reaction(em)
        except Exception: pass

    def check(reaction, user):
        return user == ctx.author and str(reaction.emoji) in ["✅","❌"] and reaction.message.id == msg.id

    try:
        reaction, _ = await bot.wait_for("reaction_add", timeout=60.0, check=check)
    except asyncio.TimeoutError:
        try: await msg.delete()
        except Exception: pass
        return await ctx.send("⏰ Waktu konfirmasi habis, mabar dibatalkan.", delete_after=5)

    if str(reaction.emoji) == "❌":
        try: await msg.delete()
        except Exception: pass
        return await ctx.send("❌ Mabar dibatalkan.", delete_after=5)

    try: await msg.delete()
    except Exception: pass

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
# RUN
# =========================
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        print("❌ Token invalid. Pastikan DISCORD_BOT_TOKEN benar.")
    except Exception as e:
        print(f"[FATAL] Error menjalankan bot: {e}")
