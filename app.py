# app.py (final version with always-reply toggle + summary)
import os
import sqlite3
from flask import Flask, request, jsonify
from twilio.rest import Client
from datetime import datetime
import re

# --- CONFIG ---
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN  = os.getenv('TWILIO_AUTH_TOKEN')
WHATSAPP_FROM      = os.getenv('WHATSAPP_FROM')  
ADMIN_WHATSAPP     = os.getenv('ADMIN_WHATSAPP')
AVAILABILITY_FILE  = 'availability.txt'
SAKHII_NAME = "Sakhii"

# NEW: always auto-reply mode (ignore availability)
AUTO_REPLY_ALWAYS = os.getenv('AUTO_REPLY_ALWAYS', 'false').lower() in ('1','true','yes')

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
app = Flask(__name__)
DB = 'whatsapp_bot.db'

# ------------------ DB INIT ------------------
def init_db():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS users
                   (phone TEXT PRIMARY KEY, display_name TEXT, preferred_lang TEXT, created_at TEXT)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS messages
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, from_num TEXT, to_num TEXT, text TEXT, media TEXT, direction TEXT, ts TEXT, urgent INTEGER)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS tasks
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, text TEXT, created_at TEXT, status TEXT)''')
    conn.commit()
    conn.close()

def _normalize_whatsapp(num: str):
    if num and not num.startswith("whatsapp:"):
        return "whatsapp:" + num.lstrip("+")
    return num

def save_user_language(phone, lang, display_name=None):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute('SELECT phone FROM users WHERE phone=?', (phone,))
    if cur.fetchone():
        cur.execute('UPDATE users SET preferred_lang=? WHERE phone=?', (lang, phone))
    else:
        cur.execute('INSERT INTO users (phone,display_name,preferred_lang,created_at) VALUES (?,?,?,?)',
                    (phone, display_name or '', lang, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def get_user_language(phone):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute('SELECT preferred_lang FROM users WHERE phone=?', (phone,))
    r = cur.fetchone()
    conn.close()
    return r[0] if r else None

def save_message(from_num, to_num, text, media, direction, urgent=0):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute('INSERT INTO messages (from_num,to_num,text,media,direction,ts,urgent) VALUES (?,?,?,?,?,?,?)',
                (from_num,to_num,text,media,direction, datetime.utcnow().isoformat(), urgent))
    conn.commit()
    conn.close()

def save_task(phone, text):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute('INSERT INTO tasks (phone,text,created_at,status) VALUES (?,?,?,?)',
                (phone, text, datetime.utcnow().isoformat(), 'open'))
    conn.commit()
    conn.close()

# ------------------ AVAILABILITY ------------------
def get_availability():
    if not os.path.exists(AVAILABILITY_FILE):
        return 'available'
    with open(AVAILABILITY_FILE,'r') as f:
        return f.read().strip()

def set_availability(state):
    with open(AVAILABILITY_FILE,'w') as f:
        f.write(state)

# ------------------ LANG DETECTION ------------------
def detect_language(text):
    if not text:
        return 'en'
    mar = sum(1 for c in text if '\u0900' <= c <= '\u097F')
    eng = sum(1 for c in text if c.isalpha())
    if mar >= 3:
        return 'mr'
    if eng >= 3:
        return 'en'
    mr_kw = ['काय','कसे','हय','कधी','धन्यवाद','नमस्कार']
    for k in mr_kw:
        if k in text:
            return 'mr'
    return 'en'

# ------------------ TEMPLATES ------------------
def sakhi_intro(lang='en'):
    return "नमस्कार — मी सखी." if lang == 'mr' else "Hi — I’m Sakhii."

def auto_reply_template(lang='en'):
    if lang == 'mr':
        return (sakhi_intro('mr') +
                " सोहम सध्या उपलब्ध नाही. मी तुमची मदत करू शकते.")
    return (sakhi_intro('en') +
            " Soham is currently unavailable. I can assist you.")

def task_saved_template(lang='en'):
    return "टास्क सेव्ह केला आहे." if lang == 'mr' else "Task saved."

def media_received_template(lang='en'):
    return "मीडिया मिळाला." if lang == 'mr' else "Media received."

def chat_mode_template(lang='en'):
    return "काय मदत करू?" if lang == 'mr' else "How can I help?"

def call_fallback_template(lang='en'):
    return "Call missed." if lang == 'mr' else "Missed call noted."

def detect_task(text):
    if not text:
        return False
    t = text.lower()
    triggers = ['tell soham', 'urgent', 'important']
    return any(k in t for k in triggers)

# ------------------ SAFE SEND ------------------
def send_whatsapp(to_num, text):
    try:
        client.messages.create(body=text, from_=WHATSAPP_FROM, to=to_num)
    except Exception as e:
        print("Twilio send error:", e)

# ------------------ SUMMARY ------------------
def build_sender_summary(phone):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.execute("SELECT text FROM messages WHERE from_num=? ORDER BY ts DESC LIMIT 3", (phone,))
    recent = cur.fetchall()

    cur.execute("SELECT text FROM tasks WHERE phone=? AND status='open' ORDER BY created_at DESC LIMIT 5", (phone,))
    tasks = cur.fetchall()

    conn.close()

    lines = []
    if recent:
        lines.append("Recent messages:")
        for r in recent:
            lines.append(f"- {r[0][:120]}")

    if tasks:
        lines.append("Open tasks:")
        for r in tasks:
            lines.append(f"- {r[0][:120]}")

    return "\n".join(lines)

# ------------------ MAIN WEBHOOK ------------------
@app.route('/webhook', methods=['POST'])
def webhook():

    from_num = request.values.get('From')
    to_num   = request.values.get('To')
    body     = request.values.get('Body', '').strip()

    # normalize
    from_num = _normalize_whatsapp(from_num)

    # language
    lang = get_user_language(from_num) or detect_language(body)
    save_user_language(from_num, lang)

    # media
    num_media = int(request.values.get('NumMedia', '0'))
    media_urls = []
    for i in range(num_media):
        m = request.values.get(f"MediaUrl{i}")
        if m:
            media_urls.append(m)
    media = ",".join(media_urls) if media_urls else None

    urgent = 1 if detect_task(body) else 0
    save_message(from_num, to_num, body, media, 'in', urgent)

    state = get_availability()
    should_reply = AUTO_REPLY_ALWAYS or (state != 'available')

    if should_reply:
        reply = auto_reply_template(lang)
        summary = build_sender_summary(from_num)
        if summary:
            reply += "\n\n" + summary

        send_whatsapp(from_num, reply)

        if media:
            send_whatsapp(from_num, media_received_template(lang))

        if urgent:
            save_task(from_num, body)
            send_whatsapp(from_num, task_saved_template(lang))

        if body.lower().startswith(("hi","hello","hey","नमस्कार")):
            send_whatsapp(from_num, chat_mode_template(lang))

    else:
        admin_msg = f"Message from {from_num}: {body[:300]}"
        send_whatsapp(ADMIN_WHATSAPP, admin_msg)

    return ('', 200)


# ------------------ VOICE FALLBACK ------------------
@app.route('/voice-webhook', methods=['POST'])
def voice_webhook():
    call_status = request.values.get('CallStatus', '').lower()
    from_num = _normalize_whatsapp(request.values.get('From'))

    if call_status in ('no-answer', 'busy', 'failed'):
        save_message(from_num, WHATSAPP_FROM, "Missed call", None, 'in', urgent=1)
        send_whatsapp(from_num, call_fallback_template('en'))
    return ('', 200)

# ------------------ STATUS SETTER ------------------
@app.route('/set_status', methods=['POST'])
def set_status():
    payload = request.get_json(silent=True)
    new_state = None

    if payload and 'state' in payload:
        new_state = payload['state']
    else:
        new_state = (
            request.form.get('state') or
            request.values.get('state')
        )

    if new_state not in ('available', 'busy', 'sleeping'):
        return jsonify({'error': 'invalid'}), 400

    set_availability(new_state)

    return jsonify({'ok': True})

# ------------------ HEALTH ------------------
@app.route('/health')
def health():
    return "OK", 200

# ------------------ APP STARTUP ------------------
init_db()
if not os.path.exists(AVAILABILITY_FILE):
    set_availability('available')

print("Sakhii: DB initialized and availability ensured. Starting app...")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
