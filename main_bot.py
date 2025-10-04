import os
import discord
from discord.ext import commands

# --- KONFIGURASI BOT ---
# Bot Token akan diambil dari Environment Variable bernama DISCORD_BOT_TOKEN
# Ini adalah langkah keamanan WAJIB saat hosting di Railway.
TOKEN = os.getenv('MTQyMzk4MDMxODQ5NDI5NDAzNg.GTfjMg.n0JNshPpbMwaWfcOmJSivOHdvjaCgmKx4tPHCc')

# ID Channel telah diperbarui sesuai input Tian:
CHANNEL_ID_WELCOME = 1423964756158447738
CHANNEL_ID_LOGS = 1423969192389902339

# --- PENGATURAN INTENTS ---
# Pastikan intents ini diaktifkan di Discord Developer Portal!
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

# Inisialisasi Bot
bot = commands.Bot(command_prefix='!', intents=intents)

# --- EVENT UTAMA ---

@bot.event
async def on_ready():
    """Event saat bot berhasil login dan siap digunakan."""
    print(f'✅ Bot siap! Terhubung sebagai: {bot.user.name} ({bot.user.id})')
    print('Bot Moderasi Anda sedang berjalan di Railway.')
    await bot.change_presence(activity=discord.Game(name="Memantau Ketertiban Server"))

@bot.event
async def on_member_join(member):
    """Mengirim pesan selamat datang."""
    try:
        channel = bot.get_channel(CHANNEL_ID_WELCOME)
        if channel:
            embed = discord.Embed(
                title="👋 Selamat Datang di EternalLights!",
                description=f"Hola, **{member.display_name}**! Jangan lupa baca #rules.",
                color=discord.Color.green()
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=f"Anggota ke-{member.guild.member_count}")
            await channel.send(embed=embed)
        else:
            print(f"❌ Channel Welcome ID {CHANNEL_ID_WELCOME} tidak ditemukan.")
    except Exception as e:
        print(f"Error Welcome Message: {e}")

@bot.event
async def on_member_remove(member):
    """Mengirim pesan perpisahan."""
    try:
        channel = bot.get_channel(CHANNEL_ID_WELCOME) 
        if channel:
            embed = discord.Embed(
                title="💔 Huhuhu... Sampai Jumpa",
                description=f"Anggota **{member.display_name}** telah meninggalkan server.",
                color=discord.Color.red()
            )
            await channel.send(embed=embed)
    except Exception as e:
        print(f"Error Farewell Message: {e}")

@bot.event
async def on_message_delete(message):
    """Mencatat pesan yang dihapus (Basic Logging)."""
    if message.author.bot or not message.content:
        return

    try:
        log_channel = bot.get_channel(CHANNEL_ID_LOGS)
        if log_channel:
            embed = discord.Embed(
                title="🗑️ Pesan Dihapus",
                description=f"Pesan yang dikirim oleh {message.author.mention} dihapus di {message.channel.mention}.",
                color=discord.Color.orange()
            )
            # Batasi konten pesan yang ditampilkan hingga 1000 karakter
            embed.add_field(name="Konten Pesan", value=f"
