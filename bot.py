from telegram import Update, InputMediaPhoto, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler
)
import sqlite3
import os
import configparser
import logging
import asyncio

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Константы состояний
ACCOUNT_INFO = 0
ADMIN_DB_FILE = "admins.db"
SQLITE_TIMEOUT = 10
MEDIA_GROUP_DELAY = 1.0  # Задержка для сбора медиагруппы

# Константы состояний
ACCOUNT_INFO = 0
ADMIN_DB_FILE = "admins.db"
SQLITE_TIMEOUT = 10  # секунд для ожидания разблокировки БД

# Константы отзывов
REVIEW_PHOTOS = [
    "AgACAgIAAxkBAAICO2hs9wABZdRD-__U8VkQ4-sGQatUMQACKvcxG2gAAWlLHUTK0lkjfD0BAAMCAAN5AAM2BA",
    "AgACAgIAAxkBAAICPWhs9wVRoEb4YYMCnB3WAUFnKjLPAAIs9zEbaAABaUvP67RaQkhiJgEAAwIAA3kAAzYE"
]
REVIEW_KEYBOARD = [["📊 Bewertungen"]]
REVIEW_MARKUP = ReplyKeyboardMarkup(REVIEW_KEYBOARD, resize_keyboard=True, one_time_keyboard=False)

# Чтение конфигурации
config = configparser.ConfigParser()
config.read('config.ini')
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or config.get('BOT', 'TOKEN', fallback=None)
FIRST_ADMIN_ID = os.getenv("FIRST_ADMIN_ID") or config.get('BOT', 'FIRST_ADMIN_ID', fallback=None)
ADMIN_GROUP_ID = os.getenv("ADMIN_GROUP_ID") or config.get('BOT', 'ADMIN_GROUP_ID', fallback=None)

if not BOT_TOKEN:
    logger.error("BOT_TOKEN не задан! Укажите TELEGRAM_BOT_TOKEN или в config.ini.")
    exit(1)

# Функция для подключения к БД с тайм-аутом
def get_conn(db_file):
    return sqlite3.connect(db_file, timeout=SQLITE_TIMEOUT)

# Инициализация БД
def init_accounts_db():
    conn = get_conn('accounts.db')
    conn.execute('PRAGMA journal_mode=WAL;')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS accounts (
                 id INTEGER PRIMARY KEY, 
                 user_id INTEGER, 
                 username TEXT,
                 account_info TEXT, 
                 status TEXT DEFAULT 'new',
                 admin_chat_id INTEGER,
                 topic_id INTEGER,
                 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
                 id INTEGER PRIMARY KEY, 
                 account_id INTEGER,
                 from_admin BOOLEAN, 
                 message_text TEXT,
                 timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def init_admins_db():
    conn = get_conn(ADMIN_DB_FILE)
    conn.execute('PRAGMA journal_mode=WAL;')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
                 id INTEGER PRIMARY KEY, 
                 chat_id INTEGER UNIQUE)''')
    conn.commit()
    conn.close()
    if FIRST_ADMIN_ID:
        try:
            add_admin(int(FIRST_ADMIN_ID))
        except Exception as e:
            logger.error(f"Не удалось добавить FIRST_ADMIN_ID: {e}")

def add_admin(chat_id):
    conn = get_conn(ADMIN_DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO admins (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

def is_admin(chat_id):
    try:
        conn = get_conn(ADMIN_DB_FILE)
        c = conn.cursor()
        c.execute("SELECT 1 FROM admins WHERE chat_id = ?", (chat_id,))
        exists = c.fetchone() is not None
        return exists
    except Exception as e:
        logger.error(f"Ошибка проверки админа: {e}")
        return False
    finally:
        if conn:
            conn.close()

def save_account(user_id, username, info):
    try:
        conn = get_conn('accounts.db')
        c = conn.cursor()
        c.execute("INSERT INTO accounts (user_id, username, account_info) VALUES (?, ?, ?)",
                (user_id, username, info))
        conn.commit()
        account_id = c.lastrowid
        return account_id
    except Exception as e:
        logger.error(f"Ошибка сохранения аккаунта: {e}")
        return None
    finally:
        if conn:
            conn.close()

def save_message(account_id, from_admin, text):
    try:
        conn = get_conn('accounts.db')
        c = conn.cursor()
        c.execute("INSERT INTO messages (account_id, from_admin, message_text) VALUES (?, ?, ?)",
                (account_id, int(from_admin), text))
        conn.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения сообщения: {e}")
    finally:
        if conn:
            conn.close()

def get_account_by_topic(topic_id):
    try:
        conn = get_conn('accounts.db')
        c = conn.cursor()
        c.execute("SELECT * FROM accounts WHERE topic_id = ?", (topic_id,))
        return c.fetchone()
    except Exception as e:
        logger.error(f"Ошибка поиска аккаунта по топику: {e}")
        return None
    finally:
        if conn:
            conn.close()

def get_active_account(user_id):
    try:
        conn = get_conn('accounts.db')
        c = conn.cursor()
        c.execute("SELECT id, admin_chat_id, topic_id FROM accounts WHERE user_id = ? AND admin_chat_id IS NOT NULL", (user_id,))
        return c.fetchone()
    except Exception as e:
        logger.error(f"Ошибка получения активного аккаунта: {e}")
        return None
    finally:
        if conn:
            conn.close()

# Добавим новую функцию для обработки альбомов
async def account_album(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        u = update.message.from_user
        user_info = f"@{u.username}" if u.username else f"{u.first_name} {u.last_name or ''}"
        
        # Получаем все фото из альбома
        photos = [photo.file_id for photo in update.message.photo]
        caption = update.message.caption or ""
        
        # Сохраняем в формате ALBUM:[file_id1,file_id2,...]:caption
        album_data = f"ALBUM:{','.join(photos)}:{caption}"
        account_id = save_account(u.id, user_info, album_data)
        
        if not account_id:
            await update.message.reply_text("❌ Fehler bei der Bearbeitung der Anfrage. Versuchen Sie es später noch einmal.")
            return ConversationHandler.END
        
        await update.message.reply_text("✅ Ich danke Dir! Bitte warte auf die Antwort des Administrators.")
        
        # Создаем топик
        topic_id = await create_support_topic(
            context,
            account_id,
            user_info,
            album_data
        )
        
        if not topic_id:
            logger.error(f"Не удалось создать топик для запроса #{account_id}")
        
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Ошибка в account_album: {e}")
        await update.message.reply_text("❌ Произошла ошибка при обработке вашего альбома")
        return ConversationHandler.END

# Функция для создания топика
async def create_support_topic(context, account_id, user_info, account_info):
    if not ADMIN_GROUP_ID:
        logger.error("ADMIN_GROUP_ID не задан! Не могу создать топик.")
        return None
    
    try:
        # Создаем новый топик в группе
        topic_name = f"Request #{account_id}: {user_info[:20]}"
        topic = await context.bot.create_forum_topic(
            chat_id=ADMIN_GROUP_ID,
            name=topic_name
        )
        
        # Сохраняем ID топика в базу
        conn = get_conn('accounts.db')
        c = conn.cursor()
        c.execute("UPDATE accounts SET admin_chat_id = ?, topic_id = ? WHERE id = ?", 
                (ADMIN_GROUP_ID, topic.message_thread_id, account_id))
        conn.commit()
        conn.close()
        
        # Формируем сообщение в зависимости от типа контента
        if account_info.startswith("PHOTO:"):
            parts = account_info.split(':', 2)
            file_id = parts[1]
            caption = parts[2] if len(parts) > 2 else ""
            
            photo_caption = (
                f"⚠️ New info #{account_id}\n"
                f"👤 User: {user_info}\n"
            )
            
            if caption:
                photo_caption += f"📝 Info: {caption}"
            
            await context.bot.send_photo(
                chat_id=ADMIN_GROUP_ID,
                photo=file_id,
                caption=photo_caption,
                message_thread_id=topic.message_thread_id
            )
        else:
            # Текстовое сообщение
            message_text = (
                f"⚠️ New info #{account_id}\n"
                f"👤 User: {user_info}\n"
                f"📝 Info:\n{account_info}"
            )
            await context.bot.send_message(
                chat_id=ADMIN_GROUP_ID,
                text=message_text,
                message_thread_id=topic.message_thread_id
            )
        
        return topic.message_thread_id
        
    except Exception as e:
        logger.error(f"Ошибка при создании топика: {e}")
        return None

# ================== ОБРАБОТЧИКИ ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(
            "Grind-Games ist die führende deutsche Seite für den An- und Verkauf von Fortnite Accounts. "
            "Unser Ziel ist es, jedem User ein faires Angebot für seinen Fortnite Account zukommen zu lassen "
            "und damit die Möglichkeit zu geben, aus einem alten Fortnite Account noch Geld machen zu können - "
            "während man gleichzeitig einem künftigen Käufer eine Freude bereiten kann!\n\n"
            "Wir antworten auf alle Account Anfragen in der Regel innerhalb von wenigen Stunden.\n\n"
            "Sobald wir uns auf einen Preis geeinigt haben, bereiten wir die Überweisung auf dein Bankkonto direkt vor. "
            "Es ist durchaus möglich, dass das Geld bereits nach wenigen Stunden bei dir ist und wir geben unser Bestes, "
            "um dafür zu sorgen, dass alle Auszahlungen an unsere Verkäufer schnellstmöglich ausgeführt werden."
        )

        await asyncio.sleep(1.5)

        await update.message.reply_text(
            "👋 Hallo. Schick uns bitte Deine Angaben in diesem Format:\n"
            "📜 Anzahl der Skins:\n"
            "💎 OG oder seltene Skins:\n"
            "📸 Fotos von Deinem Konto\n\n"
            "Du kannst auch die automatische Verifizierungsmethode verwenden und dein Konto " \
            "durch den Skin Checker überprüfen lassen und uns die Fotos zukommen lassen, " \
            "die du vom Bot in Telegram in nur wenigen Sekunden erhältst.\n@BombSkinCheckerBot",
            reply_markup=REVIEW_MARKUP
        )
        return ACCOUNT_INFO
    except Exception as e:
        logger.error(f"Ошибка в start: {e}")
        return ConversationHandler.END

async def account_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.message.text == "📊 Bewertungen":
            await show_reviews(update, context)
            return ACCOUNT_INFO
        u = update.message.from_user
        user_info = f"@{u.username}" if u.username else f"{u.first_name} {u.last_name or ''}"
        
        # Обрабатываем текст или фото
        if update.message.photo:
            # Для фото всегда сохраняем в формате "PHOTO:file_id:caption"
            file_id = update.message.photo[-1].file_id
            caption = update.message.caption or ""
            text = f"PHOTO:{file_id}:{caption}"
        else:
            text = update.message.text

        account_id = save_account(u.id, user_info, text)
        
        if not account_id:
            await update.message.reply_text("❌ Fehler bei der Bearbeitung der Anfrage. Versuchen Sie es später noch einmal.")
            return ConversationHandler.END
        
        await update.message.reply_text("✅ Ich danke Ihnen! Bitte warten Sie auf die Antwort des Administrators.", reply_markup=REVIEW_MARKUP)
        
        # Создаем топик
        topic_id = await create_support_topic(
            context,
            account_id,
            user_info,
            text
        )
        
        if not topic_id:
            logger.error(f"Не удалось создать топик для запроса #{account_id}")
        
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Ошибка в account_info: {e}")
        await update.message.reply_text("❌ Произошла ошибка при обработке вашего запроса")
        return ConversationHandler.END


async def add_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = update.message.from_user.id
        if not is_admin(uid):
            await update.message.reply_text("❌ Kein Zugang")
            return
        
        if not context.args:
            await update.message.reply_text("Использование: /addadmin <user_id>")
            return
        
        try:
            new_admin = int(context.args[0])
            add_admin(new_admin)
            await update.message.reply_text(f"✅ {new_admin} добавлен админом")
            await context.bot.send_message(chat_id=new_admin, text="🎉 Вы админ бота.")
        except:
            await update.message.reply_text("❌ Неверный ID")
    except Exception as e:
        logger.error(f"Ошибка в add_admin_cmd: {e}")

async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Проверяем, что сообщение в топике группы админов
        if not update.message.message_thread_id:
            return
        
        # Ищем аккаунт по ID топика
        acc = get_account_by_topic(update.message.message_thread_id)
        if not acc:
            return
        
        # Проверяем права админа
        if not is_admin(update.message.from_user.id):
            await update.message.reply_text("❌ Kein Zugang")
            return
        
        # Пересылаем пользователю
        text = update.message.text or ''
        save_message(acc[0], True, text)
        
        await context.bot.send_message(
            chat_id=acc[1],  # user_id
            text=f"📨 Antwort des Administrators:\n{text}"
        )
        
    except Exception as e:
        logger.error(f"Ошибка в admin_reply: {e}")

async def admin_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Проверяем, что сообщение в топике группы админов
        if not update.message.message_thread_id:
            return
        
        # Ищем аккаунт по ID топика
        acc = get_account_by_topic(update.message.message_thread_id)
        if not acc:
            return
        
        # Проверяем права админа
        if not is_admin(update.message.from_user.id):
            await update.message.reply_text("❌ Нет доступа")
            return
        
        # Сохраняем сообщение админа
        caption = update.message.caption or ""
        file_id = update.message.photo[-1].file_id
        save_message(acc[0], True, f"PHOTO:{file_id}:{caption}")
        
        # Пересылаем пользователю
        if caption:
            await context.bot.send_photo(
                chat_id=acc[1],
                photo=file_id,
                caption=f"📨 Antwort des Administrators:\n{caption}"
            )
        else:
            await context.bot.send_photo(
                chat_id=acc[1],
                photo=file_id,
                caption="📨 Antwort des Administrators"
            )
            
    except Exception as e:
        logger.error(f"Ошибка в admin_photo: {e}")

async def user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = update.message.from_user.id
        if is_admin(uid): 
            return
        
        # Получаем активный диалог пользователя
        acc = get_active_account(uid)
        if not acc:
            await update.message.reply_text("⏳ Warten Sie auf den Administrator.")
            return
        
        acc_id, admin_chat, topic_id = acc
        save_message(acc_id, False, update.message.text)
        
        try:
            await context.bot.send_message(
                chat_id=admin_chat,
                text=f"👤 User:\n{update.message.text}",
                message_thread_id=topic_id
            )
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            await update.message.reply_text("⚠️ Eine Nachricht an den Administrator konnte nicht gesendet werden")
    except Exception as e:
        logger.error(f"Ошибка в user_message: {e}")

async def user_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = update.message.from_user.id
        if is_admin(uid): 
            return
        
        # Получаем активный диалог пользователя
        acc = get_active_account(uid)
        if not acc:
            await update.message.reply_text("⏳ Warten Sie auf den Administrator.")
            return
        
        acc_id, admin_chat, topic_id = acc
        caption = update.message.caption or ""
        file_id = update.message.photo[-1].file_id
        save_message(acc_id, False, f"PHOTO:{file_id}:{caption}")
        
        try:
            if caption:
                await context.bot.send_photo(
                    chat_id=admin_chat,
                    photo=file_id,
                    caption=f"👤 User: {caption}",
                    message_thread_id=topic_id
                )
            else:
                await context.bot.send_photo(
                    chat_id=admin_chat,
                    photo=file_id,
                    caption="👤 User sent photo",
                    message_thread_id=topic_id
                )
        except Exception as e:
            logger.error(f"Error sending photo: {e}")
            await update.message.reply_text("⚠️ Foto konnte nicht an den Administrator gesendet werden")
    except Exception as e:
        logger.error(f"Ошибка в user_photo: {e}")

# Новая система обработки медиагрупп
media_groups = {}


async def handle_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик для медиагрупп (альбомов)"""
    try:
        # Определяем тип отправителя
        uid = update.message.from_user.id
        if is_admin(uid):
            sender_type = "admin"
            # Для админов получаем информацию об аккаунте из топика
            if not update.message.message_thread_id:
                return
            acc = get_account_by_topic(update.message.message_thread_id)
            if not acc:
                return
            user_id = acc[1]  # user_id для отправки
        else:
            sender_type = "user"
            # Для пользователей получаем активный диалог
            acc = get_active_account(uid)
            if not acc:
                await update.message.reply_text("⏳ Warten Sie auf den Administrator.")
                return
            user_id = None
            admin_chat = acc[1]
            topic_id = acc[2]
        
        # Получаем ID медиагруппы
        media_group_id = update.message.media_group_id
        
        # Если это первое сообщение в группе
        if media_group_id not in media_groups:
            media_groups[media_group_id] = {
                "media": [],
                "caption": update.message.caption or "",
                "sender_type": sender_type,
                "user_id": user_id,
                "admin_chat": admin_chat if sender_type == "user" else ADMIN_GROUP_ID,
                "topic_id": topic_id if sender_type == "user" else update.message.message_thread_id,
                "account_id": acc[0] if sender_type == "user" else acc[0],
                "timestamp": update.message.date
            }
            
            # Запланируем обработку группы через задержку
            context.job_queue.run_once(
                process_media_group, 
                MEDIA_GROUP_DELAY, 
                data=media_group_id,
                name=f"media_group_{media_group_id}"
            )
        
        # Добавляем медиа в группу
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            media_groups[media_group_id]["media"].append(("photo", file_id))
        # Можно добавить обработку других типов медиа здесь
        
        # Сохраняем сообщение в БД
        if sender_type == "user":
            save_message(acc[0], False, f"PHOTO:{file_id}:{update.message.caption or ''}")
        else:
            save_message(acc[0], True, f"PHOTO:{file_id}:{update.message.caption or ''}")
            
    except Exception as e:
        logger.error(f"Ошибка в handle_media_group: {e}")

async def process_media_group(context: ContextTypes.DEFAULT_TYPE):
    """Обработка собранной медиагруппы"""
    job = context.job
    media_group_id = job.data
    
    if media_group_id not in media_groups:
        return
        
    group_data = media_groups[media_group_id]
    
    try:
        # Создаем медиагруппу
        media_group = []
        base_caption = ""
        
        if group_data["sender_type"] == "user":
            base_caption = "👤 User sent album"
            if group_data["caption"]:
                base_caption += f"\n{group_data['caption']}"
        else:
            base_caption = "📨 Antwort des Administrators"
            if group_data["caption"]:
                base_caption += f"\n{group_data['caption']}"
        
        for i, (media_type, file_id) in enumerate(group_data["media"]):
            if i == 0:
                media_item = InputMediaPhoto(media=file_id, caption=base_caption)
            else:
                media_item = InputMediaPhoto(media=file_id)
            media_group.append(media_item)
        
        # Отправляем медиагруппу
        if group_data["sender_type"] == "user":
            await context.bot.send_media_group(
                chat_id=group_data["admin_chat"],
                media=media_group,
                message_thread_id=group_data["topic_id"]
            )
        else:
            await context.bot.send_media_group(
                chat_id=group_data["user_id"],
                media=media_group
            )
            
    except Exception as e:
        logger.error(f"Ошибка отправки медиагруппы: {e}")
    finally:
        # Удаляем группу из кэша
        if media_group_id in media_groups:
            del media_groups[media_group_id]

async def admin_album(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message.message_thread_id:
            return
            
        acc = get_account_by_topic(update.message.message_thread_id)
        if not acc:
            return
            
        if not is_admin(update.message.from_user.id):
            await update.message.reply_text("❌ Kein Zugang")
            return
        
        # Получаем все фото из альбома
        file_ids = [photo.file_id for photo in update.message.photo]
        caption = update.message.caption or ""
        
        # Сохраняем сообщения
        for i, file_id in enumerate(file_ids):
            save_message(acc[0], True, f"PHOTO:{file_id}:{caption if i == 0 else ''}")
        
        # Создаем медиагруппу для пользователя
        media_group = []
        base_caption = "📨 Antwort des Administrators"
        if caption:
            base_caption += f"\n{caption}"
        
        for i, file_id in enumerate(file_ids):
            if i == 0:
                media_item = InputMediaPhoto(media=file_id, caption=base_caption)
            else:
                media_item = InputMediaPhoto(media=file_id)
            media_group.append(media_item)
        
        await context.bot.send_media_group(
            chat_id=acc[1],
            media=media_group
        )
            
    except Exception as e:
        logger.error(f"Error in admin_album: {e}")

async def user_album(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = update.message.from_user.id
        if is_admin(uid):
            return
        
        acc = get_active_account(uid)
        if not acc:
            await update.message.reply_text("⏳ Warten Sie auf den Administrator.")
            return
            
        acc_id, admin_chat, topic_id = acc
        caption = update.message.caption or ""
        
        # Получаем все фото из сообщения
        file_ids = [photo.file_id for photo in update.message.photo]
        
        # Сохраняем каждое фото
        for i, file_id in enumerate(file_ids):
            save_message(acc_id, False, f"PHOTO:{file_id}:{caption if i == 0 else ''}")
        
        # Создаем медиагруппу с использованием InputMediaPhoto
        media_group = []
        base_caption = "👤 User sent Album"
        if caption:
            base_caption += f"\n{caption}"
        
        for i, file_id in enumerate(file_ids):
            if i == 0:
                media_item = InputMediaPhoto(media=file_id, caption=base_caption)
            else:
                media_item = InputMediaPhoto(media=file_id)
            media_group.append(media_item)
        
        try:
            await context.bot.send_media_group(
                chat_id=admin_chat,
                media=media_group,
                message_thread_id=topic_id
            )
        except Exception as e:
            logger.error(f"Error sending album: {e}")
            await update.message.reply_text("⚠️ Не удалось отправить альбом администратору")
            
    except Exception as e:
        logger.error(f"Error in user_album: {e}")

# Обработчик для медиа без подписи в состоянии ACCOUNT_INFO
async def invalid_account_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
            "👋 Hallo. Schicken Sie uns Ihre Angaben in diesem Format:\n"
            "📝 Anzahl der Skins:\n"
            "📝 Og oder seltene Skins:\n"
            "🖼️ Fotos von Ihrem Konto\n\n"
            "Oder Sie können die automatische Verifizierungsmethode verwenden, " \
            "das Konto durch den Checker überprüfen und uns die Fotos schicken, " \
            "die Sie vom Bot in Telegramm erhalten.\n@BombSkinCheckerBot"
    )
    return ACCOUNT_INFO


async def show_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not REVIEW_PHOTOS:
            await update.message.reply_text("⚠️ Keine Bewertungen verfügbar")
            return
        
        try:
            await update.message.delete()
        except:
            logger.warning("Konnte die Nachricht nicht löschen")

        # Отправляем все фото отзывов медиагруппой
        media_group = []
        for i, photo_id in enumerate(REVIEW_PHOTOS):
            if i == 0:
                # Для первого фото добавляем подпись
                media_group.append(InputMediaPhoto(
                    media=photo_id,
                    caption="📊 Bewertungen unserer Kunden:"
                ))
            else:
                media_group.append(InputMediaPhoto(media=photo_id))
        
        await context.bot.send_media_group(
            chat_id=update.message.chat_id,
            media=media_group
        )

    except Exception as e:
        logger.error(f"Fehler in show_reviews: {e}")
        await update.message.reply_text("❌ Fehler beim Laden der Bewertungen")


def main():
    # Проверка конфигурации группы
    if not ADMIN_GROUP_ID:
        logger.warning("ADMIN_GROUP_ID не задан! Бот не сможет создавать топики.")
    
    # Инициализация БД
    init_accounts_db()
    init_admins_db()
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    review_handler = MessageHandler(filters.Regex(r'^📊 Bewertungen$'), show_reviews)

    # Обработчики для пользователей
    user_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ACCOUNT_INFO: [
                MessageHandler(
                    filters.TEXT | filters.PHOTO, 
                    account_info
                )
            ]
        },
        fallbacks=[],
        per_user=True
    )
    
    # Обработчики для админов
    admin_handlers = [
        CommandHandler("addadmin", add_admin_cmd),
        MessageHandler(filters.TEXT & filters.ChatType.SUPERGROUP, admin_reply),
        MessageHandler(filters.PHOTO & filters.ChatType.SUPERGROUP, admin_photo),
        MessageHandler(filters.PHOTO & filters.ChatType.SUPERGROUP, handle_media_group)
    ]
    
    # Обработчики для пользовательских сообщений
    user_message_handlers = [
        MessageHandler(filters.TEXT & ~filters.COMMAND, user_message),
        MessageHandler(filters.PHOTO, user_photo),
        MessageHandler(filters.PHOTO, handle_media_group)
    ]
    
    # Регистрация обработчиков
    application.add_handlers([
        *admin_handlers,
        user_conv,
        *user_message_handlers,
        review_handler
    ])
    
    application.run_polling()

if __name__ == "__main__":
    main()
