# === main_bot.py ===
import os
import io
import re
import aiohttp
import asyncio
from datetime import datetime
import discord
from discord.ext import commands
import firebase_admin
from firebase_admin import credentials, firestore

# === KONFIGURASI ===
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Channel dan Role ID
CHANNEL_ID_WELCOME = 1423964756158447738
CHANNEL_ID_LOGS = 1423969192389902339       # moderator / admin
CHANNEL_ID_PUBLIC = 1424032583519567952      # channel publik deteksi link
CHANNEL_ID_DOWNLOADER = 1425023771185774612  # channel downloader
ROLE_ID_LIGHT = 1424026593143164958

# === FIREBASE SETUP ===
cred = credentials.Certificate("firebase-key.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# === INTENTS ===
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# === FIRESTORE UTIL ===
async def get_downloader_status():
    ref = db.collection("config").document("downloader")
    doc = ref.get()
    if doc.exists:
        return doc.to_dict()
    else:
        return {"status": "off", "info_message_id": None}

async def set_downloader_status(status: str, info_message_id=None):
    ref = db.collection("config").document("downloader")
    data = {"status": status, "last_updated": datetime.now().isoformat()}
    if info_message_id:
        data["info_message_id"] = info_message_id
    ref.set(data, merge=True)

async def log_download(user_id, platform, url, file_size, status, batch_total=1, batch_index=1):
    ref = db.collection("downloads").document()
    ref.set({
        "user_id": user_id,
        "platform": platform,
        "url": url,
        "file_size": file_size,
        "status": status,
        "batch_total": batch_total,
        "batch_index": batch_index,
        "timestamp": datetime.now().isoformat()
    })

# === EMBED PANDUAN ===
def downloader_embed():
    embed = discord.Embed(
        title="🎥 ESKA Media Downloader",
        description=(
            "Cukup kirimkan tautan postingan di sini —\n"
            "bot akan otomatis membuat **thread pribadi khusus untukmu 🤫**\n"
            "*(hanya kamu dan bot yang dapat melihat percakapan tersebut)*.\n\n"
            "📦 **Maksimum ukuran media:** 25 MB\n"
            "Lebih dari itu, bot akan mengirimkan tautan unduhan.\n\n"
            "🟢 *Fitur ini aktif untuk member dengan role 🔆 Light.*"
        ),
        color=discord.Color.green()
    )
    embed.set_footer(text="Powered by ESKA Downloader System")
    return embed

# === ON READY ===
@bot.event
async def on_ready():
    print(f"✅ Bot login sebagai {bot.user}")
    await bot.change_presence(activity=discord.Game("melayani downloader ✨"))

# === COMMAND: TOGGLE DOWNLOADER ===
@bot.command()
@commands.has_permissions(administrator=True)
async def downloader(ctx, action: str):
    if ctx.channel.id != CHANNEL_ID_LOGS:
        return await ctx.send("⚠️ Perintah ini hanya bisa di channel moderator/log.")

    status_data = await get_downloader_status()
    channel = bot.get_channel(CHANNEL_ID_DOWNLOADER)

    if action.lower() == "on":
        info_message_id = status_data.get("info_message_id")
        if info_message_id:
            await set_downloader_status("on")
            await ctx.send("✅ Downloader diaktifkan kembali tanpa membuat pesan baru.")
            return

        embed = downloader_embed()
        msg = await channel.send(embed=embed)
        await msg.pin()
        await set_downloader_status("on", msg.id)
        await ctx.send("✅ Downloader diaktifkan dan panduan dikirim ke channel downloader.")

    elif action.lower() == "off":
        await set_downloader_status("off")
        await ctx.send("🛑 Downloader telah dinonaktifkan oleh moderator.")
    else:
        await ctx.send("Gunakan `!downloader on` atau `!downloader off`.")

# === DETEKSI LINK DI CHANNEL PUBLIK ===
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.id == CHANNEL_ID_PUBLIC:
        if re.search(r"(instagram\.com|tiktok\.com)", message.content):
            reply = await message.reply(
                f"Hola {message.author.mention}, mau download medianya? "
                f"Yuk ke <#{CHANNEL_ID_DOWNLOADER}> 🎥",
                delete_after=300
            )
            await asyncio.sleep(300)
            await reply.delete()
            return

    # Downloader channel logic
    if message.channel.id == CHANNEL_ID_DOWNLOADER:
        status_data = await get_downloader_status()
        if status_data.get("status") != "on":
            return await message.channel.send(
                "⚠️ Downloader sedang nonaktif oleh moderator.", delete_after=10
            )

        role_light = discord.utils.get(message.guild.roles, id=ROLE_ID_LIGHT)
        if role_light not in message.author.roles:
            return await message.reply(
                f"⚠️ Fitur ini hanya untuk member dengan role {role_light.mention}.",
                delete_after=10
            )

        if re.search(r"(instagram\.com|tiktok\.com)", message.content):
            await handle_downloader_thread(message)
            return

    await bot.process_commands(message)

# === BUAT THREAD PRIVAT ===
async def handle_downloader_thread(message):
    user = message.author
    link = message.content.strip()

    thread = await message.channel.create_thread(
        name=f"📥 Downloader – {user.display_name}",
        type=discord.ChannelType.private_thread,
        invitable=False
    )
    await thread.add_user(user)
    await thread.send(
        f"Halo {user.mention}, kirimkan tautan di sini ya.\n"
        f"Aku akan kirim media langsung jika ukuran ≤ 25 MB, selebihnya lewat link. 🎬"
    )

    def check(m):
        return m.author == user and re.search(r"(instagram\.com|tiktok\.com)", m.content)

    try:
        msg = await bot.wait_for("message", timeout=120, check=check)
        await process_media_download(msg, thread)
    except asyncio.TimeoutError:
        await thread.send("⏰ Tidak ada aktivitas, thread akan ditutup.")
        await asyncio.sleep(10)
        await thread.delete()

# === PROSES DOWNLOAD MEDIA ===
async def process_media_download(message, thread):
    url = message.content.strip()
    platform = "instagram" if "instagram.com" in url else "tiktok"
    api = f"https://api.ryzumi.vip/api/downloader/{'igdl' if platform=='instagram' else 'ttdl'}?url={url}"

    await thread.send("⏳ Sedang mengambil media, mohon tunggu...")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api) as resp:
                data = await resp.json()

        if platform == "instagram":
            if not data.get("status"):
                await thread.send("⚠️ Gagal mengambil media Instagram.")
                return

            media_list = data.get("data", [])
            total = len(media_list)
            sent_count = 0
            total_size = 0

            for i, media in enumerate(media_list, start=1):
                media_url = media["url"]
                async with aiohttp.ClientSession() as s:
                    async with s.get(media_url) as r:
                        if r.status != 200:
                            await thread.send(f"⚠️ Gagal unduh media #{i}.")
                            await log_download(message.author.id, platform, url, 0, "download_fail", total, i)
                            continue
                        raw = await r.read()

                size_mb = len(raw) / 1024 / 1024
                total_size += size_mb
                if size_mb > 25:
                    await thread.send(f"⚠️ Media #{i} terlalu besar ({size_mb:.1f} MB). [Lihat di sini]({media_url})")
                    await log_download(message.author.id, platform, url, size_mb, "oversize", total, i)
                else:
                    filename = f"{platform}_{i}.mp4"
                    file = discord.File(io.BytesIO(raw), filename=filename)
                    await thread.send(file=file)
                    await log_download(message.author.id, platform, url, size_mb, "success", total, i)
                    sent_count += 1
                await asyncio.sleep(2)  # jeda aman antar upload

            await thread.send(
                f"✅ Semua media berhasil diproses ({sent_count}/{total}) — total {total_size:.1f} MB.\n"
                "Pilih 🔁 untuk unduh lagi atau ✅ untuk menutup thread."
            )

        else:
            if not data.get("success"):
                await thread.send("⚠️ Gagal mengambil media TikTok.")
                return
            media_url = data["data"]["data"]["play"]

            async with aiohttp.ClientSession() as s:
                async with s.get(media_url) as r:
                    if r.status != 200:
                        await thread.send("⚠️ Gagal unduh video TikTok.")
                        await log_download(message.author.id, platform, url, 0, "download_fail")
                        return
                    raw = await r.read()

            size_mb = len(raw) / 1024 / 1024
            if size_mb > 25:
                await thread.send(f"⚠️ File terlalu besar ({size_mb:.1f} MB). [Lihat di sini]({media_url})")
                await log_download(message.author.id, platform, url, size_mb, "oversize")
            else:
                filename = "tiktok.mp4"
                file = discord.File(io.BytesIO(raw), filename=filename)
                await thread.send(file=file)
                await log_download(message.author.id, platform, url, size_mb, "success")

            await thread.send("✅ Unduhan selesai! Pilih 🔁 untuk unduh lagi, ✅ untuk tutup thread.")

        # === MENU REACTION ===
        confirm_msg = await thread.send("🔁 / ✅ ?")
        await confirm_msg.add_reaction("🔁")
        await confirm_msg.add_reaction("✅")

        def check_react(reaction, user):
            return (
                user == message.author
                and str(reaction.emoji) in ["✅", "🔁"]
                and reaction.message.id == confirm_msg.id
            )

        try:
            reaction, _ = await bot.wait_for("reaction_add", timeout=60.0, check=check_react)
            if str(reaction.emoji) == "✅":
                await thread.send("👋 Thread akan ditutup dalam 30 detik...")
                await asyncio.sleep(30)
                await thread.delete()
            else:
                await thread.send("🔁 Kirim tautan lain untuk diunduh!")
        except asyncio.TimeoutError:
            await thread.send("⏰ Tidak ada respons, thread akan ditutup.")
            await asyncio.sleep(10)
            await thread.delete()

    except Exception as e:
        await thread.send(f"🚫 Terjadi error: `{e}`")
        await log_download(message.author.id, platform, url, 0, f"error: {e}")

# === RUN ===
if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_BOT_TOKEN tidak ditemukan.")
    else:
        bot.run(TOKEN)
