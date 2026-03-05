import logging
import sqlite3
import logging
import aiosqlite
from pathlib import Path
from typing import Optional, List

logging.basicConfig(level=logging.INFO)
PROJECT_ROOT = Path(__file__).parent.parent
DB_NAME = PROJECT_ROOT / 'bot_messages.db'

_db: Optional[aiosqlite.Connection] = None


async def init_db():
    global _db

    if _db is not None:
        logging.warning("БД уже инициализирована")
        return
    try:
        _db = await aiosqlite.connect(DB_NAME)
        _db.row_factory = aiosqlite.Row
        await _db.execute('''
                CREATE TABLE IF NOT EXISTS context(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message TEXT NOT NULL,
                    username TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    chat_id BIGINT NOT NULL
                )
                ''')
        await _db.commit()
        logging.info("=== БД создана ===")
    except Exception as e:
        logging.error(f"=== Ошибка создании БД {e} ===")


async def shutdown_db():
    global _db
    if _db:
        await _db.close()
        _db = None
        logging.info("Соединение с БД закрыто")


def get_db() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("БД не найдена")
    return _db


async def save_message(message, username, user_id, chat_id):
    try:
        await _db.execute('''
                INSERT INTO context(message,username,user_id, chat_id)
                VALUES (?,?,?,?)
                ''', (message, username, user_id, chat_id, ))
        await _db.commit()
        logging.info(f"сообщение //  {message}  //  от пользователя {username} с id: {user_id}  успешно сохранено в чате {chat_id}")
    except Exception as e:
        logging.error(f"Ошибка записи данных в БД -- {e}")


async def prepare_history(username, user_id, chat_id):
    try:
        cursor = await _db.execute('''
                    SELECT message
                    FROM context
                    WHERE user_id = ? AND chat_id = ?
                    ORDER BY id DESC
                    LIMIT 5
                    ''', (user_id, chat_id))

        context =await cursor.fetchall()
        history = [f"История диалога -- {username} написал : {row['message']}" for row in context]
        history.reverse()
        return history
    except Exception as e:
        logging.error(f"Ошибка подготовки истории -- {e}")
        return []


async def clean_all_messages_from_user(user_id):
    try:
        cursor = await _db.execute("DELETE FROM context WHERE user_id = ?", (user_id,))
        await _db.commit()
        logging.info(f"=== Удалено записей: {cursor.rowcount} ===")
    except Exception as e:
        logging.error(f"Ошибка удаления данных, изменения отменены -- {e}")
        await _db.rollback()


async def clean_database():
    try:
         if _db is None:
            logging.warning("=== ОШИБКА БД не инициализирована ===")
            return
         try:
            await _db.execute("DROP TABLE IF EXISTS context;")
         except Exception as e:
            logging.error(f"Ошибка удалении таблицы  --  {e}")
         try:
            await _db.execute('''
            CREATE TABLE context(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                username TEXT NOT NULL,
                user_id TEXT NOT NULL,
                chat_id BIGINT NOT NULL
                )
            ''')
            await _db.commit()
            cursor = await _db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='context'"
            )
            if await cursor.fetchall():
                logging.info("БД Пересоздана")
            else:
                logging.error("Ошибка в пересоздании БД")
         except Exception as e:
            logging.error(f"Ошибка при создании БД -- {e}")
    except Exception as e:
        logging.error(f"критическая ошибка -- {e}")






