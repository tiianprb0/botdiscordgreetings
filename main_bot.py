import os
import re
import asyncio
from datetime import datetime, timedelta
import discord
from discord.ext import commands

# === TOKEN ===
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# === KONFIGURASI ID ===
CHANNEL_ID_WELCOME = 1423964756158447738
CHANNEL_ID_LOGS = 1423969192389902339
CHANNEL_ID_MABAR = 1424029336683679794
CHANNEL_ID_INTRO = 1424033383339659334
RULES_CHANNEL_ID = 1423969192389902336
ROLE_ID_LIGHT = 1424026593143164958

REACTION_EMOJI = "🔆"
PINNED_MESSAGE_ID = None

# === INTENTS ===
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)
KONTEN_LIMIT = 1000


# === HELPER PARSER WAKTU ===
def parse_natural_time(text: str, now: datetime):
    t = text.lower().strip()
    if t in {"now", "sekarang", "skrng"}:
        return now, "sekarang"

    is_tomorrow = "besok" in t
    m = re.search(r"(\d{1,2})(?:[:.](\d{1,2}))?", t)
    hour, minute = 0, 0
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)

    pagi = "pagi" in t
    siang = "siang" in t
    sore = "sore" in t
    malam = "malam" in t

    if pagi and hour == 12:
        hour = 0
    elif sore or malam:
        if hour < 12:
            hour += 12

    target = now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    if is_tomorrow or target <= now:
        target += timedelta(days=1)
    return target, target.strftime("%H:%M")


# === HELPER EMBED ROLE ===
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
        print(f"Gagal pin pesan: {e}")
    return msg.id


# === ON_READY ===
@bot.event
async def on_ready():
    global PINNED_MESSAGE_ID

    print(f"✅ Bot login sebagai {bot.user}")
    await bot.change_presence(activity=discord.Game("menjaga server ✨"))

    welcome_channel = bot.get_channel(CHANNEL_ID_WELCOME)
    if not welcome_channel:
        print("⚠️ Channel welcome tidak ditemukan.")
        return

    pinned = await welcome_channel.pins()
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


# === GREETINGS ===
@bot.event
async def on_member_join(member):
    ch = bot.get_channel(CHANNEL_ID_WELCOME)
    if ch:
        embed = discord.Embed(
            title="🎉 Selamat Datang!",
            description=f"Halo {member.mention}, selamat datang di **{member.guild.name}**!\n"
                        f"Jangan lupa baca {member.guild.get_channel(RULES_CHANNEL_ID).mention} ya.",
            color=discord.Color.green()
        )
        await ch.send(embed=embed)


@bot.event
async def on_member_remove(member):
    ch = bot.get_channel(CHANNEL_ID_WELCOME)
    if ch:
        embed = discord.Embed(
            title="👋 Selamat Tinggal",
            description=f"{member.display_name} telah keluar dari server.",
            color=discord.Color.red()
        )
        await ch.send(embed=embed)


# === LOG PESAN DIHAPUS ===
@bot.event
async def on_message_delete(message):
    if message.author.bot:
        return
    log_channel = bot.get_channel(CHANNEL_ID_LOGS)
    if not log_channel:
        return
    konten = (message.content or "")[:KONTEN_LIMIT]
    if len(message.content or "") > KONTEN_LIMIT:
        konten += "..."
    konten = konten.replace("```", "")
    embed = discord.Embed(title="🗑️ Pesan Dihapus", color=discord.Color.orange())
    embed.add_field(name="Pengirim", value=message.author.mention, inline=False)
    embed.add_field(name="Channel", value=message.channel.mention, inline=False)
    if konten.strip():
        embed.add_field(name="Konten", value=f"```{konten}```", inline=False)
    await log_channel.send(embed=embed)


# === ON_MESSAGE: DETEKSI !main / !mabar ===
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content.lower()
    match = re.search(r'!(mabar|main)\s+(.+)', content)
    if match:
        ctx = await bot.get_context(message)
        arg = match.group(2).strip()
        await mabar(ctx, arg=arg)
        return

    await bot.process_commands(message)


# === REACTION ROLE ===
@bot.event
async def on_raw_reaction_add(payload):
    if str(payload.emoji) != REACTION_EMOJI or payload.guild_id is None:
        return
    if PINNED_MESSAGE_ID and payload.message_id != PINNED_MESSAGE_ID:
        return

    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    role = guild.get_role(ROLE_ID_LIGHT)
    if member and role and not member.bot:
        if role not in member.roles:
            try:
                await member.add_roles(role, reason="Reaction role: Light")
                intro_channel = guild.get_channel(CHANNEL_ID_INTRO)
                if intro_channel:
                    await intro_channel.send(
                        f"Ekhem… {member.mention}! Sebutin umurmu aja boleh kok. "
                        f"Kalau mau cerita lebih, juga boleh, tapi jangan terlalu detail, ya!"
                    )
            except Exception as e:
                print("Gagal memberi role:", e)


@bot.event
async def on_raw_reaction_remove(payload):
    if str(payload.emoji) != REACTION_EMOJI or payload.guild_id is None:
        return
    if PINNED_MESSAGE_ID and payload.message_id != PINNED_MESSAGE_ID:
        return

    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    role = guild.get_role(ROLE_ID_LIGHT)
    if member and role and role in member.roles:
        try:
            await member.remove_roles(role, reason="Reaction role: Light (remove)")
        except Exception as e:
            print("Gagal hapus role:", e)


# === COMMANDS DASAR ===
@bot.command()
async def ping(ctx):
    await ctx.send(f"Pong! {round(bot.latency * 1000)}ms")


@bot.command()
@commands.has_permissions(administrator=True)
async def setuplight(ctx):
    """Buat ulang reaction role Light"""
    channel = bot.get_channel(CHANNEL_ID_WELCOME)
    if not channel:
        return await ctx.send("❌ Channel welcome tidak ditemukan.")

    async for msg in channel.history(limit=50):
        if msg.author == bot.user and msg.embeds:
            if "Pilih Role Light" in msg.embeds[0].title:
                try:
                    await msg.unpin()
                    await msg.delete()
                except Exception:
                    pass

    global PINNED_MESSAGE_ID
    PINNED_MESSAGE_ID = await send_light_embed(channel)
    await ctx.send("✅ Reaction-role baru dibuat & dipin.")


# === MABAR / MAIN INTERAKTIF ===
@bot.command(aliases=["main"])
async def mabar(ctx, *, arg: str = None):
    """Contoh: !mabar Distrik Violence jam 8 malam"""
    role_light = ctx.guild.get_role(ROLE_ID_LIGHT)
    if not role_light:
        return await ctx.send("⚠️ Role Light belum diset di kode.")
    if role_light not in ctx.author.roles:
        return await ctx.send("❌ Kamu belum punya role Light untuk pakai perintah ini!")

    if not arg:
        return await ctx.send("Gunakan format: `!mabar [nama game/map] [jam]`")

    tokens = arg.strip()
    w_match = re.search(r"(?:\bjam\b|\bpukul\b|(?:\d{1,2}(?::|\.)?\d{0,2})|sekarang|now|besok)",
                        tokens, flags=re.IGNORECASE)
    if w_match:
        split_idx = w_match.start()
        map_name = tokens[:split_idx].strip(" ,.-")
        waktu_text = tokens[split_idx:].strip()
    else:
        map_name = tokens.strip(" ,.-")
        waktu_text = "sekarang"

    now = datetime.now()
    remind_at, when_str = parse_natural_time(waktu_text, now)

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
    await msg.add_reaction("✅")
    await msg.add_reaction("❌")

    def check(reaction, user):
        return (
            user == ctx.author
            and str(reaction.emoji) in ["✅", "❌"]
            and reaction.message.id == msg.id
        )

    try:
        reaction, _ = await bot.wait_for("reaction_add", timeout=60.0, check=check)
    except asyncio.TimeoutError:
        await msg.delete()
        return await ctx.send("⏰ Waktu konfirmasi habis, mabar dibatalkan.", delete_after=5)

    if str(reaction.emoji) == "❌":
        await msg.delete()
        return await ctx.send("❌ Mabar dibatalkan.", delete_after=5)

    await msg.delete()
    mabar_channel = bot.get_channel(CHANNEL_ID_MABAR)
    if not mabar_channel:
        return await ctx.send("❌ Channel mabar tidak ditemukan.")

    announce = (
        f"{role_light.mention}\n"
        f"🎮 Kalau nggak sibuk **jam {when_str}**, join mabar **{map_name.title()}**, yuk!"
    )
    announce_msg = await mabar_channel.send(announce)
    await ctx.send(f"✅ Pengumuman mabar dikirim ke <#{CHANNEL_ID_MABAR}>", delete_after=5)

    # Reminder & auto delete
    if remind_at > now + timedelta(minutes=1):
        delay = (remind_at - now).total_seconds()
        await asyncio.sleep(delay)
        try:
            await mabar_channel.send(
                f"{role_light.mention}\n⏰ Waktunya mabar **{map_name.title()}**! Siap-siap yuk 🎮"
            )
        except Exception as e:
            print("Error reminder:", e)

    # Auto-delete pengumuman 1 jam setelah lewat
    await asyncio.sleep((remind_at - now).total_seconds() + 3600)
    try:
        await announce_msg.delete()
    except Exception as e:
        print("Gagal hapus pengumuman:", e)


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
            print(f"Error menjalankan bot: {e}")
