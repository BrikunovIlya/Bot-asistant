import asyncio
import logging
import os
import time
import sys
import traceback
import re

from collections import defaultdict
import aiohttp
import httpx
from dotenv import load_dotenv
from maxapi import Bot, Dispatcher, F
from maxapi.enums.sender_action import SenderAction
from maxapi.types import BotStarted, MessageCreated, MessageChatCreated, DialogCleared, BotRemoved, BotAdded, \
    CallbackButton
from .service import  scheduled_clean, Spam, proceed_message_ollama, hash_id, hash_username, init_service, normalize_timestamp, get_spam, request_type
from .models import init_db, clean_database, clean_all_messages_from_user, shutdown_db
import schedule
from pathlib import Path


load_dotenv()
MAX_BOT_TOKEN = os.getenv('MAX_BOT_TOKEN')
mention_name = os.getenv('MENTION_NAME')
OLLAMA_HOST = os.getenv('OLLAMA_HOST')
LLM_MODEL = os.getenv('LLM_MODEL')
logging.basicConfig(level=logging.INFO)
bot = Bot(MAX_BOT_TOKEN)
dp = Dispatcher()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
SOFT_RATE_LIMIT = int(os.getenv('SOFT_RATE_LIMIT'))
SOFT_TIME_WINDOW = int(os.getenv('SOFT_TIME_WINDOW'))
MAX_LENGTH = int(os.getenv('MAX_LENGTH'))


@dp.dialog_cleared()
async def chat_cleared(event: DialogCleared):
    try:
        raw_timestamp = event.timestamp
        user_id = int(event.from_user.user_id)
        user_id_hashed = hash_id(user_id=user_id)
        username = str(event.from_user.full_name)
        username_hashed = hash_username(username=username)
        message_timestamp = normalize_timestamp(raw_timestamp)
    except Exception:
        logging.exception(f"=== ошибка получения данных в chat_cleared===")
        return

    if time.time() - message_timestamp > 120:
        logging.info(f"=== Пропущено старая команда на очистку чата от {username_hashed} ===")
        return

    await clean_all_messages_from_user(user_id=user_id_hashed)
    logging.info(f"=== пользователь {user_id_hashed}-{username_hashed} очистил диалог, история диалога удалена из БД ===")


@dp.bot_started()
async def bot_started(event: BotStarted):
    try:
        try:
            raw_timestamp = event.timestamp
            username = str(event.from_user.full_name)
            username_hashed = hash_username(username=username)
            message_timestamp = normalize_timestamp(raw_timestamp)
        except Exception as e:
            logging.error(f"Ошибка получения данных в bot_started -- {e}")
            return

        if time.time() - message_timestamp > 120:
            logging.info(f"=== Пропущена старая команда на старт чата от {username_hashed} ===")
            return

        await event.bot.send_message(
            chat_id=event.chat_id,
            text="Здравствуйте! Могу помочь с вопросами по соцвыплатам, льготам или оформлению документов в Мурманской области."
                 " Чем могу быть полезен?"
        )
    except Exception as e:
        logging.error(f"Ошибка старта бота -- {e}")
        await event.bot.send_message(
            chat_id=event.chat_id, text="Извините,произола ошибка, обратитесь позже"
        )


@dp.bot_added()
async def bot_added(event: BotAdded):
    try:
        try:
            raw_timestamp = event.timestamp
            username = str(event.from_user.full_name)
            username_hashed = hash_username(username=username)
            chat_id = event.chat_id
            message_timestamp = normalize_timestamp(raw_timestamp)
        except Exception as e:
            logging.error(f"Ошибка получения данных в bot_added -- {e}")
            return

        if time.time() - message_timestamp > 120:
            logging.info(f"=== Пропущена старая команда на добавление бота в чат от {username_hashed} в чате {chat_id} ===")
            return

        await event.bot.send_message(
            chat_id=event.chat_id,
            text="Здравствуйте, готов помочь вам с поиском информации о положенных вам социальных выплатах."
                 " Пожалуйста, опишите свою ситуацию."
        )
    except Exception as e:
        logging.error(f"Ошибка добавления боты -- {e}")
        await event.bot.send_message(
            chat_id=event.chat_id,
            text="Извините, произошла ошибка, попробуйте позже"
        )


@dp.message_created(F.message.body.text)
async def message_handler(event: MessageCreated):
    try:
        try:
            chat_id = int(event.message.recipient.chat_id)
            user_text = str(event.message.body.text)
            message = user_text.replace(mention_name, "").strip()
            username = str(event.from_user.full_name)
            username_hashed = hash_username(username=username)
            user_id = int(event.from_user.user_id)
            user_id_hashed = hash_id(user_id=user_id)
            spam = get_spam()
            raw_timestamp = event.message.timestamp
            message_timestamp = normalize_timestamp(raw_timestamp)
        except Exception as e:
            logging.error(f"Ошибка в получении данных -- {e}")
            return

        try:
            if not re.match(rf'^[\w\s\.,!?@-]{{1,{MAX_LENGTH}}}$', message, flags=re.UNICODE):
                logging.warning(f"Подозрительный контент от {user_id_hashed}")
                await event.message.answer("пожалуйста не используйте эмодзи или другие спецсимволы, такие как  ;  :  $  ` | (  )  и кавычки ")
                return

            if len(message) > MAX_LENGTH:
                await event.message.answer(f"Ваше сообщение слишком длинное, пожалуйста, не используйте больше {MAX_LENGTH} символов")
                return

            if time.time() - message_timestamp > 120:
                logging.info(f"=== Пропущено старое сообщение от пользователя {username_hashed} в чате {chat_id} ===")
                return

            if not mention_name:
                logging.error("=== Не указан mention ===")
                return
        except Exception as e:
            logging.error(f"Ошибка в проверке сообщения -- {e}")
            return

        try:
            status, wait_time = await spam.check_rate_limits(user_id=user_id_hashed)

            if status == 'banned':
                await event.message.answer(
                    f"Вы временно заблокированы за спам.\n"
                    f"Попробуйте через {wait_time} сек. ({wait_time // 60} мин.)"
                )
                return

            if status == 'hard_limit':
                await event.message.answer(
                    f"️Слишком много запросов! Вы заблокированы на {wait_time} сек."
                )
                return

            if status == 'soft_limit':
                await event.message.answer(
                    f"Пожалуйста, подождите.\n"
                    f"Не более {SOFT_RATE_LIMIT} сообщений за {SOFT_TIME_WINDOW} сек.\n"
                    f"Подождите {wait_time} сек."
                )
                return
        except Exception as e:
            logging.error(f"Ошибка в проверке на спам -- {e}")

        if chat_id > 0:
            try:
                await event.bot.send_action(chat_id=chat_id, action=SenderAction.TYPING_ON)
                await event.message.answer("Анализирую вопрос...пожалуйста, подождите")
                try:
                    message_type = await request_type(message=message)
                except Exception as e:
                    logging.error(f"Ошибка в определении типа -- {e}")
                    return
                answer_text = await proceed_message_ollama(message=message, username=username_hashed,
                                                           user_id=user_id_hashed, chat_id=chat_id,
                                                           message_type=message_type)
                if not answer_text or not answer_text.strip():
                    answer_text = "Извините, не удалось сформировать ответ, попробуйте позже"
                await event.message.answer(answer_text)
            except Exception as e:
                logging.error(f"Ошибка ответа нейросети в диалоге -- {e}")
                await event.message.answer("Извините, произошла ошибка, нам очень жаль.")
        elif mention_name in user_text:
            try:
                await event.bot.send_action(chat_id=chat_id, action=SenderAction.TYPING_ON)
                await event.message.answer("Думаю над ответом, пожалуйста подождите.")
                try:
                    message_type = await request_type(message=message)
                except Exception as e:
                    logging.error(f"Ошибка в определении типа в чате")
                    return
                answer_text = await proceed_message_ollama(message=message, username=username_hashed,
                                                           user_id=user_id_hashed, chat_id=chat_id,
                                                           message_type=message_type)
                if not answer_text or not answer_text.strip():
                    answer_text = "Извините, не удалось сформировать ответ, попробуйте позже"
                await event.message.answer(answer_text)
            except Exception as e:
                logging.error(f"Ошибка ответа нейросети в чате -- {e}")
                await event.message.answer("Извините, произошла ошибка, нам очень жаль.")
        else:
            pass
    except Exception as e:
        logging.error(f"Ошибка в обработке сообщения пользователя {e}")
        await event.message.answer(f"Извините, произошла ошибка, пожалуйста, попробуйте позже")


async def main():
    try:
        logging.getLogger('aiohttp').setLevel(logging.DEBUG)
        logging.getLogger('aiohttp.client').setLevel(logging.DEBUG)
        await init_db()
        spam = Spam()
        #await clean_database()  для тестов, потом убрать
        background_tasks = []

        async with httpx.AsyncClient(timeout=80.0, trust_env=False) as http_client:
            init_service(http_client, spam)
            try:
                logging.info("=== Бот запущен ===")
                background_tasks.append(asyncio.create_task(scheduled_clean()))
                background_tasks.append(asyncio.create_task(spam.scheduled_spam_data_clean()))
                await dp.start_polling(bot)
            except KeyboardInterrupt:
                logging.info("=== получен сигнал остановки ===")
            finally:
                await dp.stop_polling()
                if hasattr(bot, 'session') and bot.session and not bot.session.closed:
                    await bot.session.close()
                    logging.info("bot.session (aiohttp) закрыта")

                for task in background_tasks:
                    task.cancel()
                if background_tasks:
                    await asyncio.gather(*background_tasks, return_exceptions=True)
                    logging.info("=== фоновые задачи остановлены ===")

                await shutdown_db()

                logging.warning("Сессия завершена")
    except Exception as e:
        logging.error(f"Ошибка в main -- {e}")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.warning("=== Бот остановлен ===")


