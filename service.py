import asyncio
import json
import logging
import os
import sys
import time
from typing import Tuple, Optional
import requests
import httpx
from functools import lru_cache
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, timedelta
from collections import defaultdict
import hashlib
import hmac

import models
from models import clean_database

load_dotenv()



def get_project_root(marker: str = ".project_root") -> Path:
    current = Path(__file__).resolve()
    for parent in [current, *current.parents]:
        if (parent / marker).exists():
            return parent
        if parent == parent.parent:
            break
    return Path.cwd()


PROJECT_ROOT = get_project_root()


if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


PROMPTS_DIR = PROJECT_ROOT / "prompts"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
)


try:
    prompt_type = (PROMPTS_DIR / "prompt_type.md").read_text(encoding="utf-8").strip()
except Exception as e:
    logging.warning(f"Не удалось загрузить prompt_type.md: {e}")
    prompt_type = "Ты классификатор запросов. Верни только категорию."


OLLAMA_HOST = os.getenv('OLLAMA_HOST')
LLM_MODEL = os.getenv('LLM_MODEL')
LLM_MODEL_TYPE = os.getenv('LLM_MODEL_TYPE')
PEPPER = os.getenv('PEPPER')


BAN_DURATION = int(os.getenv('BAN_DURATION', 3600))
SOFT_RATE_LIMIT = int(os.getenv('SOFT_RATE_LIMIT', 10))
SOFT_TIME_WINDOW = int(os.getenv('SOFT_TIME_WINDOW', 60))
HARD_RATE_LIMIT = int(os.getenv('HARD_RATE_LIMIT', 50))
HARD_TIME_WINDOW = int(os.getenv('HARD_TIME_WINDOW', 300))


_http_client: httpx.AsyncClient | None = None
_spam_checker: Optional['Spam'] = None


if not PEPPER or len(PEPPER) < 32:
    raise RuntimeError("PEPPER не задан или короче 32 символов")

if not OLLAMA_HOST:
    logging.warning("OLLAMA_HOST не задан")


@lru_cache(maxsize=10)
def load_prompt(filename: str) -> str:
    try:
        if not filename:
            logging.warning("filename пустой или None")
            return "Ты помощник по социальной поддержке"

        prompt_path = PROMPTS_DIR / filename
        if not prompt_path.exists():
            logging.warning(f"Промт {filename} не найден в {PROMPTS_DIR}")
            return "Ты помощник по социальной поддержке"

        return prompt_path.read_text(encoding="utf-8").strip()

    except Exception as e:
        logging.error(f"=== ошибка в чтении промта -- {e} ====")
        return "вы помощник по социальным выплатам"


async def define_prompt(message_type: str) -> str:
    try:
        mt = str(message_type).strip().lower()
        prompts_map = {
            "family": "family_prompt.txt",
            "status": "status_prompt.txt",
            "employment": "employ_prompt.txt",
            "social": "social_prompt.txt",
            "loss": "loss_prompt.txt",
            "elderly": "elderly_prompt.txt",
            "health": "health_prompt.txt",
            "education": "education_prompt.txt",
            "svo": "svo_prompt.txt",
            "other": "prompt.txt",
        }
        filename = prompts_map.get(mt, "prompt.txt")
        return load_prompt(filename)
    except Exception as e:
        logging.error(f"Ошибка в define_prompt -- {e}")
        return load_prompt("prompt.txt")


def init_service(client: httpx.AsyncClient, spam: Optional['Spam'] = None):
    global _http_client, _spam_checker
    _http_client = client
    if spam is not None:
        _spam_checker = spam


def get_spam() -> 'Spam':
    if _spam_checker is None:
        raise RuntimeError("Spam checker не инициализирован")
    return _spam_checker


def _get_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("Http клиент не инициализирован")
    return _http_client


async def proceed_message_ollama(message, username, user_id, chat_id, message_type):
    client = _get_client()
    prompt = await define_prompt(message_type=message_type)
    history = []

    try:
        history = await models.prepare_history(username, user_id, chat_id)
        logging.debug(f"==== История диалога: {history} ====")
    except Exception as e:
        logging.warning(f"Ошибка загрузки истории: {e}", exc_info=True)
        history = []

    messages = [
        {'role': 'system', 'content': prompt},
        {"role": "assistant", "content": f"Контекст диалога: {history}"},
        {'role': 'user', 'content': message}
    ]

    for msg in messages:
        if not isinstance(msg, dict) or 'role' not in msg or 'content' not in msg:
            logging.error(f"Неверный формат сообщения: {msg}")
            return None

    try:
        logging.debug(f"Отправка запроса к Ollama: {OLLAMA_HOST}/api/chat")

        response = await client.post(
            f'{OLLAMA_HOST}/api/chat',
            json={
                'model': LLM_MODEL,
                'messages': messages,
                'stream': False,
                'options': {
                    'temperature': 0.1,
                    'repeat_penalty': 1.1,
                    'num_predict': 3000,
                }
            },
            timeout=120.0
        )

        logging.debug(f"Ответ Ollama: status={response.status_code}")

        if response.status_code != 200:
            logging.error(f"Ollama API error: {response.status_code} - {response.text[:500]}")
            return None

        response_json = response.json()

        if 'message' not in response_json or 'content' not in response_json['message']:
            logging.error(f"Неверная структура ответа Ollama: {response_json.keys()}")
            return None

        answer_text = response_json['message']['content']

        prepared_message = f"тип: {message_type} --- {message} / Ответ ассистента: {answer_text}"
        save_task = asyncio.create_task(
            models.save_message(prepared_message, username, user_id, chat_id)
        )
        save_task.add_done_callback(
            lambda t: logging.error(f"Ошибка сохранения: {t.exception()}")
            if t.exception() else None
        )

        return answer_text

    except httpx.ConnectError as e:
        logging.error(f"Не удалось подключиться к Ollama: {e}", exc_info=True)
        return None
    except httpx.TimeoutException as e:
        logging.error(f"Таймаут запроса к Ollama: {e}", exc_info=True)
        return None
    except httpx.HTTPStatusError as e:
        logging.error(f"HTTP ошибка Ollama: {e.response.status_code} - {e.response.text[:200]}", exc_info=True)
        return None
    except Exception as e:
        logging.error(f"Непредвиденная ошибка Ollama: {e}", exc_info=True)
        return None


async def request_type(message: str) -> str | None:
    prompt = prompt_type
    client = _get_client()

    if not LLM_MODEL_TYPE:
        logging.error("LLM_MODEL_TYPE не задан!")
        return None
    if not prompt or len(prompt.strip()) < 10:
        logging.error("Промпт пустой!")
        return None

    messages = [
        {'role': 'system', 'content': prompt.strip()},
        {'role': 'user', 'content': str(message).strip()}
    ]

    try:
        response = await client.post(
            f'{OLLAMA_HOST}/api/chat',
            json={
                'model': LLM_MODEL_TYPE,
                'messages': messages,
                'stream': False,
                'options': {
                    'temperature': 0.1,
                    'num_predict': 1024,
                }
            },
            timeout=60.0
        )

        response_json = response.json()
        response_type = response_json['message']['content']
        return response_type.strip()

    except Exception as e:
        logging.error(f"ошибка в определении типа запроса нейросетью -- {e}")
        return None


async def scheduled_clean():
    while True:
        try:
            now = datetime.now()
            next_run = now.replace(hour=5, minute=0, second=0, microsecond=0)

            if now >= next_run:
                next_run += timedelta(days=1)

            delay = (next_run - now).total_seconds()
            next_run_str = next_run.strftime("%Y-%m-%d %H:%M:%S")
            logging.info(f"=== Очистка запланирована на {next_run_str} (через {delay / 3600:.2f} часов) ===")
            await asyncio.sleep(delay)
            await clean_database()
        except asyncio.CancelledError:
            logging.info("Задача scheduled_clean отменена")
            break
        except Exception as e:
            logging.error(f"Ошибка в scheduled_clean: {e}", exc_info=True)
            await asyncio.sleep(60)


class Spam:
    def __init__(self):
        self._lock = asyncio.Lock()
        self.banned_users: dict[str, float] = {}
        self.hard_requests: dict[str, list[float]] = {}
        self.soft_requests: dict[str, list[float]] = {}

    def cleanup_old_timestamps(self, timestamps: list, window: float) -> list:
        now = time.time()
        return [ts for ts in timestamps if now - ts < window]

    async def check_ban(self, user_id: str) -> Tuple[bool, int]:
        async with self._lock:
            try:
                if user_id not in self.banned_users:
                    return False, 0

                now = time.time()
                expires_at = self.banned_users[user_id]

                if now >= expires_at:
                    del self.banned_users[user_id]
                    return False, 0

                seconds_left = int(expires_at - now) + 1
                return True, max(1, seconds_left)
            except Exception as e:
                logging.error(f"Ошибка проверки блокировки пользователя {e}")
                return True, 60

    async def add_ban(self, user_id: str, duration: int = None):
        if duration is None:
            duration = BAN_DURATION
        try:
            async with self._lock:
                self.banned_users[user_id] = time.time() + duration
                logging.warning(f" Пользователь с id {user_id} ЗАБАНЕН на {duration} сек.")
        except Exception as e:
            logging.error(f"Ошибка в функции add_ban -- {e}")

    async def check_rate_limits(self, user_id: str) -> Tuple[str, int]:
        try:
            now = time.time()

            async with self._lock:
                if user_id in self.banned_users:
                    expires_at = self.banned_users[user_id]
                    if now < expires_at:
                        seconds_left = int(expires_at - now) + 1
                        return 'banned', max(1, seconds_left)
                    else:
                        del self.banned_users[user_id]

                hard_requests = list(self.hard_requests.get(user_id, []))
                soft_requests = list(self.soft_requests.get(user_id, []))

            hard_requests = self.cleanup_old_timestamps(hard_requests, HARD_TIME_WINDOW)
            if len(hard_requests) >= HARD_RATE_LIMIT:
                async with self._lock:
                    self.banned_users[user_id] = now + BAN_DURATION
                    self.hard_requests[user_id] = hard_requests
                logging.warning(f" Пользователь {user_id} ЗАБАНЕН (hard limit) на {BAN_DURATION} сек.")
                return 'hard_limit', BAN_DURATION

            hard_requests.append(now)

            soft_requests = self.cleanup_old_timestamps(soft_requests, SOFT_TIME_WINDOW)
            if len(soft_requests) >= SOFT_RATE_LIMIT:
                oldest = min(soft_requests)
                wait_time = int(oldest + SOFT_TIME_WINDOW - now) + 1
                async with self._lock:
                    self.hard_requests[user_id] = hard_requests
                    self.soft_requests[user_id] = soft_requests
                return 'soft_limit', max(1, wait_time)

            soft_requests.append(now)

            async with self._lock:
                self.hard_requests[user_id] = hard_requests
                self.soft_requests[user_id] = soft_requests

            return 'ok', 0

        except Exception as e:
            logging.error(f"Ошибка в проверке лимитов check_rate_limit -- {e}", exc_info=True)
            return 'ok', 0

    async def cleanup_expired_bans(self):
        try:
            async with self._lock:
                now = time.time()

                expired = [uid for uid, expires in self.banned_users.items() if now >= expires]
                for uid in expired:
                    del self.banned_users[uid]

                for user_id in list(self.soft_requests.keys()):
                    self.soft_requests[user_id] = self.cleanup_old_timestamps(
                        self.soft_requests.get(user_id, []), SOFT_TIME_WINDOW * 2
                    )
                    if not self.soft_requests[user_id]:
                        del self.soft_requests[user_id]

                for user_id in list(self.hard_requests.keys()):
                    self.hard_requests[user_id] = self.cleanup_old_timestamps(
                        self.hard_requests.get(user_id, []), HARD_TIME_WINDOW * 2
                    )
                    if not self.hard_requests[user_id]:
                        del self.hard_requests[user_id]

                if expired:
                    logging.info(f" Очищено {len(expired)} истёкших банов")
        except Exception as e:
            logging.error(f"Ошибка в очистке списка банов -- {e}")

    async def scheduled_spam_data_clean(self):
        while True:
            await asyncio.sleep(180)
            await self.cleanup_expired_bans()
            logging.info("==== Список банов очищен ====")


def hash_id(user_id: int, pepper: str = PEPPER) -> str:
    try:
        hashed = hmac.new(
            key=pepper.encode(),
            msg=str(user_id).encode(),
            digestmod=hashlib.sha256
        ).hexdigest()
        return hashed[:8]
    except Exception as e:
        logging.error(f"Ошибка в hash_id -- {e}")
        raise


def hash_username(username: str, pepper: str = PEPPER) -> str:
    try:
        normalized = " ".join(username.strip().lower().split())
        hashed = hmac.new(
            key=pepper.encode(),
            msg=normalized.encode('utf-8'),
            digestmod=hashlib.sha256
        ).hexdigest()
        return hashed[:12]
    except Exception as e:
        logging.error(f"Ошибка в hash_username -- {e}")
        raise


def normalize_timestamp(ts: float) -> float:
    return ts / 1000 if ts > 1e12 else ts


def write_log(filename, level, message, **kwargs):
    log_entry = {
        "time": datetime.now().isoformat(),
        "level": level,
        "msg": message,
        **kwargs
    }
    with open(filename, 'a', encoding='utf8') as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')


