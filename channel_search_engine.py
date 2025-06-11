import asyncio
import logging
import time
import random
from datetime import datetime, timedelta
from typing import List, Tuple, Set, Optional
import re
import nest_asyncio
import os
import json
import atexit

nest_asyncio.apply()

log_filename = 'channel_search_log.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename, mode='a', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

logging.getLogger('selenium').setLevel(logging.WARNING)
logging.getLogger('seleniumbase').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('telethon').setLevel(logging.WARNING)

try:
    from seleniumbase import Driver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.keys import Keys
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from telethon import TelegramClient
    from telethon.errors import ChannelPrivateError, ChannelInvalidError
    import g4f
except ImportError as e:
    logger.error(f"Ошибка импорта библиотек: {e}")
    raise

from database import db

search_active = False
found_channels: Set[str] = set()
driver = None
current_settings = {}
telethon_client = None
search_state = {
    'current_topic_index': 0,
    'current_keyword_index': 0,
    'topics': [],
    'keywords': [],
    'last_search_time': None,
    'cycle_start_time': None,
    'search_session_id': None
}

SEARCH_STATE_FILE = 'search_state.json'

def save_search_state():
    """Сохранение состояния поиска в файл"""
    try:
        state_data = {
            'current_topic_index': search_state['current_topic_index'],
            'current_keyword_index': search_state['current_keyword_index'],
            'topics': search_state['topics'],
            'keywords': search_state['keywords'],
            'last_search_time': search_state['last_search_time'].isoformat() if search_state['last_search_time'] else None,
            'cycle_start_time': search_state['cycle_start_time'].isoformat() if search_state['cycle_start_time'] else None,
            'search_session_id': search_state['search_session_id'],
            'found_channels': list(found_channels)
        }
        
        with open(SEARCH_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state_data, f, indent=2, ensure_ascii=False)
        
        logger.debug("Состояние поиска сохранено")
    except Exception as e:
        logger.error(f"Ошибка сохранения состояния поиска: {e}")

def load_search_state():
    """Загрузка состояния поиска из файла"""
    global found_channels
    
    try:
        if os.path.exists(SEARCH_STATE_FILE):
            with open(SEARCH_STATE_FILE, 'r', encoding='utf-8') as f:
                state_data = json.load(f)
            
            search_state['current_topic_index'] = state_data.get('current_topic_index', 0)
            search_state['current_keyword_index'] = state_data.get('current_keyword_index', 0)
            search_state['topics'] = state_data.get('topics', [])
            search_state['keywords'] = state_data.get('keywords', [])
            
            if state_data.get('last_search_time'):
                search_state['last_search_time'] = datetime.fromisoformat(state_data['last_search_time'])
            
            if state_data.get('cycle_start_time'):
                search_state['cycle_start_time'] = datetime.fromisoformat(state_data['cycle_start_time'])
            
            search_state['search_session_id'] = state_data.get('search_session_id')
            
            found_channels_list = state_data.get('found_channels', [])
            found_channels = set(found_channels_list)
            
            logger.info(f"Состояние поиска восстановлено: тема {search_state['current_topic_index']}, "
                       f"ключевое слово {search_state['current_keyword_index']}, "
                       f"найдено каналов: {len(found_channels)}")
            return True
    except Exception as e:
        logger.error(f"Ошибка загрузки состояния поиска: {e}")
    
    return False

def reset_search_state():
    """Сброс состояния поиска"""
    global found_channels
    
    search_state.update({
        'current_topic_index': 0,
        'current_keyword_index': 0,
        'topics': [],
        'keywords': [],
        'last_search_time': None,
        'cycle_start_time': None,
        'search_session_id': None
    })
    
    found_channels = set()
    
    try:
        if os.path.exists(SEARCH_STATE_FILE):
            os.remove(SEARCH_STATE_FILE)
    except Exception as e:
        logger.error(f"Ошибка удаления файла состояния: {e}")

async def save_search_state_to_db():
    """Сохранение состояния поиска в базу данных"""
    try:
        await db.save_bot_state('search_state', search_state)
        await db.save_bot_state('found_channels', list(found_channels))
        logger.debug("Состояние поиска сохранено в базу данных")
    except Exception as e:
        logger.error(f"Ошибка сохранения состояния поиска в БД: {e}")

async def load_search_state_from_db():
    """Загрузка состояния поиска из базы данных"""
    global found_channels
    
    try:
        saved_state = await db.load_bot_state('search_state', {})
        saved_channels = await db.load_bot_state('found_channels', [])
        
        if saved_state:
            search_state.update(saved_state)
            
            if search_state.get('last_search_time') and isinstance(search_state['last_search_time'], str):
                search_state['last_search_time'] = datetime.fromisoformat(search_state['last_search_time'])
            
            if search_state.get('cycle_start_time') and isinstance(search_state['cycle_start_time'], str):
                search_state['cycle_start_time'] = datetime.fromisoformat(search_state['cycle_start_time'])
            
            found_channels = set(saved_channels)
            
            logger.info(f"Состояние поиска восстановлено из БД: тема {search_state['current_topic_index']}, "
                       f"ключевое слово {search_state['current_keyword_index']}, "
                       f"найдено каналов: {len(found_channels)}")
            return True
    except Exception as e:
        logger.error(f"Ошибка загрузки состояния поиска из БД: {e}")
    
    return False

def should_continue_from_saved_state():
    """Проверка, нужно ли продолжить с сохраненного состояния"""
    if not search_state.get('last_search_time'):
        return False
    
    time_since_last = datetime.now() - search_state['last_search_time']
    return time_since_last < timedelta(hours=2)

def get_progress_info():
    """Получение информации о прогрессе поиска"""
    total_combinations = len(search_state['topics']) * len(search_state['keywords'])
    current_combination = search_state['current_topic_index'] * len(search_state['keywords']) + search_state['current_keyword_index']
    
    progress_percent = (current_combination / total_combinations * 100) if total_combinations > 0 else 0
    
    return {
        'current_topic': search_state['topics'][search_state['current_topic_index']] if search_state['current_topic_index'] < len(search_state['topics']) else None,
        'current_keyword': search_state['keywords'][search_state['current_keyword_index']] if search_state['current_keyword_index'] < len(search_state['keywords']) else None,
        'progress_percent': progress_percent,
        'found_channels_count': len(found_channels),
        'total_combinations': total_combinations,
        'current_combination': current_combination
    }

def setup_driver():
    """Настройка и инициализация веб-драйвера"""
    logger.info("Инициализация веб-драйвера...")
    
    try:
        driver = Driver(uc=True, headless=False)
        driver.set_window_size(600, 1200)
        
        desktop_user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        driver.execute_cdp_cmd('Network.setUserAgentOverride', {"userAgent": desktop_user_agent})
        
        def cleanup_driver():
            try:
                if driver:
                    driver.quit()
            except:
                pass
        
        atexit.register(cleanup_driver)
        
        driver.get("about:blank")
        logger.info("Веб-драйвер успешно инициализирован")
        
        return driver
    except Exception as e:
        logger.error(f"Ошибка инициализации драйвера: {e}")
        return None

def wait_and_find_element(driver, selectors, timeout=30):
    """Поиск элемента по нескольким селекторам"""
    if isinstance(selectors, str):
        selectors = [selectors]
    
    for selector in selectors:
        try:
            if selector.startswith('//'):
                element = WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((By.XPATH, selector))
                )
            else:
                element = WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                )
            return element
        except TimeoutException:
            continue
        except Exception as e:
            logger.warning(f"Ошибка поиска элемента {selector}: {e}")
            continue
    
    return None

def wait_and_click_element(driver, selectors, timeout=30):
    """Клик по элементу с ожиданием его доступности"""
    element = wait_and_find_element(driver, selectors, timeout)
    if element:
        try:
            if isinstance(selectors, str):
                selectors = [selectors]
            
            for selector in selectors:
                try:
                    if selector.startswith('//'):
                        clickable_element = WebDriverWait(driver, timeout).until(
                            EC.element_to_be_clickable((By.XPATH, selector))
                        )
                    else:
                        clickable_element = WebDriverWait(driver, timeout).until(
                            EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                        )
                    clickable_element.click()
                    return True
                except:
                    continue
        except Exception as e:
            logger.warning(f"Ошибка клика по элементу: {e}")
    
    return False

def navigate_to_channel_search(driver):
    """Навигация к странице поиска каналов"""
    try:
        logger.info("Переход на tgstat.ru...")
        driver.get("https://tgstat.ru/")
        time.sleep(3)
        
        logger.info("Открываем меню...")
        menu_selectors = [
            'a.d-flex.d-lg-none.nav-user',
            '.nav-user',
            '[data-toggle="collapse"]',
            'i.uil-bars'
        ]
        
        if not wait_and_click_element(driver, menu_selectors, 10):
            logger.warning("Не удалось найти кнопку меню, возможно меню уже открыто")
        
        time.sleep(2)
        
        logger.info("Открываем каталог...")
        catalog_selectors = [
            '#topnav-catalog',
            'a[id="topnav-catalog"]',
            '.nav-link.dropdown-toggle',
            '//a[contains(text(), "Каталог")]'
        ]
        
        if not wait_and_click_element(driver, catalog_selectors, 10):
            logger.error("Не удалось найти кнопку каталога")
            return False
        
        time.sleep(2)
        
        logger.info("Переходим к поиску каналов...")
        search_selectors = [
            'a[href="/channels/search"]',
            '//a[contains(text(), "Поиск каналов")]',
            '.dropdown-item[href="/channels/search"]'
        ]
        
        if not wait_and_click_element(driver, search_selectors, 10):
            logger.error("Не удалось найти кнопку поиска каналов")
            return False
        
        time.sleep(3)
        logger.info("Успешно перешли на страницу поиска каналов")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка навигации: {e}")
        return False

def search_channels(driver, keyword: str, topic: str, first_search: bool = False):
    """Поиск каналов по ключевому слову и теме"""
    try:
        logger.info(f"Поиск каналов по ключевому слову: '{keyword}', тема: '{topic}'")
        
        keyword_input = wait_and_find_element(driver, [
            '#q',
            'input[name="q"]',
            '.form-control[name="q"]'
        ])
        
        if keyword_input:
            keyword_input.clear()
            time.sleep(1)
            keyword_input.send_keys(keyword)
            logger.info(f"Введено ключевое слово: {keyword}")
        else:
            logger.error("Не удалось найти поле ввода ключевого слова")
            return []
        
        time.sleep(1)
        
        topic_input = wait_and_find_element(driver, [
            '.select2-search__field',
            'input[role="searchbox"]',
            '.select2-search input'
        ])
        
        if topic_input:
            topic_input.clear()
            time.sleep(1)
            topic_input.send_keys(topic)
            time.sleep(2)
            topic_input.send_keys(Keys.ENTER)
            logger.info(f"Введена тема: {topic}")
        else:
            logger.error("Не удалось найти поле ввода темы")
            return []
        
        time.sleep(2)
        
        if first_search:
            description_checkbox = wait_and_find_element(driver, [
                '#inabout',
                'input[name="inAbout"]',
                '.custom-control-input[name="inAbout"]'
            ])
            
            if description_checkbox and not description_checkbox.is_selected():
                driver.execute_script("arguments[0].click();", description_checkbox)
                logger.info("Отмечен поиск в описании")
            
            time.sleep(1)
            
            channel_type_select = wait_and_find_element(driver, [
                '#channeltype',
                'select[name="channelType"]',
                '.custom-select[name="channelType"]'
            ])
            
            if channel_type_select:
                driver.execute_script("arguments[0].value = 'public';", channel_type_select)
                logger.info("Выбран тип канала: публичный")
            
            time.sleep(1)
        
        search_button = wait_and_find_element(driver, [
            '#search-form-submit-btn',
            'button[type="button"].btn-primary',
            '.btn.btn-primary.w-100'
        ])
        
        if search_button:
            driver.execute_script("arguments[0].click();", search_button)
            logger.info("Нажата кнопка поиска")
        else:
            logger.error("Не удалось найти кнопку поиска")
            return []
       
        time.sleep(5)
        
        channels = extract_channel_usernames(driver)
        logger.info(f"Найдено каналов: {len(channels)}")
        
        search_state['last_search_time'] = datetime.now()
        save_search_state()
        
        return channels
        
    except Exception as e:
        logger.error(f"Ошибка поиска каналов: {e}")
        return []

def extract_channel_usernames(driver) -> List[str]:
    """Извлечение юзернеймов каналов из результатов поиска"""
    usernames = []
    
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '.card.peer-item-row, .peer-item-row'))
        )
        
        channel_cards = driver.find_elements(By.CSS_SELECTOR, '.card.peer-item-row, .peer-item-row')
        
        if not channel_cards:
            logger.warning("Не найдено карточек каналов")
            return usernames
        
        for card in channel_cards:
            try:
                link_elements = card.find_elements(By.CSS_SELECTOR, 'a[href*="/channel/@"]')
                
                for link in link_elements:
                    href = link.get_attribute('href')
                    if href and '/channel/@' in href:
                        match = re.search(r'/channel/(@[^/]+)', href)
                        if match:
                            username = match.group(1)
                            if username not in usernames:
                                usernames.append(username)
                                logger.info(f"Найден канал: {username}")
                                break
                
            except Exception as e:
                logger.warning(f"Ошибка обработки карточки канала: {e}")
                continue
        
        return usernames
        
    except TimeoutException:
        logger.warning("Результаты поиска не загрузились за отведенное время")
        return usernames
    except Exception as e:
        logger.error(f"Ошибка извлечения юзернеймов: {e}")
        return usernames

async def check_channel_availability(client: TelegramClient, username: str) -> bool:
    """Проверка доступности комментариев в канале"""
    try:
        if not username.startswith('@'):
            username = '@' + username
        
        entity = await client.get_entity(username)
        
        if hasattr(entity, 'broadcast') and entity.broadcast:
            if hasattr(entity, 'linked_chat_id') and entity.linked_chat_id:
                try:
                    discussion_group = await client.get_entity(entity.linked_chat_id)
                    if hasattr(discussion_group, 'left') and discussion_group.left:
                        return False
                    return True
                except:
                    return False
            else:
                return False
        
        return True
        
    except (ChannelPrivateError, ChannelInvalidError):
        logger.warning(f"Канал {username} недоступен")
        return False
    except Exception as e:
        logger.warning(f"Ошибка проверки канала {username}: {e}")
        return False

async def analyze_channel(channel_id: int) -> Tuple[List[str], List[str]]:
    """Анализ канала для определения тематики и ключевых слов"""
    try:
        import bot_interface
        client = bot_interface.bot_data.get('telethon_client')
        
        if not client:
            logger.error("Telethon клиент не инициализирован в bot_interface")
            return [], []
        
        entity = await client.get_entity(channel_id)
        
        channel_info = []
        
        if hasattr(entity, 'title'):
            channel_info.append(f"Название: {entity.title}")
        
        if hasattr(entity, 'about') and entity.about:
            channel_info.append(f"Описание: {entity.about}")
        
        posts_text = []
        async for message in client.iter_messages(entity, limit=20):
            if message.text:
                posts_text.append(message.text)
        
        if posts_text:
            channel_info.append(f"Примеры постов: {' | '.join(posts_text)}")
        
        full_text = "\n".join(channel_info)
        
        prompt = f"""Ты эксперт по анализу контента и тематической классификации Telegram-каналов. На основе предоставленных данных о канале (название, описание и последние 20 постов) выполни следующие задачи:

1. Сгенерируй список ключевых слов, которые могут использоваться в названиях похожих каналов.  
- Ключевые слова должны **строго соответствовать основной тематике канала** и отражать его основную суть.  
- Исключи слова, которые упоминаются косвенно, в единичных случаях или не являются центральными для контента (например, если канал про спорт, не включай слова, связанные с музыкой или путешествиями, если они упомянуты случайно).  
- Ключевые слова должны быть **конкретными**, релевантными и подходящими для использования в названиях Telegram-каналов.  
- Сфокусируйся только на темах, которые явно доминируют в контенте (например, для спортивного канала — тренировки, фитнес, спорт, атлетика, а не общие слова вроде "мотивация" или "жизнь").  

2. Определи основную тему или темы канала из следующего списка: ["Бизнес и стартапы", "Блоги", "Букмекерство", "Видео и фильмы", "Даркнет", "Дизайн", "Для взрослых", "Еда и кулинария", "Здоровье и Фитнес", "Игры", "Инстаграм", "Интерьер и строительство", "Искусство", "Картинки и фото", "Карьера", "Книги", "Криптовалюты", "Курсы и гайды", "Лингвистика", "Маркетинг, PR, реклама", "Медицина", "Мода и красота", "Музыка", "Новости и СМИ", "Образование", "Познавательное", "Политика", "Право", "Природа", "Продажи", "Психология", "Путешествия", "Религия", "Рукоделие", "Семья и дети", "Софт и приложения", "Спорт", "Технологии", "Транспорт", "Цитаты", "Шок-контент", "Эзотерика", "Экономика", "Эроктика", "Юмор и развлечения", "Другое"].  
- Укажи **только те темы, которые явно и непосредственно связаны с основным содержимым канала**.  - Исключи темы, которые упоминаются случайно, косвенно или не являются центральными (например, если канал про спорт, не включай "Музыку" или "Путешествия", если они упомянуты в одном посте).  
- Если канал охватывает несколько тем, укажи их, но только если они **регулярно и явно** присутствуют в контенте. 
- Если ни одна из тем списка не подходит, укажи "Другое".

**Дополнительные указания:**  
- Анализируй контент постов, чтобы определить доминирующие темы. **Пример**: если в постах канала про спорт есть одно упоминание музыки для тренировок, это **не делает музыку основной темой**.  
- Приоритет отдавай темам, которые составляют не менее 80% контента (по смыслу, а не количеству постов).  
- Если описание канала явно указывает на одну тему (например, "Канал про спорт и тренировки"), игнорируй случайные отклонения в постах, не связанные с этой темой.  

**Пример:**  
Если канал про спорт:  
ТЕМЫ: Спорт, Здоровье и Фитнес  
КЛЮЧЕВЫЕ_СЛОВА: спорт, фитнес, тренировки, атлетика, здоровье  

Если в постах канала про спорт случайно упомянута музыка для тренировок или путешествие на спортивное событие, **не включай "Музыку" или "Путешествия" в темы**.  

Формат ответа:  
ТЕМЫ: тема1, тема2  
КЛЮЧЕВЫЕ_СЛОВА: слово1, слово2, слово3, слово4, слово5  

Входные данные:  

{full_text}"""
        
        try:
            response = g4f.ChatCompletion.create(
                model=g4f.models.gpt_4,
                messages=[{"role": "user", "content": prompt}],
                stream=False
            )
            
            topics = []
            keywords = []
            
            lines = response.split('\n')
            for line in lines:
                if line.startswith('ТЕМЫ:'):
                    topics_text = line.replace('ТЕМЫ:', '').strip()
                    topics = [topic.strip() for topic in topics_text.split(',') if topic.strip()]
                elif line.startswith('КЛЮЧЕВЫЕ_СЛОВА:'):
                    keywords_text = line.replace('КЛЮЧЕВЫЕ_СЛОВА:', '').strip()
                    keywords = [kw.strip() for kw in keywords_text.split(',') if kw.strip()]
            
            if not topics:
                topics = ['Бизнес и стартапы', 'Маркетинг, PR, реклама']
            if not keywords:
                keywords = ['бизнес', 'маркетинг', 'продвижение', 'реклама', 'стратегия']
            
            logger.info(f"Анализ канала завершен. Темы: {topics}, Ключевые слова: {keywords}")
            return topics, keywords
            
        except Exception as e:
            logger.error(f"Ошибка анализа с GPT-4: {e}")
            return ['Бизнес и стартапы', 'Маркетинг, PR, реклама'], ['бизнес', 'маркетинг', 'продвижение']
    
    except Exception as e:
        logger.error(f"Ошибка анализа канала: {e}")
        return [], []

async def process_found_channels(channels: List[str]):
    """Обработка найденных каналов"""
    import bot_interface
    client = bot_interface.bot_data.get('telethon_client')
    
    if not client:
        logger.error("Telethon клиент не инициализирован в bot_interface")
        return
    
    for username in channels:
        if username in found_channels:
            continue
        
        if not search_active:
            break
        
        try:
            if await check_channel_availability(client, username):
                logger.info(f"Канал {username} доступен для комментариев")
                found_channels.add(username)
                
                save_search_state()
                await save_search_state_to_db()
                
                try:
                    import masslooker
                    await masslooker.add_channel_to_queue(username)
                    logger.info(f"Канал {username} добавлен в очередь масслукера")
                except Exception as e:
                    logger.error(f"Ошибка добавления канала в очередь: {e}")
            else:
                logger.info(f"Канал {username} недоступен для комментариев")
        
        except Exception as e:
            logger.error(f"Ошибка обработки канала {username}: {e}")
        
        await asyncio.sleep(random.uniform(1, 3))

async def search_loop():
    """Основной цикл поиска каналов"""
    global driver, search_active
    
    state_loaded = load_search_state() or await load_search_state_from_db()
    
    if not state_loaded or not should_continue_from_saved_state():
        logger.info("Начинаем новый цикл поиска")
        reset_search_state()
        search_state['topics'] = current_settings.get('topics', [])
        search_state['keywords'] = current_settings.get('keywords', [])
        search_state['cycle_start_time'] = datetime.now()
        search_state['search_session_id'] = f"search_{int(time.time())}"
    else:
        logger.info("Продолжаем поиск с сохраненного состояния")
        progress = get_progress_info()
        logger.info(f"Прогресс: {progress['progress_percent']:.1f}% "
                   f"({progress['current_combination']}/{progress['total_combinations']})")
        logger.info(f"Текущая тема: {progress['current_topic']}")
        logger.info(f"Текущее ключевое слово: {progress['current_keyword']}")
        logger.info(f"Найдено каналов: {progress['found_channels_count']}")
    
    while search_active:
        try:
            keywords = search_state['keywords']
            topics = search_state['topics']
            
            if not keywords or not topics:
                logger.warning("Ключевые слова или темы не настроены")
                await asyncio.sleep(300)
                continue
            
            if search_state['current_topic_index'] >= len(topics):
                logger.info("Цикл поиска завершен, ожидание 30 минут...")
                reset_search_state()
                search_state['topics'] = current_settings.get('topics', [])
                search_state['keywords'] = current_settings.get('keywords', [])
                search_state['cycle_start_time'] = datetime.now()
                search_state['search_session_id'] = f"search_{int(time.time())}"
                
                for _ in range(1800):
                    if not search_active:
                        break
                    await asyncio.sleep(1)
                continue
            
            if not driver:
                driver = setup_driver()
                if not driver:
                    logger.error("Не удалось инициализировать драйвер")
                    await asyncio.sleep(60)
                    continue
            
            current_url = driver.current_url
            if "tgstat.ru/channels/search" not in current_url:
                if not navigate_to_channel_search(driver):
                    logger.error("Не удалось перейти к поиску каналов")
                    await asyncio.sleep(60)
                    continue
            
            first_search = search_state['current_topic_index'] == 0 and search_state['current_keyword_index'] == 0
            
            current_topic = topics[search_state['current_topic_index']]
            current_keyword = keywords[search_state['current_keyword_index']]
            
            try:
                logger.info(f"Поиск: тема '{current_topic}', ключевое слово '{current_keyword}' "
                           f"({search_state['current_topic_index'] + 1}/{len(topics)}, "
                           f"{search_state['current_keyword_index'] + 1}/{len(keywords)})")
                
                channels = search_channels(driver, current_keyword, current_topic, first_search)
                
                if channels:
                    await process_found_channels(channels)
                
                search_state['current_keyword_index'] += 1
                
                if search_state['current_keyword_index'] >= len(keywords):
                    search_state['current_keyword_index'] = 0
                    search_state['current_topic_index'] += 1
                
                save_search_state()
                await save_search_state_to_db()
                
                await asyncio.sleep(random.uniform(10, 20))
                
            except Exception as e:
                logger.error(f"Ошибка поиска по '{current_keyword}' и '{current_topic}': {e}")
                
                search_state['current_keyword_index'] += 1
                if search_state['current_keyword_index'] >= len(keywords):
                    search_state['current_keyword_index'] = 0
                    search_state['current_topic_index'] += 1
                
                save_search_state()
                await save_search_state_to_db()
                
                await asyncio.sleep(30)
                continue
            
        except Exception as e:
            logger.error(f"Критическая ошибка в цикле поиска: {e}")
            if driver:
                try:
                    driver.quit()
                except:
                    pass
                driver = None
            await asyncio.sleep(60)

async def start_search(settings: dict):
    """Запуск поиска каналов"""
    global search_active, current_settings
    
    if search_active:
        logger.warning("Поиск уже запущен")
        return
    
    logger.info("Запуск поиска каналов...")
    search_active = True
    current_settings = settings.copy()
    
    try:
        import bot_interface
        client = bot_interface.bot_data.get('telethon_client')
        if not client:
            logger.error("Telethon клиент не найден в bot_interface")
            search_active = False
            return
        logger.info("Telethon клиент найден в bot_interface")
    except Exception as e:
        logger.error(f"Ошибка получения Telethon клиента: {e}")
        search_active = False
        return
    
    asyncio.create_task(search_loop())
    logger.info("Поиск каналов запущен")

async def stop_search():
    """Остановка поиска каналов"""
    global search_active, driver
    
    logger.info("Остановка поиска каналов...")
    search_active = False
    
    try:
        save_search_state()
        await save_search_state_to_db()
    except Exception as e:
        logger.error(f"Ошибка сохранения состояния: {e}")
    
    if driver:
        try:
            driver.quit()
            logger.info("Драйвер закрыт")
        except Exception as e:
            logger.error(f"Ошибка закрытия драйвера: {e}")
        finally:
            driver = None
            try:
                import subprocess
                import platform
                if platform.system() == "Linux":
                    subprocess.run(["pkill", "-f", "chrome"], check=False)
                    subprocess.run(["pkill", "-f", "chromium"], check=False)
            except:
                pass
    
    logger.info("Поиск каналов остановлен")

def get_statistics():
    """Получение статистики поиска"""
    progress = get_progress_info()
    
    return {
        'found_channels': len(found_channels),
        'search_active': search_active,
        'progress_percent': progress['progress_percent'],
        'current_topic': progress['current_topic'],
        'current_keyword': progress['current_keyword'],
        'total_combinations': progress['total_combinations'],
        'current_combination': progress['current_combination'],
        'cycle_start_time': search_state.get('cycle_start_time'),
        'last_search_time': search_state.get('last_search_time')
    }

async def main():
    """Тестирование модуля"""
    test_settings = {
        'keywords': ['тест', 'пример'],
        'topics': ['Технологии', 'Образование']
    }
    
    await start_search(test_settings)
    
    await asyncio.sleep(120)
    
    await stop_search()

if __name__ == "__main__":
    asyncio.run(main())