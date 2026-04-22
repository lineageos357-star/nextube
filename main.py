import discord
from discord import ui, app_commands
from discord.ext import commands
import os
import yt_dlp
import aiohttp
import asyncio
import re
import uuid
import threading
from flask import Flask

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN = os.getenv("TOKEN")
PORT = int(os.getenv("PORT", 8080))

DOWNLOAD_DIR = "./downloads"
GOFILE_EXPIRY_DAYS = 10
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ── Flask keepalive ───────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    return "NexTube is running!", 200

@app.route("/health")
def health():
    return "OK", 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

def keep_alive():
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()

# ── Bot Setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)


# ── URL Validator ─────────────────────────────────────────────────────────────
def is_youtube_url(url: str) -> bool:
    pattern = r"(https?://)?(www\.)?(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)[\w\-]+"
    return bool(re.match(pattern, url.strip()))


# ── Shared yt-dlp options ─────────────────────────────────────────────────────
BASE_YDL_OPTS = {
    "quiet": True,
    "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    },
}


# ── Fetch Video Info ──────────────────────────────────────────────────────────
def fetch_video_info(url: str) -> dict:
    ydl_opts = {**BASE_YDL_OPTS, "skip_download": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


# ── Download Media ────────────────────────────────────────────────────────────
def download_media(url: str, fmt: str, quality: str, output_path: str) -> str:
    if fmt == "mp3":
        ydl_opts = {
            **BASE_YDL_OPTS,
            "format": "bestaudio/best",
            "outtmpl": output_path + ".%(ext)s",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": quality,
            }],
        }
    else:
        ydl_opts = {
            **BASE_YDL_OPTS,
            "format": f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]",
            "outtmpl": output_path + ".%(ext)s",
            "merge_output_format": "mp4",
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    for ext in ["mp3", "mp4", "webm", "mkv"]:
        candidate = f"{output_path}.{ext}"
        if os.path.exists(candidate):
            return candidate

    raise FileNotFoundError("Download failed — output file not found.")


# ── GoFile Upload ─────────────────────────────────────────────────────────────
async def upload_to_gofile(file_path: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.get("https://api.gofile.io/servers") as r:
            data = await r.json()
            server = data["data"]["servers"][0]["name"]

        with open(file_path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("file", f, filename=os.path.basename(file_path))
            async with session.post(f"https://{server}.gofile.io/uploadfile", data=form) as r:
                result = await r.json()

    if result["status"] != "ok":
        raise Exception("GoFile upload failed.")

    return result["data"].get("directLink") or result["data"]["downloadPage"]


# ── Formatters ────────────────────────────────────────────────────────────────
def fmt_number(n) -> str:
    if n is None:
        return "N/A"
    n = int(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def fmt_duration(seconds) -> str:
    if not seconds:
        return "N/A"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


# ── Selects ───────────────────────────────────────────────────────────────────
class FormatSelect(ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Choose a format...",
            options=[
                discord.SelectOption(label="MP4 (Video)", value="mp4", emoji="🎬"),
                discord.SelectOption(label="MP3 (Audio)", value="mp3", emoji="🎵"),
            ],
        )


class QualitySelect(ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Choose quality...",
            options=[
                discord.SelectOption(label="360p  /  128kbps", value="360|128"),
                discord.SelectOption(label="480p  /  192kbps", value="480|192"),
                discord.SelectOption(label="720p  /  192kbps", value="720|192", default=True),
                discord.SelectOption(label="1080p  /  320kbps", value="1080|320"),
            ],
        )


# ── Modal ─────────────────────────────────────────────────────────────────────
class YTModal(ui.Modal, title="🎬 NexTube Downloader"):
    url_input = ui.TextInput(
        label="YouTube URL",
        placeholder="https://www.youtube.com/watch?v=...",
        required=True,
        max_length=200,
    )
    format_label = ui.Label(
        text="Format",
        component=FormatSelect(),
    )
    quality_label = ui.Label(
        text="Quality  (left = MP4 · right = MP3)",
        component=QualitySelect(),
    )

    async def on_submit(self, interaction: discord.Interaction):
        url      = self.url_input.value.strip()
        fmt      = self.format_label.component.values[0]
        qual_raw = self.quality_label.component.values[0]
        mp4_q, mp3_q = qual_raw.split("|")
        qual     = mp4_q if fmt == "mp4" else mp3_q

        if not is_youtube_url(url):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Invalid URL",
                    description=(
                        "That doesn't look like a valid YouTube link.\n"
                        "Make sure it starts with `youtube.com/watch?v=` or `youtu.be/`."
                    ),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            loop = asyncio.get_event_loop()

            info            = await loop.run_in_executor(None, fetch_video_info, url)
            title           = info.get("title", "Unknown Title")
            thumbnail       = info.get("thumbnail", "")
            views           = fmt_number(info.get("view_count"))
            likes           = fmt_number(info.get("like_count"))
            uploader        = info.get("uploader", "Unknown")
            duration        = fmt_duration(info.get("duration"))
            upload_date_raw = info.get("upload_date", "")
            upload_date     = (
                f"{upload_date_raw[6:8]}/{upload_date_raw[4:6]}/{upload_date_raw[:4]}"
                if len(upload_date_raw) == 8 else "N/A"
            )

            uid         = str(uuid.uuid4())[:8]
            output_path = os.path.join(DOWNLOAD_DIR, uid)
            file_path   = await loop.run_in_executor(None, download_media, url, fmt, qual, output_path)

            download_url = await upload_to_gofile(file_path)
            os.remove(file_path)

            quality_label = f"{qual}kbps MP3" if fmt == "mp3" else f"{qual}p MP4"

            embed = discord.Embed(title=title, url=url, color=discord.Color.from_str("#FF0000"))
            embed.set_thumbnail(url=thumbnail)
            embed.add_field(name="👤 Channel",  value=uploader,      inline=True)
            embed.add_field(name="⏱ Duration",  value=duration,      inline=True)
            embed.add_field(name="📅 Uploaded",  value=upload_date,   inline=True)
            embed.add_field(name="👁 Views",     value=views,         inline=True)
            embed.add_field(name="👍 Likes",     value=likes,         inline=True)
            embed.add_field(name="🎚 Quality",   value=quality_label, inline=True)
            embed.add_field(
                name="📥 Download",
                value=f"[**Click here to download**]({download_url})",
                inline=False,
            )
            embed.set_footer(
                text=f"⚠️ Link expires after {GOFILE_EXPIRY_DAYS} days of inactivity • Powered by GoFile"
            )

            await interaction.followup.send(embed=embed)

        except yt_dlp.utils.DownloadError as e:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Download Failed",
                    description=f"yt-dlp couldn't process this video.\n```{str(e)[:200]}```",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Something went wrong",
                    description=f"```{str(e)[:300]}```",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )


# ── Slash Command ─────────────────────────────────────────────────────────────
@bot.tree.command(name="yt", description="Download a YouTube video or audio")
async def yt_command(interaction: discord.Interaction):
    await interaction.response.send_modal(YTModal())


# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user} — slash commands synced.")


# ── Run ───────────────────────────────────────────────────────────────────────
keep_alive()
bot.run(TOKEN)
