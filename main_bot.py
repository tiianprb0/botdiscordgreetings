# main_bot.py

import os
import re
import io
import json
import asyncio
import urllib.parse
from typing import Optional, Tuple, List, Dict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

import aiohttp

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
DOWNLOADER_LOGS  = "downloader_logs"      # dok log proses
ANNOUNCE_LOGS    = "announce_logs"        # (kalau kamu pakai fitur announce nantinya)

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
    """default: enabled True, notice_sent False"""
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

    # Kirim notice downloader (sekali) bila belum pernah
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
# DOWNLOADER (IG/TIKTOK)
# =========================

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
}

def _guess_ref_origin_from_url(url: str) -> Tuple[str, str]:
    u = url.lower()
    if "instagram" in u or "cdninstagram" in u or "rapidcdn" in u:
        base = "https://www.instagram.com/"
    elif "tiktok" in u:
        base = "https://www.tiktok.com/"
    else:
        base = "https://discord.com/"
    return base, base.rstrip("/")

async def http_get_json(url: str, referer: Optional[str] = None, retries: int = 2) -> Optional[dict]:
    timeout = aiohttp.ClientTimeout(total=45)
    headers = dict(DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = referer.rstrip("/")
    last_err = None
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
        for _ in range(retries + 1):
            try:
                async with s.get(url, allow_redirects=True) as r:
                    if r.status == 200:
                        ct = (r.headers.get("Content-Type") or "").lower()
                        if "json" in ct:
                            return await r.json()
                        text = await r.text()
                        try:
                            return json.loads(text)
                        except Exception:
                            return None
                    if r.status in (403, 429, 503):
                        last_err = r.status
                        await asyncio.sleep(1.2)
                        continue
                    return None
            except asyncio.TimeoutError:
                last_err = "timeout"
                await asyncio.sleep(1.2)
                continue
            except Exception:
                return None
    return None

async def fetch_media_bytes(url: str, max_mb: float = 25.0) -> Tuple[Optional[bytes], float, Optional[str]]:
    limit = int(max_mb * 1024 * 1024)
    timeout = aiohttp.ClientTimeout(total=300)
    referer, origin = _guess_ref_origin_from_url(url)
    headers = dict(DEFAULT_HEADERS)
    headers["Referer"] = referer
    headers["Origin"]  = origin

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
        # HEAD (jika bisa)
        try:
            async with s.head(url, allow_redirects=True) as h:
                cl = h.headers.get("Content-Length")
                if cl and cl.isdigit() and int(cl) > limit:
                    return None, int(cl)/1024/1024, None
        except Exception:
            pass

        async with s.get(url, allow_redirects=True) as r:
            if r.status in (403, 429) or r.status != 200:
                return None, 0.0, None
            ct = (r.headers.get("Content-Type") or "").lower()
            ext = ".mp4" if "video" in ct else (".jpg" if "jpeg" in ct or "jpg" in ct else (".png" if "png" in ct else ".webp"))
            cl = r.headers.get("Content-Length")
            if cl and cl.isdigit() and int(cl) > limit:
                return None, int(cl)/1024/1024, ext

            buf = io.BytesIO()
            async for chunk in r.content.iter_chunked(1<<15):
                buf.write(chunk)
                if buf.tell() > limit:
                    return None, buf.tell()/1024/1024, ext
            data = buf.getvalue()
            return data, len(data)/1024/1024, ext

def is_ig_url(s: str) -> bool:
    u = s.lower()
    return "instagram.com" in u

def is_tt_url(s: str) -> bool:
    u = s.lower()
    return "tiktok.com" in u or "vt.tiktok.com" in u

def extract_first_url(text: str) -> Optional[str]:
    if not text: return None
    m = re.search(r"(https?://\S+)", text)
    return m.group(1) if m else None

async def maybe_send_downloader_notice():
    """Kirim notice UX sekali saja (anti-duplikat via Firestore)."""
    guild = None
    # ambil guild pertama yang bot join (server kamu)
    for g in bot.guilds:
        guild = g
        break
    if not guild:
        return
    state = ensure_downloader_state(guild.id)
    if state.get("notice_sent"):
        return

    ch = bot.get_channel(CHANNEL_ID_DOWNLOADER)
    if not isinstance(ch, discord.TextChannel):
        return

    text = (
        "Cukup kirimkan tautan postingan di sini —\n"
        "bot akan otomatis membuat **thread pribadi** khusus untukmu 🤫\n"
        "(hanya kamu dan bot yang dapat melihat percakapan tersebut).\n\n"
        "📦 **Maksimum ukuran media: 25 MB**\n"
        "Lebih dari itu, bot akan mengirimkan tautan unduhan.\n"
        "| 🟢 Fitur ini aktif untuk member dengan role 🔆 Light."
    )
    # cek apakah sudah ada notice identik terbaru (sekilas) → minimal kita tandai via Firestore
    try:
        msg = await ch.send(text)
        set_downloader_state(guild.id, notice_sent=True, notice_message_id=msg.id)
    except Exception as e:
        print("[WARN] gagal kirim notice downloader:", e)

async def create_private_thread_for_user(base_channel: discord.TextChannel, user: discord.Member) -> Optional[discord.Thread]:
    """Buat private thread (archived=false), add only user & bot."""
    # Discord text channel thread tidak bisa total 'private' seperti forum,
    # tapi kita bisa buat thread & langsung invite user yang bersangkutan.
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
    """Ambil media dari API → kirim ke thread (<=25MB), sisanya link."""
    platform = "instagram" if is_ig_url(link) else ("tiktok" if is_tt_url(link) else None)
    if not platform:
        await thread.send("⚠️ Tautan tidak dikenali. Hanya Instagram/TikTok yang didukung.")
        return

    encoded = urllib.parse.quote_plus(link)
    api_url = f"https://api.ryzumi.vip/api/downloader/{'igdl' if platform=='instagram' else 'ttdl'}?url={encoded}"
    referer = "https://www.instagram.com/" if platform == "instagram" else "https://www.tiktok.com/"

    await thread.send(f"🔎 Memproses tautan {platform.title()}…")
    data = await http_get_json(api_url, referer=referer)

    if not data:
        await thread.send("🚫 API menolak/timeout (403/timeout). Coba lagi nanti atau kirim link lain.")
        log_downloader_event(thread.guild.id, {"ok": False, "platform": platform, "reason": "api_none", "url": link})
        return

    media_urls: List[str] = []

    if platform == "instagram":
        # Skema resmi: {"status": true, "data": [ { "thumbnail","url","type" } ]}
        try:
            arr = data.get("data", [])
            for item in arr:
                url_item = item.get("url") or item.get("thumbnail")
                if url_item:
                    media_urls.append(url_item)
        except Exception:
            pass

    else:  # tiktok
        # Tangani dua kemungkinan skema: yang 'sederhana' & yang nested (real)
        # 1) nested: data.data.hdplay/play/wmplay
        try:
            d2 = data.get("data", {})
            if isinstance(d2, dict) and "data" in d2:
                core = d2.get("data", {})
                url = core.get("hdplay") or core.get("play") or core.get("wmplay")
                if url:
                    media_urls.append(url)
        except Exception:
            pass
        # 2) fallback: cari di level atas keys yang mengandung play_url/url_list
        if not media_urls:
            try:
                def hunt_urls(obj):
                    if isinstance(obj, dict):
                        for k,v in obj.items():
                            if k in ("hdplay","play","wmplay") and isinstance(v,str):
                                media_urls.append(v)
                            elif isinstance(v,(dict,list)):
                                hunt_urls(v)
                    elif isinstance(obj, list):
                        for x in obj:
                            hunt_urls(x)
                hunt_urls(data)
            except Exception:
                pass

    if not media_urls:
        await thread.send("😕 Tidak menemukan URL media yang bisa diunduh dari respons.")
        log_downloader_event(thread.guild.id, {"ok": False, "platform": platform, "reason": "no_media", "url": link, "resp_keys": list(data.keys())})
        return

    sent_any = False
    for idx, murl in enumerate(media_urls[:3]):  # batasi 3 media per link
        bytes_data, size_mb, ext = await fetch_media_bytes(murl, max_mb=25.0)
        if bytes_data:
            filename = f"media_{idx+1}{ext or ''}"
            try:
                await thread.send(file=discord.File(io.BytesIO(bytes_data), filename=filename))
                sent_any = True
            except Exception as e:
                print("[WARN] gagal kirim file:", e)
        else:
            # >25MB atau gagal direct → kirim link
            note = " (>25MB)" if size_mb and size_mb > 25 else ""
            await thread.send(f"📎 Media terlalu besar{note} atau dibatasi — pakai tautan langsung saja:\n{murl}")

    if sent_any:
        await thread.send("✅ Selesai. Mau unduh lagi? Kirim link lain di thread ini. "
                          "Jika sudah beres, react ❌ pada pesan ini untuk menutup thread.")
    else:
        await thread.send("⚠️ Tidak ada file yang bisa dikirim. Coba link lain ya.")

    log_downloader_event(thread.guild.id, {"ok": sent_any, "platform": platform, "url": link})

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

    # Deteksi tautan IG/TT di channel general → arahkan ke channel downloader
    if message.channel.id == CHANNEL_ID_CHAT_GENERAL:
        url_in = extract_first_url(message.content)
        if url_in and (is_ig_url(url_in) or is_tt_url(url_in)):
            try:
                downloader_ch = bot.get_channel(CHANNEL_ID_DOWNLOADER)
                if isinstance(downloader_ch, discord.TextChannel):
                    await message.reply(
                        f"hola {message.author.mention}, mau download medianya? "
                        f"di {downloader_ch.mention} yuk! (lebih privat & rapi) ✨\n"
                        f"Ketik `!dw` di sana biar kubuatin thread pribadi buatmu.",
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
    # kirim notice jika belum ada
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

    # Buat thread
    base_ch = ctx.channel
    if not isinstance(base_ch, discord.TextChannel):
        return await ctx.reply("⚠️ Channel tidak valid.", delete_after=6)

    thread = await create_private_thread_for_user(base_ch, ctx.author)
    if not isinstance(thread, discord.Thread):
        return await ctx.reply("⚠️ Gagal membuat thread.", delete_after=6)

    await ctx.reply(f"✅ Thread dibuat: {thread.mention} — lanjut di sana ya!", delete_after=10)

    guide = (
        f"Hola {ctx.author.mention}! Kirim **tautan Instagram/TikTok** di sini.\n"
        "Aku akan ambil media dan mengirimkan file jika ≤ 25MB. Jika lebih besar, akan kukirim tautan unduhan.\n\n"
        "Contoh tautan:\n"
        "- https://www.instagram.com/reel/DPOExAWkrVL/?igsh=MWptN2hmc3RocTF2dA==\n"
        "- https://vt.tiktok.com/ZSUFxSW72\n\n"
        "Kapan saja kalau selesai, react ❌ pada pesan ini untuk menutup thread."
    )
    guide_msg = await thread.send(guide)
    try:
        await guide_msg.add_reaction("❌")
    except Exception:
        pass

    # tunggu link pertama dari user (5 menit)
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

    # handler close thread via ❌
    def check_react(reaction, user):
        return user.id == ctx.author.id and str(reaction.emoji) == "❌" and reaction.message.id == guide_msg.id

    try:
        await bot.wait_for("reaction_add", timeout=3600, check=check_react)
        try:
            await thread.send("👋 Baik, thread akan diarsip.")
            await thread.edit(archived=True, locked=True)
        except Exception:
            pass
    except asyncio.TimeoutError:
        # tidak apa-apa, biarkan thread auto-archive
        pass

# =========================
# MABAR
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
