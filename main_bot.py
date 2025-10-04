import os
import discord
from discord.ext import commands

# Ambil token dari environment variable (Railway → Variables)
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# ID channel untuk welcome dan logs
CHANNEL_ID_WELCOME = 1423964756158447738  # ganti dengan ID channel welcome
CHANNEL_ID_LOGS = 1423969192389902339    # ganti dengan ID channel logs

# Intents
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

KONTEN_LIMIT = 1000

@bot.event
async def on_ready():
    print(f"Bot sudah login sebagai {bot.user}")
    await bot.change_presence(
        activity=discord.Game("menjaga server ✨")
    )

@bot.event
async def on_member_join(member):
    try:
        channel = bot.get_channel(CHANNEL_ID_WELCOME)
        if channel:
            embed = discord.Embed(
                title="🎉 Selamat Datang!",
                description=f"Halo {member.mention}, selamat bergabung di {member.guild.name}!",
                color=discord.Color.green()
            )
            await channel.send(embed=embed)
    except Exception as e:
        print(f"Error on_member_join: {e}")

@bot.event
async def on_member_remove(member):
    try:
        channel = bot.get_channel(CHANNEL_ID_WELCOME)
        if channel:
            embed = discord.Embed(
                title="👋 Selamat Tinggal",
                description=f"{member.display_name} telah keluar dari server.",
                color=discord.Color.red()
            )
            await channel.send(embed=embed)
    except Exception as e:
        print(f"Error on_member_remove: {e}")

@bot.event
async def on_message_delete(message):
    try:
        if message.author.bot:
            return

        log_channel = bot.get_channel(CHANNEL_ID_LOGS)
        if not log_channel:
            return

        konten_pesan = (message.content or "")[:KONTEN_LIMIT]
        if len(message.content or "") > KONTEN_LIMIT:
            konten_pesan += "..."
        konten_pesan = konten_pesan.replace("```", "")

        embed = discord.Embed(
            title="🗑️ Pesan Dihapus",
            color=discord.Color.orange()
        )
        embed.add_field(name="Pengirim", value=message.author.mention, inline=False)
        embed.add_field(name="Channel", value=message.channel.mention, inline=False)
        embed.add_field(name="Konten Pesan", value="```" + konten_pesan + "```", inline=False)

        await log_channel.send(embed=embed)
    except Exception as e:
        print(f"Error on_message_delete: {e}")

@bot.command()
async def ping(ctx):
    await ctx.send(f"Pong! {round(bot.latency * 1000)}ms")

# Jalankan bot
if __name__ == "__main__":
    if not TOKEN:
        print("❌ Token tidak ditemukan. Set environment variable DISCORD_BOT_TOKEN di Railway!")
    else:
        try:
            bot.run(TOKEN)
        except discord.LoginFailure:
            print("❌ Token invalid. Pastikan DISCORD_BOT_TOKEN benar.")
        except Exception as e:
            print(f"Error menjalankan bot: {e}")
