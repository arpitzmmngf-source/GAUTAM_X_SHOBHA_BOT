import telebot
import io
from telebot.types import *
import requests
import sqlite3
import json
import time
import re
import threading
from datetime import datetime, timedelta

# ================= CONFIG =================
BOT_TOKEN = "8717813336:AAHhZP_AFg4icVwdA1n2wCQd0YpVlpSLVvs"
CHANNELS = [
    "@aghunter",
    "@nibhadarling",
    "@infobotfreet",
    "@Gautamxbackupgc",
    "-1002889292825",      # Channel 5 (private)
    "@Gautamxinfo"         # Channel 6 (public)
]
CHANNEL_INFO = {
    "@aghunter":          ("🔗 CHANNEL 1", "https://t.me/aghunter"),
    "@nibhadarling":      ("🔗 CHANNEL 2", "https://t.me/nibhadarling"),
    "@infobotfreet":      ("🔗 CHANNEL 3", "https://t.me/infobotfreet"),
    "@Gautamxbackupgc":   ("🔗 CHANNEL 4", "https://t.me/Gautamxbackupgc"),
    "-1002889292825":     ("🔗 CHANNEL 5", "https://t.me/+itOLNfNsGh02MWM1"),
    "@Gautamxinfo":       ("🔗 CHANNEL 6", "https://t.me/Gautamxinfo"),
}
ADMIN_IDS = [8643031554, 7726532679]  # add more admin user_ids here as needed
UPI_ID = "9939738510@fam"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
conn = sqlite3.connect("db.sqlite", check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    credits INTEGER DEFAULT 20,
    verified INTEGER DEFAULT 0,
    joined_at INTEGER DEFAULT 0
)''')
conn.commit()

try:
    cursor.execute("ALTER TABLE users ADD COLUMN joined_at INTEGER DEFAULT 0")
    conn.commit()
except:
    pass

try:
    cursor.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT 0")
    conn.commit()
except:
    pass

try:
    cursor.execute("ALTER TABLE users ADD COLUMN free_uses INTEGER DEFAULT 0")
    conn.commit()
except:
    pass

FREE_USES_LIMIT = 3
GROUP_LINK = "https://t.me/aghunter"

def get_free_uses(uid):
    row = cursor.execute("SELECT free_uses FROM users WHERE user_id=?", (uid,)).fetchone()
    return row[0] if row else 0

def increment_free_uses(uid):
    cursor.execute("UPDATE users SET free_uses=free_uses+1 WHERE user_id=?", (uid,))
    conn.commit()

def send_limit_promo(m):
    text = (
        "PRIVATE IS LIMITED FOR FREE\n\n"
        "💰 BUY AND USE UNLIMITED\n\n"
        f"👥 JOIN GROUP FOR UNLIMITED FREE USE\n"
        f"🔗 JOIN GROUP - @AGHUNTER"
    )
    bot.send_message(m.chat.id, f"<blockquote>{text}</blockquote>", parse_mode="HTML")

user_verified_cache = {}
copy_cache = {}  # stores plain text for Copy All button

def is_admin(uid):
    return uid in ADMIN_IDS

def format_date(timestamp):
    if not timestamp:
        return "Unknown"
    dt = datetime.fromtimestamp(timestamp)
    return dt.strftime("%d %b %Y")

def get_ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

def _fmt_num(v):
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v)

def get_user(uid, username=""):
    try:
        cursor.execute("SELECT credits, verified, joined_at FROM users WHERE user_id=?", (uid,))
        row = cursor.fetchone()
        if not row:
            join_time = int(time.time())
            v = 1 if is_admin(uid) else 0
            cursor.execute("INSERT INTO users (user_id, username, credits, verified, joined_at) VALUES (?,?,?,?,?)",
                           (uid, username, 20, v, join_time))
            conn.commit()
            user_verified_cache[uid] = bool(v)
            return 20, bool(v), join_time
        cursor.execute("UPDATE users SET username=? WHERE user_id=?", (username, uid))
        conn.commit()
        verified = bool(row[1])
        user_verified_cache[uid] = verified
        return row[0], verified, row[2]
    except:
        return 20, False, 0

def set_user(uid, credits):
    cursor.execute("UPDATE users SET credits=? WHERE user_id=?", (credits, uid))
    conn.commit()

def set_verified(uid, status):
    cursor.execute("UPDATE users SET verified=? WHERE user_id=?", (1 if status else 0, uid))
    conn.commit()
    user_verified_cache[uid] = status

def check_membership(uid):
    """
    Returns the list of channels the user has NOT joined (empty list = fully
    joined). If a channel check fails due to an API/permission error (bot
    not admin in that channel, network hiccup, etc.), we do NOT treat that
    as "user left" — we skip that channel's check so one misconfigured
    channel can't force everyone to re-verify forever.
    """
    missing = []
    for ch in CHANNELS:
        try:
            member = bot.get_chat_member(ch, uid)
            status = str(member.status).lower()
            if status not in ("member", "administrator", "creator", "restricted"):
                print(f"[VERIFY-FAIL] uid={uid} channel={ch} status={status}")
                missing.append(ch)
        except telebot.apihelper.ApiTelegramException as e:
            err_desc = str(e).lower()
            error_code = getattr(e, "error_code", None)
            print(f"[VERIFY-ERROR] uid={uid} channel={ch} code={error_code} msg={e}")
            if "user not found" in err_desc:
                missing.append(ch)
                continue
            if error_code == 429 or "too many requests" in err_desc:
                time.sleep(1)
                continue
            continue
        except Exception as e:
            print(f"[VERIFY-EXCEPTION] uid={uid} channel={ch} err={e}")
            continue
    return missing

_last_verify_prompt = {}
VERIFY_PROMPT_COOLDOWN = 30  # seconds

# Cache check_membership() results briefly so one flaky/rate-limited channel
# call can't immediately flip an already-verified user back to "not joined"
# on their very next command. Positive results are trusted longer than
# negative ones, so a real "left the channel" is still caught reasonably fast.
_membership_cache = {}
MEMBERSHIP_CACHE_OK_TTL = 300   # 5 min once confirmed joined
MEMBERSHIP_CACHE_FAIL_TTL = 15  # short, so a real leave is still detected soon

def check_membership_cached(uid):
    now = time.time()
    cached = _membership_cache.get(uid)
    if cached:
        ts, missing = cached
        ttl = MEMBERSHIP_CACHE_OK_TTL if not missing else MEMBERSHIP_CACHE_FAIL_TTL
        if now - ts < ttl:
            return missing
    missing = check_membership(uid)
    _membership_cache[uid] = (now, missing)
    return missing

def _should_prompt(uid):
    now = time.time()
    last = _last_verify_prompt.get(uid, 0)
    if now - last < VERIFY_PROMPT_COOLDOWN:
        return False
    _last_verify_prompt[uid] = now
    return True

def ensure_verified_and_member(uid, chat_id=None):
    if is_admin(uid):
        return True
    missing = check_membership_cached(uid)
    if missing:
        set_verified(uid, False)
        if _should_prompt(uid):
            try:
                u = bot.get_chat(uid)
                name = u.first_name or (f"@{u.username}" if u.username else str(uid))
            except Exception:
                name = str(uid)
            caption = (
                f"👋 Hey {name} 💗\n\n"
                "⚠️ <b>VERIFICATION REQUIRED</b>\n"
                "<b>JOIN ALL CHANLE IMPORTANT</b>\n\n"
                "Pehle inhe join karo, phir VERIFY dabao."
            )
            kb = join_kb(missing)
            target_chat = chat_id if chat_id else uid
            try:
                sent = bot.send_video(target_chat, get_random_video(), caption=caption, reply_markup=kb, parse_mode="HTML")
                threading.Thread(target=auto_delete, args=(target_chat, sent.message_id, 50), daemon=True).start()
            except Exception:
                try:
                    sent = bot.send_message(target_chat, caption.replace("<b>", "**").replace("</b>", "**"), reply_markup=kb, parse_mode="Markdown")
                    threading.Thread(target=auto_delete, args=(target_chat, sent.message_id, 50), daemon=True).start()
                except Exception:
                    pass
        return False
    _, verified = get_user(uid, "")[:2]
    if not verified:
        # At this point check_membership(uid) already passed, so the user
        # HAS joined all channels — just hasn't clicked VERIFY yet.
        # Don't tell them to "join", only ask them to confirm.
        if _should_prompt(uid):
            try:
                u = bot.get_chat(uid)
                name = u.first_name or (f"@{u.username}" if u.username else str(uid))
            except Exception:
                name = str(uid)
            msg_text = f"👋 Hey {name} 💗\n\n✅ <b>Aapne saare channels join kar liye hain!</b>\n\n👉 Bas <b>VERIFY</b> button par click karein."
            kb = join_kb([])
            target_chat = chat_id if chat_id else uid
            try:
                sent = bot.send_video(target_chat, get_random_video(), caption=msg_text, reply_markup=kb, parse_mode="HTML")
                threading.Thread(target=auto_delete, args=(target_chat, sent.message_id, 50), daemon=True).start()
            except Exception:
                try:
                    sent = bot.send_message(target_chat, msg_text.replace("<b>", "**").replace("</b>", "**"), reply_markup=kb, parse_mode="Markdown")
                    threading.Thread(target=auto_delete, args=(target_chat, sent.message_id, 50), daemon=True).start()
                except Exception:
                    pass
        return False
    return True

def join_kb(missing=None):
    """Only show buttons for channels the user hasn't joined yet (plus
    Verify). If `missing` is None, show all (used for first-time prompts
    where we haven't checked yet)."""
    kb = InlineKeyboardMarkup(row_width=1)
    targets = CHANNELS if missing is None else missing
    for ch in targets:
        name, url = CHANNEL_INFO.get(ch, (ch, None))
        if url:
            kb.add(InlineKeyboardButton(name, url=url))
    kb.add(InlineKeyboardButton("✅ VERIFY", callback_data="verify"))
    return kb

@bot.callback_query_handler(func=lambda call: call.data == "verify")
def verify_cb(call):
    uid = call.from_user.id
    if not is_admin(uid):
        missing = check_membership_cached(uid)
        if missing:
            names = ", ".join(CHANNEL_INFO.get(ch, (ch, ""))[0] for ch in missing)
            bot.answer_callback_query(call.id, f"❌ Abhi join nahi hua: {names}", show_alert=True)
            return
    # Force clear cache so next check re-fetches fresh data
    _membership_cache.pop(uid, None)
    _last_verify_prompt.pop(uid, None)
    _membership_cache[uid] = (time.time(), [])
    _, verified = get_user(uid, "")[:2]
    if verified:
        bot.answer_callback_query(call.id, "✅ Already verified!", show_alert=True)
        chat_type = "private" if call.message.chat.type == "private" else "group"
        send_welcome(call.message.chat.id, uid, call.from_user, chat_type)
        return
    set_verified(uid, True)
    ref_row = cursor.execute("SELECT referred_by FROM users WHERE user_id=?", (uid,)).fetchone()
    if ref_row and ref_row[0] and ref_row[0] != 0:
        referrer_id = ref_row[0]
        cursor.execute("UPDATE users SET credits=credits+2 WHERE user_id=?", (referrer_id,))
        conn.commit()
        try:
            bot.send_message(referrer_id, "🎉 <b>Referral Bonus!</b>\n\n✅ Aapke referral se ek naya user join karke verify ho gaya!\n💰 <b>+2 Credits</b> aapke account mein add ho gaye!", parse_mode="HTML")
        except:
            pass
    bot.answer_callback_query(call.id, "✅ Verified Successfully!", show_alert=True)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    chat_type = "private" if call.message.chat.type == "private" else "group"
    send_welcome(call.message.chat.id, uid, call.from_user, chat_type)

def private_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("📱 𝗡𝗨𝗠 𝗧𝗢 𝗜𝗡𝗙𝗢 🔍", "🔰 𝗔𝗗𝗩 𝗡𝗨𝗠 𝗜𝗡𝗙𝗢 🔮")
    kb.add("🆔 𝗔𝗔𝗗𝗛𝗔𝗥 𝗧𝗢 𝗙𝗔𝗠𝗜𝗟𝗬 👨‍👩‍👧‍👦", "📲 𝗧𝗚 𝗧𝗢 𝗡𝗨𝗠 ☎️")
    kb.add("🚗 𝗩𝗘𝗛𝗜𝗖𝗟𝗘 𝗜𝗡𝗙𝗢 🔎", "💳 𝗨𝗣𝗜 𝗜𝗡𝗙𝗢 🔰")
    kb.add("🎮 𝗙𝗙 𝗜𝗗 𝗜𝗡𝗙𝗢 ⚡", "📧 𝗚𝗠𝗔𝗜𝗟 𝗜𝗡𝗙𝗢 💠")
    kb.add("🚀 𝗕𝗢𝗠𝗕𝗘𝗥 💥", "💎 𝗕𝗨𝗬 𝗖𝗥𝗘𝗗𝗜𝗧 🛒")
    kb.add("🎁 𝗥𝗘𝗙𝗘𝗥 & 𝗘𝗔𝗥𝗡 ♻️", "👤 𝗠𝗬 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 📋")
    return kb

import random

WELCOME_VIDEOS = ["welcome1.mp4", "welcome2.mp4", "welcome4.mp4", "welcome8.mp4", "welcome10.mp4", "welcome11.mp4"]

def get_random_video():
    """Load random video from local files"""
    video_file = random.choice(WELCOME_VIDEOS)
    try:
        return open(video_file, 'rb')
    except FileNotFoundError:
        print(f"[WARNING] {video_file} not found!")
        return None

def send_welcome(chat_id, uid, user_obj, chat_type):
    if chat_type == "private":
        bal, _, _ = get_user(uid, "")
        mention = f'<a href="tg://user?id={uid}">{user_obj.first_name}</a>'
        msg = f"""━━━━━━━━ ✤ ━━━━━━━━
✨ Wᴇʟᴄᴏᴍᴇ Tᴏ Oᴜʀ Iɴꜰᴏʀᴍᴀᴛɪᴏɴ Bᴏᴛ 🌕
➖➖➖➖➖➖➖➖➖➖➖➖
🤩 Hᴇʏ {mention}  🌸

🃏 ʏᴏᴜʀ ᴅᴀꜱʜʙᴏᴀʀᴅ !!
➖➖➖➖➖➖
│ 💰 ᴄʀᴇᴅɪᴛꜱ       » {bal}
│ ♻️ᴄᴏsᴛ    » 1 ᴄʀᴇᴅɪᴛ sᴇᴀʀᴄʜ 
│ 🧸 ꜱᴛᴀᴛᴜꜱ     » 🤩 ꜰʀᴇᴇ ᴜꜱᴇʀ
━━━━━━━━ ⸙ ━━━━━━━━
ᴜꜱᴇ ɪɴʟɪɴᴇ ᴋᴇʏʙᴏʀᴅ ʙᴜᴛᴛᴏɴ ꜰᴏʀ ɪɴꜰᴏ.
➖➖➖➖➖➖➖➖➖➖➖➖
🇮🇳 ᴅᴇᴠ » ˹ @Gautamxlive !! ✅
➖➖➖➖➖➖➖➖➖➖➖➖"""
        try:
            bot.send_video(chat_id, get_random_video(), caption=msg, reply_markup=private_kb(), parse_mode="HTML")
        except Exception:
            bot.send_message(chat_id, msg, reply_markup=private_kb(), parse_mode="HTML")
    else:
        msg = """🚀 <b>Access Granted!</b>
━━━━━━━━ ✤ ━━━━━━━━
✅ <b>GROUP MODE ENABLED</b>

♾️ Unlimited Free Usage
─━━━━━━⊱✿⊰━━━━━━─
📌 Type /help to get started.
━━━━━━━━ ❆ ━━━━━━━━
🤖 Enjoy <b>GAUTAM X INFO BOT</b>!"""
        try:
            bot.send_video(chat_id, get_random_video(), caption=msg, parse_mode="HTML")
        except Exception:
            bot.send_message(chat_id, msg, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())


# ================= JSON PROCESSING =================
def replace_developer(obj, new_dev="@Gautamxlive"):
    if isinstance(obj, dict):
        new_obj = {}
        for k, v in obj.items():
            k_clean = k.lower().replace("_", "").replace(" ", "")
            if k_clean in ("developer", "apideveloper", "dev", "apidev", "owner", "apiowner", "creator"):
                new_obj[k] = new_dev
            else:
                new_obj[k] = replace_developer(v, new_dev)
        return new_obj
    elif isinstance(obj, list):
        return [replace_developer(item, new_dev) for item in obj]
    else:
        return obj

def flatten_dict(obj, prefix=""):
    items = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                items.extend(flatten_dict(v, new_key))
            else:
                items.append((new_key, v))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            new_key = f"{prefix}[{i}]" if prefix else f"[{i}]"
            if isinstance(v, (dict, list)):
                items.extend(flatten_dict(v, new_key))
            else:
                items.append((new_key, v))
    return items

TITLE_MAP = {
    "phone": "📱 NUM TO INFO",
    "aadhar": "🪪 AADHAR INFO",
    "tguser": "📲 TG TO NUM",
    "vehicle": "🚗 VEHICLE INFO",
    "upi": "💸 UPI INFO",
    "ff": "🎮 FREE FIRE INFO",
    "gmail": "📧 EMAIL INFO",
    "advnum": "🔰 ADVANCE NUM INFO",
}

FIELD_EMOJI = {
    "name": "👤", "fname": "👤", "fullname": "👤", "username": "👤",
    "father": "👨", "fathername": "👨", "father_name": "👨",
    "mother": "👩", "mothername": "👩",
    "mobile": "📱", "phone": "📱", "number": "📱", "num": "📱", "contact": "📱",
    "address": "🏠", "addr": "🏠", "city": "🏙️", "state": "📍", "district": "📍",
    "pincode": "📮", "pin": "📮", "postcode": "📮",
    "circle": "📶", "operator": "📶", "telecom": "📶", "provider": "📶",
    "email": "📧", "gmail": "📧", "mail": "📧",
    "dob": "🎂", "birthdate": "🎂", "birth": "🎂", "age": "🎂",
    "gender": "⚧️", "sex": "⚧️",
    "aadhar": "🪪", "aadhaar": "🪪",
    "vehicle": "🚗", "car": "🚗", "rc": "🚗", "reg": "🚗",
    "owner": "👤", "registrationno": "🔢", "chassisno": "🔢",
    "upi": "💸", "vpa": "💸", "bank": "🏦", "account": "🏦",
    "ifsc": "🏦", "balance": "💰",
    "id": "🆔", "userid": "🆔", "uid": "🆔",
    "level": "🎮", "rank": "🏆", "guild": "🏰", "region": "🌍",
    "country": "🌍", "nationality": "🌍", "countrycode": "🌍",
    "ip": "🌐", "device": "📲", "os": "💻",
    "latitude": "🗺️", "longitude": "🗺️", "location": "🗺️",
    "status": "⚡", "verified": "✅",
    "score": "⭐", "rating": "⭐",
    "regdate": "📅", "date": "📅", "lastlogin": "📅",
    "password": "🔑", "pass": "🔑",
    "developer": "🤖", "dev": "🤖",
    "alt": "💠", "altnumber": "💠", "altnum": "💠",
}

SHOW_IF_NULL = ["aadhar", "aadhaar", "email", "gmail", "mail", "alt", "altnumber", "altnum", "alternate"]
SEP_LINE = "- - - - - - - - - - - - - - - - - - - -"
ADDR_SEP = "━━━━━━ ❆ ━━━━━━"

def format_json(data, qtype, qvalue):
    # Special formatting for TG result
    if qtype == "tguser":
        return format_tg_result(data, qvalue)
    
    title = TITLE_MAP.get(qtype, "⚡ RESULT")
    pairs = flatten_dict(data)

    body_lines = []
    plain_lines = []
    seen_keys = set()
    address_added = False

    for k, v in pairs:
        k_lower = k.lower()
        if any(s in k_lower for s in ["success", "status_code", "service", "parameters.", "developer"]):
            continue

        key_raw = k.split(".")[-1].replace("_", " ").replace("-", " ")
        key_clean = key_raw.upper()
        key_lookup = key_raw.lower().replace(" ", "")

        dedup_key = f"{key_clean}:{v}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        is_null = v is None or v == "null" or v == "" or v == [] or v == {}

        if is_null:
            if key_lookup in SHOW_IF_NULL:
                val_str = "null"
                val_display = "❌ null"
            else:
                continue
        else:
            val_str = str(v)
            if len(val_str) > 200:
                val_str = val_str[:200] + "..."
            val_display = val_str

        emoji = "🔹"
        for ek, ev in FIELD_EMOJI.items():
            if ek in key_lookup:
                emoji = ev
                break

        if key_lookup == "address" and not address_added:
            body_lines.append(f"━━━━━━ ❆ ━━━━━━")
            plain_lines.append(ADDR_SEP)
            address_added = True

        body_lines.append(f"{emoji} {key_clean} ➜ {val_display}")
        plain_lines.append(f"{emoji} {key_clean} → {val_str}")
        body_lines.append(SEP_LINE)
        plain_lines.append(SEP_LINE)

    if not body_lines:
        body_lines.append("No data found.")
        plain_lines.append("No data found.")

    header = f"══════════════════════\n⚡ {title}  ⚡\n══════════════════════"
    footer = f"══════════════════════\n⚡  STATUS : SUCCESS\n🤖 DEV    : @Gautamxlive\n══════════════════════"

    body_text = "\n".join(body_lines)
    plain_body = "\n".join(plain_lines)
    plain_full = f"{header}\n\n{plain_body}\n\n{footer}"

    full_text = f"{header}\n\n{body_text}\n\n{footer}"

    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    html_full = f"<blockquote><code>{esc(full_text)}</code></blockquote>"

    return html_full, plain_full


def format_tg_result(data, qvalue):
    """Special formatting for TG TO NUM result with blockquote"""
    user = data.get("user", {})
    username = user.get("username", "N/A")
    user_id = user.get("user_id", "N/A")
    mobile = data.get("mobile", "N/A")
    country_code = data.get("country_code", "N/A")
    country = data.get("country", "N/A")
    status = data.get("status", "UNKNOWN")
    
    # Format with blockquote like in screenshot
    result = f"""<blockquote><b>⚡ TG TO NUM RESULT ⚡</b>
━━━━━━━━ ✤ ━━━━━━━━
👤 User → <a href="tg://user?id={user_id}">{username}</a>

🆔 Telegram ID →
<code>{user_id}</code>

📱 Mobile Number →
<code>+{country_code} {mobile}</code>

🌍 Country Code →
<code>+{country_code}</code>

🌐 Country →
🇮🇳 {country}

📊 Status →
✅ {status}
</blockquote>"""

    plain = f"""⚡ TG TO NUM RESULT ⚡
━━━━━━━━ ✤ ━━━━━━━━
👤 User → {username}

🆔 Telegram ID →
{user_id}

📱 Mobile Number →
+{country_code} {mobile}

🌍 Country Code →
+{country_code}

🌐 Country →
🇮🇳 {country}

📊 Status →
✅ {status}"""

    return result, plain


def extract_numbers(obj):
    nums = []
    def rec(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and re.match(r'^\+?\d{10,13}$', v):
                    clean = re.sub(r'\D', '', v)
                    if len(clean) >= 10 and clean not in nums:
                        nums.append(clean)
                else:
                    rec(v)
        elif isinstance(o, list):
            for i in o:
                rec(i)
    rec(obj)
    return nums

def auto_delete(chat_id, msg_id, delay=60):
    time.sleep(delay)
    try:
        bot.delete_message(chat_id, msg_id)
    except:
        pass

def send_error(m, text, delay=30):
    """Send an error/usage reply and auto-delete it after `delay` seconds (default 30s)."""
    try:
        sent = bot.reply_to(m, text)
        threading.Thread(target=auto_delete, args=(m.chat.id, sent.message_id, delay), daemon=True).start()
        return sent
    except Exception:
        return None

def is_successful(data, service_type):
    if not data:
        return False
    def has_error(obj):
        if isinstance(obj, dict):
            if obj.get("error_code") is not None:
                return True
            if obj.get("success") is False:
                return True
            if "message" in obj and "invalid" in str(obj.get("message")).lower():
                return True
            for val in obj.values():
                if has_error(val):
                    return True
        elif isinstance(obj, list):
            for item in obj:
                if has_error(item):
                    return True
        return False
    if has_error(data):
        return False
    if isinstance(data, dict):
        data_copy = {k: v for k, v in data.items() if k != "developer"}
        if data_copy:
            return True
    return False

# ================= COMMAND HANDLERS =================
@bot.message_handler(commands=['start'])
def start(m):
    uid = m.from_user.id
    get_user(uid, m.from_user.username)
    parts = m.text.split() if m.text else []
    if len(parts) > 1 and parts[1].startswith("ref"):
        try:
            referrer_id = int(parts[1][3:])
            if referrer_id != uid:
                existing = cursor.execute("SELECT referred_by FROM users WHERE user_id=?", (uid,)).fetchone()
                if existing and existing[0] == 0:
                    cursor.execute("UPDATE users SET referred_by=? WHERE user_id=?", (referrer_id, uid))
                    conn.commit()
        except:
            pass
    if not ensure_verified_and_member(uid, m.chat.id):
        return
    send_welcome(m.chat.id, uid, m.from_user, m.chat.type)

@bot.message_handler(commands=['help'])
def help_cmd(m):
    if not ensure_verified_and_member(m.from_user.id, m.chat.id):
        return
    if m.chat.type == "private":
        help_text = """
<b>🔰 Available Commands (Private)</b>

/num    → Get info from phone number
/family → Get family details from Aadhar
/tg     → Get number from Telegram username/ID
/vichel → Vehicle information
/upi    → Verify UPI ID
/ff     → Free Fire user info
/gmail  → Get info from Email ID
/advnum → Advance number info (Premium)
/account → View your profile

Use buttons below for easy access.
"""
        bot.send_message(m.chat.id, help_text, reply_markup=private_kb())
    else:
        help_text = """╔══════════════════════════╗
  ⚡ <b>GAUTAM X INFO BOT</b> ⚡
╚══════════════════════════╝

🆓 <b>GROUP — UNLIMITED FREE</b>

━━━━━━━━━━━━━━━━━━━━━━━━━━
📱 <b>PHONE INFO</b>
┗ /num <code>9876543210</code>

🪪 <b>AADHAR INFO</b>
┗ /family <code>400204118594</code>

📲 <b>TELEGRAM TO NUM</b>
┗ /tg <code>@username</code> or <code>123456789</code>

🚗 <b>VEHICLE INFO</b>
┗ /vichel <code>MP16CB6745</code>

💸 <b>UPI INFO</b>
┗ /upi <code>rohit@sbi</code>

🎮 <b>FREE FIRE INFO</b>
┗ /ff <code>46454168</code>

📧 <b>EMAIL INFO</b>
┗ /gmail <code>rohit@gmail.com</code>

🔰 <b>ADVANCE NUM INFO</b>
┗ /advnum <code>8084502203</code>

🚀 <b>BOMBER</b>
┗ /bom <code>9999999999</code>

━━━━━━━━━━━━━━━━━━━━━━━━━━
📌 <b>Example:</b> <code>/num 8084502203</code>
👨‍💻 <b>Dev:</b> @Gautamxlive"""
        bot.send_message(m.chat.id, help_text, parse_mode="HTML")

@bot.message_handler(commands=['account'])
def account_cmd(m):
    uid = m.from_user.id
    if not ensure_verified_and_member(uid, m.chat.id):
        return
    show_profile(m)

def show_profile(m):
    uid = m.from_user.id
    user = m.from_user
    bal, verified, joined = get_user(uid, user.username)
    name = user.first_name
    if user.last_name:
        name += " " + user.last_name
    username = user.username or "No username"
    if not user.username:
        username = "Not set"
    else:
        username = "@" + user.username
    member_since = format_date(joined)
    account_type = "👑 Admin" if is_admin(uid) else "👤 Regular"
    profile_text = f"""
<b>━━━━━━━━━━━━━━━━━━━━</b>
👤 <b>YOUR PROFILE</b>
<b>━━━━━━━━━━━━━━━━━━━━</b>

📛 <b>Name</b> ⫸ {name}
🔗 <b>Username</b>  ⫸ {username}
🆔 <b>User ID</b>    ⫸ <code>{uid}</code>
📅 <b>Member Since</b> ⫸ {member_since}
📦 <b>Account Type</b>  ⫸ {account_type}

🛒 <b>TOTAL BALANCE</b> : {bal} CREDIT

<b>━━━━━━━━━━━━━━━━━━━━</b>
"""
    bot.send_message(m.chat.id, profile_text, parse_mode="HTML")

@bot.message_handler(commands=['num', 'family'])
def cmd_handler(m):
    uid = m.from_user.id
    if not ensure_verified_and_member(uid, m.chat.id):
        return
    chat_type = m.chat.type
    cmd = m.text.split()[0][1:]
    try:
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2:
            send_error(m, f"❌ Usage: /{cmd} [value]")
            return
        val = parts[1].strip()
        if cmd == "num" and not re.match(r'^\d{10}$', val):
            send_error(m, "❌ Please enter a valid 10-digit phone number. No credits deducted.")
            return
        if cmd == "family" and not re.match(r'^\d{12}$', val):
            send_error(m, "❌ Please enter a valid 12-digit Aadhar number. No credits deducted.")
            return
        if chat_type == "private":
            ok, bal = check_credits(uid, chat_type)
            if not ok:
                send_error(m, "❌ Insufficient credits! Use /buy")
                return
        service_map = {"num": "phone", "family": "aadhar"}
        process(m, service_map[cmd], val, chat_type)
    except Exception as e:
        send_error(m, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['advnum'])
def advnum_cmd(m):
    uid = m.from_user.id
    if not ensure_verified_and_member(uid, m.chat.id):
        return
    chat_type = m.chat.type
    try:
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2:
            send_error(m, "❌ Usage: /advnum [10-digit number]\nEg. /advnum 8084502203")
            return
        val = parts[1].strip()
        if not re.match(r'^\d{10}$', val):
            send_error(m, "❌ Please enter a valid 10-digit phone number. No credits deducted.")
            return
        if chat_type == "private":
            ok, bal = check_credits(uid, chat_type)
            if not ok:
                send_error(m, "❌ Insufficient credits! Use /buy")
                return
        process(m, "advnum", val, chat_type)
    except Exception as e:
        send_error(m, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['tg'])
def tg_cmd(m):
    uid = m.from_user.id
    if not ensure_verified_and_member(uid, m.chat.id):
        return
    chat_type = m.chat.type
    try:
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2:
            send_error(m, "❌ Usage: /tg @username or 123456789")
            return
        val = parts[1].strip()
        if chat_type == "private":
            ok, bal = check_credits(uid, chat_type)
            if not ok:
                send_error(m, "❌ Insufficient credits! Use /buy")
                return
        process(m, "tguser", val, chat_type)
    except Exception as e:
        send_error(m, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['vichel'])
def vehicle_cmd(m):
    uid = m.from_user.id
    if not ensure_verified_and_member(uid, m.chat.id):
        return
    chat_type = m.chat.type
    try:
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2:
            send_error(m, "❌ Usage: /vichel MP16CB6745")
            return
        val = parts[1].strip().upper().replace(" ", "")
        if chat_type == "private":
            ok, bal = check_credits(uid, chat_type)
            if not ok:
                send_error(m, "❌ Insufficient credits! Use /buy")
                return
        process(m, "vehicle", val, chat_type)
    except Exception as e:
        send_error(m, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['ff'])
def ff_cmd(m):
    uid = m.from_user.id
    if not ensure_verified_and_member(uid, m.chat.id):
        return
    chat_type = m.chat.type
    try:
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2:
            send_error(m, "❌ Usage: /ff 46454168")
            return
        val = parts[1].strip()
        if chat_type == "private":
            ok, bal = check_credits(uid, chat_type)
            if not ok:
                send_error(m, "❌ Insufficient credits! Use /buy")
                return
        process(m, "ff", val, chat_type)
    except Exception as e:
        send_error(m, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['gmail'])
def gmail_cmd(m):
    uid = m.from_user.id
    if not ensure_verified_and_member(uid, m.chat.id):
        return
    chat_type = m.chat.type
    try:
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2:
            send_error(m, "❌ Usage: /gmail rohit@gmail.com")
            return
        val = parts[1].strip()
        if chat_type == "private":
            ok, bal = check_credits(uid, chat_type)
            if not ok:
                send_error(m, "❌ Insufficient credits! Use /buy")
                return
        process(m, "gmail", val, chat_type)
    except Exception as e:
        send_error(m, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['add'])
def add_credit(m):
    if m.chat.type != "private":
        return
    if not is_admin(m.from_user.id):
        send_error(m, "❌ You are not authorized to use this command.")
        return
    try:
        args = m.text.split()
        if len(args) < 3:
            bot.reply_to(m, "Usage: /add @username or user_id amount")
            return
        target = args[1]
        amount = int(args[2])
        if target.startswith("@"):
            username = target[1:].lower()
            cursor.execute("SELECT user_id, credits FROM users WHERE lower(username)=?", (username,))
            data = cursor.fetchone()
            if not data:
                send_error(m, "❌ Username not found")
                return
            user_id, old = data
        else:
            user_id = int(target)
            old, _, _ = get_user(user_id, "")
        new = old + amount
        set_user(user_id, new)
        bot.send_message(user_id, f"✅ {amount} Credits Added!\n💰 New balance: {new}")
        bot.reply_to(m, f"✅ Added {amount} credits to {user_id}")
    except Exception as e:
        bot.reply_to(m, f"Error: {str(e)}")

@bot.message_handler(commands=['stats'])
def stats(m):
    if m.chat.type != "private":
        return
    if not is_admin(m.from_user.id):
        send_error(m, "❌ You are not authorized to use this command.")
        return
    total = cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_credits = cursor.execute("SELECT SUM(credits) FROM users").fetchone()[0] or 0
    verified_count = cursor.execute("SELECT COUNT(*) FROM users WHERE verified=1").fetchone()[0]
    msg = f"""
<b>📊 BOT STATISTICS</b>
─────────────────────────
👥 Total Users: {total}
✅ Verified Users: {verified_count}
💰 Total Credits: {total_credits}
─────────────────────────
<b>Developer - @Gautamxlive</b>
"""
    bot.send_message(m.chat.id, msg, parse_mode="HTML")

@bot.message_handler(commands=['broadcast'])
def broadcast(m):
    if m.chat.type != "private":
        return
    if not is_admin(m.from_user.id):
        send_error(m, "❌ You are not authorized to use this command.")
        return
    
    sent = bot.send_message(m.chat.id, "📢 <b>Broadcast Mode Active</b>\n\n✍️ Ab jo bhi message bhejo, sab users ko jaayega!\n\n💬 Message bhejo:", parse_mode="HTML")
    bot.register_next_step_handler(m, lambda msg: broadcast_to_all_users(msg, m.from_user.id))

def broadcast_to_all_users(m, admin_id):
    """Send message to all users"""
    if m.content_type == 'text':
        broadcast_text = m.text
    elif m.content_type == 'photo':
        broadcast_text = None
        photo_file_id = m.photo[-1].file_id
    elif m.content_type == 'video':
        broadcast_text = None
        video_file_id = m.video.file_id
    else:
        send_error(m, "❌ Only text, photo, or video supported!")
        return
    
    # Get all users
    cursor.execute("SELECT user_id FROM users")
    user_ids = [row[0] for row in cursor.fetchall()]
    
    success, failed, blocked = 0, 0, 0
    
    loading_msg = bot.send_message(m.chat.id, "📤 Broadcasting... 0%", parse_mode="HTML")
    total = len(user_ids)
    
    for idx, uid in enumerate(user_ids):
        try:
            if m.content_type == 'text':
                bot.send_message(uid, broadcast_text, parse_mode="HTML")
            elif m.content_type == 'photo':
                caption = m.caption or ""
                bot.send_photo(uid, photo_file_id, caption=caption, parse_mode="HTML")
            elif m.content_type == 'video':
                caption = m.caption or ""
                bot.send_video(uid, video_file_id, caption=caption, parse_mode="HTML")
            success += 1
        except Exception as e:
            if "blocked" in str(e).lower() or "user is deactivated" in str(e).lower():
                blocked += 1
            else:
                failed += 1
        
        # Update progress every 10%
        if (idx + 1) % max(1, total // 10) == 0:
            progress = int((idx + 1) / total * 100)
            try:
                bot.edit_message_text(
                    f"📤 Broadcasting... {progress}%\n\n✅ Success: {success}\n❌ Failed: {failed}\n🚫 Blocked: {blocked}",
                    m.chat.id,
                    loading_msg.message_id,
                    parse_mode="HTML"
                )
            except:
                pass
        
        time.sleep(0.05)  # Avoid flood
    
    # Final message
    result_msg = f"""📢 <b>Broadcast Complete!</b>

━━━━━━━━━━━━━━━━━━
✅ Success: {success}
❌ Failed: {failed}
🚫 Blocked: {blocked}
━━━━━━━━━━━━━━━━━━
📊 Total: {total}
━━━━━━━━━━━━━━━━━━"""
    
    try:
        bot.edit_message_text(result_msg, m.chat.id, loading_msg.message_id, parse_mode="HTML")
    except:
        bot.send_message(m.chat.id, result_msg, parse_mode="HTML")

QR_CODE_FILE_ID = None  # Auto-set on first run

@bot.message_handler(commands=['buy'])
def buy(m):
    global QR_CODE_FILE_ID
    if m.chat.type != "private":
        return
    msg = """╔══════════════════════╗
💎 RECHARGE PLANS 💎
╚══════════════════════╝
💰 50₹   ➜ 120 Credits
💰 99₹   ➜ 250 Credits
💰 149₹ ➜ 349 Credits
💰 249₹ ➜ 1000 Credits
━━━━━━━━━━━━━━━━━━━
💳 UPI ID: gautampatel12@fam

✅ Pay Using Any UPI App
📸 Neeche QR Code Se Seedha Pay Karo

⚡ Payment Ke Baad Screenshot Send Karo
🚀 Credits Instantly Add Ho Jayenge

━━━━━━━━━━━━━━━━━━━
👨‍💻 SCREENSHOT BHEJO:

📩 ADMIN ➜ @Syko_killer
━━━━━━━━━━━━━━━━━━━
🤖 FAST • SECURE • INSTANT
━━━━━━━━━━━━━━━━━━━"""
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("👨‍💻 ADMIN @Syko_killer", url="https://t.me/Syko_killer"))
    try:
        if QR_CODE_FILE_ID:
            sent = bot.send_photo(m.chat.id, QR_CODE_FILE_ID, caption=msg, reply_markup=kb)
        else:
            with open("qr_code.jpg", "rb") as qr:
                sent = bot.send_photo(m.chat.id, qr, caption=msg, reply_markup=kb)
            QR_CODE_FILE_ID = sent.photo[-1].file_id
    except Exception:
        bot.send_message(m.chat.id, msg, reply_markup=kb)

@bot.message_handler(commands=['bom'])
def group_bom(m):
    args = m.text.split()
    if len(args) < 2:
        send_error(m, "❌ Usage: /bom <number>\nEg. /bom 9999999999")
        return
    target_num = args[1]
    msg = f"""🚀 <b>BOMBER</b> 💥
━━━━━━━━ ✤ ━━━━━━━━
📱 <b>Target:</b> <code>{target_num}</code>
━━━━━━━━ ✤ ━━━━━━━━
⚠️ <b>SERVICE UNDER MAINTENANCE</b>

🔧 Bomber API is currently under maintenance.

⏳ Coming Soon...
📅 Expected in a few days.

✨ Stay tuned for updates!
🔥 Thank you for your patience.
━━━━━━━━ ✤ ━━━━━━━━
🤖 <b>GAUTAM X INFO BOT</b>"""
    bot.reply_to(m, msg, parse_mode="HTML")

# Group message auto-detection helpers
@bot.message_handler(func=lambda m: m.chat.type in ("group", "supergroup"))
def track_group_messages(m):
    txt = m.text or ""

    def reply_and_autodelete(text):
        try:
            sent = bot.reply_to(m, text, parse_mode="HTML")
            threading.Thread(target=auto_delete, args=(m.chat.id, sent.message_id, 30), daemon=True).start()
        except Exception:
            pass

    if re.match(r'^\d{10}$', txt.strip()):
        reply_and_autodelete(f"📱 <b>Phone number detect hua!</b>\n\n✅ Aise use karo:\n<code>/num {txt.strip()}</code>\n\n👆 Ye command bhejo!")
        return

    if re.match(r'^\d{12}$', txt.strip()):
        reply_and_autodelete(f"🪪 <b>Aadhar number detect hua!</b>\n\n✅ Aise use karo:\n<code>/family {txt.strip()}</code>\n\n👆 Ye command bhejo!")
        return

    if re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', txt.strip()):
        reply_and_autodelete(f"📧 <b>Email detect hua!</b>\n\n✅ Aise use karo:\n<code>/gmail {txt.strip()}</code>\n\n👆 Ye command bhejo!")
        return

    if re.match(r'^@[a-zA-Z][a-zA-Z0-9_]{4,}$', txt.strip()):
        reply_and_autodelete(f"📲 <b>TG Username detect hua!</b>\n\n✅ Aise use karo:\n<code>/tg {txt.strip()}</code>\n\n👆 Ye command bhejo!")
        return

    if re.match(r'^[a-zA-Z0-9.\-_]+@[a-zA-Z]{2,}$', txt.strip()) and not re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', txt.strip()):
        reply_and_autodelete(f"💸 <b>UPI ID detect hua!</b>\n\n✅ Aise use karo:\n<code>/upi {txt.strip()}</code>\n\n👆 Ye command bhejo!")
        return

    if re.match(r'^[A-Z]{2}\d{2}[A-Z]{1,2}\d{4}$', txt.strip().upper().replace(" ", "")):
        vichel_num = txt.strip().upper().replace(" ", "")
        reply_and_autodelete(f"🚗 <b>Vehicle number detect hua!</b>\n\n✅ Aise use karo:\n<code>/vichel {vichel_num}</code>\n\n👆 Ye command bhejo!")
        return

    if re.match(r'^\d{6,10}$', txt.strip()):
        reply_and_autodelete(f"🎮 <b>FF UID detect hua!</b>\n\n✅ Aise use karo:\n<code>/ff {txt.strip()}</code>\n\n👆 Ye command bhejo!")
        return

# ================= PRIVATE TEXT HANDLER =================
@bot.callback_query_handler(func=lambda call: call.data == "cancel_service")
def cancel_service_cb(call):
    bot.answer_callback_query(call.id, "Returning to main menu")
    bot.clear_step_handler_by_chat_id(chat_id=call.message.chat.id)
    bal, _, _ = get_user(call.from_user.id, "")
    msg = f"✨ Welcome back!\n💰 Balance: {bal} Credits\n⚡ 1 Credit/search\n\nUse buttons below."
    bot.send_message(call.message.chat.id, msg, reply_markup=private_kb())
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("copy_"))
def copy_cb(call):
    num = call.data.split("_")[1]
    bot.answer_callback_query(call.id, f"✅ Copied: {num}", show_alert=True)
    bot.send_message(call.message.chat.id, f"📋 <code>{num}</code>", parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("copyall_"))
def copyall_cb(call):
    key = call.data
    text = copy_cache.get(key, "")
    if not text:
        bot.answer_callback_query(call.id, "⚠️ Data expired. Search again.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "✅ Text copied below!", show_alert=False)
    bot.send_message(call.message.chat.id, f"<code>{text}</code>", parse_mode="HTML")

def check_credits(uid, chat_type):
    if chat_type != "private":
        return True, -1
    bal, _, _ = get_user(uid, "")
    return bal > 0, bal

def process(m, t, value, chat_type):
    uid = m.from_user.id
    username = m.from_user.username

    if t == "phone":
        if not re.match(r'^\d{10}$', value):
            send_error(m, "❌ Invalid 10-digit phone number. No credits deducted.")
            return
    if t == "aadhar":
        if not re.match(r'^\d{12}$', value):
            send_error(m, "❌ Invalid Aadhar number (must be 12 digits). No credits deducted.")
            return
    if t == "advnum":
        if not re.match(r'^\d{10}$', value):
            send_error(m, "❌ Invalid 10-digit phone number. No credits deducted.")
            return

    if chat_type == "private" and not is_admin(uid):
        if get_free_uses(uid) >= FREE_USES_LIMIT:
            send_limit_promo(m)
            return
        bal, _, _ = get_user(uid, username)
        if bal <= 0:
            send_error(m, "❌ Insufficient credits!")
            return

    load_msg = bot.send_message(m.chat.id, "⏳ Processing...")
    try:
        urls = {
            "phone": f"https://anon-num-info.vercel.app/num?key=Arpitxlive274&num={value}",
            "aadhar": f"https://anon-family-info.vercel.app/aadhar?key=Arpitxlive284&q={value}",
            "tguser": f"https://anon-tg-info.vercel.app/tg2num/user?key=Arpitxlive205&q={value}",
            "vehicle": f"https://anon-vehicle-info.vercel.app/rc?key=Arpitxlive2405&rc={value}",
            "upi": f"https://anon-upi-info.vercel.app/verify?key=Arpitxlive2405&upi={value}",
            "ff": f"https://anon-ff-info.vercel.app/info?key=Arpitxlive2405&uid={value}",
            "gmail": f"https://anon-email-info.vercel.app/email?key=Arpitxlive3005&email={value}",
            "pak": f"https://anon-pak-info.vercel.app/num?key=temp96p&q={value}",
            "advnum": f"https://paid.originalapis.workers.dev/number?key=Gautam&num={value}",
        }
        try:
            r = requests.get(urls[t], timeout=15)
        except requests.exceptions.Timeout:
            bot.edit_message_text(
                "🌐 <b>CONNECTION ERROR</b>\n\n⏱️ Server ne response nahi diya (Timeout)\n\n♻️ <b>Try Again</b>",
                m.chat.id, load_msg.message_id, parse_mode="HTML"
            )
            threading.Thread(target=auto_delete, args=(m.chat.id, load_msg.message_id, 30), daemon=True).start()
            return
        except requests.exceptions.ConnectionError:
            bot.edit_message_text(
                "🌐 <b>CONNECTION ERROR</b>\n\n📡 Server se connect nahi ho saka\n\n♻️ <b>Try Again</b>",
                m.chat.id, load_msg.message_id, parse_mode="HTML"
            )
            threading.Thread(target=auto_delete, args=(m.chat.id, load_msg.message_id, 30), daemon=True).start()
            return
        except requests.exceptions.RequestException:
            bot.edit_message_text(
                "🌐 <b>CONNECTION ERROR</b>\n\n❌ Network issue aa gaya\n\n♻️ <b>Try Again</b>",
                m.chat.id, load_msg.message_id, parse_mode="HTML"
            )
            threading.Thread(target=auto_delete, args=(m.chat.id, load_msg.message_id, 30), daemon=True).start()
            return

        try:
            data = r.json() if r.text else {"result": r.text, "success": False}
        except Exception:
            bot.edit_message_text(
                "🌐 <b>CONNECTION ERROR</b>\n\n⚠️ Server se galat response mila\n\n♻️ <b>Try Again</b>",
                m.chat.id, load_msg.message_id, parse_mode="HTML"
            )
            threading.Thread(target=auto_delete, args=(m.chat.id, load_msg.message_id, 30), daemon=True).start()
            return

        data = replace_developer(data, "@Gautamxlive")

        if t in ("advnum", "phone", "aadhar", "vehicle", "upi"):
            type_name = {"advnum": "ADVNUM", "phone": "PHONE", "aadhar": "AADHAR", "vehicle": "VEHICLE", "upi": "UPI"}.get(t, t.upper())
            print(f"[{type_name}-RAW-RESPONSE] uid={uid} value={value} -> {data}")
            bot.delete_message(m.chat.id, load_msg.message_id)
            try:
                pretty = json.dumps(data, indent=2, ensure_ascii=False)
                code_block = f"```json\n{pretty}\n```"
                sent = bot.send_message(m.chat.id, code_block, parse_mode="Markdown")
            except Exception as fe:
                print(f"[{type_name}-JSON-SEND-ERROR] {fe}")
                sent = bot.send_message(m.chat.id, json.dumps(data, indent=2, ensure_ascii=False))
            threading.Thread(target=auto_delete, args=(m.chat.id, sent.message_id, 120), daemon=True).start()
            if chat_type == "private" and not is_admin(uid):
                success = is_successful(data, t)
                if success:
                    increment_free_uses(uid)
                    bal, _, _ = get_user(uid, username)
                    new_bal = bal - 1
                    set_user(uid, new_bal)
                    if new_bal <= 3:
                        bot.send_message(m.chat.id, "⚠️ Low credits! Please recharge.")
                else:
                    bot.send_message(m.chat.id, "⚠️ No valid data found. No credits deducted. Try again later.")
            return

        bot.delete_message(m.chat.id, load_msg.message_id)

        result_text, plain_text = format_json(data, t, value)

        nums = extract_numbers(data)

        cache_key = f"copyall_{hash(plain_text) % 10**8}"
        copy_cache[cache_key] = plain_text
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(InlineKeyboardButton("📋 Copy All Text", callback_data=cache_key))
        if nums:
            for num in nums:
                kb.add(InlineKeyboardButton(f"📱 {num}", callback_data=f"copy_{num}"))

        # TG result ke saath video, baaki sirf text
        if t == "tguser":
            try:
                sent = bot.send_video(m.chat.id, get_random_video(), caption=result_text, reply_markup=kb)
            except Exception as e:
                print(f"[TG-VIDEO-ERROR] {e}")
                sent = bot.send_message(m.chat.id, result_text, reply_markup=kb, parse_mode="HTML")
        else:
            sent = bot.send_message(m.chat.id, result_text, reply_markup=kb, parse_mode="HTML")

        no_data = "No data found." in plain_text
        delete_delay = 60 if no_data else 120
        threading.Thread(target=auto_delete, args=(m.chat.id, sent.message_id, delete_delay), daemon=True).start()

        if chat_type == "private" and not is_admin(uid):
            success = is_successful(data, t)
            if success:
                increment_free_uses(uid)
                bal, _, _ = get_user(uid, username)
                new_bal = bal - 1
                set_user(uid, new_bal)
                deduction_msg = f"\n\n<code>💳 1 credit deducted | Balance: {new_bal}</code>"
                try:
                    bot.edit_message_text(
                        chat_id=m.chat.id,
                        message_id=sent.message_id,
                        text=result_text + deduction_msg,
                        parse_mode="HTML",
                        reply_markup=kb
                    )
                except:
                    pass
                if new_bal <= 3:
                    bot.send_message(m.chat.id, "⚠️ Low credits! Please recharge.")
            else:
                bot.send_message(m.chat.id, "⚠️ No valid data found. No credits deducted. Try again later.")
    except Exception as e:
        print(f"[PROCESS-ERROR] type={t} err={e}")
        try:
            bot.edit_message_text(
                "🌐 <b>CONNECTION ERROR</b>\n\n❌ Kuch unexpected error aa gaya\n\n♻️ <b>Try Again</b>",
                m.chat.id, load_msg.message_id, parse_mode="HTML"
            )
            threading.Thread(target=auto_delete, args=(m.chat.id, load_msg.message_id, 30), daemon=True).start()
        except:
            try:
                sent = bot.send_message(
                    m.chat.id,
                    "🌐 <b>CONNECTION ERROR</b>\n\n❌ Kuch unexpected error aa gaya\n\n♻️ <b>Try Again</b>",
                    parse_mode="HTML"
                )
                threading.Thread(target=auto_delete, args=(m.chat.id, sent.message_id, 30), daemon=True).start()
            except:
                pass

@bot.message_handler(func=lambda m: m.chat.type == "private")
def private_text(m):
    txt = m.text or ""
    if txt.startswith("/"):
        return  # commands are handled by their own dedicated handlers — skip here
                 # to avoid double verification prompts / duplicate messages

    if not ensure_verified_and_member(m.from_user.id, m.chat.id):
        return
    bal, _, _ = get_user(m.from_user.id, "")

    known_buttons = [
        "📱 𝗡𝗨𝗠 𝗧𝗢 𝗜𝗡𝗙𝗢 🔍", "🆔 𝗔𝗔𝗗𝗛𝗔𝗥 𝗧𝗢 𝗙𝗔𝗠𝗜𝗟𝗬 👨‍👩‍👧‍👦",
        "📲 𝗧𝗚 𝗧𝗢 𝗡𝗨𝗠 ☎️", "🚗 𝗩𝗘𝗛𝗜𝗖𝗟𝗘 𝗜𝗡𝗙𝗢 🔎",
        "💳 𝗨𝗣𝗜 𝗜𝗡𝗙𝗢 🔰", "🎮 𝗙𝗙 𝗜𝗗 𝗜𝗡𝗙𝗢 ⚡",
        "📧 𝗚𝗠𝗔𝗜𝗟 𝗜𝗡𝗙𝗢 💠", "🇵🇰 𝗣𝗔𝗞 𝗜𝗡𝗙𝗢 🌐",
        "🌪️ 𝗙𝗙 𝗔𝗨𝗧𝗢 𝗟𝗜𝗞𝗘 💠",
        "🚀 𝗕𝗢𝗠𝗕𝗘𝗥 💥",
        "🔰 𝗔𝗗𝗩 𝗡𝗨𝗠 𝗜𝗡𝗙𝗢 🔮",
        "💎 𝗕𝗨𝗬 𝗖𝗥𝗘𝗗𝗜𝗧 🛒", "🎁 𝗥𝗘𝗙𝗘𝗥 & 𝗘𝗔𝗥𝗡 ♻️",
        "👤 𝗠𝗬 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 📋"
    ]
    if txt not in known_buttons:
        bot.reply_to(m, """⚠️ <b>Private Chat mein Commands Nahi!</b>

🔘 Niche diye <b>Buttons</b> ka use karo:

📱 <b>NUM INFO</b> → Niche button dabao
🪪 <b>AADHAR INFO</b> → Niche button dabao
📲 <b>TG TO NUM</b> → Niche button dabao
🚗 <b>VEHICLE INFO</b> → Niche button dabao
💸 <b>UPI INFO</b> → Niche button dabao
🎮 <b>FF INFO</b> → Niche button dabao
📧 <b>GMAIL INFO</b> → Niche button dabao
🇵🇰 <b>PAK INFO</b> → Niche button dabao
🔰 <b>ADV NUM INFO</b> → Niche button dabao
🌪️ <b>FF AUTO LIKE</b> → Niche button dabao
🚀 <b>BOMBER</b> → Niche button dabao

👇 <b>Buttons niche dikh rahe hain, unhe use karo!</b>""",
            parse_mode="HTML", reply_markup=private_kb())
        return

    if bal <= 0 and "BUY" not in txt.upper() and "MY ACCOUNT" not in txt.upper():
        send_error(m, "❌ No credits! Use /buy")
        return

    back_kb = InlineKeyboardMarkup()
    back_kb.add(InlineKeyboardButton("🔙 Back to Menu", callback_data="cancel_service"))

    if txt == "📱 𝗡𝗨𝗠 𝗧𝗢 𝗜𝗡𝗙𝗢 🔍":
        msg = f"""📱 <b>NUM INFO</b>

💳 Balance: {bal} Credits
⚡ Charge: 1 Credit Per Search

🔎 Indian Mobile Number Lookup

📌 Enter any 10-digit Indian mobile number
🚫 Do not add +91 or country code

✨ Example:
<code>6205923286</code>

⚠️ Only valid Indian mobile numbers are supported."""
        bot.send_message(m.chat.id, f"<blockquote>{msg}</blockquote>", reply_markup=back_kb, parse_mode="HTML")
        bot.register_next_step_handler(m, lambda x: process(x, "phone", x.text, "private"))

    elif txt == "🆔 𝗔𝗔𝗗𝗛𝗔𝗥 𝗧𝗢 𝗙𝗔𝗠𝗜𝗟𝗬 👨‍👩‍👧‍👦":
        msg = f"""<b>Family (Aadhar)</b>

💰 Balance: {bal}
⚡ Charge: 1 credit(s)

👩‍👩‍👦‍👦 Aadhar To Family Details

Send 12-digit aadhar number to get family details 

Eg. 456162100762"""
        bot.send_message(m.chat.id, f"<blockquote>{msg}</blockquote>", reply_markup=back_kb, parse_mode="HTML")
        bot.register_next_step_handler(m, lambda x: process(x, "aadhar", x.text, "private"))

    elif txt == "📲 𝗧𝗚 𝗧𝗢 𝗡𝗨𝗠 ☎️":
        msg = f"""💎 <b>TG TO NUM</b> 🔮

💳 Balance: {bal} Credits
⚡ Charge: 1 Credit Per Search

🔍 Find Mobile Number from Telegram Username

✈️ Send Telegram Username or 10-Digit User ID

✨ Examples:
✈️ <code>@monk</code>
🆔 <code>1234567890</code>

🚀 Fast • Accurate • Secure"""
        bot.send_message(m.chat.id, f"<blockquote>{msg}</blockquote>", reply_markup=back_kb, parse_mode="HTML")
        bot.register_next_step_handler(m, lambda x: process(x, "tguser", x.text, "private"))

    elif txt == "🚗 𝗩𝗘𝗛𝗜𝗖𝗟𝗘 𝗜𝗡𝗙𝗢 🔎":
        msg = f"🚘 Vehicle Info\n\n💰 Balance: {bal}\n⚡ Charge: 1 credit(s)\n\nSend vehicle number\nEg. MP16CB6745"
        bot.send_message(m.chat.id, f"<blockquote>{msg}</blockquote>", reply_markup=back_kb, parse_mode="HTML")
        bot.register_next_step_handler(m, lambda x: process(x, "vehicle", x.text.upper().replace(" ", ""), "private"))

    elif txt == "💳 𝗨𝗣𝗜 𝗜𝗡𝗙𝗢 🔰":
        msg = f"💸 UPI Info\n\n💰 Balance: {bal}\n⚡ Charge: 1 credit(s)\n\nSend UPI ID\nEg. rohit@sbi"
        bot.send_message(m.chat.id, f"<blockquote>{msg}</blockquote>", reply_markup=back_kb, parse_mode="HTML")
        bot.register_next_step_handler(m, lambda x: process(x, "upi", x.text.lower(), "private"))

    elif txt == "🎮 𝗙𝗙 𝗜𝗗 𝗜𝗡𝗙𝗢 ⚡":
        msg = f"🎮 FF Info\n\n💰 Balance: {bal}\n⚡ Charge: 1 credit(s)\n\nSend Free Fire UID\nEg. 46454168"
        bot.send_message(m.chat.id, f"<blockquote>{msg}</blockquote>", reply_markup=back_kb, parse_mode="HTML")
        bot.register_next_step_handler(m, lambda x: process(x, "ff", x.text, "private"))

    elif txt == "🔰 𝗔𝗗𝗩 𝗡𝗨𝗠 𝗜𝗡𝗙𝗢 🔮":
        msg = f"""🔰 <b>ADVANCE NUM INFO</b> 🔮

💳 Balance: {bal} Credits
⚡ Charge: 1 Credit Per Search

🔎 Indian Mobile Number Lookup

📌 Enter any 10-digit Indian mobile number
🚫 Do not add +91 or country code

✨ <b>ADV EXAMPLE :</b>
<code>6205923286</code>

⚠️ Only valid Indian mobile numbers are supported."""
        bot.send_message(m.chat.id, f"<blockquote>{msg}</blockquote>", reply_markup=back_kb, parse_mode="HTML")
        bot.register_next_step_handler(m, lambda x: process(x, "advnum", x.text.strip(), "private"))


    elif txt == "🌪️ 𝗙𝗙 𝗔𝗨𝗧𝗢 𝗟𝗜𝗞𝗘 💠":
        msg = """🎯 <b>FF AUTO LIKE</b> 💎

⚡ Get Instant Likes &amp; Auto Likes
🚀 Fast • Secure • Reliable

🤖 <b>USE THIS BOT:</b>
👉 @ffautolike_robot

📛 GROUP ME JOIN HOKE
<code>/like ind uid</code> ye command chalavo instant like milega

GROUP LINK 🔗 @AGHUNTER

✨ Enjoy instant processing
🔥 Best experience for FF players

💎 Thank You For Using Our Service!"""
        kb2 = InlineKeyboardMarkup(row_width=1)
        kb2.add(InlineKeyboardButton("🤖 FF AUTO LIKE BOT", url="https://t.me/ffautolike_robot"))
        kb2.add(InlineKeyboardButton("🔗 JOIN GROUP @AGHUNTER", url="https://t.me/AGHUNTER"))
        bot.send_message(m.chat.id, f"<blockquote>{msg}</blockquote>", reply_markup=kb2, parse_mode="HTML")

    elif txt == "🚀 𝗕𝗢𝗠𝗕𝗘𝗥 💥":
        msg = """🚀 <b>BOMBER</b> 💥

⚠️ <b>SERVICE UNDER MAINTENANCE</b>

🔧 Bomber API is currently under maintenance.

⏳ Coming Soon...
📅 Expected in a few days.

✨ Stay tuned for updates!
🔥 Thank you for your patience."""
        bot.send_message(m.chat.id, f"<blockquote>{msg}</blockquote>", parse_mode="HTML")

    elif txt == "📧 𝗚𝗠𝗔𝗜𝗟 𝗜𝗡𝗙𝗢 💠":
        msg = f"""❄ Email Info 📧

💰 Balance : {bal}
⚡ Charge : 1 credit(s)

📩 Advanced Email Info 💎

Send an email address to check for details

E.g. rohit5@gmail.com"""
        bot.send_message(m.chat.id, f"<blockquote>{msg}</blockquote>", reply_markup=back_kb, parse_mode="HTML")
        bot.register_next_step_handler(m, lambda x: process(x, "gmail", x.text, "private"))

    elif txt == "💎 𝗕𝗨𝗬 𝗖𝗥𝗘𝗗𝗜𝗧 🛒":
        buy(m)

    elif txt == "🎁 𝗥𝗘𝗙𝗘𝗥 & 𝗘𝗔𝗥𝗡 ♻️":
        uid = m.from_user.id
        bot_info = bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref{uid}"
        count = cursor.execute("SELECT COUNT(*) FROM users WHERE referred_by=?", (uid,)).fetchone()[0]
        earned = count * 2
        msg = f"""🎁 <b>REFER & EARN</b>

👥 <b>Aapne refer kiye:</b> {count} log
💰 <b>Kamai:</b> {earned} credits

━━━━━━━━━━━━━━━━━━━━
🔗 <b>Aapka Referral Link:</b>
<code>{ref_link}</code>

✅ Har successful referral pe <b>2 Credits</b> milenge!
━━━━━━━━━━━━━━━━━━━━
👇 Share karo apne dosto ke saath"""
        kb2 = InlineKeyboardMarkup()
        kb2.add(InlineKeyboardButton("📤 Share Link", url=f"https://t.me/share/url?url={ref_link}&text=Join+this+bot+and+get+20+free+credits!"))
        bot.send_message(m.chat.id, f"<blockquote>{msg}</blockquote>", reply_markup=kb2, parse_mode="HTML")

    elif txt == "👤 𝗠𝗬 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 📋":
        show_profile(m)

# ================= AUTO REACTION =================
import random as _random
REACTIONS = ["⚡", "🔥", "👍", "❤️", "🎉", "👏", "🤩", "💯"]

@bot.message_handler(func=lambda m: True, content_types=['text','photo','video','audio','document','sticker','voice','animation'])
def auto_react(m):
    emoji = _random.choice(REACTIONS)
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setMessageReaction",
            json={
                "chat_id": m.chat.id,
                "message_id": m.message_id,
                "reaction": [{"type": "emoji", "emoji": emoji}],
                "is_big": False
            }
        )
    except:
        pass

if __name__ == "__main__":
    print("=" * 50)
    print("🤖 GAUTAM X INFO BOT")
    print("=" * 50)
    try:
        bot_info = bot.get_me()
        print(f"✅ Bot: @{bot_info.username}")
    except:
        print("❌ Invalid Bot Token! Check BOT_TOKEN")
        exit(1)
    print("👨‍💻 Developer - @Gautamxlive")
    print("=" * 50)
    print("🟢 Bot is running...")
    print("📍 PRIVATE: 20 FREE Credits + Keyboard Buttons")
    print("📍 GROUP: UNLIMITED FREE (commands only)")
    print("📍 JSON auto-delete after 60 seconds")
    print("📍 Credits deducted ONLY on successful data")
    print("📍 Auto-detect channel leave – will block immediately")
    print("=" * 50)

    # ===== WEEKLY BROADCAST =====
    def get_all_user_ids():
        cursor.execute("SELECT user_id FROM users")
        return [row[0] for row in cursor.fetchall()]

    WEEKLY_PROMO_MSG = """━━━━━━━━ ✤ ━━━━━━━━
🔔 <b>Hᴇʏ! Gᴀᴜᴛᴀᴍ X Iɴꜰᴏ Bᴏᴛ</b> 🌕
➖➖➖➖➖➖➖➖➖➖➖➖
✨ Sᴀʙsᴇ ʙᴇsᴛ ɪɴꜰᴏ ʙᴏᴛ ʙᴀᴄᴋ ʜᴀɪ! 🤩

🃏 ᴀᴠᴀɪʟᴀʙʟᴇ sᴇʀᴠɪᴄᴇs:
➖➖➖➖➖➖
│ 📱 Nᴜᴍ Tᴏ Iɴꜰᴏ
│ 🪪 Aᴀᴅʜᴀʀ Iɴꜰᴏ
│ 📲 TG Usᴇʀɴᴀᴍᴇ Iɴꜰᴏ
│ 🚗 Vᴇʜɪᴄʟᴇ Iɴꜰᴏ
│ 💸 UPI Iɴꜰᴏ
│ 🎮 Fʀᴇᴇ Fɪʀᴇ Iɴꜰᴏ
│ 📧 Gᴍᴀɪʟ Iɴꜰᴏ
│ 🔰 Aᴅᴠᴀɴᴄᴇ Nᴜᴍ Iɴꜰᴏ
━━━━━━━━ ⸙ ━━━━━━━━
👉 /start ᴋᴀʀᴏ ᴀᴜʀ ᴜsᴇ ᴋᴀʀᴏ!
➖➖➖➖➖➖➖➖➖➖➖➖
🇮🇳 ᴅᴇᴠ » ˹ @Gautamxlive !! ✅
➖➖➖➖➖➖➖➖➖➖➖➖"""

    def weekly_broadcast():
        while True:
            time.sleep(7 * 24 * 60 * 60)  # 7 din wait
            user_ids = get_all_user_ids()
            success, failed = 0, 0
            for uid in user_ids:
                try:
                    bot.send_video(uid, get_random_video(), caption=WEEKLY_PROMO_MSG, parse_mode="HTML")
                    success += 1
                    time.sleep(0.05)  # flood se bachne ke liye
                except Exception:
                    try:
                        bot.send_message(uid, WEEKLY_PROMO_MSG, parse_mode="HTML")
                        success += 1
                    except Exception:
                        failed += 1
            print(f"[WEEKLY BROADCAST] Sent: {success} | Failed: {failed}")

    threading.Thread(target=weekly_broadcast, daemon=True).start()
    print("📅 Weekly broadcast scheduler started!")
    # ===========================

    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=30)
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(5)




