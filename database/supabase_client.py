from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_SERVICE

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE)
