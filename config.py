import os

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE = os.getenv("SUPABASE_SERVICE_KEY")

if not all([BOT_TOKEN, SUPABASE_URL, SUPABASE_SERVICE]):
    raise ValueError("Missing required environment variables")
