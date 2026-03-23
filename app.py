"""
VOICEVOX Discord 読み上げBot
============================
機能:
  - テキストチャンネルのメッセージをVOICEVOXで読み上げ
  - VC自動参加/退出
  - 話者変更コマンド
  - 読み上げスキップ
  - URL・メンション・絵文字の自動省略
  - キューイング(複数メッセージの順次再生)
"""
import asyncio
import io
import os
import re
import urllib.parse
from collections import defaultdict
from typing import Optional

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ───────────────────────── 設定 ─────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_TOKEN_HERE")
VOICEVOX_URL  = os.getenv("VOICEVOX_URL", "http://localhost:50021")
print("TOKEN:", DISCORD_TOKEN)

# デフォルト話者ID (0=四国めたん, 1=ずんだもん, 2=春日部つむぎ…)
DEFAULT_SPEAKER = int(os.getenv("DEFAULT_SPEAKER", "1"))

# 読み上げる最大文字数
MAX_TEXT_LENGTH = int(os.getenv("MAX_TEXT_LENGTH", "100"))

# ───────────────────────── Bot 初期化 ─────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="v!", intents=intents)

# ── ギルドごとの状態管理 ──
# { guild_id: speaker_id }
guild_speaker: dict[int, int] = defaultdict(lambda: DEFAULT_SPEAKER)
user_speaker: dict[int, int] = {}
# { guild_id: asyncio.Queue }
guild_queue: dict[int, asyncio.Queue] = {}

# { guild_id: channel_id }  読み上げ対象テキストチャンネル
guild_text_channel: dict[int, int] = {}

# { guild_id: asyncio.Task }
guild_task: dict[int, asyncio.Task] = {}


# ─────────────────────── VOICEVOX ヘルパー ─────────────────
async def voicevox_synthesize(text: str, speaker: int) -> Optional[bytes]:
    """VOICEVOXでテキストを音声合成してWAVバイト列を返す"""
    encoded = urllib.parse.quote(text)
    query_url = f"{VOICEVOX_URL}/audio_query?text={encoded}&speaker={speaker}"
    synth_url = f"{VOICEVOX_URL}/synthesis?speaker={speaker}"

    async with aiohttp.ClientSession() as session:
        # 1. audio_query
        async with session.post(query_url) as resp:
            if resp.status != 200:
                return None
            query = await resp.json()

        # 2. synthesis
        async with session.post(synth_url, json=query,
                                headers={"Content-Type": "application/json"}) as resp:
            if resp.status != 200:
                return None
            return await resp.read()


async def voicevox_speakers() -> list[dict]:
    """利用可能な話者一覧を返す"""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{VOICEVOX_URL}/speakers") as resp:
            if resp.status == 200:
                return await resp.json()
    return []


# ─────────────────────── テキスト前処理 ──────────────────────
_URL_RE      = re.compile(r"https?://\S+")
_MENTION_RE  = re.compile(r"<@!?\d+>|<@&\d+>|<#\d+>")
_EMOJI_RE    = re.compile(r"<a?:\w+:\d+>")
_CODEBLOCK_RE = re.compile(r"```[\s\S]*?```|`[^`]+`")


def preprocess(text: str) -> str:
    text = _CODEBLOCK_RE.sub("コード省略", text)
    text = _URL_RE.sub("URL省略", text)
    text = _MENTION_RE.sub("メンション", text)
    text = _EMOJI_RE.sub("", text)
    text = text.strip()
    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH] + "、以下省略"
    return text


# ──────────────────────── キューワーカー ─────────────────────
async def queue_worker(guild: discord.Guild):
    """キューからテキストを取り出して順番に再生する"""
    q = guild_queue[guild.id]
    while True:
        text, speaker = await q.get()
        vc: discord.VoiceClient = guild.voice_client
        if vc is None or not vc.is_connected():
            q.task_done()
            continue

        wav = await voicevox_synthesize(text, speaker)
        if wav is None:
            print(f"[WARN] 音声合成失敗: {text!r}")
            q.task_done()
            continue

        # 再生が終わるまで待機
        done_event = asyncio.Event()

        def after(_):
            bot.loop.call_soon_threadsafe(done_event.set)

        source = discord.FFmpegPCMAudio(
            io.BytesIO(wav), pipe=True,
            options="-vn"
        )
        vc.play(source, after=after)
        await done_event.wait()
        q.task_done()


def ensure_worker(guild: discord.Guild):
    """ギルド用のキューとワーカータスクを確保する"""
    if guild.id not in guild_queue:
        guild_queue[guild.id] = asyncio.Queue()
    task = guild_task.get(guild.id)
    if task is None or task.done():
        guild_task[guild.id] = asyncio.create_task(queue_worker(guild))


# ────────────────────────── イベント ─────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (id={bot.user.id})")
    print(f"   VOICEVOX: {VOICEVOX_URL}")
    print(f"   デフォルト話者: {DEFAULT_SPEAKER}")
@bot.command(name="usersp")
async def cmd_user_speaker(ctx: commands.Context, speaker_id: int):
    user_speaker[ctx.author.id] = speaker_id
    await ctx.send(f" {ctx.author.display_name} さんの話者を `{speaker_id}` に設定しました。")


@bot.event
async def on_message(message: discord.Message):
    # Bot自身・コマンドは無視
    if message.author.bot:
        return
    await bot.process_commands(message)

    # コマンドメッセージは読み上げない
    ctx = await bot.get_context(message)
    if ctx.valid:
        return

    guild = message.guild
    if guild is None:
        return

    # 読み上げ対象チャンネルチェック
    target_ch = guild_text_channel.get(guild.id)
    if target_ch is None or message.channel.id != target_ch:
        return

    # VCに接続していないなら何もしない
    vc = guild.voice_client
    if vc is None or not vc.is_connected():
        return

    text = preprocess(message.content)
    if not text:
        return
    

    speaker = user_speaker.get(message.author.id, guild_speaker[guild.id])
    #speaker = guild_speaker[guild.id]
    ensure_worker(guild)
    await guild_queue[guild.id].put((text, speaker))


@bot.event
async def on_voice_state_update(member: discord.Member,
                                before: discord.VoiceState,
                                after: discord.VoiceState):
    """Botだけが残ったVCから自動退出"""
    guild = member.guild
    vc = guild.voice_client
    if vc is None:
        return
    if len(vc.channel.members) == 1 and vc.channel.members[0].id == bot.user.id:
        await vc.disconnect()
        guild_text_channel.pop(guild.id, None)
        print(f"[INFO] 誰もいないので {guild.name} のVCから退出しました")


# ────────────────────────── コマンド ─────────────────────────
@bot.command(name="join", aliases=["j"])
async def cmd_join(ctx: commands.Context):
    """VCに参加して読み上げを開始する"""
    if ctx.author.voice is None:
        await ctx.send("❌ 先にボイスチャンネルに入ってください。")
        return

    channel = ctx.author.voice.channel
    vc = ctx.guild.voice_client

    if vc is not None and vc.is_connected():
        if vc.channel.id == channel.id:
            await ctx.send("✅ すでに同じVCにいます。")
        else:
            await vc.move_to(channel)
            await ctx.send(f"✅ {channel.name} に移動しました。")
    else:
        await channel.connect()
        await ctx.send(f"✅ {channel.name} に参加しました。")

    guild_text_channel[ctx.guild.id] = ctx.channel.id
    ensure_worker(ctx.guild)
    await ctx.send(f"📢 このチャンネル `{ctx.channel.name}` の発言を読み上げます。")


@bot.command(name="leave", aliases=["l", "bye"])
async def cmd_leave(ctx: commands.Context):
    """VCから退出する"""
    vc = ctx.guild.voice_client
    if vc is None or not vc.is_connected():
        await ctx.send("❌ VCに接続していません。")
        return
    await vc.disconnect()
    guild_text_channel.pop(ctx.guild.id, None)

    # キューをクリア
    q = guild_queue.get(ctx.guild.id)
    if q:
        while not q.empty():
            q.get_nowait()
            q.task_done()

    await ctx.send("👋 退出しました。")


@bot.command(name="skip", aliases=["s"])
async def cmd_skip(ctx: commands.Context):
    """再生中の音声をスキップする"""
    vc = ctx.guild.voice_client
    if vc is None or not vc.is_playing():
        await ctx.send("❌ 再生中ではありません。")
        return
    vc.stop()
    await ctx.send("⏭️ スキップしました。")


@bot.command(name="speaker", aliases=["sp"])
async def cmd_speaker(ctx: commands.Context, speaker_id: Optional[int] = None):
    """話者を変更する。IDなしで一覧表示。"""
    if speaker_id is None:
        # 話者一覧を表示
        speakers = await voicevox_speakers()
        if not speakers:
            await ctx.send("❌ VOICEVOXに接続できません。")
            return
        lines = ["**🎙️ 利用可能な話者一覧**"]
        for s in speakers:
            for style in s.get("styles", []):
                lines.append(f"`{style['id']:3d}` {s['name']} ({style['name']})")
        # 長すぎる場合は分割送信
        msg = "\n".join(lines)
        if len(msg) > 1900:
            for i in range(0, len(lines), 30):
                await ctx.send("\n".join(lines[i:i+30]))
        else:
            await ctx.send(msg)
        current = guild_speaker[ctx.guild.id]
        await ctx.send(f"現在の話者ID: `{current}`")
        return

    guild_speaker[ctx.guild.id] = speaker_id
    await ctx.send(f"✅ 話者を ID `{speaker_id}` に変更しました。")


@bot.command(name="say")
async def cmd_say(ctx: commands.Context, *, text: str):
    """指定テキストを即座に読み上げキューへ追加する"""
    vc = ctx.guild.voice_client
    if vc is None or not vc.is_connected():
        await ctx.send("❌ 先に `v!join` でVCに参加してください。")
        return
    processed = preprocess(text)
    if not processed:
        await ctx.send("❌ 読み上げるテキストがありません。")
        return
    speaker = user_speaker.get(ctx.author.id, guild_speaker[ctx.guild.id])
    ensure_worker(ctx.guild)
    await guild_queue[ctx.guild.id].put((processed, speaker))

    #speaker = guild_speaker[ctx.guild.id]
    #ensure_worker(ctx.guild)
    #await guild_queue[ctx.guild.id].put((processed, speaker))
    await ctx.message.add_reaction("🔊")


@bot.command(name="channel", aliases=["ch"])
async def cmd_channel(ctx: commands.Context):
    """読み上げ対象チャンネルをこのチャンネルに変更する"""
    vc = ctx.guild.voice_client
    if vc is None or not vc.is_connected():
        await ctx.send("❌ 先に `v!join` でVCに参加してください。")
        return
    guild_text_channel[ctx.guild.id] = ctx.channel.id
    await ctx.send(f"✅ 読み上げチャンネルを `{ctx.channel.name}` に変更しました。")

@bot.event
async def on_voice_state_update(member: discord.Member,
                                before: discord.VoiceState,
                                after: discord.VoiceState):

    guild = member.guild
    vc = guild.voice_client

    # --- 入室読み上げ ---
    # before.channel: 以前のVC
    # after.channel:  現在のVC
    if before.channel is None and after.channel is not None:
        # Bot自身は無視
        if member.id != bot.user.id:
            # 読み上げ対象チャンネルが設定されているか
            target_ch = guild_text_channel.get(guild.id)
            if target_ch is not None and vc and vc.is_connected():
                text = f"{member.display_name} さんが参加しました"
                speaker = guild_speaker[guild.id]
                ensure_worker(guild)
                await guild_queue[guild.id].put((text, speaker))
    if before.channel is not None and after.channel is None:
        if member.id != bot.user.id:
            target_ch = guild_text_channel.get(guild.id)
            if target_ch is not None and vc and vc.is_connected():
                text = f"{member.display_name} さんが退出しました"
                speaker = guild_speaker[guild.id]
                ensure_worker(guild)
                await guild_queue[guild.id].put((text, speaker))
    # --- 既存の「Botだけ残ったら退出」処理 ---
    if vc is None:
        return
    if len(vc.channel.members) == 1 and vc.channel.members[0].id == bot.user.id:
        await vc.disconnect()
        guild_text_channel.pop(guild.id, None)
        print(f"[INFO] 誰もいないので {guild.name} のVCから退出しました")

@bot.command(name="status")
async def cmd_status(ctx: commands.Context):
    """Botの現在の状態を表示する"""
    vc = ctx.guild.voice_client
    vc_name = vc.channel.name if (vc and vc.is_connected()) else "未接続"
    ch_id = guild_text_channel.get(ctx.guild.id)
    ch_name = f"<#{ch_id}>" if ch_id else "未設定"
    speaker = guild_speaker[ctx.guild.id]
    q = guild_queue.get(ctx.guild.id)
    q_size = q.qsize() if q else 0

    embed = discord.Embed(title="📊 Bot ステータス", color=0x00bfff)
    embed.add_field(name="VC",       value=vc_name,  inline=True)
    embed.add_field(name="読み上げch", value=ch_name, inline=True)
    embed.add_field(name="話者ID",   value=str(speaker), inline=True)
    embed.add_field(name="キュー数", value=str(q_size),  inline=True)
    embed.add_field(name="VOICEVOX", value=VOICEVOX_URL,  inline=False)
    await ctx.send(embed=embed)


@bot.command(name="help_vv", aliases=["h"])
async def cmd_help(ctx: commands.Context):
    """コマンド一覧を表示する"""
    embed = discord.Embed(title="🤖 VOICEVOX Bot コマンド一覧",
                          color=0x57f287)
    cmds = [
        ("v!join / v!j",        "VCに参加して読み上げ開始"),
        ("v!leave / v!l / v!bye","VCから退出"),
        ("v!skip / v!s",        "再生中の音声をスキップ"),
        ("v!speaker / v!sp [ID]","話者変更 (IDなしで一覧)"),
        ("v!say <テキスト>",   "テキストを直接読み上げ"),
        ("v!channel / v!ch",    "読み上げチャンネルを変更"),
        ("v!status",           "現在の状態を表示"),
        ("v!help_vv / v!h",     "このヘルプを表示"),
    ]
    for name, desc in cmds:
        embed.add_field(name=f"`{name}`", value=desc, inline=False)
    await ctx.send(embed=embed)


# ──────────────────────────── 起動 ───────────────────────────
if __name__ == "__main__":
    bot.run("")
