import asyncio
import logging
import os
import tempfile
import pathlib

from dotenv import load_dotenv
from prompts import PROMPTS_FILE

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, Voice, PhotoSize
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram import BaseMiddleware 
from typing import Callable, Dict, Any, Awaitable


import google.generativeai as genai
from google.generativeai.types import generation_types

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-1.5-flash-latest")

if not BOT_TOKEN:
    raise ValueError("Необходимо установить BOT_TOKEN")
if not GEMINI_API_KEY:
    raise ValueError("Необходимо установить GEMINI_API_KEY")

# --- СНАЧАЛА ОПРЕДЕЛИМ ВСЕ СИСТЕМНЫЕ ПРОМПТЫ ---
PSYCHOLOGIST_PROMPT = PROMPTS_FILE
IMAGE_SYSTEM_PROMPT = "Ты - умный ИИ-ассистент. Тебе предоставлено изображение. Опиши это изображение коротко, интересно и лаконично, как если бы ты описывал его другу. Помоги с вопросом пользователя. Используй эмоджи, если это уместно. 🖼️✨"

# --- НАЧАЛО КОДА ДЛЯ ИСТОРИИ СООБЩЕНИЙ В ТЕКСТОВОМ ЧАТЕ ---
# 1. Системный промпт для текстового чата. Теперь он будет "психологом".
TEXT_CHAT_SYSTEM_PROMPT = PSYCHOLOGIST_PROMPT # <--- ИСПОЛЬЗУЕМ ПРОМПТ ПСИХОЛОГА

# 2. Максимальное количество сообщений USER/ASSISTANT в хранимой истории.
MAX_HISTORY_MESSAGES_IN_LIST = 9


class LoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        # Получаем информацию о пользователе
        user = event.from_user
        user_info = f"ID: {user.id}, Name: {user.full_name}"
        if user.username:
            user_info += f", @{user.username}"

        # Получаем информацию о содержимом сообщения
        content_info = "[Неизвестный тип сообщения]"
        if event.text:
            content_info = f"Текст: '{event.text}'"
        elif event.voice:
            content_info = f"Голосовое сообщение (длительность: {event.voice.duration}с)"
        elif event.photo:
            photo = event.photo[-1]
            content_info = f"Фото (разрешение: {photo.width}x{photo.height})"

        logging.info(f"Получено сообщение от [{user_info}]. {content_info}")

        return await handler(event, data)

# 3. Глобальный словарь для хранения историй чатов
chat_histories = {}

# 4. Настройка Gemini API и модели
try:
    genai.configure(api_key=GEMINI_API_KEY) # Конфигурируем один раз
    
    # Модель СПЕЦИАЛЬНО для текстового чата с историей
    gemini_text_chat_model = genai.GenerativeModel(
        MODEL_NAME,
        system_instruction=TEXT_CHAT_SYSTEM_PROMPT # Теперь здесь промпт психолога
    )
    
    # Модель Gemini для общих задач (аудио, изображения)
    gemini_general_purpose_model = genai.GenerativeModel(MODEL_NAME)

except Exception as e:
    logging.error(f"Ошибка конфигурации Gemini моделей: {e}")
    gemini_text_chat_model = None
    gemini_general_purpose_model = None
# --- КОНЕЦ КОДА ДЛЯ ИСТОРИИ СООБЩЕНИЙ В ТЕКСТОВОМ ЧАТЕ ---

# Для аудио будем использовать тот же промпт психолога
AUDIO_SYSTEM_PROMPT = PSYCHOLOGIST_PROMPT


router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"Привет, {message.from_user.full_name}!\n"
        "Я твой психологический компаньон. 🤖 Расскажи, что тебя беспокоит.\n" # Изменил приветствие
        "Можешь написать мне текст (я запомню наш разговор!), отправить голосовое или картинку."
    )
    chat_id = message.chat.id
    if chat_id in chat_histories:
        del chat_histories[chat_id]
        logging.info(f"История текстового чата для {chat_id} очищена по команде /start")

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Этот бот использует Gemini AI.\n\n"
        "<b>Команды:</b>\n"
        "/start - Начать диалог (очищает историю текстового чата)\n"
        "/help - Показать это сообщение\n\n"
        "Я могу отвечать на текстовые сообщения (с сохранением истории), "
        "обрабатывать голосовые 🎤 и описывать картинки 🖼️ (без истории)."
    )

@router.message(F.text)
async def handle_text_message_with_history(message: Message):
    if not gemini_text_chat_model:
        await message.answer("Извините, наш сервис AI для текстового чата временно недоступен. 😔")
        return

    user_text = message.text
    if not user_text:
        await message.answer("Пожалуйста, введите какой-нибудь текст. ✍️")
        return

    processing_message = await message.answer("Слушаю вас... 🤔") # Обновил сообщение
    chat_id = message.chat.id

    if chat_id not in chat_histories:
        chat_histories[chat_id] = []
        logging.info(f"Новая история текстового чата инициализирована для chat_id: {chat_id}")

    chat_histories[chat_id].append({"role": "user", "parts": [{"text": user_text}]})
    
    if len(chat_histories[chat_id]) > MAX_HISTORY_MESSAGES_IN_LIST:
        chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY_MESSAGES_IN_LIST:]

    try:
        chat_session = gemini_text_chat_model.start_chat(history=chat_histories[chat_id][:-1])
        response = await chat_session.send_message_async(chat_histories[chat_id][-1]['parts'])
        await processing_message.delete()

        if response.text:
            response_text = response.text
            chat_histories[chat_id].append({"role": "model", "parts": [{"text": response_text}]})
            await message.answer(response_text)
            
            if len(chat_histories[chat_id]) > MAX_HISTORY_MESSAGES_IN_LIST:
                chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY_MESSAGES_IN_LIST:]
        else:
            if chat_histories[chat_id] and chat_histories[chat_id][-1]["role"] == "user":
                chat_histories[chat_id].pop()
            logging.warning(f"Gemini API вернул пустой текстовый ответ (chat_id: {chat_id}): {user_text}")
            # Добавьте вашу логику safety_feedback_info сюда, если нужно
            await message.answer(f"К сожалению, я не смог сгенерировать ответ. 😔")

    except generation_types.BlockedPromptException as bpe:
        logging.error(f"Текстовый запрос (с историей) заблокирован: {bpe} для {chat_id}, текст: {user_text}")
        await processing_message.delete()
        if chat_histories[chat_id] and chat_histories[chat_id][-1]["role"] == "user":
            chat_histories[chat_id].pop()
        await message.answer("Мой внутренний фильтр счел ваш запрос неприемлемым. 🙅‍♂️")
    except Exception as e:
        logging.error(f"Ошибка Gemini API (текст с историей, {chat_id}): {e}", exc_info=True)
        try: await processing_message.delete()
        except Exception: pass
        await message.answer("Извините, произошла ошибка. 😵‍💫")


@router.message(F.voice)
async def handle_voice_message(message: Message, bot: Bot):
    if not gemini_general_purpose_model:
        await message.answer("Сервис AI для аудио временно недоступен. 😔")
        return

    voice: Voice = message.voice
    processing_message = await message.answer("Обрабатываю голосовое... 🎤🎧")
    gemini_file_resource = None

    with tempfile.TemporaryDirectory() as temp_dir_name:
        temp_dir_path = pathlib.Path(temp_dir_name)
        local_ogg_path = temp_dir_path / f"{voice.file_unique_id}.ogg"
        try:
            await bot.download(file=voice.file_id, destination=local_ogg_path)
            if local_ogg_path.stat().st_size == 0:
                await processing_message.edit_text("Не удалось скачать аудио. 😥")
                return

            gemini_file_resource = await asyncio.to_thread(
                genai.upload_file, path=local_ogg_path, mime_type="audio/ogg" # Убедитесь, что MIME-тип правильный
            )
            
            # Используем AUDIO_SYSTEM_PROMPT (который теперь PSYCHOLOGIST_PROMPT)
            contents_for_gemini = [AUDIO_SYSTEM_PROMPT, gemini_file_resource]
            response = await gemini_general_purpose_model.generate_content_async(contents_for_gemini)
            await processing_message.delete()

            if response.text:
                await message.answer(response.text)
            else:
                logging.warning(f"Gemini API пустой ответ для аудио: {voice.file_id}")
                # Добавьте вашу логику safety_feedback_info сюда
                await message.answer(f"Не смог обработать голосовое. 🎤❌")
        except generation_types.BlockedPromptException as bpe:
            logging.error(f"Запрос Gemini для аудио заблокирован: {bpe}")
            await processing_message.delete()
            await message.answer("Фильтр счел аудио неприемлемым. 🙅‍♂️")
        except Exception as e:
            logging.error(f"Ошибка обработки голосового: {e}", exc_info=True)
            try: await processing_message.delete()
            except Exception: pass
            await message.answer("Ошибка обработки голосового. 😵‍💫")
        finally:
            if gemini_file_resource and hasattr(gemini_file_resource, 'name'):
                try: await asyncio.to_thread(genai.delete_file, name=gemini_file_resource.name)
                except Exception as e_del: logging.error(f"Ошибка удаления файла Gemini {gemini_file_resource.name}: {e_del}")

@router.message(F.photo)
async def handle_photo_message(message: Message, bot: Bot):
    if not gemini_general_purpose_model:
        await message.answer("Сервис AI для изображений временно недоступен. 😔")
        return

    photo: PhotoSize = message.photo[-1]
    processing_message = await message.answer("Анализирую изображение... 🖼️👀")
    gemini_file_resource = None

    with tempfile.TemporaryDirectory() as temp_dir_name:
        temp_dir_path = pathlib.Path(temp_dir_name)
        local_jpg_path = temp_dir_path / f"{photo.file_unique_id}.jpg"
        try:
            await bot.download(file=photo.file_id, destination=local_jpg_path)
            if local_jpg_path.stat().st_size == 0:
                await processing_message.edit_text("Не удалось скачать изображение. 😥")
                return
            
            gemini_file_resource = await asyncio.to_thread(
                genai.upload_file, path=local_jpg_path, mime_type="image/jpeg"
            )

            contents_for_gemini = [IMAGE_SYSTEM_PROMPT, gemini_file_resource]
            response = await gemini_general_purpose_model.generate_content_async(contents_for_gemini)
            await processing_message.delete()

            if response.text:
                await message.answer(response.text)
            else:
                logging.warning(f"Gemini API пустой ответ для изображения: {photo.file_id}")
                # Добавьте вашу логику safety_feedback_info сюда
                await message.answer(f"Не смог описать изображение. 🖼️❌")
        except generation_types.BlockedPromptException as bpe:
            logging.error(f"Запрос Gemini для изображения заблокирован: {bpe}")
            await processing_message.delete()
            await message.answer("Фильтр счел изображение неприемлемым. 🙅‍♂️")
        except Exception as e:
            logging.error(f"Ошибка обработки изображения: {e}", exc_info=True)
            try: await processing_message.delete()
            except Exception: pass
            await message.answer("Ошибка обработки изображения. 😵‍💫")
        finally:
            if gemini_file_resource and hasattr(gemini_file_resource, 'name'):
                try: await asyncio.to_thread(genai.delete_file, name=gemini_file_resource.name)
                except Exception as e_del: logging.error(f"Ошибка удаления файла Gemini {gemini_file_resource.name}: {e_del}")

async def main():
    default_properties = DefaultBotProperties(parse_mode=ParseMode.HTML)
    bot = Bot(token=BOT_TOKEN, default=default_properties)
    dp = Dispatcher()

    dp.message.middleware(LoggingMiddleware())
    
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запускается...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logging.info("Бот остановлен.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    asyncio.run(main())
