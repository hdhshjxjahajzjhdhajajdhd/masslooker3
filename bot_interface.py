import logging
import json
import os
import re
import asyncio
import threading
from datetime import datetime
from typing import Dict, List, Optional
import base64
import nest_asyncio
import signal
import sys
import atexit

nest_asyncio.apply()

log_filename = 'bot_interface_log.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename, mode='a', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('telethon').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
    from telethon import TelegramClient, events
    from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PasswordHashInvalidError
    import g4f
    import aiosqlite
except ImportError as e:
    logger.error(f"Ошибка импорта библиотек: {e}")
    raise

# Импорт модуля базы данных
from database import db, init_database, close_database

# Глобальные переменные
bot_data = {
    'settings': {
        'max_channels': 150,
        'posts_range': (1, 5),
        'delay_range': (20, 1000),
        'target_channel': 'https://t.me/cosmoptichka5',
        'topics': ['Мода и красота', 'Бизнес и стартапы', 'Маркетинг, PR, реклама'],
        'keywords': ['бренд', 'мода', 'fashion', 'beauty', 'запуск бренда', 'маркетинг', 'упаковка', 'WB', 'Wildberries', 'Ozon', 'стратегия маркетинга', 'продвижение', 'реклама', 'бренд одежды', 'собственный бренд']
    },
    'statistics': {
        'comments_sent': 0,
        'channels_processed': 0,
        'reactions_set': 0
    },
    'active_users': set(),
    'admin_user': None,
    'is_running': False,
    'access_restricted': False,
    'telethon_client': None,
    'selected_topics': set(),
    'pending_manual_setup': {},
    'user_states': {}  # Для отслеживания состояния пользователей
}

# Доступные темы
AVAILABLE_TOPICS = [
    'Бизнес и стартапы', 'Блоги', 'Букмекерство', 'Видео и фильмы', 'Даркнет',
    'Дизайн', 'Для взрослых', 'Еда и кулинария', 'Здоровье и Фитнес', 'Игры',
    'Инстаграм', 'Интерьер и строительство', 'Искусство', 'Картинки и фото',
    'Карьера', 'Книги', 'Криптовалюты', 'Курсы и гайды', 'Лингвистика',
    'Маркетинг, PR, реклама', 'Медицина', 'Мода и красота', 'Музыка',
    'Новости и СМИ', 'Образование', 'Познавательное', 'Политика', 'Право',
    'Природа', 'Продажи', 'Психология', 'Путешествия', 'Религия', 'Рукоделие',
    'Семья и дети', 'Софт и приложения', 'Спорт', 'Технологии', 'Транспорт',
    'Цитаты', 'Шок-контент', 'Эзотерика', 'Экономика', 'Эроктика',
    'Юмор и развлечения', 'Другое'
]

def simple_encrypt(text, key="telegram_mass_looker_2024"):
    """Простое шифрование"""
    if not text:
        return ""
    key_nums = [ord(c) for c in key]
    encrypted = []
    for i, char in enumerate(text):
        key_char = key_nums[i % len(key_nums)]
        encrypted_char = chr((ord(char) + key_char) % 256)
        encrypted.append(encrypted_char)
    encrypted_text = ''.join(encrypted)
    return base64.b64encode(encrypted_text.encode('latin-1')).decode()

def simple_decrypt(encrypted_text, key="telegram_mass_looker_2024"):
    """Простая расшифровка"""
    if not encrypted_text:
        return ""
    try:
        encrypted_bytes = base64.b64decode(encrypted_text.encode())
        encrypted = encrypted_bytes.decode('latin-1')
        key_nums = [ord(c) for c in key]
        decrypted = []
        for i, char in enumerate(encrypted):
            key_char = key_nums[i % len(key_nums)]
            decrypted_char = chr((ord(char) - key_char) % 256)
            decrypted.append(decrypted_char)
        return ''.join(decrypted)
    except Exception:
        return ""

async def save_bot_state():
    """Сохранение полного состояния бота"""
    try:
        # Сохраняем основные настройки
        await db.save_bot_state('settings', bot_data['settings'])
        await db.save_bot_state('admin_user', bot_data['admin_user'])
        await db.save_bot_state('access_restricted', bot_data['access_restricted'])
        await db.save_bot_state('is_running', bot_data['is_running'])
        
        # Сохраняем статистику
        await db.save_statistics(bot_data['statistics'])
        
        # Сохраняем состояния пользователей
        for user_id, state in bot_data['user_states'].items():
            await db.save_user_session(user_id, {'state': state})
        
        logger.info("Состояние бота сохранено в базу данных")
    except Exception as e:
        logger.error(f"Ошибка сохранения состояния бота: {e}")

async def load_bot_state():
    """Загрузка полного состояния бота"""
    try:
        # Загружаем основные настройки
        settings = await db.load_bot_state('settings', bot_data['settings'])
        if settings:
            bot_data['settings'] = settings
        
        admin_user = await db.load_bot_state('admin_user')
        if admin_user:
            bot_data['admin_user'] = admin_user
        
        access_restricted = await db.load_bot_state('access_restricted', False)
        bot_data['access_restricted'] = access_restricted
        
        is_running = await db.load_bot_state('is_running', False)
        bot_data['is_running'] = is_running
        
        # Загружаем статистику
        statistics = await db.load_statistics()
        bot_data['statistics'] = statistics
        
        logger.info("Состояние бота загружено из базы данных")
    except Exception as e:
        logger.error(f"Ошибка загрузки состояния бота: {e}")

def load_user_config():
    """Загрузка конфигурации пользователя"""
    config_file = 'config.json'
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            for key in ['api_id', 'api_hash', 'phone', 'password']:
                if key in config and config[key]:
                    config[key] = simple_decrypt(config[key])
            return config
        except Exception as e:
            logger.error(f"Ошибка загрузки конфигурации: {e}")
    return {}

def save_user_config(config):
    """Сохранение конфигурации пользователя"""
    config_file = 'config.json'
    try:
        existing_config = {}
        if os.path.exists(config_file):
            with open(config_file, 'r', encoding='utf-8') as f:
                existing_config = json.load(f)
        
        existing_config.update(config)
        
        # Шифруем данные
        encrypted_config = existing_config.copy()
        for key in ['api_id', 'api_hash', 'phone', 'password']:
            if key in encrypted_config and encrypted_config[key]:
                encrypted_config[key] = simple_encrypt(encrypted_config[key])
        
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(encrypted_config, f, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения конфигурации: {e}")

def check_access(user_id):
    """Проверка доступа пользователя"""
    if bot_data['access_restricted'] and user_id != bot_data['admin_user']:
        return False
    return True

def get_back_button():
    """Получение кнопки Назад"""
    return InlineKeyboardButton("◀️ Назад", callback_data="back")

def get_main_menu_keyboard():
    """Получение клавиатуры главного меню"""
    config = load_user_config()
    account_button_text = "👤 Сменить аккаунт" if config.get('phone') else "➕ Добавить аккаунт"
    run_button_text = "⏹️ Остановить рассылку" if bot_data['is_running'] else "▶️ Запустить рассылку"
    access_button_text = "🔓 Вернуть всем доступ к боту" if bot_data['access_restricted'] else "🔒 Ограничить доступ к боту"
    
    keyboard = [
        [InlineKeyboardButton(account_button_text, callback_data="account_setup")],
        [InlineKeyboardButton("📺 Выбрать целевой канал", callback_data="target_channel")],
        [InlineKeyboardButton("⚙️ Параметры масслукинга", callback_data="settings")],
        [InlineKeyboardButton(run_button_text, callback_data="toggle_run")],
        [InlineKeyboardButton("📊 Статистика", callback_data="statistics")],
        [InlineKeyboardButton(access_button_text, callback_data="toggle_access")]
    ]
    
    return InlineKeyboardMarkup(keyboard)

def get_code_input_keyboard():
    """Получение правильной клавиатуры для ввода кода"""
    keyboard = [
        # Ряд 1-2-3
        [InlineKeyboardButton("1", callback_data="code_1"),
         InlineKeyboardButton("2", callback_data="code_2"),
         InlineKeyboardButton("3", callback_data="code_3")],
        # Ряд 4-5-6
        [InlineKeyboardButton("4", callback_data="code_4"),
         InlineKeyboardButton("5", callback_data="code_5"),
         InlineKeyboardButton("6", callback_data="code_6")],
        # Ряд 7-8-9
        [InlineKeyboardButton("7", callback_data="code_7"),
         InlineKeyboardButton("8", callback_data="code_8"),
         InlineKeyboardButton("9", callback_data="code_9")],
        # Ряд отправить-0-стереть
        [InlineKeyboardButton("отправить ✅", callback_data="code_send"),
         InlineKeyboardButton("0", callback_data="code_0"),
         InlineKeyboardButton("стереть ⬅️", callback_data="code_delete")],
        # Ряд отмена
        [InlineKeyboardButton("Отмена ❌", callback_data="code_cancel")]
    ]
    
    return InlineKeyboardMarkup(keyboard)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=True):
    """Показать главное меню"""
    welcome_text = """🤖 Система нейрокомментинга и массреакшена

Добро пожаловать! Выберите действие:"""
    
    reply_markup = get_main_menu_keyboard()
    
    # Очищаем состояние пользователя
    user_id = update.effective_user.id
    bot_data['user_states'][user_id] = 'main_menu'
    context.user_data.clear()
    
    # Сохраняем состояние
    await save_bot_state()
    
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(welcome_text, reply_markup=reply_markup)
    else:
        if update.callback_query:
            await update.callback_query.message.reply_text(welcome_text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_id = update.effective_user.id
    
    if not check_access(user_id):
        await update.message.reply_text("❌ Доступ к боту ограничен")
        return
    
    if bot_data['admin_user'] is None:
        bot_data['admin_user'] = user_id
        await save_bot_state()
    
    bot_data['active_users'].add(user_id)
    logger.info(f"Пользователь {user_id} запустил бота")
    
    await show_main_menu(update, context, edit=False)

async def handle_back_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопки Назад"""
    user_id = update.effective_user.id
    current_state = bot_data['user_states'].get(user_id, 'main_menu')
    
    # Определяем куда вернуться в зависимости от текущего состояния
    if current_state in ['account_setup', 'target_channel', 'settings', 'statistics']:
        await show_main_menu(update, context)
    elif current_state in ['manual_setup', 'topic_selection']:
        await target_channel_setup(update, context)
    elif current_state in ['api_id', 'api_hash', 'phone', 'code', 'password']:
        await show_main_menu(update, context)
    else:
        await show_main_menu(update, context)

async def account_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройка аккаунта"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if not check_access(user_id):
        await query.answer("❌ Доступ ограничен", show_alert=True)
        return
    
    bot_data['user_states'][user_id] = 'account_setup'
    await save_bot_state()
    
    config = load_user_config()
    
    # Запрос API ID и API Hash если их нет
    if not config.get('api_id') or not config.get('api_hash'):
        keyboard = [[get_back_button()]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "📱 Настройка аккаунта\n\n"
            "Для работы с Telegram аккаунтом необходимо получить API ID и API Hash.\n\n"
            "1. Перейдите на https://my.telegram.org\n"
            "2. Войдите в свой аккаунт\n"
            "3. Перейдите в 'API development tools'\n"
            "4. Создайте приложение\n\n"
            "Отправьте API ID:",
            reply_markup=reply_markup
        )
        context.user_data['setup_step'] = 'api_id'
        bot_data['user_states'][user_id] = 'api_id'
        await save_bot_state()
        return
    
    # Запрос номера телефона
    keyboard = [[get_back_button()]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "📱 Настройка аккаунта\n\n"
        "API ID и API Hash найдены.\n\n"
        "Отправьте номер телефона в международном формате (например, +79123456789):",
        reply_markup=reply_markup
    )
    context.user_data['setup_step'] = 'phone'
    bot_data['user_states'][user_id] = 'phone'
    await save_bot_state()

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений"""
    user_id = update.effective_user.id
    if not check_access(user_id):
        await update.message.reply_text("❌ Доступ ограничен")
        return
    
    text = update.message.text
    step = context.user_data.get('setup_step')
    
    if step == 'api_id':
        if text.isdigit():
            config = load_user_config()
            config['api_id'] = text
            save_user_config(config)
            
            keyboard = [[get_back_button()]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "✅ API ID сохранен.\n\nТеперь отправьте API Hash:",
                reply_markup=reply_markup
            )
            context.user_data['setup_step'] = 'api_hash'
            bot_data['user_states'][user_id] = 'api_hash'
            await save_bot_state()
        else:
            await update.message.reply_text("❌ API ID должен состоять только из цифр. Попробуйте еще раз:")
    
    elif step == 'api_hash':
        config = load_user_config()
        config['api_hash'] = text
        save_user_config(config)
        
        keyboard = [[get_back_button()]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "✅ API Hash сохранен.\n\nОтправьте номер телефона в международном формате (например, +79123456789):",
            reply_markup=reply_markup
        )
        context.user_data['setup_step'] = 'phone'
        bot_data['user_states'][user_id] = 'phone'
        await save_bot_state()
    
    elif step == 'phone':
        if re.match(r'^\+\d{10,15}$', text):
            config = load_user_config()
            config['phone'] = text
            save_user_config(config)
            
            # Попытка входа в аккаунт
            try:
                client = TelegramClient('user_session', config['api_id'], config['api_hash'])
                await client.connect()
                
                result = await client.send_code_request(text)
                context.user_data['phone_code_hash'] = result.phone_code_hash
                context.user_data['client'] = client
                
                # Создаем правильную клавиатуру для ввода кода
                reply_markup = get_code_input_keyboard()
                
                await update.message.reply_text(
                    "📱 Код подтверждения отправлен на ваш номер.\n\n"
                    "Введенный код: \n\n"
                    "Введите код с помощью кнопок ниже:",
                    reply_markup=reply_markup
                )
                
                context.user_data['setup_step'] = 'code'
                context.user_data['entered_code'] = ''
                bot_data['user_states'][user_id] = 'code'
                await save_bot_state()
                
            except Exception as e:
                logger.error(f"Ошибка отправки кода: {e}")
                await update.message.reply_text(f"❌ Ошибка отправки кода: {e}\n\nПопробуйте еще раз:")
        else:
            await update.message.reply_text("❌ Неверный формат номера телефона. Используйте международный формат (+79123456789):")
    
    elif step == 'password':
        # Ввод пароля двухфакторной аутентификации
        try:
            client = context.user_data['client']
            await client.sign_in(password=text)
            
            config = load_user_config()
            config['password'] = text
            save_user_config(config)
            
            bot_data['telethon_client'] = client
            await save_bot_state()
            
            await update.message.reply_text("✅ Успешный вход в аккаунт!")
            
            # Возвращаемся к главному меню
            await show_main_menu(update, context, edit=False)
            
        except PasswordHashInvalidError:
            await update.message.reply_text("❌ Неверный пароль. Попробуйте еще раз:")
        except Exception as e:
            logger.error(f"Ошибка входа с паролем: {e}")
            await update.message.reply_text(f"❌ Ошибка входа: {e}")
    
    elif step == 'settings':
        # Обработка настроек масслукинга
        await parse_settings(update, context, text)
    
    elif step == 'manual_keywords':
        # Обработка ключевых слов для ручной настройки
        user_data = bot_data['pending_manual_setup'].get(user_id, {})
        keywords = [kw.strip() for kw in text.split(',') if kw.strip()]
        user_data['keywords'] = keywords
        bot_data['pending_manual_setup'][user_id] = user_data
        
        # Проверяем, выбраны ли темы
        if user_data.get('topics'):
            # Сохраняем настройки
            bot_data['settings']['keywords'] = keywords
            bot_data['settings']['topics'] = user_data['topics']
            bot_data['settings']['target_channel'] = ''
            await save_bot_state()
            
            await update.message.reply_text(
                f"✅ Настройки сохранены!\n\n"
                f"Ключевые слова: {', '.join(keywords)}\n"
                f"Темы: {', '.join(user_data['topics'])}"
            )
            
            # Очищаем временные данные
            del bot_data['pending_manual_setup'][user_id]
            context.user_data.clear()
            
            # Возвращаемся к главному меню
            await show_main_menu(update, context, edit=False)
        else:
            await update.message.reply_text(
                "✅ Ключевые слова сохранены. Теперь выберите темы и нажмите 'Готово ✅'"
            )

async def handle_code_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода кода подтверждения"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if not context.user_data.get('setup_step') == 'code':
        return
    
    data = query.data
    
    if data.startswith('code_'):
        action = data.split('_')[1]
        
        if action == 'delete':
            # Удаляем последнюю цифру
            if context.user_data['entered_code']:
                context.user_data['entered_code'] = context.user_data['entered_code'][:-1]
        
        elif action == 'send':
            # Отправляем код
            code = context.user_data['entered_code']
            if len(code) >= 5:
                try:
                    client = context.user_data['client']
                    phone_code_hash = context.user_data['phone_code_hash']
                    config = load_user_config()
                    
                    await client.sign_in(
                        phone=config['phone'],
                        code=code,
                        phone_code_hash=phone_code_hash
                    )
                    
                    bot_data['telethon_client'] = client
                    await save_bot_state()
                    
                    await query.edit_message_text("✅ Успешный вход в аккаунт!")
                    context.user_data.clear()
                    
                    # Показываем главное меню
                    await show_main_menu(update, context, edit=False)
                    return
                    
                except SessionPasswordNeededError:
                    keyboard = [[get_back_button()]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await query.edit_message_text(
                        "🔐 Требуется пароль двухфакторной аутентификации.\n\n"
                        "Отправьте ваш пароль:",
                        reply_markup=reply_markup
                    )
                    context.user_data['setup_step'] = 'password'
                    bot_data['user_states'][user_id] = 'password'
                    await save_bot_state()
                    return
                    
                except PhoneCodeInvalidError:
                    await query.edit_message_text(
                        "❌ Неверный код. Попробуйте еще раз.\n\n"
                        f"Введенный код: {context.user_data['entered_code']}\n\n"
                        "Введите код с помощью кнопок ниже:",
                        reply_markup=get_code_input_keyboard()
                    )
                    context.user_data['entered_code'] = ''
                    return
                    
                except Exception as e:
                    logger.error(f"Ошибка входа: {e}")
                    await query.edit_message_text(f"❌ Ошибка входа: {e}")
                    return
            else:
                await query.answer("Код должен содержать минимум 5 цифр", show_alert=True)
                return
        
        elif action == 'cancel':
            # Отменяем ввод кода
            if 'client' in context.user_data:
                await context.user_data['client'].disconnect()
            context.user_data.clear()
            await query.edit_message_text("❌ Настройка аккаунта отменена.")
            await show_main_menu(update, context, edit=False)
            return
        
        elif action.isdigit():
            # Добавляем цифру к коду
            if len(context.user_data['entered_code']) < 10:
                context.user_data['entered_code'] += action
        
        # Обновляем сообщение с введенным кодом
        entered_code = context.user_data['entered_code']
        await query.edit_message_text(
            f"📱 Код подтверждения отправлен на ваш номер.\n\n"
            f"Введенный код: {entered_code}\n\n"
            f"Введите код с помощью кнопок ниже:",
            reply_markup=get_code_input_keyboard()
        )

async def target_channel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройка целевого канала"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if not check_access(user_id):
        await query.answer("❌ Доступ ограничен", show_alert=True)
        return
    
    bot_data['user_states'][user_id] = 'target_channel'
    await save_bot_state()
    
    settings = bot_data['settings']
    
    current_channel = settings.get('target_channel', 'Не выбран')
    topics_text = ', '.join([f'"{topic}"' for topic in settings['topics']])
    keywords_text = ', '.join(settings['keywords'])
    
    message_text = f"""📺 Выбор целевого канала

Вы можете выбрать канал и бот будет рассылать комментарии и ставить реакции похожим каналам. Похожие каналы определяются по ключевым словам в названиях и тематике.

{'Текущий канал: ' + current_channel if current_channel != 'Не выбран' else ''}

Тематика: {topics_text}

Ключевые слова для поиска: {keywords_text}"""
    
    # Создаем специальную клавиатуру с кнопкой для выбора канала
    keyboard = [
        [InlineKeyboardButton("📺 Выбрать канал", callback_data="select_channel")],
        [InlineKeyboardButton("✏️ Настроить вручную", callback_data="manual_setup")],
        [get_back_button()]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(message_text, reply_markup=reply_markup)

async def select_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать инструкцию по выбору канала"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    # Создаем обычную клавиатуру с кнопкой для выбора канала
    keyboard = [
        [KeyboardButton(
            "📺 Поделиться каналом",
            request_chat={
                'request_id': 1,
                'chat_is_channel': True
            }
        )]
    ]
    
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    
    await query.edit_message_text(
        "📺 Выбор канала\n\n"
        "Нажмите кнопку ниже, чтобы выбрать канал для анализа.\n"
        "После выбора канал будет проанализирован с помощью GPT-4."
    )
    
    await context.bot.send_message(
        chat_id=user_id,
        text="👇 Нажмите кнопку для выбора канала:",
        reply_markup=reply_markup
    )

async def manual_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручная настройка тем и ключевых слов"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if not check_access(user_id):
        await query.answer("❌ Доступ ограничен", show_alert=True)
        return
    
    bot_data['user_states'][user_id] = 'manual_setup'
    await save_bot_state()
    
    # Инициализируем данные пользователя
    bot_data['pending_manual_setup'][user_id] = {'topics': [], 'keywords': []}
    
    # Создаем клавиатуру с темами (4 столбца)
    keyboard = []
    for i in range(0, len(AVAILABLE_TOPICS), 4):
        row = []
        for j in range(4):
            if i + j < len(AVAILABLE_TOPICS):
                topic = AVAILABLE_TOPICS[i + j]
                row.append(InlineKeyboardButton(topic, callback_data=f"topic_{i+j}"))
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("✅ Готово", callback_data="topics_done")])
    keyboard.append([get_back_button()])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "✏️ Ручная настройка\n\n"
        "Пожалуйста, отправьте список ключевых слов через запятую и выберите темы из списка ниже. Нажмите 'Готово ✅' когда выберете все нужные темы.\n\n"
        "📝 Отправьте ключевые слова одним сообщением:",
        reply_markup=reply_markup
    )
    
    context.user_data['setup_step'] = 'manual_keywords'
    bot_data['user_states'][user_id] = 'topic_selection'
    await save_bot_state()

async def handle_topic_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора тем"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    if data.startswith('topic_'):
        topic_index = int(data.split('_')[1])
        topic = AVAILABLE_TOPICS[topic_index]
        
        user_data = bot_data['pending_manual_setup'].get(user_id, {'topics': [], 'keywords': []})
        
        if topic in user_data['topics']:
            # Убираем тему
            user_data['topics'].remove(topic)
        else:
            # Добавляем тему
            user_data['topics'].append(topic)
        
        bot_data['pending_manual_setup'][user_id] = user_data
        
        # Обновляем клавиатуру
        keyboard = []
        for i in range(0, len(AVAILABLE_TOPICS), 4):
            row = []
            for j in range(4):
                if i + j < len(AVAILABLE_TOPICS):
                    topic_name = AVAILABLE_TOPICS[i + j]
                    if topic_name in user_data['topics']:
                        display_name = f"✅ {topic_name}"
                    else:
                        display_name = topic_name
                    row.append(InlineKeyboardButton(display_name, callback_data=f"topic_{i+j}"))
            keyboard.append(row)
        
        keyboard.append([InlineKeyboardButton("✅ Готово", callback_data="topics_done")])
        keyboard.append([get_back_button()])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_reply_markup(reply_markup=reply_markup)
    
    elif data == 'topics_done':
        user_data = bot_data['pending_manual_setup'].get(user_id, {'topics': [], 'keywords': []})
        
        if not user_data['topics']:
            await query.answer("❌ Выберите хотя бы одну тему", show_alert=True)
            return
        
        # Проверяем, есть ли ключевые слова
        if user_data.get('keywords'):
            # Сохраняем настройки
            bot_data['settings']['keywords'] = user_data['keywords']
            bot_data['settings']['topics'] = user_data['topics']
            bot_data['settings']['target_channel'] = ''
            await save_bot_state()
            
            keyboard = [[get_back_button()]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"✅ Настройки сохранены!\n\n"
                f"🔑 Ключевые слова: {', '.join(user_data['keywords'])}\n\n"
                f"🏷️ Темы: {', '.join(user_data['topics'])}",
                reply_markup=reply_markup
            )
            
            # Очищаем временные данные
            del bot_data['pending_manual_setup'][user_id]
            context.user_data.clear()
        else:
            keyboard = [[get_back_button()]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"✅ Темы выбраны: {', '.join(user_data['topics'])}\n\n"
                "📝 Теперь отправьте список ключевых слов через запятую:",
                reply_markup=reply_markup
            )

async def parse_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Парсинг настроек масслукинга"""
    try:
        lines = [line.strip() for line in text.lower().split('\n') if line.strip()]
        
        new_settings = {}
        
        for line in lines:
            if 'максимальное количество каналов:' in line:
                value = line.split(':')[1].strip()
                if value == '∞':
                    new_settings['max_channels'] = float('inf')
                else:
                    new_settings['max_channels'] = int(value)
            
            elif 'количество последних постов:' in line:
                value = line.split(':')[1].strip()
                if '-' in value:
                    min_val, max_val = map(int, value.split('-'))
                    new_settings['posts_range'] = (min_val, max_val)
                else:
                    posts_num = int(value)
                    new_settings['posts_range'] = (posts_num, posts_num)
            
            elif 'задержка между действиями:' in line:
                value = line.split(':')[1].strip()
                if value == '_':
                    new_settings['delay_range'] = (0, 0)
                elif '-' in value:
                    parts = value.replace('секунд', '').strip().split('-')
                    min_val, max_val = map(int, parts)
                    new_settings['delay_range'] = (min_val, max_val)
                else:
                    delay = int(value.replace('секунд', '').strip())
                    new_settings['delay_range'] = (delay, delay)
        
        if new_settings:
            bot_data['settings'].update(new_settings)
            await save_bot_state()
            await update.message.reply_text("✅ Настройки успешно обновлены!")
            await show_main_menu(update, context, edit=False)
        else:
            await update.message.reply_text(
                "❌ Неверный формат. Используйте формат:\n\n"
                "Максимальное количество каналов: 150\n"
                "Количество последних постов: 1-5\n"
                "Задержка между действиями: 20-1000"
            )
    
    except Exception as e:
        logger.error(f"Ошибка парсинга настроек: {e}")
        await update.message.reply_text("❌ Ошибка в формате настроек. Проверьте правильность ввода.")
    
    finally:
        context.user_data.clear()

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню настроек масслукинга"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if not check_access(user_id):
        await query.answer("❌ Доступ ограничен", show_alert=True)
        return
    
    bot_data['user_states'][user_id] = 'settings'
    await save_bot_state()
    
    settings = bot_data['settings']
    
    max_channels = "∞" if settings['max_channels'] == float('inf') else str(settings['max_channels'])
    posts_range = f"{settings['posts_range'][0]}-{settings['posts_range'][1]}" if settings['posts_range'][0] != settings['posts_range'][1] else str(settings['posts_range'][0])
    delay_range = "_" if settings['delay_range'] == (0, 0) else f"{settings['delay_range'][0]}-{settings['delay_range'][1]}"
    
    message_text = f"""⚙️ Параметры масслукинга

📊 Текущие параметры:

🎯 Максимальное количество каналов для масслукинга: {max_channels}

📝 Количество последних постов для комментариев и реакций: {posts_range}

⏱️ Задержка между действиями: {delay_range} секунд

Для смены параметров отправьте сообщение с параметрами в следующем формате:

`Максимальное количество каналов: число или ∞ для неограниченного количества`

`Количество последних постов: число минимум-максимум фиксированное число` (отправка комментариев под фиксированное количество последних постов в каждом канале)

`Задержка между действиями: минимум-максимум секунд или _ для отключения задержки` (отключать задержку категорически не рекомендуется)

🔧 Пример:

Максимальное количество каналов: 150
Количество последних постов: 1-5
Задержка между действиями: 20-1000"""
    
    keyboard = [[get_back_button()]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(message_text, parse_mode='Markdown', reply_markup=reply_markup)
    context.user_data['setup_step'] = 'settings'

async def toggle_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск/остановка рассылки"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if not check_access(user_id):
        await query.answer("❌ Доступ ограничен", show_alert=True)
        return
    
    if bot_data['is_running']:
        bot_data['is_running'] = False
        await save_bot_state()
        
        keyboard = [[get_back_button()]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text("⏹️ Рассылка остановлена", reply_markup=reply_markup)
        
        # Остановка поисковика каналов
        try:
            import channel_search_engine
            await channel_search_engine.stop_search()
        except Exception as e:
            logger.error(f"Ошибка остановки поисковика: {e}")
            
        # Остановка масслукера
        try:
            import masslooker
            await masslooker.stop_masslooking()
        except Exception as e:
            logger.error(f"Ошибка остановки масслукера: {e}")
    else:
        if not bot_data['telethon_client']:
            keyboard = [[get_back_button()]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("❌ Сначала добавьте аккаунт", reply_markup=reply_markup)
            return
        
        bot_data['is_running'] = True
        await save_bot_state()
        
        keyboard = [[get_back_button()]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text("▶️ Рассылка запущена", reply_markup=reply_markup)
        
        # Запуск поисковика каналов
        try:
            import channel_search_engine
            await channel_search_engine.start_search(bot_data['settings'])
        except Exception as e:
            logger.error(f"Ошибка запуска поисковика: {e}")
        
        # Запуск масслукера
        try:
            import masslooker
            await masslooker.start_masslooking(bot_data['telethon_client'], bot_data['settings'])
        except Exception as e:
            logger.error(f"Ошибка запуска масслукера: {e}")

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать статистику"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if not check_access(user_id):
        await query.answer("❌ Доступ ограничен", show_alert=True)
        return
    
    bot_data['user_states'][user_id] = 'statistics'
    await save_bot_state()
    
    stats = bot_data['statistics']
    
    message_text = f"""📊 Статистика

💬 Отправлено комментариев: {stats['comments_sent']}

📺 Обработано каналов: {stats['channels_processed']}

👍 Поставлено реакций: {stats['reactions_set']}"""
    
    keyboard = [[get_back_button()]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(message_text, reply_markup=reply_markup)

async def toggle_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ограничение/возврат доступа"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if user_id != bot_data['admin_user']:
        await query.answer("❌ Только администратор может управлять доступом", show_alert=True)
        return
    
    keyboard = [[get_back_button()]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if bot_data['access_restricted']:
        bot_data['access_restricted'] = False
        await save_bot_state()
        await query.edit_message_text("🔓 Доступ к боту восстановлен для всех пользователей", reply_markup=reply_markup)
    else:
        bot_data['access_restricted'] = True
        await save_bot_state()
        await query.edit_message_text("🔒 Доступ к боту ограничен только для администратора", reply_markup=reply_markup)

async def handle_channel_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора канала"""
    if update.message and hasattr(update.message, 'chat_shared'):
        chat_shared = update.message.chat_shared
        if chat_shared.request_id == 1:  # ID запроса для выбора канала
            chat_id = chat_shared.chat_id
            
            # Убираем клавиатуру
            from telegram import ReplyKeyboardRemove
            await update.message.reply_text("📺 Канал получен, анализируем...", reply_markup=ReplyKeyboardRemove())
            
            # Получаем информацию о канале
            try:
                if bot_data['telethon_client']:
                    entity = await bot_data['telethon_client'].get_entity(chat_id)
                    channel_username = entity.username if hasattr(entity, 'username') and entity.username else None
                    
                    if channel_username:
                        channel_link = f"https://t.me/{channel_username}"
                        
                        # Анализируем канал через модуль поиска
                        try:
                            import channel_search_engine
                            topics, keywords = await channel_search_engine.analyze_channel(chat_id)
                            
                            # Сохраняем настройки
                            bot_data['settings']['target_channel'] = channel_link
                            bot_data['settings']['topics'] = topics
                            bot_data['settings']['keywords'] = keywords
                            await save_bot_state()
                            
                            await update.message.reply_text(
                                f"✅ Канал выбран и проанализирован!\n\n"
                                f"📺 Канал: {channel_link}\n\n"
                                f"🏷️ Темы: {', '.join(topics)}\n\n"
                                f"🔑 Ключевые слова: {', '.join(keywords)}"
                            )
                            
                            # Возвращаемся к главному меню
                            await show_main_menu(update, context, edit=False)
                        except Exception as e:
                            logger.error(f"Ошибка анализа канала: {e}")
                            await update.message.reply_text(f"❌ Ошибка анализа канала: {e}")
                    else:
                        await update.message.reply_text("❌ Канал должен быть публичным (иметь username)")
                else:
                    await update.message.reply_text("❌ Сначала добавьте аккаунт")
                    
            except Exception as e:
                logger.error(f"Ошибка получения информации о канале: {e}")
                await update.message.reply_text(f"❌ Ошибка получения информации о канале: {e}")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Общий обработчик callback запросов"""
    query = update.callback_query
    data = query.data
    
    if data == "back":
        await handle_back_button(update, context)
    elif data == "account_setup":
        await account_setup(update, context)
    elif data == "target_channel":
        await target_channel_setup(update, context)
    elif data == "select_channel":
        await select_channel(update, context)
    elif data == "manual_setup":
        await manual_setup(update, context)
    elif data.startswith("topic_") or data == "topics_done":
        await handle_topic_selection(update, context)
    elif data == "settings":
        await settings_menu(update, context)
    elif data == "toggle_run":
        await toggle_run(update, context)
    elif data == "statistics":
        await show_statistics(update, context)
    elif data == "toggle_access":
        await toggle_access(update, context)
    elif data.startswith("code_"):
        await handle_code_input(update, context)

def update_statistics(comments=0, channels=0, reactions=0):
    """Обновление статистики"""
    bot_data['statistics']['comments_sent'] += comments
    bot_data['statistics']['channels_processed'] += channels
    bot_data['statistics']['reactions_set'] += reactions
    
    # Асинхронно сохраняем в базу данных
    asyncio.create_task(save_bot_state())

async def ensure_telethon_client_initialized():
    """Проверка и инициализация Telethon клиента"""
    if bot_data['telethon_client'] is None:
        config = load_user_config()
        if config.get('api_id') and config.get('api_hash') and config.get('phone'):
            try:
                client = TelegramClient('user_session', config['api_id'], config['api_hash'])
                await client.start(phone=config['phone'])
                bot_data['telethon_client'] = client
                logger.info("Telethon клиент успешно инициализирован")
                return True
            except Exception as e:
                logger.error(f"Ошибка инициализации Telethon клиента: {e}")
                return False
    return True

async def run_bot(bot_token):
    """Асинхронная функция запуска бота"""
    logger.info("Запуск бота интерфейса...")
    
    try:
        # Инициализируем базу данных
        await init_database()
        
        # Загружаем состояние бота
        await load_bot_state()
        
        # Создаем приложение
        application = Application.builder().token(bot_token).build()
        
        # Добавляем обработчики
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CallbackQueryHandler(handle_callback_query))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
        application.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED, handle_channel_selection))
        
        logger.info("Бот запущен и готов к работе")
        
        # Запускаем бота с правильным управлением lifecycle
        async with application:
            await application.start()
            await application.updater.start_polling()
            
            # Ждем завершения (будет прервано через CancelledError)
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                logger.info("Получен сигнал завершения бота")
            finally:
                logger.info("Завершение работы бота...")
                await save_bot_state()
                await close_database()
                await application.updater.stop()
                await application.stop()
        
    except Exception as e:
        logger.error(f"Ошибка в run_bot: {e}")
        raise

# Упростить функцию main:
def main(bot_token):
    """Основная функция бота"""
    asyncio.run(run_bot(bot_token))

if __name__ == "__main__":
    # Для тестирования
    import sys
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        print("Укажите токен бота как аргумент")