import os
import re
import asyncio
from datetime import timedelta, datetime
from zoneinfo import ZoneInfo  # <- WIB/Asia-Jakarta timezone

import discord
from discord.ext import commands

# === TOKEN ===
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# === KONFIGURASI ID ===
CHANNEL_ID_WELCOME = 1423964756158447738
CHANNEL_ID_LOGS    = 1423969192389902339
CHANNEL_ID_MABAR   = 1424029336683679794
CHANNEL_ID_INTRO   = 1424033383339659334
RULES_CHANNEL_ID   = 1423969192389902336
ROLE_ID_LIGHT      = 1424026593143164958

REACTION_EMOJI     = "🔆"
PINNED_MESSAGE_ID  = None

# === TIMEZONE (WIB) ===
TZ = ZoneInfo("Asia/Jakarta")

# === INTENTS ===
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)
KONTEN_LIMIT = 1000


# === HELPER: PARSER WAKTU (WIB) ===
def parse_natural_time(text: str, now_wib: datetime):
    t = text.lower().strip()
    if t in {"now", "sekarang", "skrng"}:
        return now_wib, "sekarang (WIB)"

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

    # 12 pagi -> 00:00
    if pagi and hour == 12:
        hour = 0
    # sore/malam -> PM
    elif (sore or malam) and hour < 12:
        hour += 12

    target = now_wib.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    if is_tomorrow or target <= now_wib:
        target += timedelta(days=1)

    return target, target.strftime("%H:%M WIB")


# === HELPER: KIRIM EMBED REACTION-ROLE ===
async def send_light_embed(channel: discord.TextChannel):
    rules_mention = f"<#{RULES_CHANNEL_ID}>"
    embed = discord.Embed(
        title="✨ Pilih Role Light",
        description=(
            f"Klik reaksi {REACTION_EMOJI} di bawah untuk **mendapatkan role Light**.\n\n"
            f"Pastikan juga membaca {rules_mention} agar pengalamanmu nyaman dan tertib 😉"
        ),
        color=discord.Color.gold()
    )
    embed.set_footer(text="Klik 🔆 untuk ambil role. Klik lagi untuk melepas.")
    msg = await channel.send(embed=embed)
    await msg.add_reaction(REACTION_EMOJI)
    try:
        await msg.pin()
    except Exception as e:
        print(f"[WARN] Gagal pin pesan: {e}")
    return msg.id


# === ON_READY ===
@bot.event
async def on_ready():
    global PINNED_MESSAGE_ID

    print(f"✅ Bot login sebagai {bot.user}")
    try:
        await bot.change_presence(activity=discord.Game("menjaga server ✨"))
    except Exception:
        pass

    welcome_channel = bot.get_channel(CHANNEL_ID_WELCOME)
    if not welcome_channel:
        print("[WARN] Channel welcome tidak ditemukan.")
        return

    # Cari pesan pinned bot utk reaction-role
    try:
        pinned = await welcome_channel.pins()
    except Exception as e:
        print(f"[WARN] Tidak bisa mengambil pins: {e}")
        pinned = []

    found = None
    for msg in pinned:
        if msg.author == bot.user and msg.embeds:
            if "Pilih Role Light" in (msg.embeds[0].title or ""):
                found = msg
                break

    if found:
        PINNED_MESSAGE_ID = found.id
        print(f"📌 Pesan pinned Light ditemukan: {PINNED_MESSAGE_ID}")
    else:
        print("❌ Tidak ada pesan pinned Light, membuat baru...")
        PINNED_MESSAGE_ID = await send_light_embed(welcome_channel)
        print(f"📌 Pesan Light baru dikirim & dipin: {PINNED_MESSAGE_ID}")


# === GREETINGS (welcome + info role jadi satu chat) ===
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
        f"• Ambil role {role_text} dengan klik reaksi {REACTION_EMOJI} pada **pesan yang dipin** di channel ini."
    )

    embed = discord.Embed(
        title="🎉 Selamat Datang!",
        description=desc,
        color=discord.Color.green()
    )
    embed.set_footer(text="Selamat bergabung & have fun! ✨")
    await ch.send(embed=embed)


@bot.event
async def on_member_remove(member: discord.Member):
    ch = bot.get_channel(CHANNEL_ID_WELCOME)
    if not isinstance(ch, discord.TextChannel):
        return
    embed = discord.Embed(
        title="👋 Selamat Tinggal",
        description=f"{member.display_name} telah keluar dari server.",
        color=discord.Color.red()
    )
    await ch.send(embed=embed)


# === LOG PESAN DIHAPUS ===
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


# === ON_MESSAGE: DETEKSI !main / !mabar ===
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.lower()
    match_cmd = re.search(r'!(mabar|main)\s+(.+)', content)
    if match_cmd:
        ctx = await bot.get_context(message)
        arg = match_cmd.group(2).strip()
        await mabar(ctx, arg=arg)
        return

    await bot.process_commands(message)


# === REACTION ROLE ===
async def _safe_get_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            member = None
    return member


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # Hanya emote yg ditentukan & hanya di pesan pinned kita
    if str(payload.emoji) != REACTION_EMOJI or payload.guild_id is None:
        return
    if not PINNED_MESSAGE_ID or payload.message_id != PINNED_MESSAGE_ID:
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

    if role not in member.roles:
        try:
            await member.add_roles(role, reason="Reaction role: Light")
            intro_channel = guild.get_channel(CHANNEL_ID_INTRO)
            if isinstance(intro_channel, discord.TextChannel):
                await intro_channel.send(
                    f"Ekhem… {member.mention}! Sebutin umurmu aja boleh kok. "
                    f"Kalau mau cerita lebih, juga boleh, tapi jangan terlalu detail, ya!"
                )
        except Exception as e:
            print("[ERROR] Gagal memberi role:", e)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) != REACTION_EMOJI or payload.guild_id is None:
        return
    if not PINNED_MESSAGE_ID or payload.message_id != PINNED_MESSAGE_ID:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return

    member = await _safe_get_member(guild, payload.user_id)
    if not member:
        return

    role = guild.get_role(ROLE_ID_LIGHT)
    if role and role in member.roles:
        try:
            await member.remove_roles(role, reason="Reaction role: Light (remove)")
        except Exception as e:
            print("[ERROR] Gagal hapus role:", e)


# === COMMANDS DASAR ===
@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send(f"Pong! {round(bot.latency * 1000)}ms")


@bot.command()
@commands.has_permissions(administrator=True)
async def setuplight(ctx: commands.Context):
    """Buat ulang reaction role Light"""
    channel = bot.get_channel(CHANNEL_ID_WELCOME)
    if not isinstance(channel, discord.TextChannel):
        return await ctx.send("❌ Channel welcome tidak ditemukan.")

    # bersihkan pesan lama
    async for msg in channel.history(limit=50):
        if msg.author == bot.user and msg.embeds:
            if msg.embeds[0].title and "Pilih Role Light" in msg.embeds[0].title:
                try:
                    await msg.unpin()
                except Exception:
                    pass
                try:
                    await msg.delete()
                except Exception:
                    pass

    global PINNED_MESSAGE_ID
    PINNED_MESSAGE_ID = await send_light_embed(channel)
    await ctx.send("✅ Reaction-role baru dibuat & dipin.")


# === MABAR / MAIN INTERAKTIF (WIB) ===
@bot.command(aliases=["main"])
async def mabar(ctx: commands.Context, *, arg: str = None):
    """Contoh: !mabar Distrik Violence jam 8 malam"""
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

    now_wib = datetime.now(TZ)
    remind_at_wib, when_str = parse_natural_time(waktu_text, now_wib)

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

    announce = (
        f"{role_light.mention}\n"
        f"🎮 Kalau nggak sibuk **{when_str}**, join mabar **{map_name.title()}**, yuk!"
    )
    announce_msg = await mabar_channel.send(announce)
    await ctx.send(f"✅ Pengumuman mabar dikirim ke <#{CHANNEL_ID_MABAR}>", delete_after=5)

    # === Jadwalkan reminder & auto-delete sebagai background tasks (pakai WIB)
    async def remind_task():
        delay = max(0, (remind_at_wib - datetime.now(TZ)).total_seconds())
        if delay > 60:  # kirim reminder hanya jika > 1 menit dari sekarang
            await asyncio.sleep(delay)
            try:
                await mabar_channel.send(
                    f"{role_light.mention}\n⏰ Waktunya mabar **{map_name.title()}**! Siap-siap yuk 🎮"
                )
            except Exception as e:
                print("[ERROR] Reminder gagal:", e)

    async def autodelete_task():
        total = max(0, (remind_at_wib - datetime.now(TZ)).total_seconds()) + 3600
        await asyncio.sleep(total)
        try:
            await announce_msg.delete()
        except Exception as e:
            print("[WARN] Gagal hapus pengumuman:", e)

    asyncio.create_task(remind_task())
    asyncio.create_task(autodelete_task())


# === RUN ===
if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_BOT_TOKEN tidak ditemukan.")
    else:
        try:
            bot.run(TOKEN)
        except discord.LoginFailure:
            print("❌ Token invalid. Pastikan DISCORD_BOT_TOKEN benar.")
        except Exception as e:
            print(f"[FATAL] Error menjalankan bot: {e}")
