import aiosqlite
import asyncio
import json
import logging
from typing import Dict, Any, Optional
import os
from datetime import datetime

logger = logging.getLogger(__name__)

DATABASE_FILE = 'bot_state.db'

class BotDatabase:
    def __init__(self, db_file: str = DATABASE_FILE):
        self.db_file = db_file
        self._connection = None
    
    async def connect(self):
        """Подключение к базе данных"""
        try:
            self._connection = await aiosqlite.connect(self.db_file)
            # Добавляем проверку подключения
            if self._connection is None:
                raise Exception("Не удалось установить подключение к базе данных")
            await self._init_tables()
            logger.info("Подключение к базе данных установлено")
        except Exception as e:
            logger.error(f"Ошибка подключения к базе данных: {e}")
            raise
    
    async def disconnect(self):
        """Отключение от базы данных"""
        if self._connection:
            await self._connection.close()
            self._connection = None
            logger.info("Подключение к базе данных закрыто")
    
    async def _init_tables(self):
        """Инициализация таблиц в базе данных"""
        await self._connection.execute('''
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        await self._connection.execute('''
            CREATE TABLE IF NOT EXISTS user_sessions (
                user_id INTEGER PRIMARY KEY,
                session_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        await self._connection.execute('''
            CREATE TABLE IF NOT EXISTS statistics (
                id INTEGER PRIMARY KEY,
                comments_sent INTEGER DEFAULT 0,
                channels_processed INTEGER DEFAULT 0,
                reactions_set INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        await self._connection.execute('''
            CREATE TABLE IF NOT EXISTS processed_channels (
                username TEXT PRIMARY KEY,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        await self._connection.commit()
    
    async def save_bot_state(self, key: str, value: Any):
        """Сохранение состояния бота"""
        try:
            if self._connection is None:
                await self.connect()
            json_value = json.dumps(value, ensure_ascii=False, default=str)
            await self._connection.execute('''
                INSERT OR REPLACE INTO bot_state (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (key, json_value))
            await self._connection.commit()
        except Exception as e:
            logger.error(f"Ошибка сохранения состояния {key}: {e}")

    async def load_bot_state(self, key: str, default: Any = None) -> Any:
        """Загрузка состояния бота"""
        try:
            if self._connection is None:
                await self.connect()
            cursor = await self._connection.execute(
                'SELECT value FROM bot_state WHERE key = ?', (key,)
            )
            row = await cursor.fetchone()
            if row:
                return json.loads(row[0])
            return default
        except Exception as e:
            logger.error(f"Ошибка загрузки состояния {key}: {e}")
            return default
    
    async def save_user_session(self, user_id: int, session_data: Dict):
        """Сохранение данных сессии пользователя"""
        try:
            json_data = json.dumps(session_data, ensure_ascii=False, default=str)
            await self._connection.execute('''
                INSERT OR REPLACE INTO user_sessions (user_id, session_data, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (user_id, json_data))
            await self._connection.commit()
        except Exception as e:
            logger.error(f"Ошибка сохранения сессии пользователя {user_id}: {e}")
    
    async def load_user_session(self, user_id: int) -> Dict:
        """Загрузка данных сессии пользователя"""
        try:
            cursor = await self._connection.execute(
                'SELECT session_data FROM user_sessions WHERE user_id = ?', (user_id,)
            )
            row = await cursor.fetchone()
            if row:
                return json.loads(row[0])
            return {}
        except Exception as e:
            logger.error(f"Ошибка загрузки сессии пользователя {user_id}: {e}")
            return {}
    
    async def save_statistics(self, stats: Dict):
        """Сохранение статистики"""
        try:
            if self._connection is None:
                await self.connect()
            await self._connection.execute('''
                INSERT OR REPLACE INTO statistics (id, comments_sent, channels_processed, reactions_set, updated_at)
                VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (stats.get('comments_sent', 0), stats.get('channels_processed', 0), stats.get('reactions_set', 0)))
            await self._connection.commit()
        except Exception as e:
            logger.error(f"Ошибка сохранения статистики: {e}")

    async def load_statistics(self) -> Dict:
        """Загрузка статистики"""
        try:
            if self._connection is None:
                await self.connect()
            cursor = await self._connection.execute(
                'SELECT comments_sent, channels_processed, reactions_set FROM statistics WHERE id = 1'
            )
            row = await cursor.fetchone()
            if row:
                return {
                    'comments_sent': row[0],
                    'channels_processed': row[1],
                    'reactions_set': row[2]
                }
            return {'comments_sent': 0, 'channels_processed': 0, 'reactions_set': 0}
        except Exception as e:
            logger.error(f"Ошибка загрузки статистики: {e}")
            return {'comments_sent': 0, 'channels_processed': 0, 'reactions_set': 0}
    
    async def add_processed_channel(self, username: str):
        """Добавление обработанного канала"""
        try:
            await self._connection.execute('''
                INSERT OR REPLACE INTO processed_channels (username, processed_at)
                VALUES (?, CURRENT_TIMESTAMP)
            ''', (username,))
            await self._connection.commit()
        except Exception as e:
            logger.error(f"Ошибка добавления обработанного канала {username}: {e}")
    
    async def get_processed_channels(self) -> set:
        """Получение списка обработанных каналов"""
        try:
            cursor = await self._connection.execute('SELECT username FROM processed_channels')
            rows = await cursor.fetchall()
            return {row[0] for row in rows}
        except Exception as e:
            logger.error(f"Ошибка получения обработанных каналов: {e}")
            return set()
    
    async def clear_old_processed_channels(self, days: int = 30):
        """Очистка старых обработанных каналов"""
        try:
            await self._connection.execute('''
                DELETE FROM processed_channels 
                WHERE processed_at < datetime('now', '-{} days')
            '''.format(days))
            await self._connection.commit()
        except Exception as e:
            logger.error(f"Ошибка очистки старых каналов: {e}")

# Глобальный экземпляр базы данных
db = BotDatabase()

async def init_database():
    """Инициализация базы данных"""
    await db.connect()

async def close_database():
    """Закрытие базы данных"""
    await db.disconnect()