from database.supabase_client import supabase


def get_user(user_id, chat_id):
    res = supabase.table("users").select("*").eq("user_id", user_id).eq("chat_id", chat_id).execute()
    return res.data[0] if res.data else None


def create_user(user_id, chat_id, username=None, first_name=None):
    data = {
        "user_id": user_id,
        "chat_id": chat_id,
        "username": username,
        "first_name": first_name,
    }
    res = supabase.table("users").insert(data).execute()
    return res.data[0] if res.data else None


def update_balance(user_id, chat_id, amount, currency="sweets"):
    user = get_user(user_id, chat_id)
    if not user:
        return None
    col = "balance" if currency == "sweets" else "gold"
    new_val = user[col] + amount
    supabase.table("users").update({col: new_val}).eq("user_id", user_id).eq("chat_id", chat_id).execute()
    return new_val


def add_warn(user_id, chat_id):
    user = get_user(user_id, chat_id)
    if not user:
        return None
    new_w = user["warns"] + 1
    supabase.table("users").update({"warns": new_w}).eq("user_id", user_id).eq("chat_id", chat_id).execute()
    return new_w


def reset_warns(user_id, chat_id):
    supabase.table("users").update({"warns": 0}).eq("user_id", user_id).eq("chat_id", chat_id).execute()


def set_rank(user_id, chat_id, rank):
    supabase.table("users").update({"rank": rank}).eq("user_id", user_id).eq("chat_id", chat_id).execute()


def log_transaction(user_id, chat_id, amount, currency, description):
    supabase.table("transactions").insert({
        "user_id": user_id,
        "chat_id": chat_id,
        "amount": amount,
        "currency": currency,
        "description": description,
    }).execute()


def get_chat(chat_id):
    res = supabase.table("chats").select("*").eq("chat_id", chat_id).execute()
    return res.data[0] if res.data else None
