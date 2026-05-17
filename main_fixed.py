import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
import requests
import threading
import time
import os
import json
import asyncio
import re
import uuid
from telethon import TelegramClient, functions, types, errors

# --- CONFIGURATION ---
BOT_TOKEN = "8197540716:AAGv-TIzFRMR1nMxkMEynLlubqjrKPMTYNE"
ADMIN_IDS = [6908091275, 7066485347]
ADMIN_ID = 6908091275 # Primary admin for backward compatibility in some strings
ADMIN_USERNAME = "@rikton16"
API_ID = 27699293
API_HASH = "2f0aa06fe4f782c5ebd5454c19774c79"
DB_FILE = "bot_data.json"
LOG_BOT_TOKEN = "8235712205:AAGeXQRbuDFX439qgByF_e-CSC2fPaXXxyg"
LOG_GROUP_ID = -1003604406909

bot = telebot.TeleBot(BOT_TOKEN)
db_lock = threading.Lock()

# --- DATABASE LOGIC ---
def load_db():
    if not os.path.exists(DB_FILE):
        return {
            "approved": list(ADMIN_IDS),
            "pending_approval": [],
            "blocked": [],
            "stats": {"checked": 0, "fresh": 0, "used": 0, "banned": 0, "hits": 0, "misses": 0},
            "last_cleanup": {},
            "users": {},
            "global_prices": {} # {country: {service: price}}
        }
    try:
        with open(DB_FILE, "r") as f:
            data = json.load(f)
            if "users" not in data: data["users"] = {}
            if "approved" not in data: data["approved"] = list(ADMIN_IDS)
            if "pending_approval" not in data: data["pending_approval"] = []
            if "global_prices" not in data: data["global_prices"] = {}
            
            new_users = {}
            for uid_str, udata in data["users"].items():
                try:
                    uid = int(uid_str)
                    udata['stopped_searches'] = set(udata.get('stopped_searches', []))
                    udata['cancelled_orders'] = set(udata.get('cancelled_orders', []))
                    udata['orders_with_otp'] = set(udata.get('orders_with_otp', []))
                    udata['active_threads'] = {} # Threads don't survive restart
                    new_users[uid] = udata
                except: continue
            data["users"] = new_users
            return data
    except:
        return load_db() # Fallback if file corrupted

def save_db(data):
    with db_lock:
        try:
            # Round all user balances to 2 decimal places to avoid float issues
            for uid in data.get("users", {}):
                if "balance" in data["users"][uid]:
                    data["users"][uid]["balance"] = round(float(data["users"][uid]["balance"]), 2)
            
            save_data = {k: v for k, v in data.items() if k != "users"}
            save_users = {}
            for uid, udata in data.get("users", {}).items():
                user_copy = udata.copy()
                user_copy['stopped_searches'] = list(udata.get('stopped_searches', []))
                user_copy['cancelled_orders'] = list(udata.get('cancelled_orders', []))
                user_copy['orders_with_otp'] = list(udata.get('orders_with_otp', []))
                user_copy['active_threads'] = {}
                save_users[str(uid)] = user_copy
            save_data["users"] = save_users
            
            with open(DB_FILE, "w") as f:
                json.dump(save_data, f, indent=4)
        except Exception as e:
            print(f"Error saving DB: {e}")

db_data = load_db()
users_db = db_data["users"]

# --- CHECKER MANAGER (TELETHON) ---
class CheckerManager:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.clients = {}
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def get_client(self, user_id):
        if user_id not in self.clients:
            if not os.path.exists('sessions'):
                os.makedirs('sessions')
            self.clients[user_id] = TelegramClient(f'sessions/checker_{user_id}', API_ID, API_HASH)
        return self.clients[user_id]

    async def _is_authorized(self, user_id):
        try:
            client = self.get_client(user_id)
            if not client.is_connected():
                await asyncio.wait_for(client.connect(), timeout=5)
            return await asyncio.wait_for(client.is_user_authorized(), timeout=5)
        except:
            return False

    def is_authorized(self, user_id):
        try:
            future = asyncio.run_coroutine_threadsafe(self._is_authorized(user_id), self.loop)
            return future.result(timeout=10)
        except:
            return False

    async def _send_code(self, user_id, phone):
        session_path = f'sessions/checker_{user_id}.session'
        client = self.get_client(user_id)
        try:
            if not client.is_connected():
                await asyncio.wait_for(client.connect(), timeout=15)
            return await asyncio.wait_for(client.send_code_request(phone), timeout=20)
        except (errors.AuthKeyDuplicatedError, errors.SessionPasswordNeededError, errors.SessionRevokedError):
            # Session is dirty — kill it and start fresh
            await client.disconnect()
            if os.path.exists(session_path): os.remove(session_path)
            if user_id in self.clients: del self.clients[user_id]
            client = self.get_client(user_id)
            await asyncio.wait_for(client.connect(), timeout=15)
            return await asyncio.wait_for(client.send_code_request(phone), timeout=20)

    def send_code(self, user_id, phone):
        # FIX: Added timeout so it never hangs forever
        future = asyncio.run_coroutine_threadsafe(self._send_code(user_id, phone), self.loop)
        return future.result(timeout=40)

    async def _sign_in(self, user_id, phone, code, phone_code_hash):
        client = self.get_client(user_id)
        return await asyncio.wait_for(
            client.sign_in(phone, code, phone_code_hash=phone_code_hash), timeout=20
        )

    def sign_in(self, user_id, phone, code, phone_code_hash):
        # FIX: Added timeout
        future = asyncio.run_coroutine_threadsafe(
            self._sign_in(user_id, phone, code, phone_code_hash), self.loop
        )
        return future.result(timeout=30)

    async def _logout(self, user_id):
        client = self.get_client(user_id)
        session_path = f'sessions/checker_{user_id}.session'
        try:
            if not client.is_connected():
                await client.connect()
            await client.log_out()
        except: pass
        finally:
            await client.disconnect()
            if user_id in self.clients:
                del self.clients[user_id]
            if os.path.exists(session_path):
                try: os.remove(session_path)
                except: pass

    def logout(self, user_id):
        future = asyncio.run_coroutine_threadsafe(self._logout(user_id), self.loop)
        try:
            return future.result(timeout=15)
        except:
            return False

    async def _check_number(self, user_id, phone):
        try:
            client = self.get_client(user_id)
            if not client.is_connected():
                await asyncio.wait_for(client.connect(), timeout=10)

            clean_phone = '+' + phone.replace('+', '').strip()

            async with client.conversation('@TelCheckers_bot', timeout=20) as conv:
                await conv.send_message(clean_phone)
                try:
                    for _ in range(5):
                        response = await conv.get_response(timeout=6)
                        text = response.text.lower()
                        if re.search(r"([1-9][0-9]*)\s*unopened number", text):
                            return 'fresh'
                        elif re.search(r"([1-9][0-9]*)\s*number\(s\) has been opened", text) or "🔐" in text:
                            return 'used'
                        elif re.search(r"([1-9][0-9]*)\s*banned number", text) or "❌" in text:
                            return 'banned'
                    print(f"Failed to find status for {phone} in @TelCheckers_bot responses")
                    return 'used'
                except asyncio.TimeoutError:
                    print(f"Timeout checking {phone} with @TelCheckers_bot")
                    return 'used'
        except Exception as e:
            print(f"Checker error {phone} via @TelCheckers_bot: {e}")
            return 'used'

    def check_number(self, user_id, phone):
        # FIX: Added 60s hard timeout — prevents background thread from hanging forever
        future = asyncio.run_coroutine_threadsafe(self._check_number(user_id, phone), self.loop)
        try:
            return future.result(timeout=60)
        except Exception as e:
            print(f"[check_number] Hard timeout or error for {phone}: {e}")
            return 'used'  # Safe fallback


checker = CheckerManager()

# --- UTILS ---
# users_db initialized from db_data above
cached_services = []
cached_countries = []
flag_cache = {
    'russia': '🇷🇺', 'england': '🇬🇧', 'usa': '🇺🇸', 'vietnam': '🇻🇳',
    'ivorycoast': '🇨🇮', 'macau': '🇲🇴', 'myanmar': '🇲🇲', 'kazakhstan': '🇰🇿',
    'ukraine': '🇺🇦', 'indonesia': '🇮🇩', 'india': '🇮🇳', 'philippines': '🇵🇭'
}

def get_flag(country_name):
    country_name = country_name.lower()
    if country_name in flag_cache: return flag_cache[country_name]
    try:
        import pycountry
        try:
            c = pycountry.countries.get(name=country_name.title())
            if not c: c = pycountry.countries.search_fuzzy(country_name)[0]
        except: return '🏳️'
        code = c.alpha_2
        flag = chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)
        flag_cache[country_name] = flag
        return flag
    except: return '🏳️'

def cleanup_messages(chat_id):
    if str(chat_id) in db_data["last_cleanup"]:
        for mid in db_data["last_cleanup"][str(chat_id)]:
            try: bot.delete_message(chat_id, mid)
            except: pass
        db_data["last_cleanup"][str(chat_id)] = []
        save_db(db_data)

def add_cleanup(chat_id, message_id):
    if str(chat_id) not in db_data["last_cleanup"]:
        db_data["last_cleanup"][str(chat_id)] = []
    db_data["last_cleanup"][str(chat_id)].append(message_id)
    save_db(db_data)

def get_services():
    global cached_services
    if not cached_services:
        try:
            response = requests.get('https://5sim.net/v1/guest/products/any/any', timeout=10)
            if response.status_code == 200:
                data = response.json()
                cached_services = list(data.keys())
        except Exception as e:
            print("Error fetching services:", e)
    return cached_services or ["telegram", "whatsapp", "google"]

def get_service_prices(service):
    try:
        response = requests.get(f'https://5sim.net/v1/guest/prices?product={service}', timeout=10)
        if response.status_code == 200: return response.json()
    except: pass
    return {}

def get_user_price(user_id, country, service, operator='any', prices=None):
    # Check User Overrides
    u = users_db.get(user_id, {})
    price = u.get('price_overrides', {}).get(country, {}).get(service)
    if price is not None: return float(price)
    
    # Check Global Overrides (Specific Country)
    price = db_data.get("global_prices", {}).get(country, {}).get(service)
    if price is not None: return float(price)
    
    # Check Truly Global (All Countries, Specific Service)
    price = db_data.get("global_prices", {}).get('all', {}).get(service)
    if price is not None: return float(price)
    
    # Check Master Global (All Countries, All Services)
    price = db_data.get("global_prices", {}).get('all', {}).get('all')
    if price is not None: return float(price)
    
    # Fallback to 5sim
    if prices is None:
        prices = get_service_prices(service)
        
    cost = prices.get(service, {}).get(country, {}).get(operator, {}).get('cost')
    if cost is None:
        cost = prices.get(service, {}).get(country, {}).get('any', {}).get('cost', 0)
    return float(cost)

def get_all_countries():
    global cached_countries
    if not cached_countries:
        try:
            response = requests.get('https://5sim.net/v1/guest/countries', timeout=10)
            if response.status_code == 200:
                cached_countries = list(response.json().keys())
        except: pass
    if not cached_countries:
        return ['russia', 'england', 'usa', 'indonesia', 'india']
    return cached_countries

def verify_5sim_apikey(api_key):
    headers = {'Authorization': 'Bearer ' + api_key, 'Accept': 'application/json'}
    response = requests.get('https://5sim.net/v1/user/profile', headers=headers, timeout=10)
    if response.status_code == 200: return True, response.json()
    return False, {}

def init_user(user_id):
    if user_id not in users_db:
        users_db[user_id] = {
            'state': 'MAIN_MENU', 'api_key': '', 'logged_in': False,
            'favorites': [], 'fav_countries': [], 'operator_defaults': {}, 'temp_data': {}, 'stop_search': False,
            'prefixes': {}, 'stopped_searches': set(), 'cancelled_orders': set(), 'orders_with_otp': set(), 'active_threads': {},
            'purchase_context': {}, 'number_type': 'fresh',
            'balance': 0, 'allowed_countries': 'all', 'allowed_services': 'all', 'price_overrides': {} # {country: {service: price}}
        }
        # Migration for existing users
    else:
        u = users_db[user_id]
        if 'balance' not in u: u['balance'] = 0
        if 'allowed_countries' not in u: u['allowed_countries'] = 'all'
        if 'allowed_services' not in u: u['allowed_services'] = 'all'
        if 'price_overrides' not in u: u['price_overrides'] = {}
        
    save_db(db_data)
    return users_db[user_id]

# --- ACCESS CONTROL DECORATOR ---
def access_required(func):
    def wrapper(message, *args, **kwargs):
        user_id = message.from_user.id if hasattr(message, 'from_user') else message.chat.id
        if user_id in db_data.get("blocked", []):
            bot.reply_to(message, "❌ You have been blocked by the admin.")
            return
        if user_id not in ADMIN_IDS and user_id not in db_data.get("approved", []):
            bot.reply_to(message, "⏳ Your account is waiting for admin approval. Please wait.")
            return
        return func(message, *args, **kwargs)
    return wrapper

# --- HANDLERS ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    init_user(user_id)
    
    if user_id in ADMIN_IDS:
        bot.send_message(message.chat.id, "👑 Welcome Admin! Use `/admin` to control the bot.")
        show_main_menu(message)
        return

    if user_id in db_data.get("blocked", []):
        bot.reply_to(message, "❌ You have been blocked by the admin.")
        return

    if user_id not in db_data.get("approved", []):
        if user_id not in db_data.get("pending_approval", []):
            db_data.setdefault("pending_approval", []).append(user_id)
            save_db(db_data)
            # Notify Admin
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("✅ Approve", callback_data=f"adm_approve_{user_id}"),
                       InlineKeyboardButton("🚫 Block", callback_data=f"adm_block_{user_id}"))
            # Notify All Admins
            for adm in ADMIN_IDS:
                try: bot.send_message(adm, f"🔔 New Access Request\n\nUser: {message.from_user.first_name} (@{message.from_user.username})\nID: {user_id}", reply_markup=markup)
                except: pass
        
        bot.reply_to(message, "⏳ *Your account is waiting for admin approval.*\n\nPlease wait until the admin approves your request. You will be notified once approved.", parse_mode="Markdown")
        return

    if not users_db[user_id].get('logged_in'):
        users_db[user_id]['state'] = 'WAITING_FOR_API_KEY'
        bot.reply_to(message, "Welcome! 🚀\n\nPlease enter your API Key to login:")
    else:
        show_main_menu(message)

@bot.message_handler(commands=['admin'])
def admin_panel(message, edit=False):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS: return
    markup = InlineKeyboardMarkup()
    pending_count = len(db_data.get("pending_approval", []))
    markup.add(InlineKeyboardButton(f"🆕 Pending Approvals ({pending_count})", callback_data="adm_reqlist"))
    markup.add(InlineKeyboardButton("👥 User List", callback_data="adm_userlist"))
    markup.add(InlineKeyboardButton("� Set Global Prices", callback_data="adm_globalprice"))
    markup.add(InlineKeyboardButton("�📊 Checker Stats", callback_data="adm_stats"))
    text = "🛠 *Admin Panel*\n\nManage users, approvals, and permissions."
    if edit:
        try: bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=markup, parse_mode="Markdown")
        except: bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")
    else:
        bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="Markdown")

# FIX: Single unified admin callback handler (removed duplicate)
@bot.callback_query_handler(func=lambda call: call.data.startswith('adm_'))
def handle_admin_callbacks(call):
    if call.from_user.id not in ADMIN_IDS: return
    data = call.data.split('_')
    cmd = data[1]

    if cmd == 'reqlist':
        pending = db_data.get("pending_approval", [])
        if not pending:
            bot.answer_callback_query(call.id, "No pending requests.")
            return
        markup = InlineKeyboardMarkup()
        for uid in pending:
            try:
                chat = bot.get_chat(uid)
                name = chat.first_name or f"User {uid}"
                if chat.username: name += f" (@{chat.username})"
            except: name = f"User {uid}"
            markup.add(InlineKeyboardButton(name, callback_data=f"adm_manageuser_{uid}"))
        markup.add(InlineKeyboardButton("🔙 Back", callback_data="adm_main"))
        bot.edit_message_text("🆕 *Pending Approval Requests*", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

    elif cmd == 'userlist':
        approved = db_data.get("approved", [])
        if not approved:
            bot.answer_callback_query(call.id, "No approved users.")
            return
        markup = InlineKeyboardMarkup()
        for uid in approved:
            try:
                if uid in ADMIN_IDS: 
                    name = f"👑 Admin ({'Self' if uid == call.from_user.id else 'Partner'})"
                else:
                    chat = bot.get_chat(uid)
                    name = chat.first_name or str(uid)
                    if chat.username: name += f" (@{chat.username})"
            except: name = f"User {uid}"
            markup.add(InlineKeyboardButton(name, callback_data=f"adm_manageuser_{uid}"))
        markup.add(InlineKeyboardButton("🔙 Back", callback_data="adm_main"))
        bot.edit_message_text("👥 *Approved User List*", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

    elif cmd == 'globalprice':
        page = int(data[2]) if len(data) > 2 else 0
        search_query = data[3] if len(data) > 3 else None
        
        countries = get_all_countries()
        if search_query:
            countries = [c for c in countries if search_query.lower() in c.lower()]
            
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⭐ SET ALL (Truly Global)", callback_data="adm_glpsel_all"))
        
        items_per_page = 15
        total_pages = max(1, len(countries) // items_per_page + (1 if len(countries) % items_per_page > 0 else 0))
        current_countries = countries[page * items_per_page : (page + 1) * items_per_page]
        
        for c in current_countries:
            markup.add(InlineKeyboardButton(c.capitalize(), callback_data=f"adm_glpsel_{c}"))
            
        nav_buttons = []
        if page > 0: nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"adm_globalprice_{page-1}"))
        if page < total_pages - 1: nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"adm_globalprice_{page+1}"))
        if nav_buttons: markup.add(*nav_buttons)
        
        markup.add(InlineKeyboardButton("🔍 Search Country", callback_data="adm_glpsearch"))
        markup.add(InlineKeyboardButton("🔙 Back", callback_data="adm_main"))
        
        text = f"💹 *Select Country for Global Price* (Page {page+1}/{total_pages})"
        if search_query: text += f"\n🔍 Search: `{search_query}`"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

    elif cmd == 'glpsearch':
        uid = call.from_user.id
        users_db[uid]['state'] = 'ADM_WAITING_GLP_SEARCH'
        users_db[uid]['temp_data']['mng_msg_id'] = call.message.message_id
        prompt = bot.send_message(call.message.chat.id, "🔍 *Enter country name to filter pricing menu:*", parse_mode="Markdown")
        users_db[uid]['temp_data']['prompt_msg_id'] = prompt.message_id
        bot.answer_callback_query(call.id)

    elif cmd == 'glpsel':
        ctry = data[2]
        search_query = data[3] if len(data) > 3 else None
        services = get_services()
        if search_query:
            services = [s for s in services if search_query.lower() in s.lower()]
            
        markup = InlineKeyboardMarkup()
        if not search_query:
            if ctry == 'all':
                markup.add(InlineKeyboardButton("⭐ SET ALL SERVICES (Flat Price)", callback_data="adm_glpfinal_all_all_any"))
            
            top = ["telegram", "whatsapp", "google", "instagram", "facebook"]
            for s in top:
                markup.add(InlineKeyboardButton(s.capitalize(), callback_data=f"adm_glpsrv_{ctry}_{s}"))
            markup.add(InlineKeyboardButton("📦 More Services", callback_data=f"adm_glpsrvlist_{ctry}_0"))
        else:
            for s in services[:15]:
                markup.add(InlineKeyboardButton(s.capitalize(), callback_data=f"adm_glpsrv_{ctry}_{s}"))
                
        markup.add(InlineKeyboardButton("🔍 Search Service", callback_data=f"adm_glpsrvsrch_{ctry}"))
        markup.add(InlineKeyboardButton("🔙 Back", callback_data="adm_globalprice"))
        
        text = f"💹 *Select Service for {ctry.capitalize()}*"
        if search_query: text += f"\n🔍 Search: `{search_query}`"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

    elif cmd == 'glpsrvsrch':
        ctry = data[2]
        uid = call.from_user.id
        users_db[uid]['state'] = 'ADM_WAITING_GLPSRV_SEARCH'
        users_db[uid]['temp_data']['target_ctry'] = ctry
        users_db[uid]['temp_data']['mng_msg_id'] = call.message.message_id
        prompt = bot.send_message(call.message.chat.id, f"🔍 *Enter service name to filter for {ctry.capitalize()}:*", parse_mode="Markdown")
        users_db[uid]['temp_data']['prompt_msg_id'] = prompt.message_id
        bot.answer_callback_query(call.id)

    elif cmd == 'glpsrvlist':
        ctry, page = data[2], int(data[3])
        services = get_services()
        items_per_page = 20
        total_pages = max(1, len(services) // items_per_page + (1 if len(services) % items_per_page > 0 else 0))
        current_srv = services[page * items_per_page : (page+1) * items_per_page]
        
        markup = InlineKeyboardMarkup()
        for s in current_srv:
            markup.add(InlineKeyboardButton(s.capitalize(), callback_data=f"adm_glpsrv_{ctry}_{s}"))
        
        nav = []
        if page > 0: nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"adm_glpsrvlist_{ctry}_{page-1}"))
        if page < total_pages - 1: nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"adm_glpsrvlist_{ctry}_{page+1}"))
        if nav: markup.add(*nav)
        markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"adm_glpsel_{ctry}"))
        bot.edit_message_text(f"💹 *Select Service for {ctry.capitalize()}* (Page {page+1}/{total_pages})", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

    elif cmd == 'glpsrv':
        ctry, srv = data[2], data[3]
        prices = get_service_prices(srv)
        srv_data = prices.get(srv, {}).get(ctry, {})
        operators = list(srv_data.keys())
        
        markup = InlineKeyboardMarkup()
        if not operators:
            markup.add(InlineKeyboardButton("📶 ANY Operator", callback_data=f"adm_glpfinal_{ctry}_{srv}_any"))
        else:
            for op in sorted(operators):
                cost = srv_data[op].get('cost', '?')
                markup.add(InlineKeyboardButton(f"📶 {op.upper()} ({cost}₽)", callback_data=f"adm_glpfinal_{ctry}_{srv}_{op}"))
            if 'any' not in operators:
                markup.add(InlineKeyboardButton("📶 ANY (Manual)", callback_data=f"adm_glpfinal_{ctry}_{srv}_any"))

        markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"adm_glpsel_{ctry}"))
        bot.edit_message_text(f"💹 *Select Operator for {srv.capitalize()} in {ctry.capitalize()}*", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

    elif cmd == 'glpfinal':
        ctry, srv, op = data[2], data[3], data[4]
        data_prices = get_service_prices(srv) if srv != 'all' else {}
        
        # Robust Price Fetch
        cost_5sim = data_prices.get(srv, {}).get(ctry, {}).get(op, {}).get('cost')
        if cost_5sim is None and op == 'any':
            # Fallback to first available operator if 'any' is missing
            c_data = data_prices.get(srv, {}).get(ctry, {})
            if c_data:
                first_op = list(c_data.keys())[0]
                cost_5sim = c_data[first_op].get('cost', 'Unknown')
            else: cost_5sim = 'Unknown'
        elif cost_5sim is None: cost_5sim = 'Unknown'
        
        current = db_data.get("global_prices", {}).get(ctry, {}).get(srv, 'Not Set')

        uid = call.from_user.id
        users_db[uid]['init'] = True # Ensure dict exists
        users_db[uid]['state'] = 'ADM_WAITING_GLOBAL_PRICE'
        users_db[uid]['temp_data'].update({'target_ctry': ctry, 'target_srv': srv, 'target_target_op': op})
        
        target_str = 'ALL Countries' if ctry == 'all' else ctry.capitalize()
        service_str = 'ALL Services' if srv == 'all' else srv.capitalize()
        
        text = (f"💹 *Set Price for {service_str} ({op.upper()})*\n"
                f"🌍 *Target:* {target_str}\n\n"
                f"💰 5sim Price: `{cost_5sim}` ₽\n"
                f"📌 Current Price: `{current}` ₽\n\n"
                f"✍️ *Send the new price:*")
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="Markdown")

    elif cmd == 'manageuser':
        target_id = int(data[2])
        init_user(target_id)
        u = users_db[target_id]
        status = "✅ Approved" if target_id in db_data["approved"] else "⏳ Pending"
        if target_id in db_data["blocked"]: status = "🚫 Blocked"
        
        text = (f"👤 *User Management*\n\n"
                f"🆔 ID: `{target_id}`\n"
                f"📊 Status: {status}\n"
                f"💰 Balance: `{u['balance']}` ₽\n"
                f"🌍 Countries: {u['allowed_countries']}\n"
                f"📦 Services: {u['allowed_services']}")
        
        markup = InlineKeyboardMarkup()
        if target_id not in db_data["approved"]:
            markup.add(InlineKeyboardButton("✅ Approve", callback_data=f"adm_approve_{target_id}"))
        
        markup.add(InlineKeyboardButton("💵 Edit Balance", callback_data=f"adm_editbal_{target_id}"))
        markup.add(InlineKeyboardButton("🌍 Manage Countries", callback_data=f"adm_mngctry_{target_id}"))
        markup.add(InlineKeyboardButton("📦 Manage Services", callback_data=f"adm_mngsrv_{target_id}"))
        # REMOVED: markup.add(InlineKeyboardButton("💹 Set Custom Prices", callback_data=f"adm_setprice_{target_id}"))
        
        if target_id in db_data["blocked"]:
            markup.add(InlineKeyboardButton("🔓 Unblock", callback_data=f"adm_unblock_{target_id}"))
        else:
            markup.add(InlineKeyboardButton("🚫 Block", callback_data=f"adm_block_{target_id}"))
            
        markup.add(InlineKeyboardButton("🔙 Back", callback_data="adm_userlist"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

    elif cmd == 'approve':
        target_id = int(data[2])
        if target_id not in db_data["approved"]:
            db_data["approved"].append(target_id)
            if target_id in db_data.get("pending_approval", []):
                db_data["pending_approval"].remove(target_id)
            save_db(db_data)
            bot.answer_callback_query(call.id, "User Approved!")
            bot.send_message(target_id, "✅ Your access has been approved! Send /start to begin.")
            handle_admin_callbacks(type('obj', (object,), {'from_user': call.from_user, 'data': f"adm_manageuser_{target_id}", 'id': call.id, 'message': call.message}))
            
    elif cmd == 'block':
        target_id = int(data[2])
        if target_id not in db_data["blocked"]:
            db_data["blocked"].append(target_id)
            if target_id in db_data["approved"]: db_data["approved"].remove(target_id)
            if target_id in db_data.get("pending_approval", []): db_data["pending_approval"].remove(target_id)
            save_db(db_data)
            bot.answer_callback_query(call.id, "User Blocked!")
            handle_admin_callbacks(type('obj', (object,), {'from_user': call.from_user, 'data': f"adm_manageuser_{target_id}", 'id': call.id, 'message': call.message}))

    elif cmd == 'unblock':
        target_id = int(data[2])
        if target_id in db_data["blocked"]:
            db_data["blocked"].remove(target_id)
            save_db(db_data)
            bot.answer_callback_query(call.id, "User Unblocked!")
            handle_admin_callbacks(type('obj', (object,), {'from_user': call.from_user, 'data': f"adm_manageuser_{target_id}", 'id': call.id, 'message': call.message}))

    elif cmd == 'editbal':
        target_id = int(data[2])
        uid = call.from_user.id
        users_db[uid]['state'] = 'ADM_WAITING_BALANCE'
        users_db[uid]['temp_data']['target_user'] = target_id
        bot.edit_message_text(f"� Enter new balance for user `{target_id}`:", call.message.chat.id, call.message.message_id, parse_mode="Markdown")

    elif cmd == 'mngctry':
        target_id = int(data[2])
        search_query = data[3] if len(data) > 3 else None
        u = users_db[target_id]
        countries = get_all_countries()
        if search_query:
            countries = [c for c in countries if search_query.lower() in c.lower()]
        
        markup = InlineKeyboardMarkup(row_width=2)
        allowed = u.get('allowed_countries', 'all')
        
        all_text = "⭐ Allowed All" if allowed == 'all' else "✅ Allow All"
        markup.add(InlineKeyboardButton(all_text, callback_data=f"adm_ctryset_{target_id}_all"))
        
        # Only show up to 15 countries to avoid button limit
        for c in countries[:15]:
            is_allowed = allowed == 'all' or c in allowed
            txt = f"{'✅' if is_allowed else '❌'} {c.capitalize()}"
            markup.add(InlineKeyboardButton(txt, callback_data=f"adm_ctrytoggle_{target_id}_{c}"))
            
        markup.add(InlineKeyboardButton("� Search Country", callback_data=f"adm_ctrysrch_{target_id}"))
        markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"adm_manageuser_{target_id}"))
        
        text = f"🌍 *Manage Countries for {target_id}*"
        if search_query: text += f"\n🔍 Search query: `{search_query}`"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

    elif cmd == 'ctrysrch':
        target_id = int(data[2])
        uid = call.from_user.id
        users_db[uid]['state'] = 'ADM_WAITING_CTRY_SEARCH'
        users_db[uid]['temp_data']['target_user'] = target_id
        users_db[uid]['temp_data']['mng_msg_id'] = call.message.message_id
        prompt = bot.send_message(call.message.chat.id, f"🔍 *Enter country name to filter list for user {target_id}:*", parse_mode="Markdown")
        users_db[uid]['temp_data']['prompt_msg_id'] = prompt.message_id
        bot.answer_callback_query(call.id)

    elif cmd == 'ctryset':
        target_id, cval = int(data[2]), data[3]
        current = users_db[target_id].get('allowed_countries', 'all')
        
        if cval == 'all':
            # Toggle logic
            new_val = [] if current == 'all' else 'all'
            users_db[target_id]['allowed_countries'] = new_val
        else:
            users_db[target_id]['allowed_countries'] = cval
            
        save_db(db_data)
        bot.answer_callback_query(call.id, "Updated!")
        handle_admin_callbacks(type('obj', (object,), {'from_user': call.from_user, 'data': f"adm_mngctry_{target_id}", 'id': call.id, 'message': call.message}))

    elif cmd == 'ctrytoggle':
        target_id, ctry = int(data[2]), data[3]
        allowed = users_db[target_id].get('allowed_countries', 'all')
        if allowed == 'all': allowed = [ctry] # Start with just this one if toggled from 'all'
        elif ctry in allowed:
            allowed.remove(ctry)
            if not allowed: allowed = []
        else:
            allowed.append(ctry)
        users_db[target_id]['allowed_countries'] = allowed
        save_db(db_data)
        handle_admin_callbacks(type('obj', (object,), {'from_user': call.from_user, 'data': f"adm_mngctry_{target_id}", 'id': call.id, 'message': call.message}))

    elif cmd == 'mngsrv':
        target_id = int(data[2])
        search_query = data[3] if len(data) > 3 else None
        u = users_db[target_id]
        services = get_services()
        if search_query:
            services = [s for s in services if search_query.lower() in s.lower()]
        
        markup = InlineKeyboardMarkup(row_width=2)
        allowed = u.get('allowed_services', 'all')
        
        all_text = "⭐ Allowed All" if allowed == 'all' else "✅ Allow All"
        markup.add(InlineKeyboardButton(all_text, callback_data=f"adm_srvset_{target_id}_all"))
        
        for s in services[:15]:
            is_allowed = allowed == 'all' or s in allowed
            txt = f"{'✅' if is_allowed else '❌'} {s.capitalize()}"
            markup.add(InlineKeyboardButton(txt, callback_data=f"adm_srvtoggle_{target_id}_{s}"))
            
        markup.add(InlineKeyboardButton("� Search Service", callback_data=f"adm_srvsrch_{target_id}"))
        markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"adm_manageuser_{target_id}"))
        
        text = f"📦 *Manage Services for {target_id}*"
        if search_query: text += f"\n🔍 Search query: `{search_query}`"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

    elif cmd == 'srvsrch':
        target_id = int(data[2])
        uid = call.from_user.id
        users_db[uid]['state'] = 'ADM_WAITING_SRV_SEARCH'
        users_db[uid]['temp_data']['target_user'] = target_id
        users_db[uid]['temp_data']['mng_msg_id'] = call.message.message_id
        prompt = bot.send_message(call.message.chat.id, f"🔍 *Enter service name to filter list for user {target_id}:*", parse_mode="Markdown")
        users_db[uid]['temp_data']['prompt_msg_id'] = prompt.message_id
        bot.answer_callback_query(call.id)

    elif cmd == 'srvset':
        target_id, sval = int(data[2]), data[3]
        current = users_db[target_id].get('allowed_services', 'all')
        
        if sval == 'all':
            new_val = [] if current == 'all' else 'all'
            users_db[target_id]['allowed_services'] = new_val
        else:
            users_db[target_id]['allowed_services'] = sval
            
        save_db(db_data)
        bot.answer_callback_query(call.id, "Updated!")
        handle_admin_callbacks(type('obj', (object,), {'from_user': call.from_user, 'data': f"adm_mngsrv_{target_id}", 'id': call.id, 'message': call.message}))

    elif cmd == 'srvtoggle':
        target_id, srv = int(data[2]), data[3]
        allowed = users_db[target_id].get('allowed_services', 'all')
        if allowed == 'all': allowed = [srv]
        elif srv in allowed:
            allowed.remove(srv)
            if not allowed: allowed = []
        else:
            allowed.append(srv)
        users_db[target_id]['allowed_services'] = allowed
        save_db(db_data)
        handle_admin_callbacks(type('obj', (object,), {'from_user': call.from_user, 'data': f"adm_mngsrv_{target_id}", 'id': call.id, 'message': call.message}))

    elif cmd == 'setprice':
        target_id = int(data[2])
        # Select country first
        countries = ["russia", "england", "usa", "indonesia", "india"]
        markup = InlineKeyboardMarkup()
        for c in countries:
            markup.add(InlineKeyboardButton(c.capitalize(), callback_data=f"adm_prcselctry_{target_id}_{c}"))
        markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"adm_manageuser_{target_id}"))
        bot.edit_message_text("💹 *Select Country to set price*", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

    elif cmd == 'prcselctry':
        target_id, ctry = int(data[2]), data[3]
        services = ["telegram", "whatsapp", "google"]
        markup = InlineKeyboardMarkup()
        for s in services:
            markup.add(InlineKeyboardButton(s.capitalize(), callback_data=f"adm_prcset_{target_id}_{ctry}_{s}"))
        markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"adm_setprice_{target_id}"))
        bot.edit_message_text(f"💹 *Select Service for {ctry.capitalize()}*", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

    elif cmd == 'prcset':
        target_id, ctry, srv = int(data[2]), data[3], data[4]
        prices = get_service_prices(srv)
        srv_data = prices.get(srv, {}).get(ctry, {})
        operators = list(srv_data.keys())
        
        markup = InlineKeyboardMarkup()
        if not operators:
            markup.add(InlineKeyboardButton("📶 ANY Operator", callback_data=f"adm_prcfinal_{target_id}_{ctry}_{srv}_any"))
        else:
            for op in sorted(operators):
                cost = srv_data[op].get('cost', '?')
                markup.add(InlineKeyboardButton(f"📶 {op.upper()} ({cost}₽)", callback_data=f"adm_prcfinal_{target_id}_{ctry}_{srv}_{op}"))
            if 'any' not in operators:
                markup.add(InlineKeyboardButton("📶 ANY (Manual)", callback_data=f"adm_prcfinal_{target_id}_{ctry}_{srv}_any"))
        
        markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"adm_prcselctry_{target_id}_{ctry}"))
        bot.edit_message_text(f"💹 *Select Operator for {srv.capitalize()} in {ctry.capitalize()}*", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

    elif cmd == 'prcfinal':
        target_id, ctry, srv, op = int(data[2]), data[3], data[4], data[5]
        # Show 5sim price
        data_prices = get_service_prices(srv)
        cost_5sim = data_prices.get(srv, {}).get(ctry, {}).get(op, {}).get('cost')
        if cost_5sim is None and op == 'any':
            c_data = data_prices.get(srv, {}).get(ctry, {})
            if c_data:
                first_op = list(c_data.keys())[0]
                cost_5sim = c_data[first_op].get('cost', 'Unknown')
            else: cost_5sim = 'Unknown'
        elif cost_5sim is None: cost_5sim = 'Unknown'
        
        current_price = users_db[target_id].get('price_overrides', {}).get(ctry, {}).get(srv, 'Not Set')

        users_db[call.from_user.id]['state'] = 'ADM_WAITING_PRICE'
        users_db[call.from_user.id]['temp_data'].update({'target_user': target_id, 'target_ctry': ctry, 'target_srv': srv, 'target_op': op})
        
        text = (f"💹 *Set Price for {srv.capitalize()} ({op.upper()}) in {ctry.capitalize()}*\n\n"
                f"👤 User: `{target_id}`\n"
                f"💰 5sim Price: `{cost_5sim}` ₽\n"
                f"📌 Current User Price: `{current_price}` ₽\n\n"
                f"✍️ *Send the new price to set:*")
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="Markdown")

    elif cmd == 'main':
        admin_panel(call.message, edit=True)

    elif cmd == 'stats':
        s = db_data["stats"]
        text = (
            "📊 *Checker Real-time Stats*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ *Unopened (Fresh):* `{s.get('fresh', 0)}`\n"
            f"🔐 *Opened (Used):* `{s.get('used', 0)}`\n"
            f"❌ *Banned (Blocked):* `{s.get('banned', 0)}`\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 *Total Scanned:* `{s['checked']}`"
        )
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, text, parse_mode="Markdown")

def show_main_menu(message):
    user_id = message.from_user.id
    init_user(user_id)
    users_db[user_id]['state'] = 'MAIN_MENU'
    cleanup_messages(message.chat.id)
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    markup.add(
        KeyboardButton('📱 Buy Number'), KeyboardButton('📨 Check SMS'),
        KeyboardButton('💰 My Balance'), KeyboardButton('📋 My Orders'),
        KeyboardButton('⚙️ Setup Checker'), KeyboardButton('🗑️ Remove Checker'),
        KeyboardButton('📈 Checker Stats'), KeyboardButton('👥 Accounts'),
        KeyboardButton('🚪 Logout')
    )
    bot.send_message(message.chat.id, "🏠 *Main Menu*\n\nChoose an option:", reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == '⚙️ Setup Checker')
@access_required
def prompt_setup_checker(message):
    user_id = message.from_user.id
    init_user(user_id)
    msg = bot.reply_to(message, "⏳ *Checking current session status...*", parse_mode="Markdown")
    try:
        if checker.is_authorized(user_id):
            bot.edit_message_text("✅ You already have an active checker session! If you want to replace it, use 🗑️ Remove Checker first.", message.chat.id, msg.message_id)
            return
        users_db[user_id]['state'] = 'WAITING_FOR_CHECKER_PHONE'
        bot.edit_message_text("📞 *Enter your checking Account Phone Number*\n(including country code, e.g. +880...):\n\n*Make sure you have started `@TelCheckers_bot` on this account first!*", message.chat.id, msg.message_id, parse_mode="Markdown")
    except Exception as e:
        users_db[user_id]['state'] = 'WAITING_FOR_CHECKER_PHONE'
        bot.edit_message_text(f"⚠️ Checker system warning, but you can try anyway.\n📞 Enter Phone Number (+880...):", message.chat.id, msg.message_id)

@bot.message_handler(func=lambda message: message.text == '🗑️ Remove Checker')
@access_required
def cmd_remove_checker(message):
    user_id = message.from_user.id
    if checker.is_authorized(user_id):
        checker.logout(user_id)
        bot.reply_to(message, "🗑️ Session deleted successfully.")
    else:
        bot.reply_to(message, "❌ You don't have an active checker session.")

@bot.message_handler(func=lambda message: message.text == '📈 Checker Stats')
@access_required
def show_checker_stats(message):
    s = db_data["stats"]
    text = (
        "📈 *Checker Real-time Stats*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ *Unopened (Fresh):* `{s.get('fresh', 0)}`\n"
        f"🔐 *Opened (Used):* `{s.get('used', 0)}`\n"
        f"❌ *Banned (Blocked):* `{s.get('banned', 0)}`\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔄 *Total Scanned:* `{s['checked']}`"
    )
    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == '🚪 Logout')
@access_required
def logout_user(message):
    user_id = message.from_user.id
    if user_id in users_db:
        users_db[user_id]['logged_in'] = False
        users_db[user_id]['state'] = 'WAITING_FOR_API_KEY'
    bot.reply_to(message, "You have been logged out.", reply_markup=telebot.types.ReplyKeyboardRemove())

@bot.message_handler(func=lambda message: message.text == '💰 My Balance')
@access_required
def check_balance(message):
    user_id = message.from_user.id
    init_user(user_id)
    bal = users_db[user_id].get('balance', 0)
    bot.reply_to(message, f"💰 *Your Account Balance:* `{bal}` ₽", parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == '📱 Buy Number')
@access_required
def buy_number_countries_start(message):
    user_id = message.from_user.id
    if user_id in users_db and users_db[user_id].get('logged_in'):
        cleanup_messages(message.chat.id)
        msg = show_countries_page(message.chat.id, user_id, msg_id=None, page=0)
        users_db[user_id]['temp_data']['last_msg_id'] = msg.message_id

def show_services_page(chat_id, user_id, page=0, search_query=None, edit_msg_id=None):
    services = get_services()
    
    # Permission Filter
    allowed = users_db.get(user_id, {}).get('allowed_services', 'all')
    if allowed != 'all':
        allowed_list = list(allowed) if isinstance(allowed, (list, set)) else [allowed]
        services = [s for s in services if s in allowed_list]

    if search_query: services = [s for s in services if search_query.lower() in s.lower()]

    favs = users_db.get(user_id, {}).get('favorites', [])
    fav_services = [s for s in services if s in favs]
    other_services = [s for s in services if s not in favs]
    services = sorted(fav_services) + sorted(other_services)

    items_per_page = 30
    total_pages = max(1, len(services) // items_per_page + (1 if len(services) % items_per_page > 0 else 0))
    current_services = services[page * items_per_page : (page + 1) * items_per_page]

    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for srv in current_services:
        prefix = "⭐ " if srv in favs else ""
        buttons.append(InlineKeyboardButton(f"{prefix}{srv.capitalize()}", callback_data=f"srv_{srv}"))
    for i in range(0, len(buttons), 2): markup.add(*buttons[i:i+2])

    nav_buttons = []
    if page > 0: nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{page-1}"))
    if page < total_pages - 1: nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page+1}"))
    if nav_buttons: markup.add(*nav_buttons)

    markup.add(InlineKeyboardButton("🔍 Search Service", callback_data="search_service"))
    markup.add(InlineKeyboardButton("🌍 Back to Countries", callback_data="back_to_countries"))
    text = f"Select a service (Page {page+1}/{total_pages}):"
    if search_query: text = f"Search results for '{search_query}':"
    if edit_msg_id:
        try: return bot.edit_message_text(text, chat_id, edit_msg_id, reply_markup=markup)
        except: return bot.send_message(chat_id, text, reply_markup=markup)
    return bot.send_message(chat_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('fav_'))
def handle_favorite_service(call):
    service = call.data.split('_')[1]
    user_id = call.from_user.id
    favs = users_db.get(user_id, {}).get('favorites', [])
    if service in favs:
        favs.remove(service)
        bot.answer_callback_query(call.id, f"❌ {service.capitalize()} removed from favorites!")
    else:
        favs.append(service)
        bot.answer_callback_query(call.id, f"⭐ {service.capitalize()} added to favorites!")
    users_db[user_id]['favorites'] = favs
    save_db(db_data)
    txt = getattr(call.message, 'text', '')
    parts = call.data.split('_')

    if "Select an operator" in txt:
        show_operators_page(call.message.chat.id, user_id, call.message.message_id)
    elif "Purchase Details" in txt:
        operator = parts[2] if len(parts) > 2 else users_db[user_id]['temp_data'].get('operator')
        if operator:
            show_purchase_confirmation(call.message.chat.id, user_id, operator, call.message.message_id)
        else:
            show_services_page(call.message.chat.id, user_id, edit_msg_id=call.message.message_id)
    else:
        show_services_page(call.message.chat.id, user_id, edit_msg_id=call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('page_'))
def paginate_services(call):
    user_id = call.from_user.id
    show_services_page(call.message.chat.id, user_id, page=int(call.data.split('_')[1]), edit_msg_id=call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == 'search_service')
def search_service_prompt(call):
    user_id = call.from_user.id
    init_user(user_id)
    users_db[user_id]['state'] = 'WAITING_FOR_SEARCH'
    users_db[user_id]['temp_data']['last_msg_id'] = call.message.message_id
    bot.edit_message_text("🔍 *Enter service name to search (e.g. Telegram):*", call.message.chat.id, call.message.message_id, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('srv_'))
def service_selected(call):
    service = call.data.split('_')[1]
    user_id = call.from_user.id
    init_user(user_id)
    users_db[user_id]['temp_data']['service'] = service

    country = users_db[user_id]['temp_data'].get('country')
    default_op = users_db[user_id].get('operator_defaults', {}).get(country)

    if default_op:
        show_purchase_confirmation(call.message.chat.id, user_id, default_op, call.message.message_id)
    else:
        show_operators_page(call.message.chat.id, user_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('buy_'))
def start_operator_selection(call):
    service = call.data.split('_')[1]
    user_id = call.from_user.id
    users_db[user_id]['temp_data']['service'] = service
    show_operators_page(call.message.chat.id, user_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == 'back_to_countries')
def return_to_countries(call):
    show_countries_page(call.message.chat.id, call.from_user.id, call.message.message_id, page=0)

def show_countries_page(chat_id, user_id, msg_id, page=0, search_query=None):
    countries = get_all_countries()
    
    # Permission Filter
    allowed = users_db.get(user_id, {}).get('allowed_countries', 'all')
    if allowed != 'all':
        # Ensure we always treat it as a list of strings for inclusion check
        allowed_list = list(allowed) if isinstance(allowed, (list, set)) else [allowed]
        countries = [c for c in countries if c in allowed_list]

    if search_query: countries = [c for c in countries if search_query.lower() in c.lower()]

    favs = users_db.get(user_id, {}).get('fav_countries', [])
    fav_c = [c for c in countries if c in favs]
    other_c = [c for c in countries if c not in favs]
    countries = sorted(fav_c) + sorted(other_c)

    items_per_page = 20
    total_pages = max(1, len(countries) // items_per_page + (1 if len(countries) % items_per_page > 0 else 0))
    current_countries = countries[page * items_per_page : (page + 1) * items_per_page]
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for c in current_countries:
        prefix = "⭐ " if c in favs else ""
        buttons.append(InlineKeyboardButton(f"{prefix}{get_flag(c)} {c.capitalize()}", callback_data=f"selctry_{c}"))
    for i in range(0, len(buttons), 2): markup.add(*buttons[i:i+2])
    nav_buttons = []
    if page > 0: nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"cpage_{page-1}"))
    if page < total_pages - 1: nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"cpage_{page+1}"))
    if nav_buttons: markup.add(*nav_buttons)

    markup.add(InlineKeyboardButton("🔍 Search Country", callback_data="search_country"))
    text = f"Select a country (Page {page+1}/{total_pages}):"
    if search_query: text = f"Country search results for '{search_query}':"
    if msg_id:
        try: return bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup)
        except: return bot.send_message(chat_id, text, reply_markup=markup)
    return bot.send_message(chat_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'search_country')
def search_country_prompt(call):
    users_db[call.from_user.id]['state'] = 'WAITING_FOR_CSEARCH'
    users_db[call.from_user.id]['temp_data']['last_msg_id'] = call.message.message_id
    bot.edit_message_text("🔍 *Enter country name to search:*", call.message.chat.id, call.message.message_id, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('cpage_'))
def handle_cpage_call(call):
    user_id = call.from_user.id
    page = int(call.data.split('_')[1])
    show_countries_page(call.message.chat.id, user_id, call.message.message_id, page=page)

# FIX: Single unified stopsearch handler (removed duplicate).
# This version correctly invalidates thread token AND adds to stopped_searches.
@bot.callback_query_handler(func=lambda call: call.data.startswith('stopsearch_'))
def handle_stopsearch_call(call):
    user_id = call.from_user.id
    msg_id = call.message.message_id
    operator = call.data.split('_')[1]

    if user_id in users_db:
        # Kill thread by invalidating its token
        users_db[user_id].setdefault('active_threads', {})[msg_id] = "STOPPED"
        # Also set the set-based flag as a fallback
        users_db[user_id].setdefault('stopped_searches', set()).add(msg_id)

    bot.answer_callback_query(call.id, "🛑 Stopping search...")
    show_purchase_confirmation(call.message.chat.id, user_id, operator, msg_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('selctry_'))
def country_selected(call):
    country = call.data.split('_')[1]
    user_id = call.from_user.id
    init_user(user_id)
    users_db[user_id]['temp_data']['country'] = country
    show_services_page(call.message.chat.id, user_id, page=0, edit_msg_id=call.message.message_id)

def show_operators_page(chat_id, user_id, msg_id):
    service = users_db[user_id]['temp_data'].get('service')
    country = users_db[user_id]['temp_data'].get('country')
    data = get_service_prices(service)
    service_data = data.get(service, {}).get(country, {})
    operators = list(service_data.keys())
    
    markup = InlineKeyboardMarkup(row_width=1)
    for op in sorted(operators):
        cost = get_user_price(user_id, country, service, op, prices=data)
        count = service_data[op].get('count', '?')
        markup.add(InlineKeyboardButton(f"📶 {op.upper()} - {count} pcs. - {cost}₽", callback_data=f"selop_{op}"))

    is_fav = service in users_db.get(user_id, {}).get('favorites', [])
    fav_text = "❌ Remove from Favorites" if is_fav else "⭐ Add to Favorites"
    markup.add(InlineKeyboardButton(fav_text, callback_data=f"fav_{service}"))
    markup.add(InlineKeyboardButton("🔙 Back to Services", callback_data="page_0"))
    bot.edit_message_text(f"Select an operator for *{service.capitalize()}* in *{country.capitalize()}*:", chat_id, msg_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('selop_'))
def operator_selected(call):
    show_purchase_confirmation(call.message.chat.id, call.from_user.id, operator=call.data.split('_')[1], msg_id=call.message.message_id)

def show_purchase_confirmation(chat_id, user_id, operator, msg_id):
    service = users_db[user_id]['temp_data'].get('service')
    country = users_db[user_id]['temp_data'].get('country')
    prefixes = users_db[user_id].get('prefixes', {}).get(country, [])
    if isinstance(prefixes, str): prefixes = [prefixes] if prefixes != 'None' else []
    display_prefix = ", ".join(prefixes) if prefixes else 'None'
    data = get_service_prices(service)
    op_data = data.get(service, {}).get(country, {}).get(operator, {})
    
    # Resolve Price
    cost = get_user_price(user_id, country, service, operator, prices=data)
    count = op_data.get('count', 'N/A')

    is_default = users_db[user_id].get('operator_defaults', {}).get(country) == operator
    is_fav_ctry = country in users_db[user_id].get('fav_countries', [])
    num_type = users_db[user_id].get('number_type', 'fresh')
    
    num_type_map = {'fresh': 'Fresh (Unopened)', 'opened': 'Opened 🔐', 'all': 'Fresh & Opened'}
    display_num_type = num_type_map.get(num_type, 'Fresh (Unopened)')

    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("🛒 Confirm Purchase (Buy)", callback_data=f"buyfinal_{operator}"))
    markup.add(InlineKeyboardButton(f"📁 Number Type: {display_num_type}", callback_data=f"selntype_{operator}"))
    markup.add(InlineKeyboardButton(f"🔢 Prefix Settings (Current: {display_prefix})", callback_data=f"setprefix_{operator}"))

    if is_fav_ctry:
        markup.add(InlineKeyboardButton("❌ Remove Country from Favorites", callback_data=f"unfavc_{country}_{operator}"))
    else:
        markup.add(InlineKeyboardButton("⭐ Add Country to Favorites", callback_data=f"favc_{country}_{operator}"))

    if is_default:
        markup.add(InlineKeyboardButton("❌ Remove Default Operator", callback_data=f"rmdefop_{operator}"))
    else:
        markup.add(InlineKeyboardButton("✅ Set as Default Operator", callback_data=f"setdefop_{operator}"))

    is_fav_srv = service in users_db.get(user_id, {}).get('favorites', [])
    fav_srv_text = "❌ Remove Service from Favorites" if is_fav_srv else "⭐ Add Service to Favorites"
    markup.add(InlineKeyboardButton(fav_srv_text, callback_data=f"fav_{service}_{operator}"))

    markup.add(InlineKeyboardButton("📶 Select Different Operator", callback_data="show_ops"))
    markup.add(InlineKeyboardButton("🔙 Back to Services", callback_data="page_0"))

    text = (f"📦 *Purchase Details*\n\n"
            f"🔹 *Service:* {service.capitalize()}\n"
            f"🔹 *Country:* {get_flag(country)} {country.capitalize()}\n"
            f"🔹 *Operator:* {operator.upper()}\n\n"
            f"💰 *Price:* {cost} ₽\n"
            f"📊 *Available:* {count} numbers")
    if is_default: text += "\n\n⭐ *This is your default operator for this country.*"
    if is_fav_ctry: text += "\n⭐ *This country is in your favorites.*"

    if 'purchase_context' not in users_db[user_id]: users_db[user_id]['purchase_context'] = {}
    users_db[user_id]['purchase_context'][str(msg_id)] = {'service': service, 'country': country}

    bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('favc_') or call.data.startswith('unfavc_'))
def handle_favorite_country(call):
    parts = call.data.split('_')
    action = parts[0]
    country = parts[1]
    operator = parts[2]
    user_id = call.from_user.id

    fav_c = users_db.get(user_id, {}).get('fav_countries', [])
    if action == 'favc' and country not in fav_c:
        fav_c.append(country)
        bot.answer_callback_query(call.id, f"⭐ {country.capitalize()} added to favorites!")
    elif action == 'unfavc' and country in fav_c:
        fav_c.remove(country)
        bot.answer_callback_query(call.id, f"❌ {country.capitalize()} removed from favorites!")

    users_db[user_id]['fav_countries'] = fav_c
    save_db(db_data)
    show_purchase_confirmation(call.message.chat.id, user_id, operator, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('setdefop_'))
def set_default_op(call):
    operator = call.data.split('_')[1]
    country = users_db[call.from_user.id]['temp_data'].get('country')
    if country:
        users_db[call.from_user.id]['operator_defaults'][country] = operator
        save_db(db_data)
        bot.answer_callback_query(call.id, f"✅ {operator.upper()} set as default for {country.capitalize()}!")
        show_purchase_confirmation(call.message.chat.id, call.from_user.id, operator, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('rmdefop_'))
def remove_default_op(call):
    country = users_db[call.from_user.id]['temp_data'].get('country')
    if country and country in users_db[call.from_user.id]['operator_defaults']:
        del users_db[call.from_user.id]['operator_defaults'][country]
        save_db(db_data)
        bot.answer_callback_query(call.id, "❌ Default operator removed.")
        operator = call.data.split('_')[1]
        show_purchase_confirmation(call.message.chat.id, call.from_user.id, operator, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('selntype_'))
def handle_select_num_type(call):
    operator = call.data.split('_')[1]
    user_id = call.from_user.id
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("✅ Fresh (Unopened)", callback_data=f"ntype_fresh_{operator}"),
        InlineKeyboardButton("🔐 Opened (Used)", callback_data=f"ntype_opened_{operator}"),
        InlineKeyboardButton("🔄 Fresh & Opened", callback_data=f"ntype_all_{operator}"),
        InlineKeyboardButton("🔙 Back", callback_data=f"selop_{operator}")
    )
    bot.edit_message_text("📂 *Select Number Type:*\n\nChoose what kind of numbers the bot should look for.", 
                         call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('ntype_'))
def set_num_type(call):
    parts = call.data.split('_')
    ntype = parts[1]
    operator = parts[2]
    user_id = call.from_user.id
    
    users_db[user_id]['number_type'] = ntype
    save_db(db_data)
    bot.answer_callback_query(call.id, f"✅ Type set to: {ntype.capitalize()}")
    show_purchase_confirmation(call.message.chat.id, user_id, operator, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == 'show_ops')
def show_ops_manual(call):
    show_operators_page(call.message.chat.id, call.from_user.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('setprefix_'))
def prompt_prefix(call):
    user_id = call.from_user.id
    op = call.data.split('_')[1]
    country = users_db[user_id]['temp_data'].get('country')
    prefixes = users_db[user_id].get('prefixes', {}).get(country, [])
    if isinstance(prefixes, str): prefixes = [prefixes] if prefixes != 'None' else []

    markup = InlineKeyboardMarkup(row_width=2)
    for p in prefixes:
        markup.add(InlineKeyboardButton(f"❌ Remove {p}", callback_data=f"rmpref_{p}_{op}"))

    markup.add(InlineKeyboardButton("➕ Add New Prefix", callback_data=f"addpref_{op}"))
    markup.add(InlineKeyboardButton("🗑️ Clear All", callback_data=f"clearallpref_{op}"))
    markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"selop_{op}"))

    text = f"🔢 *Prefix Settings for {country.capitalize()}*\n\nCurrent prefixes: `{', '.join(prefixes) if prefixes else 'None'}`"
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('addpref_'))
def start_add_prefix(call):
    op = call.data.split('_')[1]
    users_db[call.from_user.id]['state'] = 'WAITING_FOR_PREFIX'
    users_db[call.from_user.id]['temp_data']['prefix_op'] = op
    users_db[call.from_user.id]['temp_data']['last_msg_id'] = call.message.message_id
    msg = bot.send_message(call.message.chat.id, "✍️ *Send me the prefix (e.g. 7963):*", parse_mode="Markdown")
    users_db[call.from_user.id]['temp_data']['prompt_msg_id'] = msg.message_id
    add_cleanup(call.message.chat.id, msg.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('rmpref_'))
def handle_remove_prefix(call):
    parts = call.data.split('_')
    prefix_to_rm = parts[1]
    op = parts[2]
    user_id = call.from_user.id
    country = users_db[user_id]['temp_data'].get('country')

    prefixes = users_db[user_id].get('prefixes', {}).get(country, [])
    if isinstance(prefixes, str): prefixes = [prefixes] if prefixes != 'None' else []

    if prefix_to_rm in prefixes:
        prefixes.remove(prefix_to_rm)
        users_db[user_id]['prefixes'][country] = prefixes
        save_db(db_data)
        bot.answer_callback_query(call.id, f"❌ Removed prefix {prefix_to_rm}")

    prompt_prefix(call)

@bot.callback_query_handler(func=lambda call: call.data.startswith('clearallpref_'))
def handle_clear_prefixes(call):
    op = call.data.split('_')[1]
    user_id = call.from_user.id
    country = users_db[user_id]['temp_data'].get('country')
    if country in users_db[user_id].get('prefixes', {}):
        users_db[user_id]['prefixes'][country] = []
        save_db(db_data)
    bot.answer_callback_query(call.id, "🗑️ All prefixes cleared.")
    prompt_prefix(call)

@bot.callback_query_handler(func=lambda call: call.data == 'cancel_prefix')
def cancel_prefix_input(call):
    users_db[call.from_user.id]['state'] = 'MAIN_MENU'
    op = users_db[call.from_user.id]['temp_data'].get('prefix_op')
    if op: show_purchase_confirmation(call.message.chat.id, call.from_user.id, op, call.message.message_id)

# FIX: answer_callback_query added immediately — this was the main cause of the
# "stuck loading for 3-5 minutes" bug. Telegram waits up to 5min for an answer
# before auto-dismissing, which made the button appear frozen.
@bot.callback_query_handler(func=lambda call: call.data.startswith('buyfinal_'))
def handle_final_purchase(call):
    operator = call.data.split('_')[1]
    user_id = call.from_user.id
    init_user(user_id)
    msg_id = call.message.message_id  # int

    # CRITICAL: Must answer immediately or Telegram shows loading spinner for 3-5 minutes
    bot.answer_callback_query(call.id, "⏳ Starting search...")

    # Clear any stale stop/cancelled flags so a fresh search always starts clean
    users_db[user_id].setdefault('stopped_searches', set()).discard(msg_id)

    ctx = users_db[user_id].get('purchase_context', {}).get(str(msg_id), {})
    service = ctx.get('service') or users_db[user_id]['temp_data'].get('service')
    country = ctx.get('country') or users_db[user_id]['temp_data'].get('country')

    threading.Thread(
        target=background_buy_loop,
        args=(call.message.chat.id, user_id, operator, msg_id, service, country),
        daemon=True
    ).start()

def background_buy_loop(chat_id, user_id, operator, msg_id, service, country):
    api_key = users_db[user_id].get('api_key')
    user_price = get_user_price(user_id, country, service, operator)

    if not checker.is_authorized(user_id):
        bot.send_message(chat_id, "⚠️ *Checker is not logged in!*\n\nYou must setup the checker session via `⚙️ Setup Checker` from the main menu before buying numbers.", parse_mode="Markdown")
        return

    # Balance Check
    if users_db[user_id].get('balance', 0) < user_price:
        bot.send_message(chat_id, f"❌ *Insufficient Balance!*\n\nYou need `{user_price}` ₽, but you have `{users_db[user_id].get('balance', 0)}` ₽.", parse_mode="Markdown")
        return

    users_db[user_id].setdefault('active_threads', {})
    thread_token = str(uuid.uuid4())
    users_db[user_id]['active_threads'][msg_id] = thread_token

    headers = {'Authorization': 'Bearer ' + api_key, 'Accept': 'application/json'}

    for attempt in range(1, 51):
        if users_db[user_id].get('active_threads', {}).get(msg_id) != thread_token:
            return

        if msg_id in users_db[user_id].get('stopped_searches', set()):
            users_db[user_id]['stopped_searches'].discard(msg_id)
            show_purchase_confirmation(chat_id, user_id, operator, msg_id)
            return

        prefixes = users_db[user_id].get('prefixes', {}).get(country, [])
        if isinstance(prefixes, str): prefixes = [prefixes] if prefixes != 'None' else []
        display_prefix = ", ".join(prefixes) if prefixes else 'None'
        
        num_type = users_db[user_id].get('number_type', 'fresh')
        num_type_map = {'fresh': 'Only Fresh', 'opened': 'Only Opened', 'all': 'Fresh & Opened'}
        display_num_type = num_type_map.get(num_type, 'Only Fresh')

        if attempt == 1 or attempt % 5 == 0:
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🛑 Stop Search", callback_data=f"stopsearch_{operator}"))
            text = (f"⏳ *Searching for clean number...*\n\n"
                    f"🌍 *Country:* {get_flag(country)} {country.capitalize()}\n"
                    f"📦 *Service:* {service.capitalize()}\n"
                    f"📂 *Type:* {display_num_type}\n"
                    f"📱 *Prefixes:* {display_prefix}\n\n"
                    f"🔄 Attempt #{attempt} – buying & checking...")
            try:
                bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup, parse_mode="Markdown")
                time.sleep(0.7)
            except: pass
        else:
            time.sleep(0.3)

        # FINAL BALANCE CHECK BEFORE 5SIM REQUEST
        with db_lock:
            current_bal = users_db[user_id].get('balance', 0)
            if current_bal < user_price:
                bot.send_message(chat_id, f"❌ *Insufficient Balance!*\n\nYou need `{user_price}` ₽, but you have `{round(current_bal, 2)}` ₽.\n\nSearch stopped.", parse_mode="Markdown")
                return

        try:
            buy_url = f"https://5sim.net/v1/user/buy/activation/{country}/{operator}/{service}"
            response = requests.get(buy_url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                phone, order_id = data.get('phone'), data.get('id')
                clean_phone = phone.replace('+', '').strip()

                if msg_id in users_db[user_id].get('stopped_searches', set()):
                    requests.get(f"https://5sim.net/v1/user/cancel/{order_id}", headers=headers, timeout=10)
                    return

                # Prefix check
                prefix_matched = not prefixes
                for p in prefixes:
                    if clean_phone.startswith(p):
                        prefix_matched = True
                        break

                if not prefix_matched:
                    requests.get(f"https://5sim.net/v1/user/cancel/{order_id}", headers=headers, timeout=10)
                    continue

                # SUCCESSFUL 5SIM PURCHASE -> DEDUCT BALANCE
                with db_lock:
                    users_db[user_id]['balance'] = round(users_db[user_id]['balance'] - user_price, 2)
                save_db(db_data)

                # Checker Check
                db_data["stats"]["checked"] += 1
                status = checker.check_number(user_id, phone)
                
                num_type = users_db[user_id].get('number_type', 'fresh')
                keep_number = False
                if (num_type == 'fresh' and status == 'fresh') or \
                   (num_type == 'opened' and status == 'used') or \
                   (num_type == 'all' and (status == 'fresh' or status == 'used')):
                    keep_number = True

                if keep_number:
                    if status == 'fresh':
                        db_data["stats"]["fresh"] = db_data["stats"].get("fresh", 0) + 1
                        status_text = "Fresh (No Account)"
                    else:
                        db_data["stats"]["used"] = db_data["stats"].get("used", 0) + 1
                        status_text = "Opened 🔐 (Existing Account)"
                    db_data["stats"]["hits"] += 1
                    save_db(db_data)

                    otp_val = "Waiting..."
                    wait_text = (f"✅ *Number Found!*\n\n"
                                 f"📱 *Phone:* `{phone}`\n"
                                 f"📦 *Service:* {service.capitalize()}\n"
                                 f"💰 *Price:* {user_price} ₽\n"
                                 f"🔑 *OTP:* `{otp_val}`\n\n"
                                 f"🟢 *Status:* {status_text}\n⏳ Waiting for SMS...")
                    sms_markup = InlineKeyboardMarkup()
                    sms_markup.add(InlineKeyboardButton("📨 Check SMS Manually", callback_data=f"checksms_{order_id}"))
                    sms_markup.add(InlineKeyboardButton("❌ Cancel and back", callback_data=f"cancelitem_{order_id}_{user_price}"))
                    sms_markup.add(InlineKeyboardButton("🔄 Again", callback_data=f"cancelagain_{order_id}_{operator}_{user_price}"))
                    bot.edit_message_text(wait_text, chat_id, msg_id, reply_markup=sms_markup, parse_mode="Markdown")

                    start_poll = time.time()
                    received_sms_texts = set()
                    while time.time() - start_poll < 600:
                        try:
                            if order_id in users_db[user_id].get('cancelled_orders', set()):
                                return
                            p_resp = requests.get(f"https://5sim.net/v1/user/check/{order_id}", headers=headers, timeout=10)
                            if p_resp.status_code == 200:
                                p_data = p_resp.json()
                                sms_list = p_data.get('sms', [])
                                if sms_list:
                                    new_sms_found = False
                                    for sms in sms_list:
                                        if sms.get('text', '') not in received_sms_texts:
                                            received_sms_texts.add(sms.get('text', ''))
                                            new_sms_found = True
                                            otp_val = sms.get('code', 'N/A')
                                            users_db[user_id].setdefault('orders_with_otp', set()).add(str(order_id))
                                            save_db(db_data)
                                            # Log success
                                            try:
                                                chat_info = bot.get_chat(user_id)
                                                user_log = f"@{chat_info.username}" if chat_info.username else str(user_id)
                                                log_msg = (f"🚀 <b>SUCCESSFUL ACTIVATION</b>\n"
                                                           f"📦 <b>SERVICE: {service.upper()}</b>\n"
                                                           f"🌍 <b>COUNTRY: {country.upper()}</b>\n"
                                                           f"📱 <b>NUMBER:</b> <code>{phone}</code>\n"
                                                           f"🔑 <b>OTP:</b> <code>{otp_val}</code>\n"
                                                           f"� <b>PRICE:</b> {user_price} ₽\n"
                                                           f"👤 <b>USER:</b> {user_log}")
                                                requests.post(f"https://api.telegram.org/bot{LOG_BOT_TOKEN}/sendMessage", json={"chat_id": LOG_GROUP_ID, "text": log_msg, "parse_mode": "HTML"}, timeout=10)
                                            except: pass
                                            bot.send_message(chat_id, f"🎯 *SMS Received!*\n\n📱 *Phone:* `{phone}`\n🔑 *OTP:* `{otp_val}`\n📨 *Full:* `{sms.get('text')}`", parse_mode="Markdown")
                                    if new_sms_found:
                                        if users_db[user_id].get('active_threads', {}).get(msg_id) == thread_token:
                                            done_text = (f"✅ *Number Found!*\n\n📱 *Phone:* `{phone}`\n📦 *Service:* {service.capitalize()}\n💰 *Price:* {user_price} ₽\n"
                                                         f"🔑 *Latest OTP:* `{otp_val}`\n🟢 *Status:* {status_text}\n✅ SMS Received!")
                                            done_markup = InlineKeyboardMarkup()
                                            done_markup.add(InlineKeyboardButton("🔄 Again", callback_data=f"cancelagain_{order_id}_{operator}_{user_price}"))
                                            try: bot.edit_message_text(done_text, chat_id, msg_id, reply_markup=done_markup, parse_mode="Markdown")
                                            except: pass
                                if p_data.get('status') in ['CANCELED', 'FINISHED']: return
                                if users_db[user_id].get('active_threads', {}).get(msg_id) == thread_token:
                                    try:
                                        elapsed = int(time.time() - start_poll)
                                        bot.edit_message_text(f"{wait_text}\n\n🕵️ *Probing for SMS...* ({elapsed}s)", chat_id, msg_id, reply_markup=sms_markup, parse_mode="Markdown")
                                    except: pass
                            time.sleep(4)
                        except: time.sleep(4)
                    return
                else:
                    # Banned/Used -> Refund and try next
                    with db_lock:
                        users_db[user_id]['balance'] = round(users_db[user_id]['balance'] + user_price, 2)
                    save_db(db_data)
                    db_data["stats"]["misses"] += 1
                    requests.get(f"https://5sim.net/v1/user/cancel/{order_id}", headers=headers, timeout=10)
                    continue
            else: time.sleep(1.5)
        except: time.sleep(1)

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔙 Back to Services", callback_data=f"srv_{service}"))
    try: bot.edit_message_text("❌ Failed to find a clean matching number after 50 attempts.", chat_id, msg_id, reply_markup=markup)
    except: pass

@bot.callback_query_handler(func=lambda call: call.data.startswith('cancelitem_'))
def cancel_order_call(call):
    data = call.data.split('_')
    order_id = data[1]
    refund_amount = float(data[2]) if len(data) > 2 else 0
    user_id = call.from_user.id
    api_key = users_db[user_id].get('api_key')
    users_db[user_id].setdefault('cancelled_orders', set()).add(order_id)
    try:
        requests.get(f"https://5sim.net/v1/user/cancel/{order_id}", headers={'Authorization': 'Bearer ' + api_key}, timeout=10)
    except: pass
    
    # Refund Balance
    if refund_amount > 0:
        users_db[user_id]['balance'] += refund_amount
        save_db(db_data)
        
    bot.answer_callback_query(call.id, f"❌ Cancelled. Refunded {refund_amount}₽")
    show_services_page(call.message.chat.id, user_id, page=0, edit_msg_id=call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('cancelagain_'))
def cancel_order_again(call):
    parts = call.data.split('_')
    order_id = parts[1]
    operator = parts[2]
    refund_amount = float(parts[3]) if len(parts) > 3 else 0
    user_id = call.from_user.id
    api_key = users_db[user_id].get('api_key')
    
    has_otp = str(order_id) in users_db[user_id].get('orders_with_otp', set())
    
    if not has_otp:
        users_db[user_id].setdefault('cancelled_orders', set()).add(order_id)
        try:
            requests.get(f"https://5sim.net/v1/user/cancel/{order_id}", headers={'Authorization': 'Bearer ' + api_key}, timeout=10)
        except: pass
        # Refund only if no OTP
        if refund_amount > 0:
            with db_lock:
                users_db[user_id]['balance'] = round(users_db[user_id]['balance'] + refund_amount, 2)
            save_db(db_data)
        bot.answer_callback_query(call.id, "❌ No OTP. Refunded. Searching again...")
    else:
        # If it had OTP, user is clicking "Again" to buy a NEW number. 
        # MUST Check balance here before starting a new search.
        user_price = get_user_price(user_id, users_db[user_id]['temp_data'].get('country'), users_db[user_id]['temp_data'].get('service'), operator)
        if users_db[user_id].get('balance', 0) < user_price:
            bot.answer_callback_query(call.id, f"❌ Insufficient Balance! Need {user_price}₽", show_alert=True)
            return
        bot.answer_callback_query(call.id, "✅ OTP received. Starting new search...")

    msg_id = call.message.message_id
    users_db[user_id].setdefault('stopped_searches', set()).discard(msg_id)

    ctx = users_db[user_id].get('purchase_context', {}).get(str(msg_id), {})
    service = ctx.get('service') or users_db[user_id]['temp_data'].get('service')
    country = ctx.get('country') or users_db[user_id]['temp_data'].get('country')
    threading.Thread(
        target=background_buy_loop,
        args=(call.message.chat.id, user_id, operator, msg_id, service, country),
        daemon=True
    ).start()

# FIX: Added answer_callback_query to checksms handler
@bot.callback_query_handler(func=lambda call: call.data.startswith('checksms_'))
def check_order_sms(call):
    order_id = call.data.split('_')[1]
    user_id = call.from_user.id
    api_key = users_db[user_id].get('api_key')
    try:
        resp = requests.get(f"https://5sim.net/v1/user/check/{order_id}", headers={'Authorization': 'Bearer ' + api_key}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            sms = data.get('sms', [])
            if sms:
                all_sms_text = ""
                for s in sms:
                    otp_code = s.get('code', 'N/A')
                    full_sms = s.get('text', '')
                    all_sms_text += (f"🔑 *OTP Code:* `{otp_code}`\n"
                                   f"� *Full SMS:* `{full_sms}`\n"
                                   f"━━━━━━━━━━━━━━\n")
                
                text = (f"✅ *SMS Received ({len(sms)})!*\n\n{all_sms_text}")
                bot.answer_callback_query(call.id, f"✅ {len(sms)} SMS Received!")
                bot.send_message(call.message.chat.id, text, parse_mode="Markdown")
                # Removed finish call and markup removal to allow multi-OTP
            else:
                bot.answer_callback_query(call.id, "⏳ Still waiting for SMS...", show_alert=False)
    except Exception as e:
        print(f"[checksms] Error: {e}")
        bot.answer_callback_query(call.id, "⚠️ Error checking SMS. Try again.", show_alert=False)

@bot.callback_query_handler(func=lambda call: call.data == 'stop_search')
def stop_search_cb(call):
    users_db[call.from_user.id]['stop_search'] = True
    bot.answer_callback_query(call.id, "Stopping search...")

# --- STATE HANDLERS ---
@bot.message_handler(func=lambda message: message.from_user.id in ADMIN_IDS and users_db.get(message.from_user.id, {}).get('state') == 'ADM_WAITING_GLPSRV_SEARCH')
def handle_adm_glpsrv_search(message):
    query = message.text.strip()
    uid = message.from_user.id
    ctry = users_db[uid]['temp_data'].get('target_ctry')
    msg_id = users_db[uid]['temp_data'].get('mng_msg_id')
    prompt_id = users_db[uid]['temp_data'].get('prompt_msg_id')
    users_db[uid]['state'] = 'MAIN_MENU'
    try:
        bot.delete_message(message.chat.id, message.message_id)
        if prompt_id: bot.delete_message(message.chat.id, prompt_id)
    except: pass
    
    class MockMessage:
        def __init__(self, chat_id, message_id):
            self.chat = type('obj', (object,), {'id': chat_id})
            self.message_id = message_id
            
    mock_call = type('obj', (object,), {
        'from_user': message.from_user, 
        'data': f"adm_glpsel_{ctry}_{query}", 
        'id': 'search', 
        'message': MockMessage(message.chat.id, msg_id)
    })
    handle_admin_callbacks(mock_call)

@bot.message_handler(func=lambda message: message.from_user.id in ADMIN_IDS and users_db.get(message.from_user.id, {}).get('state') == 'ADM_WAITING_GLP_SEARCH')
def handle_adm_glp_search(message):
    query = message.text.strip()
    uid = message.from_user.id
    msg_id = users_db[uid]['temp_data'].get('mng_msg_id')
    prompt_id = users_db[uid]['temp_data'].get('prompt_msg_id')
    users_db[uid]['state'] = 'MAIN_MENU'
    try:
        bot.delete_message(message.chat.id, message.message_id)
        if prompt_id: bot.delete_message(message.chat.id, prompt_id)
    except: pass
    
    class MockMessage:
        def __init__(self, chat_id, message_id):
            self.chat = type('obj', (object,), {'id': chat_id})
            self.message_id = message_id
            
    mock_call = type('obj', (object,), {
        'from_user': message.from_user, 
        'data': f"adm_globalprice_0_{query}", 
        'id': 'search', 
        'message': MockMessage(message.chat.id, msg_id)
    })
    handle_admin_callbacks(mock_call)

@bot.message_handler(func=lambda message: message.from_user.id in ADMIN_IDS and users_db.get(message.from_user.id, {}).get('state') == 'ADM_WAITING_CTRY_SEARCH')
def handle_adm_ctry_search(message):
    query = message.text.strip()
    uid = message.from_user.id
    target_id = users_db[uid]['temp_data'].get('target_user')
    msg_id = users_db[uid]['temp_data'].get('mng_msg_id')
    prompt_id = users_db[uid]['temp_data'].get('prompt_msg_id')
    users_db[uid]['state'] = 'MAIN_MENU'
    
    # Cleanup search messages
    try:
        bot.delete_message(message.chat.id, message.message_id) # User query
        if prompt_id: bot.delete_message(message.chat.id, prompt_id) # Bot prompt
    except: pass
    
    # Use a dummy call object with the stored message ID
    class MockMessage:
        def __init__(self, chat_id, message_id):
            self.chat = type('obj', (object,), {'id': chat_id})
            self.message_id = message_id
            
    mock_call = type('obj', (object,), {
        'from_user': message.from_user, 
        'data': f"adm_mngctry_{target_id}_{query}", 
        'id': 'search', 
        'message': MockMessage(message.chat.id, msg_id)
    })
    handle_admin_callbacks(mock_call)

@bot.message_handler(func=lambda message: message.from_user.id in ADMIN_IDS and users_db.get(message.from_user.id, {}).get('state') == 'ADM_WAITING_SRV_SEARCH')
def handle_adm_srv_search(message):
    query = message.text.strip()
    uid = message.from_user.id
    target_id = users_db[uid]['temp_data'].get('target_user')
    msg_id = users_db[uid]['temp_data'].get('mng_msg_id')
    prompt_id = users_db[uid]['temp_data'].get('prompt_msg_id')
    users_db[uid]['state'] = 'MAIN_MENU'

    # Cleanup search messages
    try:
        bot.delete_message(message.chat.id, message.message_id)
        if prompt_id: bot.delete_message(message.chat.id, prompt_id)
    except: pass
    
    class MockMessage:
        def __init__(self, chat_id, message_id):
            self.chat = type('obj', (object,), {'id': chat_id})
            self.message_id = message_id

    mock_call = type('obj', (object,), {
        'from_user': message.from_user, 
        'data': f"adm_mngsrv_{target_id}_{query}", 
        'id': 'search', 
        'message': MockMessage(message.chat.id, msg_id)
    })
    handle_admin_callbacks(mock_call)

@bot.message_handler(func=lambda message: message.from_user.id in ADMIN_IDS and users_db.get(message.from_user.id, {}).get('state') == 'ADM_WAITING_BALANCE')
def handle_adm_balance(message):
    try:
        uid = message.from_user.id
        new_bal = float(message.text.strip())
        target_id = users_db[uid]['temp_data'].get('target_user')
        if target_id and target_id in users_db:
            users_db[target_id]['balance'] = new_bal
            save_db(db_data)
            bot.reply_to(message, f"✅ Balance for `{target_id}` updated to `{new_bal}` ₽", parse_mode="Markdown")
        users_db[uid]['state'] = 'MAIN_MENU'
        admin_panel(message)
    except ValueError:
        bot.reply_to(message, "❌ Invalid amount. Please send a number.")

@bot.message_handler(func=lambda message: message.from_user.id in ADMIN_IDS and users_db.get(message.from_user.id, {}).get('state') == 'ADM_WAITING_GLOBAL_PRICE')
def handle_adm_global_price(message):
    try:
        uid = message.from_user.id
        new_price = float(message.text.strip())
        td = users_db[uid]['temp_data']
        ctry, srv = td.get('target_ctry'), td.get('target_srv')
        if ctry and srv:
            if 'global_prices' not in db_data: db_data['global_prices'] = {}
            if ctry not in db_data['global_prices']: db_data['global_prices'][ctry] = {}
            db_data['global_prices'][ctry][srv] = new_price
            save_db(db_data)
            bot.reply_to(message, f"✅ Global Price for {srv} in {ctry} set to `{new_price}` ₽", parse_mode="Markdown")
        users_db[uid]['state'] = 'MAIN_MENU'
        admin_panel(message)
    except ValueError:
        bot.reply_to(message, "❌ Invalid price. Please send a number.")

@bot.message_handler(func=lambda message: message.from_user.id in ADMIN_IDS and users_db.get(message.from_user.id, {}).get('state') == 'ADM_WAITING_PRICE')
def handle_adm_price(message):
    try:
        uid = message.from_user.id
        new_price = float(message.text.strip())
        td = users_db[uid]['temp_data']
        target_id, ctry, srv = td.get('target_user'), td.get('target_ctry'), td.get('target_srv')
        if target_id and ctry and srv:
            if 'price_overrides' not in users_db[target_id]: users_db[target_id]['price_overrides'] = {}
            if ctry not in users_db[target_id]['price_overrides']: users_db[target_id]['price_overrides'][ctry] = {}
            users_db[target_id]['price_overrides'][ctry][srv] = new_price
            save_db(db_data)
            bot.reply_to(message, f"✅ Price for {srv} in {ctry} for user `{target_id}` set to `{new_price}` ₽", parse_mode="Markdown")
        users_db[uid]['state'] = 'MAIN_MENU'
        admin_panel(message)
    except ValueError:
        bot.reply_to(message, "❌ Invalid price. Please send a number.")
@bot.message_handler(func=lambda message: users_db.get(message.from_user.id, {}).get('state') == 'WAITING_FOR_CHECKER_PHONE')
def handle_checker_phone(message):
    phone = message.text.strip()
    user_id = message.from_user.id
    init_user(user_id)
    msg = bot.reply_to(message, "⏳ Connecting to Telegram and sending code... Please wait.")
    try:
        sent_code = checker.send_code(user_id, phone)
        users_db[user_id]['temp_data']['phone'] = phone
        users_db[user_id]['temp_data']['phone_code_hash'] = sent_code.phone_code_hash
        users_db[user_id]['state'] = 'WAITING_FOR_CHECKER_OTP'
        bot.edit_message_text(f"📩 OTP sent to `{phone}`.\n\nPlease enter the OTP code:", message.chat.id, msg.message_id, parse_mode="Markdown")
    except Exception as e:
        bot.edit_message_text(f"❌ Error sending code: {e}", message.chat.id, msg.message_id)
        users_db[user_id]['state'] = 'MAIN_MENU'

@bot.message_handler(func=lambda message: users_db.get(message.from_user.id, {}).get('state') == 'WAITING_FOR_CHECKER_OTP')
def handle_checker_otp(message):
    otp = message.text.strip()
    user_id = message.from_user.id
    init_user(user_id)
    td = users_db[user_id].get('temp_data', {})
    msg = bot.reply_to(message, "⏳ Authenticating... Please wait.")
    try:
        checker.sign_in(user_id, td['phone'], otp, td['phone_code_hash'])
        bot.edit_message_text("✅ Checker Session successfully added!", message.chat.id, msg.message_id)
        users_db[user_id]['state'] = 'MAIN_MENU'
    except Exception as e:
        bot.edit_message_text(f"❌ Sign-in failed: {e}", message.chat.id, msg.message_id)
        users_db[user_id]['state'] = 'MAIN_MENU'

@bot.message_handler(func=lambda message: users_db.get(message.from_user.id, {}).get('state') == 'WAITING_FOR_API_KEY')
@access_required
def handle_api_key(message):
    user_id = message.from_user.id
    api_key = message.text.strip()
    is_valid, profile = verify_5sim_apikey(api_key)
    if is_valid:
        init_user(user_id)
        users_db[user_id]['api_key'] = api_key
        users_db[user_id]['logged_in'] = True
        users_db[user_id]['state'] = 'MAIN_MENU'
        save_db(db_data)
        bot.reply_to(message, "Login successful! 🎉")
        show_main_menu(message)
    else:
        bot.reply_to(message, "❌ Invalid API Key! Please try again.")

# FIX: Removed duplicate handle_search — was firing twice per message
@bot.message_handler(func=lambda message: users_db.get(message.from_user.id, {}).get('state') == 'WAITING_FOR_SEARCH')
@access_required
def handle_search(message):
    user_id = message.from_user.id
    users_db[user_id]['state'] = 'MAIN_MENU'
    query = message.text.strip()
    msg_id = users_db[user_id]['temp_data'].get('last_msg_id')
    try: bot.delete_message(message.chat.id, message.message_id)
    except: pass
    if msg_id: show_services_page(message.chat.id, user_id, page=0, search_query=query, edit_msg_id=msg_id)
    else: show_services_page(message.chat.id, user_id, page=0, search_query=query)

@bot.message_handler(func=lambda message: users_db.get(message.from_user.id, {}).get('state') == 'WAITING_FOR_CSEARCH')
@access_required
def handle_country_search(message):
    user_id = message.from_user.id
    users_db[user_id]['state'] = 'MAIN_MENU'
    csearch = message.text.strip()
    msg_id = users_db[user_id]['temp_data'].get('last_msg_id')
    try: bot.delete_message(message.chat.id, message.message_id)
    except: pass
    if msg_id: show_countries_page(message.chat.id, user_id, msg_id, page=0, search_query=csearch)

@bot.message_handler(func=lambda message: users_db.get(message.from_user.id, {}).get('state') == 'WAITING_FOR_PREFIX')
@access_required
def handle_prefix(message):
    user_id = message.from_user.id
    prefix = message.text.replace('+', '').strip()
    users_db[user_id]['state'] = 'MAIN_MENU'
    country = users_db[user_id]['temp_data'].get('country')
    if country:
        if 'prefixes' not in users_db[user_id]: users_db[user_id]['prefixes'] = {}
        current = users_db[user_id]['prefixes'].get(country, [])
        if isinstance(current, str): current = [current] if current != 'None' else []
        if prefix not in current:
            current.append(prefix)
            users_db[user_id]['prefixes'][country] = current
            save_db(db_data)
    op = users_db[user_id]['temp_data'].get('prefix_op')
    msg_id = users_db[user_id]['temp_data'].get('last_msg_id')
    prompt_id = users_db[user_id]['temp_data'].get('prompt_msg_id')

    try: bot.delete_message(message.chat.id, message.message_id)
    except: pass
    if prompt_id:
        try: bot.delete_message(message.chat.id, prompt_id)
        except: pass

    if op and msg_id: show_purchase_confirmation(message.chat.id, user_id, op, msg_id)

# --- STARTUP ---
def preload_data():
    print("Pre-loading API data...")
    get_services()
    get_all_countries()
    print("Data pre-loaded.")

if __name__ == '__main__':
    threading.Thread(target=preload_data, daemon=True).start()
    print("Bot is successfully running!")
    bot.delete_webhook(drop_pending_updates=True)
    bot.infinity_polling()
