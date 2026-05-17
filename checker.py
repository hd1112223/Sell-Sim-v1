import asyncio
import re
import os
import json
import telethon.sync
import requests
from telethon import TelegramClient, events, functions, types, errors, Button

# --- ক্রেডেনশিয়ালস ---
API_ID = 27699293
API_HASH = '2f0aa06fe4f782c5ebd5454c19774c79'
BOT_TOKEN = '8197540716:AAGv-TIzFRMR1nMxkMEynLlubqjrKPMTYNE'
ADMIN_ID = 6908091275

# সেশন এবং ডাটাবেস ফাইল
DB_FILE = 'bot_db.json'

# Initialize Clients
bot = TelegramClient('bot_ui_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)
user_client = TelegramClient('user_checker_session', API_ID, API_HASH)

# ডাটাবেস লোড করা
if not os.path.exists(DB_FILE):
    with open(DB_FILE, 'w') as f:
        json.dump({"users": [], "blocked": []}, f)

def get_db():
    with open(DB_FILE, 'r') as f: return json.load(f)

def save_db(data):
    with open(DB_FILE, 'w') as f: json.dump(data, f)

def normalize_number(phone):
    clean_num = re.sub(r'[^\d]', '', str(phone))
    return '+' + clean_num if not clean_num.startswith('+') else clean_num

def get_flag(phone):
    if phone.startswith('+54'): return "🇦🇷"
    if phone.startswith('+91'): return "🇮🇳"
    if phone.startswith('+1'): return "🇺🇸"
    if phone.startswith('+880'): return "🇧🇩"
    if phone.startswith('+44'): return "🇬🇧"
    if phone.startswith('+7'): return "🇷🇺"
    if phone.startswith('+62'): return "🇮🇩"
    if phone.startswith('+234'): return "🇳🇬"
    if phone.startswith('+92'): return "🇵🇰"
    if phone.startswith('+55'): return "🇧🇷"
    if phone.startswith('+254'): return "🇰🇪"
    return "🌐"

# --- HTTP API Helper for Native Copy Buttons ---
def send_native_copy_results(chat_id, results):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    
    # Create the buttons in the format Telegram Bot API expects for native copy
    keyboard = []
    for res in results:
        btn = {
            "text": res["btn_text"],
            "copy_text": {"text": res["phone"]} # Native click-to-copy phone number
        }
        keyboard.append([btn])
    
    payload = {
        "chat_id": chat_id,
        "text": "📊 Results (Tap to copy):",
        "reply_markup": {"inline_keyboard": keyboard}
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

# --- Start & Admin Commands ---

@bot.on(events.NewMessage(pattern='/start'))
async def start(event):
    db = get_db()
    if event.sender_id not in db['users']:
        db['users'].append(event.sender_id)
        save_db(db)
    
    if event.sender_id in db['blocked']:
        return await event.reply("❌ দুঃখিত, আপনাকে ব্লক করা হয়েছে।")
        
    await event.reply(
        "👋 Welcome to Number Checker Bot!\n\n"
        "নাম্বার লিস্ট পাঠান চেক করার জন্য।\n"
        "অ্যাকাউন্ট থাকলে: 🔵 | না থাকলে: 🚫\n"
        "অ্যাডমিন হলে টাইপ করুন: `/admin`"
    )

@bot.on(events.NewMessage(pattern='/admin'))
async def admin_panel(event):
    if event.sender_id != ADMIN_ID:
        return await event.reply("❌ আপনি অ্যাডমিন নন।")
    
    buttons = [
        [Button.inline("➕ Add Session (/login)", b"login_session")],
        [Button.inline("📢 Send Notice", b"send_notice"), Button.inline("🚫 Block User", b"block_user")],
        [Button.inline("🔓 Unblock User", b"unblock_user"), Button.inline("📊 Stats", b"stats")]
    ]
    await event.reply("🛠 Admin Control Panel", buttons=buttons)

@bot.on(events.CallbackQuery)
async def callback(event):
    db = get_db()
    if event.data == b"stats":
        msg = f"👥 মোট ইউজার: {len(db['users'])}\n🚫 ব্লকড: {len(db['blocked'])}"
        await event.answer(msg, alert=True)
    elif event.data == b"login_session":
        await event.edit("🔄 সেশন অ্যাড করতে সরাসরি `/login` লিখুন।")
    elif event.data == b"send_notice":
        async with bot.conversation(event.chat_id) as conv:
            await conv.send_message("📝 নোটিশটি লিখুন:")
            notice = await conv.get_response()
            for user in db['users']:
                try: await bot.send_message(user, f"📢 NOTICE:\n\n{notice.text}")
                except: pass
            await conv.send_message("✅ নোটিশ পাঠানো হয়েছে!")
    elif event.data == b"block_user":
        async with bot.conversation(event.chat_id) as conv:
            await conv.send_message("🆔 ইউজারের ID দিন:")
            uid = await conv.get_response()
            target = int(uid.text)
            if target not in db['blocked']:
                db['blocked'].append(target)
                save_db(db)
            await conv.send_message(f"✅ {target} ব্লক করা হয়েছে।")

@bot.on(events.NewMessage(pattern='/login'))
async def login(event):
    if event.sender_id != ADMIN_ID: return
    async with bot.conversation(event.chat_id) as conv:
        await conv.send_message("📞 Number (with code):")
        num = (await conv.get_response()).text
        if not user_client.is_connected(): await user_client.connect()
        await user_client.send_code_request(num)
        await conv.send_message("📩 OTP:")
        otp = (await conv.get_response()).text
        await user_client.sign_in(num, otp)
        await conv.send_message("✅ Session Added!")

async def check_number(phone):
    try:
        flag = get_flag(phone)
        c = types.InputPhoneContact(client_id=0, phone=phone, first_name="Chk", last_name="")
        res = await user_client(functions.contacts.ImportContactsRequest([c]))
        exists = True if res.users else False
        status_icon = "🔵" if exists else "🚫"
        btn_text = f"{flag} {phone} {status_icon}              ❐"
        return {"phone": phone, "exists": exists, "btn_text": btn_text}
    except errors.FloodWaitError as e: 
        return {"phone": phone, "error": True, "btn_text": f"⚠️ {phone} (Wait {e.seconds}s) ❐"}
    except: 
        return {"phone": phone, "exists": False, "btn_text": f"📱 {phone} 🚫              ❐"}

@bot.on(events.NewMessage)
async def handle_check(event):
    db = get_db()
    if event.text.startswith('/') or event.sender_id in db['blocked']: return
    print(f"Received message from {event.sender_id}: {event.text[:20]}...")
    
    try:
        if not user_client.is_connected(): await user_client.connect()
        if not await user_client.is_user_authorized():
            return await event.reply("⚠️ Admin has not logged in yet. Use `/login` to authorize.")
    except Exception as e:
        return await event.reply(f"⚠️ Connection Error: {str(e)}")

    nums = [normalize_number(n) for n in event.text.split('\n') if len(n.strip()) > 5]
    if not nums: return
    
    status = await event.reply("⚡ Checking...")
    results = []
    for i in range(0, len(nums), 10):
        batch = nums[i:i+10]
        results.extend(await asyncio.gather(*(check_number(n) for n in batch)))
        await asyncio.sleep(1.5)
    
    await status.delete()
    if len(results) > 20:
        out = "\n".join([f"{r.get('btn_text', r['phone'])}" for r in results])
        with open("res.txt", "w", encoding="utf-8") as f: f.write(out)
        await event.reply("📊 Results Summary (File):", file="res.txt")
        os.remove("res.txt")
    else:
        # Use HTTP API to send the specialized native copy buttons
        send_native_copy_results(event.chat_id, results)

print("Bot is running with Admin Panel!")
bot.run_until_disconnected()
