# app.py (fixed and improved)
import os
import sqlite3
from flask import Flask, request, jsonify
from twilio.rest import Client
from datetime import datetime
import re

# --- CONFIG ---
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN  = os.getenv('TWILIO_AUTH_TOKEN')
WHATSAPP_FROM      = os.getenv('WHATSAPP_FROM')  # e.g. 'whatsapp:+1415xxxxxxx'
ADMIN_WHATSAPP     = os.getenv('ADMIN_WHATSAPP') # 'whatsapp:+91...'
AVAILABILITY_FILE  = 'availability.txt'
SAKHII_NAME = "Sakhii"

# create Twilio client lazily
_twilio_client = None
def get_twilio_client():
    global _twilio_client
    if _twilio_client is None:
        if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
            print("Warning: Twilio credentials are missing. Bot will not send WhatsApp messages.")
            return None
        _twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio_client

app = Flask(__name__)
DB = 'whatsapp_bot.db'

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

def _now_sql():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

def save_user_language(phone, lang, display_name=None):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute('SELECT phone FROM users WHERE phone=?', (phone,))
    if cur.fetchone():
        cur.execute('UPDATE users SET preferred_lang=? WHERE phone=?', (lang, phone))
    else:
        cur.execute('INSERT INTO users (phone,display_name,preferred_lang,created_at) VALUES (?,?,?,?)',
                    (phone, display_name or '', lang, _now_sql()))
    conn.commit()
    conn.close()

def get_user_language(phone):
    if not phone:
        return None
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
                (from_num or '', to_num or '', text or '', media or None, direction or '', _now_sql(), urgent))
    conn.commit()
    conn.close()

def save_task(phone, text):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute('INSERT INTO tasks (phone,text,created_at,status) VALUES (?,?,?,?)',
                (phone or '', text or '', _now_sql(), 'open'))
    conn.commit()
    conn.close()

def get_availability():
    if not os.path.exists(AVAILABILITY_FILE):
        return 'available'
    with open(AVAILABILITY_FILE,'r') as f:
        return f.read().strip()

def set_availability(state):
    with open(AVAILABILITY_FILE,'w') as f:
        f.write(state)

def detect_language(text):
    if not text:
        return 'en'
    marathi_chars = sum(1 for c in text if '\u0900' <= c <= '\u097F')
    english_chars = sum(1 for c in text if c.isalpha() and ('a' <= c.lower() <= 'z'))
    if marathi_chars >= 3:
        return 'mr'
    elif english_chars >= 3:
        return 'en'
    else:
        marathi_keywords = ['काय','कसे','हय','कधी','धन्यवाद','होय','नमस्कार','संगी']
        txt_lower = text.lower()
        for kw in marathi_keywords:
            if kw in txt_lower:
                return 'mr'
        return 'en'

def sakhi_intro(lang='en'):
    if lang == 'mr':
        return f"नमस्कार — मी {SAKHII_NAME}, सोहमची सहाय्यक."
    return f"Hi — I’m {SAKHII_NAME}, Soham's assistant."

def auto_reply_template(lang='en'):
    if lang == 'mr':
        return (sakhi_intro('mr') + " सोहम सध्या उपलब्ध नाही (बिझी आहे किंवा विश्रांती घेत आहे). "
                "मी लगेच मदत करू शकते, मेसेज नोंदवू शकते, किंवा काही तातडीचे असल्यास मार्क करू शकते. "
                "तातडीसाठी 'URGENT' लिहा. सोहमसाठी मेसेज देण्यासाठी 'Tell Soham:' ने सुरू करा.")
    return (sakhi_intro('en') + " Soham is currently unavailable. I can help with quick questions, take notes, or mark something as urgent for him. "
            "If it’s urgent, reply with 'URGENT'. To leave a message for Soham, start with 'Tell Soham:'.")

def task_saved_template(lang='en'):
    if lang == 'mr':
        return "लक्षात घेतले! हा मेसेज मी सोहमसाठी टास्क म्हणून सेव्ह केला आहे."
    return "Noted! I’ve saved this as a task for Soham."

def media_received_template(lang='en'):
    if lang == 'mr':
        return "मी तुम्हाला पाठवलेले मीडिया सेव्ह केले आहे आणि सोहमला दाखवीन."
    return "I’ve received the media and saved it for Soham."

def chat_mode_template(lang='en'):
    if lang == 'mr':
        return "मी सखी. सोहम उपलब्ध नसताना मी तुम्हाला मदत करू शकते. सांगू का, तुम्हाला काय हवं आहे?"
    return "I’m Sakhii. I can assist you while Soham is unavailable. What would you like to do?"

def call_fallback_template(lang='en'):
    if lang == 'mr':
        return (sakhi_intro('mr') + " सोहमला हा कॉल आला होता पण तो घेऊ शकला नाही. "
                "तुम्हाला पुढे काय करायचं आहे?\n1) व्हॉईस मेसेज (REPLY: VOICE)\n2) टेक्स्ट नोट (REPLY: TEXT)\n3) कॉलबॅक (REPLY: CALLBACK)")
    return (sakhi_intro('en') + " Soham received a call from this number but couldn’t pick up. You may:\n"
            "1) Send a voice message (reply VOICE)\n2) Send a text note (reply TEXT)\n3) Request a callback (reply CALLBACK)")

def detect_task(text):
    if not text:
        return False
    text_l = text.lower()
    triggers = ['tell soham', 'tell him', 'tell soham to', 'important', 'urgent', 'please inform', 'please tell']
    for t in triggers:
        if t in text_l:
            return True
    if re.search(r'\b(call|meeting|interview|urgent)\b', text_l):
        return True
    return False

def _normalize_whatsapp(num):
    if not num:
        return None
    if num.startswith('whatsapp:'):
        return num
    num = num.strip()
    if num.startswith('+'):
        return f'whatsapp:{num}'
    return f'whatsapp:{num}'

def send_whatsapp(to, body):
    to = _normalize_whatsapp(to)
    from_ = WHATSAPP_FROM
    if not to or not from_:
        print("send_whatsapp: missing 'to' or WHATSAPP_FROM'")
        return None
    client = get_twilio_client()
    if client is None:
        return None
    try:
        return client.messages.create(body=body, from_=from_, to=to)
    except Exception as e:
        print("Twilio send error:", e)
        return None

@app.route('/webhook', methods=['POST'])
def webhook():
    from_num = request.values.get('From')
    to_num = request.values.get('To')
    body = request.values.get('Body', '').strip()
    num_media = int(request.values.get('NumMedia', '0'))
    media_urls = []
    for i in range(num_media):
        m = request.values.get(f'MediaUrl{i}')
        if m:
            media_urls.append(m)
    media = ','.join(media_urls) if media_urls else None

    if from_num and not from_num.startswith('whatsapp:'):
        from_num = _normalize_whatsapp(from_num)

    lang = get_user_language(from_num)
    if not lang:
        lang = detect_language(body)
        save_user_language(from_num, lang)

    urgent = 1 if detect_task(body) else 0
    save_message(from_num, to_num, body, media, 'in', urgent)

    state = get_availability()
    if state != 'available':
        send_whatsapp(from_num, auto_reply_template(lang))
        if media:
            send_whatsapp(from_num, media_received_template(lang))
        if urgent:
            save_task(from_num, body)
            send_whatsapp(from_num, task_saved_template(lang))
        low = body.lower()
        if low.startswith(('hi','hello','hey','नमस्कार','हाय','हॅलो')) or 'how are' in low:
            send_whatsapp(from_num, chat_mode_template(lang))
    else:
        summary = f"Message from {from_num}: {body[:300]}"
        send_whatsapp(ADMIN_WHATSAPP, summary)

    return ('', 200)

@app.route('/voice-webhook', methods=['POST'])
def voice_webhook():
    call_status = request.values.get('CallStatus', '').lower()
    from_num = request.values.get('From')
    if from_num and not from_num.startswith('whatsapp:'):
        caller_whatsapp = _normalize_whatsapp(from_num)
    else:
        caller_whatsapp = from_num

    if call_status in ('no-answer', 'busy', 'failed'):
        lang = get_user_language(caller_whatsapp) or detect_language('')
        send_whatsapp(caller_whatsapp, call_fallback_template(lang))
        save_message(caller_whatsapp, WHATSAPP_FROM, f"Call fallback: {call_status}", None, 'in', urgent=1)
    return ('', 200)

@app.route('/set_status', methods=['POST'])
def set_status():
    payload = request.get_json(silent=True) or {}
    new_state = payload.get('state')
    if new_state not in ('available','busy','sleeping'):
        return jsonify({'error':'invalid'}), 400
    set_availability(new_state)
    if new_state == 'available':
        send_summary_to_admin()
    return jsonify({'ok':True})

def send_summary_to_admin():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM messages WHERE ts >= datetime("now", "-1 day")')
    total_row = cur.fetchone()
    total = total_row[0] if total_row else 0
    cur.execute('SELECT COUNT(*) FROM tasks WHERE status="open"')
    tasks_open = cur.fetchone()[0]
    cur.execute('SELECT from_num, COUNT(*) c FROM messages GROUP BY from_num ORDER BY c DESC LIMIT 5')
    top = cur.fetchall()
    top_lines = '\n'.join([f"{r[0]} ({r[1]})" for r in top])
    msg = f"Soham — you are now available.\nMessages last 24h: {total}\nOpen tasks: {tasks_open}\nTop contacts:\n{top_lines}"
    send_whatsapp(ADMIN_WHATSAPP, msg)
    conn.close()

@app.route('/health', methods=['GET'])
def health():
    return "OK", 200

# --- init on import ---
init_db()
if not os.path.exists(AVAILABILITY_FILE):
    set_availability('available')

print("Sakhii: DB initialized and availability ensured. Starting app...")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
