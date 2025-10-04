import discord
from discord.ext import commands

# --- KONFIGURASI BOT (HARDCODED) ---
# PERHATIAN: MENYIMPAN TOKEN DI KODE BERSIFAT SANGAT TIDAK AMAN.
# Segera ganti praktik ini dengan os.getenv('DISCORD_BOT_TOKEN') setelah berhasil.
TOKEN = 'MTQyMzk4MDMxODQ5NDI5NDAzNg.GTfjMg.n0JNshPpbMwaWfcOmJSivOHdvjaCgmKx4tPHCc'

# ID Channel (Disesuaikan dengan input Tian)
CHANNEL_ID_WELCOME = 1423964756158447738
CHANNEL_ID_LOGS = 1423969192389902339

# --- PENGATURAN INTENTS ---
# Membutuhkan intent 'members' dan 'message_content' diaktifkan di Discord Developer Portal!
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
        # Mengambil channel welcome
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
    """Mencatat pesan yang dihapus (Basic Logging). Syntax error sebelumnya sudah diperbaiki di sini."""
    
    # Abaikan pesan dari bot atau pesan tanpa konten
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
            
            # Logika untuk membatasi konten pesan
            KONTEN_LIMIT = 1000
            konten_pesan = message.content[:KONTEN_LIMIT]
            
            # Tambahkan elipsis jika pesan dipotong
            if len(message.content) > KONTEN_LIMIT:
                konten_pesan += "..."

            # KOREKSI SINTAKS F-STRING: Variabel {konten_pesan} sudah dibungkus kurawal dengan benar
            embed.add_field(name="Konten Pesan", value=f"```{konten_pesan}```", inline=False)
            
            # Tambahkan detail penting lainnya
            embed.set_footer(text=f"ID Pengguna: {message.author.id} | ID Pesan: {message.id}")
            embed.timestamp = message.created_at # Waktu asli pesan dibuat

            await log_channel.send(embed=embed)
        else:
            print(f"❌ Channel Logs ID {CHANNEL_ID_LOGS} tidak ditemukan.")
            
    except Exception as e:
        print(f"Error Logging Message Delete: {e}")

# --- COMMANDS (Contoh) ---
@bot.command(name='ping')
async def ping(ctx):
    """Menanggapi dengan latency bot."""
    latency = round(bot.latency * 1000)
    await ctx.send(f'Pong! Latency: {latency}ms')

# --- JALANKAN BOT ---
if TOKEN:
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        print("❌ Login Gagal: Pastikan TOKEN Discord kamu valid dan benar. (Periksa kembali token yang di-hardcode)")
    except Exception as e:
        print(f"❌ Terjadi kesalahan saat menjalankan bot: {e}")
else:
    print("❌ Bot tidak dapat dijalankan karena TOKEN tidak tersedia.")
