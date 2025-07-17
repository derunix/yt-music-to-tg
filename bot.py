from dotenv import load_dotenv
import json
import redis.asyncio as redis
from collections import defaultdict
import re
from hashlib import sha256
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram import F
import os
load_dotenv()
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import FSInputFile
from aiogram.filters import Command
from yt_dlp import YoutubeDL

from functools import partial
SEARCH_RESULTS = {}
SEARCH_LAST_QUERY = {}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "your_bot_token_here")
ADMIN_USER_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.from_url(redis_url)

# Путь для временных файлов
DOWNLOAD_DIR = "/tmp/ytmusicbot"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

YDL_OPTS = {
    'format': 'bestaudio/best',
    'outtmpl': os.path.join(DOWNLOAD_DIR, '%(title).80s.%(ext)s'),
    'noplaylist': True,
    'quiet': False,
    'verbose': True,
    'logger': None,
    'cookiefile': 'cookies.txt',  # ← добавь эту строку
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
}

async def _search_and_reply(chat_id: int, raw_query: str):
    # Сохраняем исходный текст запроса
    SEARCH_LAST_QUERY[chat_id] = raw_query
    if ADMIN_USER_ID:
        user = await bot.get_chat(chat_id)
        await bot.send_message(ADMIN_USER_ID, f"🔍 Поиск: \"{raw_query}\" от @{user.username or user.full_name}")
    # Нормализация (дублируем текущую)
    query = raw_query.lower()
    query = re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060\u3000]', '', query)  # невидимые символы
    query = re.sub(r'[^\wа-яё0-9]+', ' ', query, flags=re.UNICODE)  # спецсимволы на пробел
    query = re.sub(r'\s+', ' ', query).strip()
    query = re.sub(r'ё', 'е', query)  # унификация кириллицы

    await bot.send_message(chat_id, f"Ищу: {query}...")

    max_results = 15
    search_url = f"ytsearch{max_results}:{query}"

    cached = await redis_client.get(f"search:{query}")
    if cached:
        entries = json.loads(cached)
        if entries:
            # Попытка загрузить метаданные видео из Redis по ключу video:{id} и использовать их, если доступны
            new_entries = []
            for e in entries:
                vid = e.get("id")
                if not vid:
                    new_entries.append(e)
                    continue
                cached_vid = await redis_client.get(f"video:{vid}")
                if cached_vid:
                    try:
                        new_entries.append(json.loads(cached_vid))
                        continue
                    except Exception:
                        pass
                new_entries.append(e)
            entries = new_entries

            SEARCH_RESULTS[chat_id] = entries
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=entry['title'][:60], callback_data=f"dl_{idx}")]
                    for idx, entry in enumerate(entries)
                ] + [[InlineKeyboardButton(text="🔄 Принудительно искать заново", callback_data="force_search")]]
            )
            await bot.send_message(chat_id, "Результаты из кэша. Выбери нужную песню или нажми кнопку ниже для повторного поиска:", reply_markup=kb)
            return

    try:
        info = None
        # Пробуем загрузить из кеша поисковый результат, если он есть
        cached_info = await redis_client.get(f"search_raw:{query}")
        if cached_info:
            try:
                info = json.loads(cached_info)
            except Exception:
                info = None

        if not info:
            with YoutubeDL({**YDL_OPTS, "extract_flat": "in_playlist"}) as ydl:
                info = ydl.extract_info(search_url, download=False)
                await redis_client.setex(f"search_raw:{query}", 1800, json.dumps(info))
        # Проверка, есть ли кэшированные метаданные для всех видео
        all_cached_entries = []
        uncached_entries = []
        for e in info.get("entries", []):
            vid = e.get("id")
            if not vid:
                uncached_entries.append(e)
                continue
            cache_key = f"video:{vid}"
            cached = await redis_client.get(cache_key)
            if cached:
                try:
                    all_cached_entries.append(json.loads(cached))
                    continue
                except Exception:
                    pass
            uncached_entries.append(e)
        info["entries"] = uncached_entries + all_cached_entries

        # Сохраняем метаданные каждого найденного видео в Redis (до фильтрации)
        for e in info.get("entries", []):
            vid = e.get("id")
            if not vid:
                continue
            cache_key = f"video:{vid}"
            if not await redis_client.exists(cache_key):
                minimal = {
                    "id": vid,
                    "title": e.get("title"),
                    "webpage_url": e.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
                    "duration": e.get("duration"),
                    "channel": e.get("channel"),
                    "channel_id": e.get("channel_id"),
                    "description": e.get("description"),
                    "source": "search_entry"
                }
                await redis_client.setex(cache_key, 86400, json.dumps(minimal))

        # --- фильтры (скопированы из прежнего search_and_download) ---
        filter_words_title = [
            "official", "official video", "clip", "lyrics", "audio", "track", "single", "remix", "full album", "album version", "hd",
            "официальный", "официальное видео", "клип", "лирика", "аудио", "песня", "сингл", "ремикс", "альбом", "видео", "трек",
            "акустика", "acoustic", "live",
            "живьем", "живое", "live version", "performance", "концерт", "вживую",
            "feat", "ft.", "auto-generated by youtube",
            "premiere", "debut", "original", "recording", "studio version", "официальная версия", "премьера", "дебют", "оригинал", "студийная версия"
        ]
        filter_words_channel = [
            "music", "музыка", "official", "официальный", "records", "рекордс", "sound", "звук", "audiovisual",
            "label", "лейбл", "entertainment", "media", "studio", "студия", "production", "tv",
            "artist", "channel", "музыкант", "проект",
            "видеоклип", "канал", "музыка видео", "официальный канал", "лэйбл", "звукозапись"
        ]
        block_words = [
            "разбор", "разбирает", "разобрал", "разбираю", "разбирать", "разборка",
            "обзор", "обзоры", "обозревает", "обозрение",
            "урок", "уроки", "обучение", "учебник", "how to", "lesson", "tutorial", "туториал", "как играть", "обучаю",
            "караоке", "минус", "минусовка", "playthrough",
            "reaction", "реакция", "реагирует", "анализ", "analysis", "review", "рецензия",
            "live stream", "live concert",  "трансляция",
            "стримил", "обсуждаем", "мнение", "комментарии", "болтовня", "подкаст", "reupload", "перезалив",
            "making of", "мейкинг", "бекстейдж", "backstage", "behind the scenes", "за кадром", "о создании", "съёмки", "making-of"
        ]
        filtered = [
            e for e in info['entries']
            if (e.get("duration") or 0) < 400 and not e.get("is_short")
            and not any(word in (e.get("title") or "").lower() for word in block_words)
            and not any(word in (e.get("description") or "").lower() for word in block_words)
            and (
                any(word in (e.get("title") or "").lower() for word in filter_words_title)
                or any(word in (e.get("channel") or "").lower() for word in filter_words_channel)
                or any(word in (e.get("channel_id") or "").lower() for word in filter_words_channel)
                or any(word in (e.get("description") or "").lower() for word in filter_words_channel + filter_words_title)
            )
        ]
        entries = []
        for e in filtered:
            vid = e.get("id")
            cache_key = f"video:{vid}"
            cached_vid = await redis_client.get(cache_key)
            if cached_vid:
                try:
                    entries.append(json.loads(cached_vid))
                    continue
                except Exception:
                    pass
            entries.append(e)
            if len(entries) == 5:
                break
        await redis_client.setex(f"search:{query}", 3600, json.dumps(entries))
        if not entries:
            await bot.send_message(chat_id, "Не удалось найти подходящие музыкальные видео.")
            return

        # Кешируем по video_id с минимальными метаданными, не перезаписывая полный кэш
        for entry in entries:
            vid = entry.get("id")
            if not vid:
                continue
            cache_key = f"video:{vid}"
            cached_vid = await redis_client.get(cache_key)
            if not cached_vid:
                minimal = {
                    "id": vid,
                    "title": entry.get("title"),
                    "webpage_url": entry.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
                    "duration": entry.get("duration"),
                    "channel": entry.get("channel"),
                    "channel_id": entry.get("channel_id"),
                    "description": entry.get("description"),
                    "source": "search_entry"
                }
                await redis_client.setex(cache_key, 86400, json.dumps(minimal))

        SEARCH_RESULTS[chat_id] = entries

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=entry['title'][:60], callback_data=f"dl_{idx}")]
                for idx, entry in enumerate(entries)
            ] + [[InlineKeyboardButton(text="🔄 Принудительно искать заново", callback_data="force_search")]]
        )
        await bot.send_message(chat_id, "Выбери нужную песню:", reply_markup=kb)

    except Exception as e:
        await bot.send_message(chat_id, f"Ошибка: {e}")

@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("Пришли название песни, и я найду её на YouTube, скачаю и пришлю в mp3.")

@dp.message()
async def search_and_download(message: types.Message):
    await _search_and_reply(message.chat.id, message.text or "")

# Новый handler для callback'ов
@dp.callback_query(F.data.startswith("dl_"))
async def callback_download(call: CallbackQuery):
    idx = int(call.data.split("_")[1])
    entries = SEARCH_RESULTS.get(call.message.chat.id)
    if not entries or idx >= len(entries):
        await call.message.answer("Ошибка: выбранный элемент не найден.")
        return

    entry = entries[idx]

    try:
        with YoutubeDL(YDL_OPTS) as ydl:
            vid = entry.get("id")
            cache_key = f"video:{vid}"
            cached_meta = await redis_client.get(cache_key)
            if cached_meta:
                try:
                    cached_meta = json.loads(cached_meta)
                except Exception:
                    cached_meta = None
            # Всегда парсим страницу заново, даже если видео уже есть в кеше
            fresh_info = ydl.extract_info(entry['webpage_url'], download=False)
            processed_info = ydl.process_ie_result(fresh_info, download=True)
            if ADMIN_USER_ID:
                user = await bot.get_chat(call.message.chat.id)
                await bot.send_message(
                    ADMIN_USER_ID,
                    f"⬇️ Скачивание: \"{processed_info.get('title')}\"\nURL: {entry.get('webpage_url')}\nЗапросил: @{user.username or user.full_name}"
                )
            full_meta = {
                "id": vid,
                "title": processed_info.get("title"),
                "webpage_url": processed_info.get("webpage_url") or entry.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
                "duration": processed_info.get("duration"),
                "channel": processed_info.get("channel"),
                "channel_id": processed_info.get("channel_id"),
                "description": processed_info.get("description"),
                "uploader": processed_info.get("uploader"),
                "source": "download_full"
            }
            await redis_client.setex(cache_key, 86400, json.dumps(full_meta))
            file_path = ydl.prepare_filename(processed_info).rsplit(".", 1)[0] + ".mp3"

        if not os.path.exists(file_path):
            await call.message.answer("Не удалось скачать файл.")
            return

        audio = FSInputFile(file_path)
        await bot.send_audio(call.message.chat.id, audio, title=processed_info.get('title'), performer=processed_info.get('uploader'))
        os.remove(file_path)

    except Exception as e:
        await call.message.answer(f"Ошибка при скачивании: {e}")

# Новый handler для принудительного поиска
@dp.callback_query(lambda c: c.data == "force_search")
async def force_search_trigger(call: CallbackQuery):
    print("🔁 force_search_trigger CALLED")
    chat_id = call.message.chat.id
    raw_query = SEARCH_LAST_QUERY.get(chat_id)
    if not raw_query:
        await bot.send_message(chat_id, "Исходный запрос не найден в памяти. Отправь текст запроса ещё раз.")
        return

    # Очистка кеша по нормализованному ключу
    norm_query = raw_query.lower()
    norm_query = re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060\u3000]', '', norm_query)
    norm_query = re.sub(r'[^\wа-яё0-9]+', ' ', norm_query, flags=re.UNICODE)
    norm_query = re.sub(r'\s+', ' ', norm_query).strip()
    norm_query = re.sub(r'ё', 'е', norm_query)
    await redis_client.delete(f"search:{norm_query}")
    await redis_client.delete(f"search_raw:{norm_query}")

    await bot.send_message(chat_id, "Перезапускаю поиск (игнорируем кэш)...")
    await _search_and_reply(chat_id, raw_query)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())