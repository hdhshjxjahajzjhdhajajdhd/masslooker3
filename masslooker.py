import asyncio
import logging
import random
import time
from datetime import datetime
from typing import List, Optional, Set
import os
import nest_asyncio

nest_asyncio.apply()

log_filename = 'masslooker_log.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename, mode='a', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

logging.getLogger('telethon').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)

try:
    from telethon import TelegramClient, events
    from telethon.errors import ChannelPrivateError, ChatWriteForbiddenError, FloodWaitError
    from telethon.tl.types import Channel, Chat, MessageMediaPhoto, MessageMediaDocument
    from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji
    import g4f
except ImportError as e:
    logger.error(f"Ошибка импорта библиотек: {e}")
    raise

# Импорт модуля базы данных
from database import db

# Глобальные переменные
masslooking_active = False
client: Optional[TelegramClient] = None
settings = {}
channel_queue = asyncio.Queue()
processed_channels: Set[str] = set()
statistics = {
    'comments_sent': 0,
    'reactions_set': 0,
    'channels_processed': 0,
    'errors': 0
}

# Положительные реакции для Telegram
POSITIVE_REACTIONS = [
    '👍', '❤️', '🔥', '🥰', '👏', '😍', '🤩', '🤝', '💯', '⭐',
    '🎉', '🙏', '💪', '👌', '✨', '💝', '🌟', '🏆', '🚀', '💎'
]

async def load_processed_channels():
    """Загрузка обработанных каналов из базы данных"""
    global processed_channels
    try:
        processed_channels = await db.get_processed_channels()
        logger.info(f"Загружено {len(processed_channels)} обработанных каналов из базы данных")
    except Exception as e:
        logger.error(f"Ошибка загрузки обработанных каналов: {e}")

async def save_processed_channel(username: str):
    """Сохранение обработанного канала в базу данных"""
    try:
        await db.add_processed_channel(username)
        processed_channels.add(username)
    except Exception as e:
        logger.error(f"Ошибка сохранения обработанного канала {username}: {e}")

def load_comment_prompt():
    """Загрузка промпта для генерации комментариев"""
    prompt_file = 'prompt_for_generating_comments.txt'
    
    try:
        if os.path.exists(prompt_file):
            with open(prompt_file, 'r', encoding='utf-8') as f:
                return f.read().strip()
        else:
            logger.warning(f"Файл {prompt_file} не найден, используется базовый промпт")
            return """Создай короткий, естественный комментарий к посту на русском языке. 

Текст поста: {text_of_the_post}

Тематика канала: {topics}

Требования к комментарию:
- Максимум 2-3 предложения
- Естественный стиль общения
- Положительная или нейтральная тональность
- Без спама и навязчивости
- Соответствует тематике поста
- Выглядит как реальный отзыв пользователя

Создай комментарий:"""
    except Exception as e:
        logger.error(f"Ошибка загрузки промпта: {e}")
        return "Создай короткий позитивный комментарий к посту: {text_of_the_post}"

async def generate_comment(post_text: str, topics: List[str]) -> str:
    """Генерация комментария с помощью GPT-4"""
    try:
        prompt_template = load_comment_prompt()
        
        # Подготавливаем промпт
        topics_text = ', '.join(topics) if topics else 'общая тематика'
        
        # Проверяем наличие плейсхолдеров в промпте
        if '{text_of_the_post}' in prompt_template:
            prompt = prompt_template.replace('{text_of_the_post}', post_text[:1000])
        else:
            prompt = prompt_template + f"\n\nТекст поста: {post_text[:1000]}"
        
        if '{topics}' in prompt:
            prompt = prompt.replace('{topics}', topics_text)
        
        # Генерируем комментарий - исправленный вызов для новой версии g4f
        response = g4f.ChatCompletion.create(
            model=g4f.models.gpt_4,
            messages=[{"role": "user", "content": prompt}],
            stream=False
        )
        
        # Очищаем ответ
        comment = response.strip()
        
        # Ограничиваем длину комментария
        if len(comment) > 200:
            comment = comment[:200] + '...'
        
        # Удаляем кавычки в начале и конце если есть
        if comment.startswith('"') and comment.endswith('"'):
            comment = comment[1:-1]
        
        if comment.startswith("'") and comment.endswith("'"):
            comment = comment[1:-1]
        
        logger.info(f"Сгенерирован комментарий: {comment[:50]}...")
        return comment
        
    except Exception as e:
        logger.error(f"Ошибка генерации комментария: {e}")
        # Возвращаем простой комментарий в случае ошибки
        fallback_comments = [
            "Интересно, спасибо за пост!",
            "Полезная информация",
            "Актуальная тема",
            "Хороший материал",
            "Согласен с автором"
        ]
        return random.choice(fallback_comments)

async def add_reaction_to_post(message, max_retries=3):
    """Добавление реакции к посту"""
    for attempt in range(max_retries):
        try:
            # Выбираем случайную положительную реакцию
            reaction = random.choice(POSITIVE_REACTIONS)
            
            # Отправляем реакцию
            await client.send_reaction(
                entity=message.peer_id,
                message=message.id,
                reaction=ReactionEmoji(emoticon=reaction)
            )
            
            logger.info(f"Поставлена реакция {reaction} к посту {message.id}")
            statistics['reactions_set'] += 1
            
            # Обновляем статистику в bot_interface
            try:
                import bot_interface
                bot_interface.update_statistics(reactions=1)
            except:
                pass
            
            return True
            
        except FloodWaitError as e:
            wait_time = e.seconds
            logger.warning(f"FloodWait при добавлении реакции: {wait_time} секунд")
            if attempt < max_retries - 1:
                await asyncio.sleep(wait_time + 1)
                continue
            else:
                return False
        except Exception as e:
            logger.error(f"Ошибка добавления реакции (попытка {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(random.uniform(5, 10))
                continue
            else:
                return False
    
    return False

async def send_comment_to_post(message, comment_text: str, max_retries=3):
    """Отправка комментария к посту"""
    for attempt in range(max_retries):
        try:
            # Проверяем, есть ли группа для обсуждений
            if hasattr(message.peer_id, 'channel_id'):
                entity = await client.get_entity(message.peer_id)
                
                if hasattr(entity, 'linked_chat_id') and entity.linked_chat_id:
                    # Отправляем комментарий в группу обсуждений
                    discussion_group = await client.get_entity(entity.linked_chat_id)
                    
                    # Проверяем, состоим ли мы в группе
                    try:
                        await client.get_participants(discussion_group, limit=1)
                    except:
                        # Нужно вступить в группу
                        try:
                            await client(JoinChannelRequest(discussion_group))
                            logger.info(f"Вступили в группу обсуждений {discussion_group.title}")
                            await asyncio.sleep(2)
                        except Exception as e:
                            logger.error(f"Не удалось вступить в группу обсуждений: {e}")
                            return False
                    
                    # Отправляем комментарий в группу обсуждений
                    await client.send_message(
                        discussion_group,
                        comment_text,
                        reply_to=message.id
                    )
                    
                    logger.info(f"Отправлен комментарий в группу обсуждений: {comment_text[:50]}...")
                    
                else:
                    # Пытаемся отправить комментарий напрямую
                    await client.send_message(
                        message.peer_id,
                        comment_text,
                        reply_to=message.id
                    )
                    
                    logger.info(f"Отправлен комментарий: {comment_text[:50]}...")
            
            statistics['comments_sent'] += 1
            
            # Обновляем статистику в bot_interface
            try:
                import bot_interface
                bot_interface.update_statistics(comments=1)
            except:
                pass
            
            return True
            
        except FloodWaitError as e:
            wait_time = e.seconds
            logger.warning(f"FloodWait при отправке комментария: {wait_time} секунд")
            if attempt < max_retries - 1:
                await asyncio.sleep(wait_time + 1)
                continue
            else:
                return False
        except ChatWriteForbiddenError:
            logger.warning("Запрещена отправка сообщений в этот канал")
            return False
        except Exception as e:
            logger.error(f"Ошибка отправки комментария (попытка {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(random.uniform(5, 10))
                continue
            else:
                return False
    
    return False

async def process_channel(username: str):
    """Обработка канала: подписка, комментарии, реакции, отписка"""
    try:
        logger.info(f"Начинаем обработку канала {username}")
        
        if not username.startswith('@'):
            username = '@' + username
        
        # Получаем информацию о канале
        try:
            entity = await client.get_entity(username)
        except Exception as e:
            logger.error(f"Не удалось получить информацию о канале {username}: {e}")
            return False
        
        # Подписываемся на канал
        try:
            if hasattr(entity, 'left') and entity.left:
                await client(JoinChannelRequest(entity))
                logger.info(f"Подписались на канал {username}")
                await asyncio.sleep(random.uniform(2, 5))
        except Exception as e:
            logger.warning(f"Ошибка подписки на канал {username}: {e}")
        
        # Определяем количество постов для обработки
        posts_range = settings.get('posts_range', (1, 5))
        posts_count = random.randint(posts_range[0], posts_range[1])
        
        logger.info(f"Обрабатываем {posts_count} последних постов в {username}")
        
        # Получаем последние посты
        processed_posts = 0
        topics = settings.get('topics', [])
        
        async for message in client.iter_messages(entity, limit=posts_count * 2):
            if not masslooking_active:
                break
            
            if processed_posts >= posts_count:
                break
            
            # Пропускаем сообщения без текста
            if not message.text or len(message.text.strip()) < 10:
                continue
            
            try:
                # Генерируем и отправляем комментарий
                comment = await generate_comment(message.text, topics)
                comment_sent = await send_comment_to_post(message, comment)
                
                if comment_sent:
                    # Добавляем задержку между комментарием и реакцией
                    delay = random.uniform(2, 8)
                    await asyncio.sleep(delay)
                
                # Ставим реакцию
                await add_reaction_to_post(message)
                
                processed_posts += 1
                
                # Задержка между обработкой постов
                delay_range = settings.get('delay_range', (20, 1000))
                if delay_range != (0, 0):
                    delay = random.uniform(delay_range[0], delay_range[1])
                    logger.info(f"Задержка {delay:.1f} секунд перед следующим действием")
                    await asyncio.sleep(delay)
                
            except Exception as e:
                logger.error(f"Ошибка обработки поста {message.id}: {e}")
                statistics['errors'] += 1
                continue
        
        # Отписываемся от канала
        try:
            await client(LeaveChannelRequest(entity))
            logger.info(f"Отписались от канала {username}")
        except Exception as e:
            logger.warning(f"Ошибка отписки от канала {username}: {e}")
        
        statistics['channels_processed'] += 1
        
        # Сохраняем обработанный канал в базу данных
        await save_processed_channel(username)
        
        # Обновляем статистику в bot_interface
        try:
            import bot_interface
            bot_interface.update_statistics(channels=1)
        except:
            pass
        
        logger.info(f"Обработка канала {username} завершена")
        return True
        
    except Exception as e:
        logger.error(f"Критическая ошибка обработки канала {username}: {e}")
        statistics['errors'] += 1
        return False

async def masslooking_worker():
    """Рабочий процесс масслукинга"""
    global masslooking_active
    
    while masslooking_active:
        try:
            # Получаем канал из очереди с таймаутом
            try:
                username = await asyncio.wait_for(channel_queue.get(), timeout=10.0)
            except asyncio.TimeoutError:
                continue
            
            if not masslooking_active:
                break
            
            # Проверяем лимит каналов
            max_channels = settings.get('max_channels', 150)
            if max_channels != float('inf') and len(processed_channels) >= max_channels:
                logger.info(f"Достигнут лимит каналов: {max_channels}")
                await asyncio.sleep(60)
                continue
            
            # Пропускаем уже обработанные каналы
            if username in processed_channels:
                continue
            
            # Обрабатываем канал
            success = await process_channel(username)
            
            # Задержка между каналами
            delay_range = settings.get('delay_range', (20, 1000))
            if delay_range != (0, 0):
                channel_delay = random.uniform(delay_range[0], delay_range[1])
                logger.info(f"Задержка {channel_delay:.1f} секунд перед следующим каналом")
                await asyncio.sleep(channel_delay)
            
        except Exception as e:
            logger.error(f"Ошибка в рабочем процессе масслукинга: {e}")
            await asyncio.sleep(30)

async def add_channel_to_queue(username: str):
    """Добавление канала в очередь обработки"""
    if username not in processed_channels:
        await channel_queue.put(username)
        logger.info(f"Канал {username} добавлен в очередь обработки")

async def start_masslooking(telegram_client: TelegramClient, masslooking_settings: dict):
    """Запуск масслукинга"""
    global masslooking_active, client, settings
    
    if masslooking_active:
        logger.warning("Масслукинг уже запущен")
        return
    
    logger.info("Запуск масслукинга...")
    
    client = telegram_client
    settings = masslooking_settings.copy()
    masslooking_active = True
    
    # Загружаем обработанные каналы из базы данных
    await load_processed_channels()
    
    # Запускаем рабочий процесс
    asyncio.create_task(masslooking_worker())
    
    logger.info("Масслукинг запущен")

async def stop_masslooking():
    """Остановка масслукинга"""
    global masslooking_active
    
    logger.info("Остановка масслукинга...")
    masslooking_active = False
    
    # Очищаем очередь
    while not channel_queue.empty():
        try:
            channel_queue.get_nowait()
        except:
            break
    
    logger.info("Масслукинг остановлен")

def get_statistics():
    """Получение статистики масслукинга"""
    return statistics.copy()

def reset_statistics():
    """Сброс статистики"""
    global statistics
    statistics = {
        'comments_sent': 0,
        'reactions_set': 0,
        'channels_processed': 0,
        'errors': 0
    }

# Основная функция для тестирования
async def main():
    """Тестирование модуля"""
    # Этот код предназначен только для тестирования
    print("Модуль masslooker готов к работе")
    print("Для запуска используйте функции start_masslooking() и add_channel_to_queue()")

if __name__ == "__main__":
    asyncio.run(main())