import os
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telethon import TelegramClient, events
from motor.motor_asyncio import AsyncIOMotorClient
import json
import asyncio
from functools import partial
from telethon.tl.functions.channels import JoinChannelRequest
from openai import AsyncOpenAI
import re

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Telegram API credentials
BOT_TOKEN = os.getenv('BOT_TOKEN')
API_ID = int(os.getenv('API_ID', 0))
API_HASH = os.getenv('API_HASH')

# OpenAI client
openai_client = AsyncOpenAI(api_key=os.getenv('OPENAI_API_KEY'))

logger.info(f"Bot Token: {BOT_TOKEN[:10]}...")
logger.info(f"API ID: {API_ID}")
logger.info(f"API Hash: {API_HASH[:10]}...")

# MongoDB connection
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017')
logger.info(f"Connecting to MongoDB at: {MONGO_URI}")

# Global variables
user_client = None  # Клієнт для читання повідомлень (звичайний акаунт)
bot_client = None   # Клієнт для відправки повідомлень (бот)
application = None
mongo_client = None

async def init_mongodb():
    global mongo_client
    try:
        mongo_client = AsyncIOMotorClient(MONGO_URI)
        await mongo_client.admin.command('ping')
        logger.info("Successfully connected to MongoDB and verified connection")
        return mongo_client
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        raise

async def setup_clients():
    """Setup both Telethon clients - user account and bot."""
    global user_client, bot_client
    
    # Налаштовуємо клієнт для читання повідомлень (звичайний акаунт)
    if user_client is None:
        logger.info("Setting up user client for reading messages")
        user_client = TelegramClient('user_session', API_ID, API_HASH)
        await user_client.start()
        logger.info("User client started successfully")
        
        # Отримуємо список джерел для моніторингу
        sources = await get_monitored_sources()
        logger.info(f"Monitoring sources: {sources}")
        
        @user_client.on(events.NewMessage())
        async def message_handler(event):
            try:
                # Отримуємо інформацію про чат
                chat = await event.get_chat()
                chat_username = f"@{chat.username}" if chat.username else str(chat.id)
                
                # Отримуємо актуальний список джерел
                sources = await get_monitored_sources()
                
                # Перевіряємо чи це повідомлення з моніторингового каналу
                if chat_username in sources:
                    logger.info(f"✅ Нове повідомлення з {chat_username}")
                    logger.info(f"📝 Текст: {event.message.text}")
                    
                    # Перевіряємо наявність ціни
                    price = extract_price(event.message.text)
                    if price is not None:
                        logger.info(f"💰 Знайдено ціну: ${price}")
                        
                        if price <= 10000:
                            logger.info(f"✨ Ціна ${price} в межах ліміту")
                            
                            # Пересилаємо повідомлення через бота
                            target_chat_id = os.getenv('TARGET_CHAT_ID')
                            source_link = f"https://t.me/{chat.username}/{event.message.id}"
                            forward_text = f"🚗 Нова пропозиція: ${price}\n\nДжерело: {source_link}"
                            
                            if bot_client:
                                await bot_client.send_message(target_chat_id, forward_text)
                                logger.info(f"✅ Переслано в {target_chat_id}")
                        else:
                            logger.info(f"❌ Ціна ${price} перевищує ліміт")
                    else:
                        logger.info("❌ Ціну не знайдено в повідомленні")
            except Exception as e:
                logger.error(f"❌ Помилка: {e}", exc_info=True)
    
    # Налаштовуємо клієнт для відправки повідомлень (бот)
    if bot_client is None:
        logger.info("Setting up bot client for sending messages")
        bot_client = TelegramClient('bot_session', API_ID, API_HASH)
        await bot_client.start(bot_token=BOT_TOKEN)
        logger.info("Bot client started successfully")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /start is issued."""
    logger.info(f"Start command received from user {update.effective_user.id}")
    await update.message.reply_text(
        'Привіт! Я бот для моніторингу оголошень про продаж авто. '
        'Використовуйте /help для перегляду доступних команд.'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /help is issued."""
    logger.info(f"Help command received from user {update.effective_user.id}")
    help_text = """
Доступні команди:
/start - Почати роботу з ботом
/help - Показати це повідомлення
/add_source <username> - Додати нове джерело для парсингу
/list_sources - Показати всі джерела
/remove_source <username> - Видалити джерело
    """
    await update.message.reply_text(help_text)

async def check_historical_messages(source, hours=72):
    """Check messages from the last 72 hours in the source channel."""
    try:
        logger.info(f"Checking historical messages from {source} for the last {hours} hours")
        
        # Get the timestamp from 72 hours ago
        time_threshold = datetime.now() - timedelta(hours=hours)
        
        # Get messages from the channel
        message_count = 0
        async for message in user_client.iter_messages(source, offset_date=time_threshold, reverse=True):
            message_count += 1
            if message.text:  # Check only text messages
                logger.info(f"Processing message: {message.text}")
                message_text = message.text.lower()
                
                # Розширена перевірка на наявність цінових маркерів
                price_markers = ['$', 'usd', 'дол', 'dollar']
                if any(marker in message_text for marker in price_markers):
                    price = extract_price(message_text)
                    logger.info(f"Found message with potential price. Text: {message_text}, Extracted price: {price}")
                    
                    if price and price <= 10000:
                        logger.info(f"Price {price} is within range")
                        db = mongo_client.car_bot
                        
                        # Check if we already have this message
                        existing_post = await db.posts.find_one({
                            'source': source,
                            'original_id': message.id
                        })
                        
                        if not existing_post:
                            logger.info(f"New message found, preparing to forward")
                            post_data = {
                                'text': message.text,
                                'source': source,
                                'posted_at': datetime.utcnow(),
                                'original_id': message.id,
                                'price': price,
                                'message_date': message.date
                            }
                            await db.posts.insert_one(post_data)
                            
                            target_chat_id = os.getenv('TARGET_CHAT_ID')
                            logger.info(f"Sending to target chat: {target_chat_id}")
                            
                            source_link = f"https://t.me/{source.replace('@', '')}/{message.id}"
                            forward_text = f"🚗 Нова пропозиція: ${price}\n\nДжерело: {source_link}"
                            
                            try:
                                await bot_client.send_message(target_chat_id, forward_text)
                                logger.info(f"Successfully forwarded message with price ${price} from {source}")
                            except Exception as send_error:
                                logger.error(f"Error sending message to target chat: {send_error}")
                            
                            # Add small delay to avoid flooding
                            await asyncio.sleep(1)
                        else:
                            logger.info(f"Message already exists in database")
                    else:
                        logger.info(f"Price {price} is not within range or is None")
        
        logger.info(f"Finished checking historical messages from {source}. Processed {message_count} messages")
    except Exception as e:
        logger.error(f"Error checking historical messages from {source}: {e}")

async def add_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a new source to monitor."""
    logger.info(f"Add source command received from user {update.effective_user.id}")
    
    if not context.args:
        await update.message.reply_text('Будь ласка, вкажіть username джерела.')
        return

    source = context.args[0]
    if not source.startswith('@'):
        source = '@' + source

    try:
        # Перевірка чи існує канал
        try:
            channel = await user_client.get_entity(source)
            logger.info(f"Successfully found channel: {source}")
            
            # Додаємо канал до бази даних одразу
            db = mongo_client.car_bot
            
            # Check if source already exists
            existing_source = await db.sources.find_one({'username': source})
            if existing_source:
                await update.message.reply_text('Це джерело вже додано до списку моніторингу.')
                return

            # Add new source
            await db.sources.insert_one({
                'username': source,
                'added_at': datetime.utcnow(),
                'added_by': update.effective_user.id
            })
            logger.info(f"Successfully added source: {source}")
            await update.message.reply_text(f'Джерело {source} успішно додано до списку моніторингу.')
                
        except Exception as e:
            logger.error(f"Error finding channel {source}: {e}")
            await update.message.reply_text(f'Помилка: Не можу знайти канал {source}. Переконайтеся, що канал публічний (має username).')
            return
        
    except Exception as e:
        logger.error(f"Error adding source {source}: {e}")
        await update.message.reply_text(f'Помилка при додаванні джерела: {str(e)}')

async def list_sources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all monitored sources."""
    logger.info(f"List sources command received from user {update.effective_user.id}")
    
    try:
        db = mongo_client.car_bot
        # Спроба отримати дані
        logger.info("Attempting to fetch sources from database...")
        sources = await db.sources.find().to_list(length=None)
        logger.info(f"Database query completed. Found {len(sources) if sources else 0} sources")
        
        if not sources:
            logger.info("No sources found in database")
            await update.message.reply_text('Список джерел порожній.')
            return

        sources_text = 'Список джерел для моніторингу:\n'
        for source in sources:
            sources_text += f"- {source['username']}\n"
        logger.info(f"Found {len(sources)} sources")
        await update.message.reply_text(sources_text)
    except Exception as e:
        error_msg = f"Error listing sources: {str(e)}"
        logger.error(error_msg)
        await update.message.reply_text(f'Помилка при отриманні списку джерел: {str(e)}')

async def remove_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a source from monitoring."""
    logger.info(f"Remove source command received from user {update.effective_user.id}")
    
    if not context.args:
        await update.message.reply_text('Будь ласка, вкажіть username джерела для видалення.')
        return

    source = context.args[0]
    if not source.startswith('@'):
        source = '@' + source

    try:
        db = mongo_client.car_bot
        result = await db.sources.delete_one({'username': source})
        if result.deleted_count:
            logger.info(f"Successfully removed source: {source}")
            await update.message.reply_text(f'Джерело {source} успішно видалено.')
        else:
            logger.warning(f"Source not found: {source}")
            await update.message.reply_text(f'Джерело {source} не знайдено в списку моніторингу.')
    except Exception as e:
        logger.error(f"Error removing source {source}: {e}")
        await update.message.reply_text(f'Помилка при видаленні джерела: {str(e)}')

async def get_monitored_sources():
    """Get list of monitored sources from database."""
    try:
        db = mongo_client.car_bot
        sources = await db.sources.find().to_list(length=None)
        source_list = [source['username'] for source in sources]
        logger.info(f"Retrieved {len(source_list)} monitored sources")
        return source_list
    except Exception as e:
        logger.error(f"Error getting monitored sources: {e}")
        return []

async def join_channels(client, sources):
    """Join all channels in the sources list."""
    for source in sources:
        try:
            logger.info(f"Attempting to join channel: {source}")
            await client(JoinChannelRequest(source))
            logger.info(f"Successfully joined channel: {source}")
        except Exception as e:
            logger.error(f"Failed to join channel {source}: {e}")

async def analyze_message_with_gpt(text):
    """Analyze message text with GPT to extract price and determine if it's a car sale post."""
    try:
        prompt = f"""Проаналізуй це повідомлення з Telegram каналу:
        
        {text}
        
        Дай відповідь в форматі JSON:
        {{
            "is_car_sale": true/false,  // чи це оголошення про продаж авто
            "price_usd": number or null,  // ціна в USD (null якщо не знайдено)
            "car_info": {{  // інформація про авто (null якщо не знайдено)
                "brand": string,  // марка
                "model": string,  // модель
                "year": number or null,  // рік випуску
                "condition": string  // стан авто
            }}
        }}
        
        Відповідай ТІЛЬКИ в форматі JSON, без додаткових коментарів."""

        response = await openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Ти асистент, який аналізує оголошення про продаж авто. Відповідай строго в форматі JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )
        
        result = json.loads(response.choices[0].message.content)
        logger.info(f"GPT analysis result: {result}")
        return result
    except Exception as e:
        logger.error(f"Error analyzing message with GPT: {e}")
        return None

async def handle_new_message(event):
    try:
        chat = await event.get_chat()
        logger.info(f"Processing message from {chat.username or chat.id}")
        logger.info(f"Message text: {event.message.text}")
        
        # Аналізуємо повідомлення за допомогою GPT
        analysis = await analyze_message_with_gpt(event.message.text)
        
        if analysis and analysis.get('is_car_sale'):
            price = analysis.get('price_usd')
            car_info = analysis.get('car_info')
            
            if price and price <= 10000:
                logger.info(f"Found valid car sale post with price: ${price}")
                db = mongo_client.car_bot
                
                # Check if we already have this message
                existing_post = await db.posts.find_one({
                    'source': chat.username or str(chat.id),
                    'original_id': event.message.id
                })
                
                if not existing_post:
                    post_data = {
                        'text': event.message.text,
                        'source': chat.username or str(chat.id),
                        'posted_at': datetime.utcnow(),
                        'original_id': event.message.id,
                        'price': price,
                        'car_info': car_info
                    }
                    await db.posts.insert_one(post_data)
                    logger.info(f"Saved message to database")
                    
                    target_chat_id = os.getenv('TARGET_CHAT_ID')
                    logger.info(f"Attempting to forward to {target_chat_id}")
                    
                    source_link = f"https://t.me/{chat.username}/{event.message.id}"
                    car_info_text = f"\nАвто: {car_info['brand']} {car_info['model']}"
                    if car_info.get('year'):
                        car_info_text += f"\nРік: {car_info['year']}"
                    if car_info.get('condition'):
                        car_info_text += f"\nСтан: {car_info['condition']}"
                    
                    forward_text = f"{event.message.text}\n\nДжерело: {source_link}\nЗнайдена ціна: ${price}{car_info_text}"
                    
                    try:
                        await bot_client.send_message(target_chat_id, forward_text)
                        logger.info(f"Successfully forwarded message to {target_chat_id}")
                    except Exception as send_error:
                        logger.error(f"Error sending message to target chat: {send_error}")
                else:
                    logger.info(f"Message already exists in database")
            else:
                logger.info(f"Price ${price} is above threshold or not found")
        else:
            logger.info("Message is not a car sale post or analysis failed")
    except Exception as e:
        logger.error(f"Error processing message: {e}", exc_info=True)

async def main():
    """Start the bot."""
    global application, user_client, bot_client, mongo_client
    
    logger.info("Starting bot...")
    try:
        # Initialize MongoDB
        await init_mongodb()
        
        # Setup both clients
        await setup_clients()
        logger.info("Both clients setup completed")
        
        # Create the Application for bot commands
        application = Application.builder().token(BOT_TOKEN).build()
        logger.info("Telegram application created")

        # Add command handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("add_source", add_source))
        application.add_handler(CommandHandler("list_sources", list_sources))
        application.add_handler(CommandHandler("remove_source", remove_source))
        logger.info("Command handlers registered")

        # Run the bot
        logger.info("Starting bot polling...")
        
        # Start periodic historical messages check
        async def check_historical_periodically():
            while True:
                try:
                    sources = await get_monitored_sources()
                    logger.info(f"Starting periodic historical check for sources: {sources}")
                    for source in sources:
                        await check_historical_messages(source, hours=6)
                    logger.info("Periodic historical check completed")
                except Exception as e:
                    logger.error(f"Error in periodic historical check: {e}")
                await asyncio.sleep(6 * 3600)  # Перевіряємо кожні 6 годин
        
        # Start both clients
        async with application:
            await application.start()
            await application.updater.start_polling()
            logger.info("Bot is running...")
            
            # Start historical check in background
            asyncio.create_task(check_historical_periodically())
            
            # Keep the bot running
            while True:
                await asyncio.sleep(1)
        
    except Exception as e:
        logger.error(f"Error in main function: {e}")
    finally:
        # Cleanup
        if application:
            await application.stop()
        if user_client:
            await user_client.disconnect()
        if bot_client:
            await bot_client.disconnect()
        if mongo_client:
            mongo_client.close()

def run_bot():
    """Run the bot with proper async handling."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")

def extract_price(text):
    """Extract price from message text."""
    if not text:
        return None
        
    # Шукаємо цифри з доларом
    patterns = [
        r'\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)',  # $1000 or $1,000 or $1000.00
        r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*\$',  # 1000$ or 1,000$ or 1000.00$
        r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*(?:USD|usd)',  # 1000USD or 1,000USD
        r'(\d+(?:,\d{3})*(?:\.\d{2})?)\s*(?:дол|dol)',  # 1000дол or 1000dol
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                # Видаляємо коми та конвертуємо в число
                price_str = match.group(1).replace(',', '')
                return float(price_str)
            except ValueError:
                continue
                
    return None

if __name__ == '__main__':
    run_bot() 