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
CHANNEL_ID_LOGS    = 1423969192389902339
RULES_CHANNEL_ID   = 1423969192389902336
ROLE_ID_LIGHT      = 1424026593143164958
CHANNEL_ID_MABAR   = 1424029336683679794

REACTION_EMOJI = "🔆"
LIGHT_MSG_ID = None  # Akan diisi oleh !setuplight

# === INTENTS ===
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
KONTEN_LIMIT = 1000


# === HELPER ===
async def send_light_embed(channel: discord.TextChannel) -> int:
    rules_mention = f"<#{RULES_CHANNEL_ID}>"
    embed = discord.Embed(
        title="✨ Pilih Role Light",
        description=(
            f"Klik reaksi {REACTION_EMOJI} di bawah untuk **mendapatkan role Light**.\n\n"
            f"Pastikan juga membaca {rules_mention} agar pengalamanmu nyaman dan tertib 😉"
        ),
        color=discord.Color.gold()
    )
    embed.set_footer(text="Klik sekali untuk ambil role, klik lagi untuk melepas.")
    msg = await channel.send(embed=embed)
    await msg.add_reaction(REACTION_EMOJI)
    return msg.id


# === EVENTS ===
@bot.event
async def on_ready():
    print(f"✅ Bot login sebagai {bot.user}")
    await bot.change_presence(activity=discord.Game("menjaga server ✨"))


@bot.event
async def on_member_join(member):
    channel = bot.get_channel(CHANNEL_ID_WELCOME)
    if channel:
        embed = discord.Embed(
            title="🎉 Selamat Datang!",
            description=f"Halo {member.mention}, selamat datang di **{member.guild.name}**!\n"
                        f"Jangan lupa baca {member.guild.get_channel(RULES_CHANNEL_ID).mention} ya.",
            color=discord.Color.green()
        )
        await channel.send(embed=embed)


@bot.event
async def on_member_remove(member):
    channel = bot.get_channel(CHANNEL_ID_WELCOME)
    if channel:
        embed = discord.Embed(
            title="👋 Selamat Tinggal",
            description=f"{member.display_name} telah keluar dari server.",
            color=discord.Color.red()
        )
        await channel.send(embed=embed)


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
        embed.add_field(name="Konten", value="```" + konten + "```", inline=False)
    await log_channel.send(embed=embed)


# === REACTION ROLE ===
@bot.event
async def on_raw_reaction_add(payload):
    if str(payload.emoji) != REACTION_EMOJI or payload.user_id == bot.user.id:
        return
    if payload.message_id != (LIGHT_MSG_ID or 0):
        return
    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    role = guild.get_role(ROLE_ID_LIGHT)
    if guild and member and role and role not in member.roles:
        await member.add_roles(role, reason="Reaction role: Light")


@bot.event
async def on_raw_reaction_remove(payload):
    if str(payload.emoji) != REACTION_EMOJI:
        return
    if payload.message_id != (LIGHT_MSG_ID or 0):
        return
    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    role = guild.get_role(ROLE_ID_LIGHT)
    if guild and member and role and role in member.roles:
        await member.remove_roles(role, reason="Reaction role: Light (remove)")


# === COMMANDS ===
@bot.command()
@commands.has_permissions(administrator=True)
async def setuplight(ctx):
    """Kirim pesan reaction-role di channel welcome"""
    channel = bot.get_channel(CHANNEL_ID_WELCOME)
    if not channel:
        return await ctx.send("❌ Channel welcome tidak ditemukan.")
    global LIGHT_MSG_ID
    LIGHT_MSG_ID = await send_light_embed(channel)
    await ctx.send(f"✅ Reaction-role sudah dibuat (Message ID: {LIGHT_MSG_ID})")


@bot.command()
async def ping(ctx):
    await ctx.send(f"Pong! {round(bot.latency * 1000)}ms")


# === MABAR COMMAND (dengan mention @Light) ===
@bot.command()
async def mabar(ctx, *, arg: str = None):
    """Contoh: !mabar Distrik Violence jam 7 sore"""
    role_light = ctx.guild.get_role(ROLE_ID_LIGHT)
    if not role_light:
        return await ctx.send("⚠️ Role Light belum diset di kode.")
    if role_light not in ctx.author.roles:
        return await ctx.send("❌ Kamu belum punya role Light untuk pakai perintah ini!")

    if not arg:
        return await ctx.send("Format salah!\nGunakan: `!mabar [nama map/game] [jam]`")

    pattern = r"(.+)\s+(?:jam|pukul)?\s*(.+)"
    match = re.match(pattern, arg, re.IGNORECASE)
    if not match:
        return await ctx.send("Format salah!\nContoh: `!mabar Distrik Violence jam 7 sore`")

    map_name = match.group(1).strip().title()
    waktu_raw = match.group(2).strip().lower()

    now = datetime.now()
    reminder_time = None
    waktu_str = waktu_raw

    if waktu_raw in ["now", "sekarang", "skrng"]:
        reminder_time = now
        waktu_str = "sekarang"
    else:
        jam_match = re.search(r"(\d{1,2})(?:[:.](\d{2}))?", waktu_raw)
        if jam_match:
            hour = int(jam_match.group(1))
            minute = int(jam_match.group(2) or 0)
            if "sore" in waktu_raw or "malam" in waktu_raw:
                if hour < 12:
                    hour += 12
            reminder_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if reminder_time < now:
                reminder_time += timedelta(days=1)
            waktu_str = reminder_time.strftime("%H:%M")
        else:
            reminder_time = now

    mabar_channel = bot.get_channel(CHANNEL_ID_MABAR)
    if not mabar_channel:
        return await ctx.send("❌ Channel mabar tidak ditemukan.")

    mention_role = role_light.mention
    announcement = f"{mention_role}\n🎮 Kalau nggak sibuk **jam {waktu_str}**, join mabar **{map_name}**, yuk!"
    await mabar_channel.send(announcement)
    await ctx.send(f"✅ Event mabar untuk **{map_name}** sudah diumumkan di <#{CHANNEL_ID_MABAR}>")

    if reminder_time > now + timedelta(minutes=1):
        delay = (reminder_time - now).total_seconds()
        await asyncio.sleep(delay)
        try:
            await mabar_channel.send(f"{mention_role}\n⏰ Waktunya mabar **{map_name}**! Siap-siap yuk 🎮")
        except Exception as e:
            print(f"Error mengirim reminder: {e}")


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
