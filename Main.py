#!/usr/bin/env python3
import os, sqlite3, secrets, smtplib, base64
from email.message import EmailMessage
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from flask import Flask, request, redirect, url_for, make_response, render_template_string, abort, send_from_directory, session
from dotenv import load_dotenv

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

# Allow HTTP for local development
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()
APP_TITLE = "User Study Booking"
TZ = ZoneInfo("America/Toronto")
PORT = int(os.getenv("PORT", "5000"))
HOST_BASE = "https://uncut-jocelynn-pronunciative.ngrok-free.dev"      # Hardcoded ngrok URL
CALENDAR_ID = os.getenv("CALENDAR_ID", "")                          # e.g. your_shared_calendar_id@group.calendar.google.com
OAUTH_CLIENT_JSON = os.getenv("GOOGLE_CLIENT_SECRETS", "credentials.json")
TOKEN_JSON = os.getenv("GOOGLE_TOKEN", "token.json")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CLIENT_SECRETS_JSON", "")                 # saved after first auth
# Optional SMTP for the **initial email** with booking link (Calendar invite emails come from Google)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "User Study <no-reply@example.com>")

SCOPES = ["https://www.googleapis.com/auth/calendar"]

DBPATH = os.getenv("DB_PATH", "study.db")
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "uploads")
ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx'}

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", secrets.token_hex(16))
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB limit

# Create uploads directory
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# DB
# ──────────────────────────────────────────────────────────────────────────────
def db():
    conn = sqlite3.connect(DBPATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            k TEXT PRIMARY KEY,
            v TEXT
        );
        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS consent_files (
            id INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY,
            participant_id INTEGER NOT NULL,
            preference1_start TEXT NOT NULL,
            preference1_end TEXT NOT NULL,
            preference2_start TEXT,
            preference2_end TEXT,
            preference3_start TEXT,
            preference3_end TEXT,
            selected_start_time TEXT NULL,
            selected_end_time TEXT NULL,
            status TEXT DEFAULT 'pending',
            calendar_event_id TEXT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            admin_confirmed_at TIMESTAMP NULL,
            FOREIGN KEY (participant_id) REFERENCES participants (id)
        );
        CREATE TABLE IF NOT EXISTS blocked_slots (
            id INTEGER PRIMARY KEY,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # defaults
        if not con.execute("SELECT 1 FROM settings WHERE k='email_body'").fetchone():
            con.execute("INSERT INTO settings(k,v) VALUES(?,?)",
                        ("email_body",
                         "Hi {{name}},\\n\\nThank you for volunteering to participate in our user study at Synlab (Toronto Metropolitan University)!\\n\\nWe are conducting a user study under the supervision of Professor Ali Mazalek to evaluate the efficiency of tangibles. Your participation will help advance our research.\\n\\nPlease access our interactive calendar and select your preferred time slot:\\n\\nCALENDAR LINK: {{link}}\\n\\nThis will take you to our availability calendar where you can see all open time slots."))
        if not con.execute("SELECT 1 FROM settings WHERE k='consent_html'").fetchone():
            con.execute("INSERT INTO settings(k,v) VALUES(?,?)",
                        ("consent_html",
                         "<h2>Consent Form</h2><p>Please read this consent carefully before booking. You agree to participate voluntarily. Contact us with any questions.</p>"))

        # Update existing email template to include Synlab branding
        con.execute("UPDATE settings SET v=? WHERE k='email_body'",
                   ("Hi {{name}},\\n\\nThank you for volunteering to participate in our user study at Synlab (Toronto Metropolitan University)!\\n\\nWe are conducting a user study under the supervision of Professor Ali Mazalek to evaluate the efficiency of tangibles. Your participation will help advance our research.\\n\\nAs compensation for your participation, you will receive a $15 CAD Amazon gift card upon completion of the study.\\n\\nPlease access our interactive calendar and select your preferred time slots (choose 3 options):\\n\\nCALENDAR LINK: {{link}}\\n\\nThis will take you to our availability calendar where you can select up to 3 preferred time slots.",))

        # Migrate existing bookings table to new schema
        try:
            # Check if old columns exist
            old_columns = con.execute("PRAGMA table_info(bookings)").fetchall()
            old_column_names = [col[1] for col in old_columns]

            if 'start_time' in old_column_names and 'preference1_start' not in old_column_names:
                # Backup old data
                old_bookings = con.execute("SELECT * FROM bookings").fetchall()

                # Drop old table and recreate with new schema
                con.execute("DROP TABLE bookings")
                con.execute("""
                CREATE TABLE bookings (
                    id INTEGER PRIMARY KEY,
                    participant_id INTEGER NOT NULL,
                    preference1_start TEXT NOT NULL,
                    preference1_end TEXT NOT NULL,
                    preference2_start TEXT,
                    preference2_end TEXT,
                    preference3_start TEXT,
                    preference3_end TEXT,
                    selected_start_time TEXT NULL,
                    selected_end_time TEXT NULL,
                    status TEXT DEFAULT 'pending',
                    calendar_event_id TEXT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    admin_confirmed_at TIMESTAMP NULL,
                    FOREIGN KEY (participant_id) REFERENCES participants (id)
                )
                """)

                # Migrate old data (convert single booking to preference1)
                for booking in old_bookings:
                    con.execute("""
                    INSERT INTO bookings (id, participant_id, preference1_start, preference1_end,
                                        selected_start_time, selected_end_time, status, calendar_event_id,
                                        created_at, admin_confirmed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (booking[0], booking[1], booking[2], booking[3], booking[2], booking[3],
                          booking[4], booking[5], booking[6], booking[7]))
        except Exception as e:
            print(f"[DB MIGRATION] {e}")  # Non-fatal migration error

init_db()

def get_setting(key):
    with db() as con:
        r = con.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
        return r["v"] if r else ""

def set_setting(key, val):
    with db() as con:
        con.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, val))

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_consent_files():
    with db() as con:
        return con.execute("SELECT * FROM consent_files ORDER BY upload_date DESC").fetchall()

def get_pending_bookings():
    with db() as con:
        return con.execute("""
            SELECT b.*, p.name, p.email
            FROM bookings b
            JOIN participants p ON b.participant_id = p.id
            WHERE b.status = 'pending'
            ORDER BY b.created_at ASC
        """).fetchall()

def send_confirmation_email(to_email, to_name, start_time, end_time):
    start_dt = datetime.fromisoformat(start_time)
    end_dt = datetime.fromisoformat(end_time)
    start_str = start_dt.strftime('%a %b %d, %I:%M %p').replace(' 0', ' ')
    end_str = end_dt.strftime('%I:%M %p').replace(' 0', ' ')

    body = f"""
Hi {to_name},

Great news! Your user study booking has been CONFIRMED by our admin team.

📅 CONFIRMED APPOINTMENT:
{start_str} – {end_str} (Toronto time)

✅ WHAT'S NEXT:
• You'll receive a Google Calendar invitation shortly
• The event will be automatically added to your calendar
• You'll get email reminders before your session

📍 STUDY DETAILS:
All study details, including location or meeting link, will be included in your calendar invitation.

If you have any questions or need to reschedule, please reply to this email.

Thank you for participating in our research!

Best regards,
The Research Team

---
User Study Booking System
"""

    subject = "✅ User Study Booking CONFIRMED - Calendar Invite Coming Soon"

    # Try Gmail API first, fallback to SMTP
    result = send_email_with_gmail_api(to_email, to_name, subject, body)

    if result == "SUCCESS":
        return result

    # Fallback to SMTP if Gmail API fails
    print(f"[CONFIRMATION EMAIL] Gmail API failed, trying SMTP fallback...")

    if not SMTP_HOST:
        print(f"[DRY-RUN CONFIRMATION EMAIL] To: {to_name} <{to_email}>\n{body}\n")
        return "DRY-RUN: No SMTP configured"

    try:
        print(f"[CONFIRMATION EMAIL] Attempting to send confirmation via SMTP to {to_email}")
        msg = EmailMessage()
        msg["From"] = SMTP_FROM
        msg["To"] = f"{to_name} <{to_email}>"
        msg["Subject"] = subject
        msg.set_content(body)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            print(f"[CONFIRMATION EMAIL] Connected to SMTP server")
            s.starttls()
            print(f"[CONFIRMATION EMAIL] STARTTLS enabled")
            s.login(SMTP_USER, SMTP_PASS)
            print(f"[CONFIRMATION EMAIL] Logged in successfully")
            s.send_message(msg)
            print(f"[CONFIRMATION EMAIL] Confirmation email sent successfully to {to_email}")
        return "SUCCESS"
    except smtplib.SMTPAuthenticationError as e:
        error_msg = f"SMTP Authentication failed: {str(e)}"
        print(f"[CONFIRMATION EMAIL ERROR] {error_msg}")
        return f"ERROR: {error_msg}"
    except smtplib.SMTPException as e:
        error_msg = f"SMTP error: {str(e)}"
        print(f"[CONFIRMATION EMAIL ERROR] {error_msg}")
        return f"ERROR: {error_msg}"
    except Exception as e:
        error_msg = f"General error: {str(e)}"
        print(f"[CONFIRMATION EMAIL ERROR] {error_msg}")
        return f"ERROR: {error_msg}"

# Simple username/password authentication
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "password123")

def update_env_with_user_email(user_email):
    """Update .env file with user's email for SMTP"""
    try:
        env_path = ".env"
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                content = f.read()

            # Update SMTP_USER and SMTP_FROM
            import re
            content = re.sub(r'SMTP_USER=.*', f'SMTP_USER={user_email}', content)
            content = re.sub(r'SMTP_FROM=.*', f'SMTP_FROM=User Study <{user_email}>', content)

            with open(env_path, 'w') as f:
                f.write(content)

            print(f"[INFO] Updated .env with user email: {user_email}")
    except Exception as e:
        print(f"[ERROR] Failed to update .env: {e}")

def require_auth(f):
    def decorated_function(*args, **kwargs):
        if 'authenticated' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

# ──────────────────────────────────────────────────────────────────────────────
# Google Calendar auth + service
# ──────────────────────────────────────────────────────────────────────────────
def have_token():
    return os.path.exists(TOKEN_JSON)

def get_creds():
    print(f"[GET_CREDS] Checking for credentials...")

    # Try to get token from environment first
    token_data = os.getenv("GOOGLE_TOKEN_JSON", "")
    if token_data:
        try:
            import json
            token_info = json.loads(token_data)
            print(f"[GET_CREDS] Found environment token with scopes: {token_info.get('scopes', 'NONE')}")
            # Don't use SCOPES constant, use the scopes from the saved token
            creds = Credentials.from_authorized_user_info(token_info)
            return creds
        except Exception as e:
            print(f"[GET_CREDS] Environment token error: {e}")
            pass

    # Fallback to file
    if os.path.exists(TOKEN_JSON):
        print(f"[GET_CREDS] Found token file")
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_JSON)
            print(f"[GET_CREDS] File token scopes: {creds.scopes}")
            return creds
        except Exception as e:
            print(f"[GET_CREDS] File token error: {e}")

    print(f"[GET_CREDS] No valid credentials found")
    return None

def save_creds(creds: Credentials):
    with open(TOKEN_JSON, "w") as f:
        f.write(creds.to_json())

def calendar_service():
    creds = get_creds()
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh()  # google-auth handles via Request auto; not needed with discovery v2
        else:
            raise RuntimeError("Google token missing. Visit /google-auth to connect.")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def to_iso_utc(dt_local: datetime):
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=TZ)
    return dt_local.astimezone(timezone.utc).replace(microsecond=0).isoformat()

def parse_iso(s):  # RFC3339 -> aware datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def slot_range_for_day(day_local: datetime):
    """Yield (start_local, end_local) 1-hour slots 09:00..01:00 (last start 24:00)."""
    base = day_local.replace(hour=9, minute=0, second=0, microsecond=0, tzinfo=TZ)
    for h in range(0, 16):  # 9..24 inclusive
        start = base + timedelta(hours=h)
        end = start + timedelta(hours=1)
        yield start, end

def freebusy_blocks(service, start_utc_iso, end_utc_iso):
    body = {
        "timeMin": start_utc_iso,
        "timeMax": end_utc_iso,
        "timeZone": "UTC",
        "items": [{"id": CALENDAR_ID}],
    }
    fb = service.freebusy().query(body=body).execute()
    return fb["calendars"][CALENDAR_ID]["busy"]  # list of {start,end}

def is_free(service, start_local: datetime, end_local: datetime):
    # Check if slot is blocked by admin
    with db() as con:
        blocked = con.execute(
            "SELECT 1 FROM blocked_slots WHERE start_time = ? AND end_time = ?",
            (start_local.isoformat(), end_local.isoformat())
        ).fetchone()
        if blocked:
            return False

    # Query only the 2h window around it for speed
    min_iso = to_iso_utc(start_local - timedelta(minutes=1))
    max_iso = to_iso_utc(end_local + timedelta(minutes=1))
    for b in freebusy_blocks(service, min_iso, max_iso):
        bstart = parse_iso(b["start"])
        bend = parse_iso(b["end"])
        if not (end_local <= bstart.astimezone(TZ) or start_local >= bend.astimezone(TZ)):
            return False
    return True

def send_email_with_gmail_api(to_email, to_name, subject, body):
    """Send email using Gmail API instead of SMTP"""
    try:
        # Get credentials for Gmail API
        creds = get_creds()
        if not creds or not creds.valid:
            print("[GMAIL API] No valid credentials available")
            return "ERROR: Gmail API credentials not available - please sign in with Google"

        # Check if we have Gmail scope
        if not creds.scopes or 'https://www.googleapis.com/auth/gmail.send' not in creds.scopes:
            print(f"[GMAIL API] Missing Gmail send scope. Current scopes: {creds.scopes}")
            return "ERROR: Gmail send permission not granted - please re-authenticate with Google"

        # Build Gmail service
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)

        # Create message
        message = MIMEText(body)
        message['to'] = f"{to_name} <{to_email}>"
        message['from'] = SMTP_FROM
        message['subject'] = subject

        # Encode message
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()

        # Send email
        print(f"[GMAIL API] Attempting to send email to {to_email}")
        print(f"[GMAIL API] Message size: {len(raw_message)} chars")

        result = service.users().messages().send(
            userId='me',
            body={'raw': raw_message}
        ).execute()

        print(f"[GMAIL API] Email sent successfully. Message ID: {result.get('id')}")
        return "SUCCESS"

    except Exception as e:
        import traceback
        error_str = str(e)
        if not error_str.strip():
            error_str = f"Empty error from {type(e).__name__}"
        error_msg = f"Gmail API error: {error_str}"
        full_traceback = traceback.format_exc()
        print(f"[GMAIL API ERROR] {error_msg}")
        print(f"[GMAIL API ERROR TYPE] {type(e)}")
        print(f"[GMAIL API ERROR ARGS] {e.args}")
        print(f"[GMAIL API ERROR TRACEBACK] {full_traceback}")
        return f"ERROR: {error_msg}"

def send_initial_email(to_email, to_name, link):
    """Send email using simple method that works"""
    body_tpl = get_setting("email_body")
    body = body_tpl.replace("{{name}}", to_name).replace("{{link}}", link)

    # Enhanced email body with calendar selection info
    enhanced_body = f"""
Hi {to_name},

Thank you for volunteering to participate in our user study at Synlab (Toronto Metropolitan University)!

We are conducting a user study under the supervision of Professor Ali Mazalek to evaluate the efficiency of tangibles with an interactive AR app on Meta Quest headset. Your participation will help advance our research and you will receive a $15 Amazon gift card for your time.

Please access our interactive calendar and select your preferred time slot:

CALENDAR LINK: {link}

This will take you to our availability calendar where you can see all open time slots.

📅 INTERACTIVE CALENDAR SELECTION:
• Click the link above to view our availability calendar
• See all available time slots from 9:00 AM to 9:00 PM (Toronto time)
• Select your preferred 1-hour time slot directly on the calendar
• Available slots are shown hour by hour for the next 2 weeks

🔄 CONFIRMATION PROCESS:
• After selecting your slot, your request will be reviewed by our admin team
• You'll receive a confirmation email within 24 hours
• Once approved, you'll automatically get a Google Calendar invitation
• The event will be added to your calendar with all study details

❓ QUESTIONS?
Reply to this email if you need assistance.

---
Synlab - Toronto Metropolitan University
Professor Ali Mazalek
"""

    subject = "Research Participation Invitation - Synlab TMU (Prof. Ali Mazalek)"

    # Send email directly using SMTP
    print(f"[INITIAL EMAIL] Attempting to send via SMTP to {to_email}")

    if not SMTP_HOST:
        print(f"[EMAIL FALLBACK] No SMTP configured, logging email for manual sending")
    else:
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart

            # Create email
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = f"Prof. Ali Mazalek - Synlab TMU <{SMTP_USER}>"
            msg['To'] = f"{to_name} <{to_email}>"

            # Plain text part
            text_part = MIMEText(enhanced_body, 'plain')
            msg.attach(text_part)

            # HTML part with clickable link
            html_body = enhanced_body.replace(f"CALENDAR LINK: {link}",
                f'<p><strong>CALENDAR LINK:</strong> <a href="{link}" style="color: #0066cc; text-decoration: underline;">{link}</a></p>')
            html_part = MIMEText(html_body.replace('\n', '<br>'), 'html')
            msg.attach(html_part)

            # Send email
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)

            print(f"[INITIAL EMAIL] Email sent successfully via SMTP to {to_email}")
            return "SUCCESS"

        except Exception as e:
            print(f"[INITIAL EMAIL ERROR] SMTP failed: {str(e)}")

    # BACKUP: Simple email logging that always works
    print(f"[EMAIL FALLBACK] Logging email for manual sending")
    # Remove emojis for Windows console compatibility
    clean_subject = subject.encode('ascii', 'ignore').decode('ascii')
    clean_body = enhanced_body.encode('ascii', 'ignore').decode('ascii')
    print(f"""
=== EMAIL TO SEND MANUALLY ===
To: {to_name} <{to_email}>
Subject: {clean_subject}

{clean_body}

Booking Link: {link}
===============================
""")

    return "SUCCESS"

# ──────────────────────────────────────────────────────────────────────────────
# Authentication Routes
# ──────────────────────────────────────────────────────────────────────────────
LOGIN_HTML = """
<!doctype html><meta charset="utf-8">
<title>Login - {{title}}</title>
<style>
 * { box-sizing: border-box; }
 body {
   font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, system-ui, sans-serif;
   background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
   min-height: 100vh; display: flex; align-items: center; justify-content: center;
   margin: 0; padding: 20px;
 }
 .login-card {
   background: white; border-radius: 16px; padding: 40px; width: 100%; max-width: 400px;
   box-shadow: 0 20px 40px rgba(0,0,0,0.1); text-align: center;
 }
 h1 { margin: 0 0 30px; color: #2d3748; }
 .form-group { margin: 20px 0; text-align: left; }
 label { display: block; margin-bottom: 8px; color: #4a5568; font-weight: 600; }
 input[type="text"], input[type="password"] {
   width: 100%; padding: 12px 16px; border: 2px solid #e2e8f0; border-radius: 10px;
   font-size: 16px; transition: all 0.2s; font-family: inherit;
 }
 input[type="text"]:focus, input[type="password"]:focus {
   outline: none; border-color: #667eea; box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
 }
 .login-btn {
   width: 100%; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
   color: white; border: none; padding: 12px 24px; border-radius: 10px;
   cursor: pointer; font-weight: 600; font-size: 16px; transition: all 0.2s;
   font-family: inherit; margin: 20px 0;
 }
 .login-btn:hover {
   transform: translateY(-2px); box-shadow: 0 10px 20px rgba(102, 126, 234, 0.3);
 }
 .google-btn {
   display: flex; align-items: center; justify-content: center; gap: 12px;
   width: 100%; background: white; border: 2px solid #e2e8f0; border-radius: 10px;
   padding: 12px 24px; font-size: 16px; font-weight: 600; color: #2d3748;
   text-decoration: none; transition: all 0.2s; margin: 10px 0;
 }
 .google-btn:hover {
   border-color: #4285f4; box-shadow: 0 4px 12px rgba(66, 133, 244, 0.2);
   transform: translateY(-2px);
 }
 .google-icon {
   width: 20px; height: 20px;
   background: url('data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="%234285f4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/><path fill="%2334a853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="%23fbbc05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/><path fill="%23ea4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/></svg>') center/contain no-repeat;
 }
 .error { background: #fed7d7; color: #9b2c2c; padding: 15px; border-radius: 10px; margin: 15px 0; }
 .note { color: #718096; font-size: 14px; margin-top: 20px; line-height: 1.4; }
 .divider { margin: 30px 0; text-align: center; color: #718096; position: relative; }
 .divider:before { content: ''; position: absolute; top: 50%; left: 0; right: 0; height: 1px; background: #e2e8f0; }
 .divider span { background: white; padding: 0 20px; }
</style>
<div class="login-card">
  <h1>🔐 Admin Login</h1>

  {% if error %}
  <div class="error">❌ {{error}}</div>
  {% endif %}

  <a href="/google-login" class="google-btn">
    <div class="google-icon"></div>
    Sign in with Google (Recommended)
  </a>

  <div class="divider"><span>OR</span></div>

  <form method="post" action="/login">
    <div class="form-group">
      <label for="username">Username</label>
      <input type="text" id="username" name="username" required>
    </div>

    <div class="form-group">
      <label for="password">Password</label>
      <input type="password" id="password" name="password" required>
    </div>

    <button type="submit" class="login-btn">Sign In with Password</button>
  </form>

  <div class="note">
    <strong>Email Setup Required</strong><br>
    Google sign-in is required for email functionality. Username/password login requires manual email sending.
  </div>
</div>
"""

@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['authenticated'] = True
            session['user_email'] = SMTP_USER  # Use the configured email
            session['user_name'] = "Admin"
            session['user_org'] = "Admin"
            return redirect(url_for('admin'))
        else:
            error = "Invalid username or password."

    if request.args.get('error') == 'auth_failed':
        error = "Authentication failed. Please try again."
    return render_template_string(LOGIN_HTML, title=APP_TITLE, error=error)

@app.get("/google-login")
def google_login():
    # Use the exact scopes that Google returns
    admin_scopes = [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "openid"
    ]

    # Create Google OAuth flow
    print(f"[OAUTH] Requesting scopes: {admin_scopes}")
    try:
        if GOOGLE_CREDENTIALS_JSON:
            try:
                import json
                client_config = json.loads(GOOGLE_CREDENTIALS_JSON)
                flow = Flow.from_client_config(
                    client_config,
                    scopes=admin_scopes,
                    redirect_uri=f"{HOST_BASE}/oauth2callback",
                )
            except json.JSONDecodeError as json_err:
                print(f"[OAUTH WARNING] Invalid JSON in GOOGLE_CLIENT_SECRETS_JSON: {json_err}")
                print(f"[OAUTH INFO] Falling back to credentials file: {OAUTH_CLIENT_JSON}")
                flow = Flow.from_client_secrets_file(
                    OAUTH_CLIENT_JSON,
                    scopes=admin_scopes,
                    redirect_uri=f"{HOST_BASE}/oauth2callback",
                )
        else:
            flow = Flow.from_client_secrets_file(
                OAUTH_CLIENT_JSON,
                scopes=admin_scopes,
                redirect_uri=f"{HOST_BASE}/oauth2callback",
            )
    except Exception as e:
        print(f"[OAUTH ERROR] Failed to create OAuth flow: {e}")
        return f"OAuth configuration error: {str(e)}", 500

    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="false",  # Force fresh scope request
        prompt="consent",
    )

    session['oauth_state'] = state
    session['auth_type'] = 'admin'
    return redirect(auth_url)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.get("/reset-auth")
def reset_auth():
    """Reset Google authentication to force re-authentication with all scopes"""
    # Delete token file to force re-authentication
    if os.path.exists(TOKEN_JSON):
        os.remove(TOKEN_JSON)
        print("[RESET AUTH] Deleted token file")

    # Clear any environment token data
    if "GOOGLE_TOKEN_JSON" in os.environ:
        os.environ.pop("GOOGLE_TOKEN_JSON", None)
        print("[RESET AUTH] Cleared environment token")

    # Clear any cached credentials and session data
    session.clear()
    print("[RESET AUTH] Cleared all session data")

    # Force logout and re-login with Google to get fresh scopes
    return redirect(url_for('google_login'))

@app.get("/clear-env-token")
def clear_env_token():
    """Clear environment token to force file-based token"""
    if "GOOGLE_TOKEN_JSON" in os.environ:
        del os.environ["GOOGLE_TOKEN_JSON"]
        print("[CLEAR ENV] Removed GOOGLE_TOKEN_JSON from environment")
        return "Environment token cleared. <a href='/debug'>Check debug</a> | <a href='/admin'>Go to admin</a>"
    else:
        return "No environment token found. <a href='/debug'>Check debug</a> | <a href='/admin'>Go to admin</a>"

@app.get("/force-gmail-auth")
def force_gmail_auth():
    """Force Gmail authentication with explicit scope"""
    # Clear everything first - file, environment, and session
    if os.path.exists(TOKEN_JSON):
        os.remove(TOKEN_JSON)
        print("[FORCE GMAIL AUTH] Deleted token file")

    if "GOOGLE_TOKEN_JSON" in os.environ:
        del os.environ["GOOGLE_TOKEN_JSON"]
        print("[FORCE GMAIL AUTH] Cleared environment token")

    session.clear()
    print("[FORCE GMAIL AUTH] Cleared session")

    # Explicit Gmail + Calendar scopes
    gmail_scopes = [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "openid"
    ]

    print(f"[FORCE GMAIL AUTH] Requesting scopes: {gmail_scopes}")

    try:
        if GOOGLE_CREDENTIALS_JSON:
            try:
                import json
                client_config = json.loads(GOOGLE_CREDENTIALS_JSON)
                flow = Flow.from_client_config(
                    client_config,
                    scopes=gmail_scopes,
                    redirect_uri=f"{HOST_BASE}/oauth2callback",
                )
            except json.JSONDecodeError:
                flow = Flow.from_client_secrets_file(
                    OAUTH_CLIENT_JSON,
                    scopes=gmail_scopes,
                    redirect_uri=f"{HOST_BASE}/oauth2callback",
                )
        else:
            flow = Flow.from_client_secrets_file(
                OAUTH_CLIENT_JSON,
                scopes=gmail_scopes,
                redirect_uri=f"{HOST_BASE}/oauth2callback",
            )

        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="false",
            prompt="consent",
        )

        session['oauth_state'] = state
        session['auth_type'] = 'admin'
        return redirect(auth_url)

    except Exception as e:
        return f"Error setting up Gmail authentication: {str(e)}", 500

@app.get("/")
def index():
    if 'authenticated' in session:
        return redirect(url_for('admin'))
    return redirect(url_for('login'))

@app.get("/debug")
def debug():
    """Debug endpoint to check configuration"""
    debug_info = {
        "HOST_BASE": HOST_BASE,
        "CALENDAR_ID": CALENDAR_ID[:20] + "..." if CALENDAR_ID else "NOT SET",
        "SMTP_HOST": SMTP_HOST,
        "GOOGLE_CREDENTIALS_JSON": "SET" if GOOGLE_CREDENTIALS_JSON else "NOT SET",
        "OAUTH_CLIENT_JSON": "EXISTS" if os.path.exists(OAUTH_CLIENT_JSON) else "MISSING",
        "TOKEN_JSON": "EXISTS" if os.path.exists(TOKEN_JSON) else "MISSING"
    }

    # Check current credentials
    try:
        creds = get_creds()
        if creds:
            debug_info["CREDENTIALS_VALID"] = "YES" if creds.valid else "NO (expired)"
            debug_info["CREDENTIALS_SCOPES"] = ", ".join(creds.scopes) if creds.scopes else "NONE"
            debug_info["HAS_GMAIL_SCOPE"] = "YES" if creds.scopes and 'https://www.googleapis.com/auth/gmail.send' in creds.scopes else "NO"
        else:
            debug_info["CREDENTIALS_VALID"] = "NO CREDENTIALS"
    except Exception as e:
        debug_info["CREDENTIALS_ERROR"] = str(e)

    # Test Gmail API with your own email
    try:
        test_email = session.get('user_email', 'mo.amin797@gmail.com')  # Use actual email
        debug_info["GMAIL_TEST_EMAIL"] = test_email

        # Test Gmail service creation first
        try:
            creds = get_creds()
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            debug_info["GMAIL_SERVICE"] = "OK"

            # Test simple API call
            profile = service.users().getProfile(userId='me').execute()
            debug_info["GMAIL_PROFILE"] = f"Email: {profile.get('emailAddress', 'NONE')}"

            # Now test actual email sending
            result = send_email_with_gmail_api(test_email, "Test User", "Test Subject", "Test Body")
            debug_info["GMAIL_TEST"] = result

        except Exception as service_error:
            debug_info["GMAIL_SERVICE_ERROR"] = str(service_error)
            debug_info["GMAIL_TEST"] = f"Service creation failed: {str(service_error)}"

    except Exception as e:
        debug_info["GMAIL_TEST_ERROR"] = str(e)

    return f"""
    <h2>Debug Information</h2>
    <pre>{chr(10).join(f"{k}: {v}" for k, v in debug_info.items())}</pre>
    <p><a href="/login">Go to Login</a></p>
    <p><a href="/admin">Go to Admin</a></p>
    """

# ──────────────────────────────────────────────────────────────────────────────
# Routes: Google OAuth
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/google-auth")
@require_auth
def google_auth():
    # For calendar-only auth after admin login
    user_email = session.get('user_email', '')
    user_domain = user_email.split('@')[-1] if '@' in user_email else 'torontomu.ca'

    try:
        if GOOGLE_CREDENTIALS_JSON:
            try:
                import json
                client_config = json.loads(GOOGLE_CREDENTIALS_JSON)
                flow = Flow.from_client_config(
                    client_config,
                    scopes=SCOPES,
                    redirect_uri=f"{HOST_BASE}/oauth2callback",
                )
            except json.JSONDecodeError as json_err:
                print(f"[OAUTH WARNING] Invalid JSON in GOOGLE_CLIENT_SECRETS_JSON: {json_err}")
                print(f"[OAUTH INFO] Falling back to credentials file: {OAUTH_CLIENT_JSON}")
                flow = Flow.from_client_secrets_file(
                    OAUTH_CLIENT_JSON,
                    scopes=SCOPES,
                    redirect_uri=f"{HOST_BASE}/oauth2callback",
                )
        else:
            flow = Flow.from_client_secrets_file(
                OAUTH_CLIENT_JSON,
                scopes=SCOPES,
                redirect_uri=f"{HOST_BASE}/oauth2callback",
            )
    except Exception as e:
        print(f"[OAUTH ERROR] Failed to create OAuth flow: {e}")
        return f"OAuth configuration error: {str(e)}", 500
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        hd=user_domain,  # Use user's organization
    )
    session['oauth_state'] = state
    session['auth_type'] = 'calendar'
    return redirect(auth_url)

@app.get("/oauth2callback")
def oauth2callback():
    stored_state = session.get('oauth_state')
    auth_type = session.get('auth_type', 'calendar')

    if not stored_state:
        return redirect(url_for('login'))

    try:
        # Determine scopes based on auth type
        if auth_type == 'admin':
            scopes = [
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile",
                "openid"
            ]
        else:
            scopes = SCOPES

        try:
            if GOOGLE_CREDENTIALS_JSON:
                try:
                    import json
                    client_config = json.loads(GOOGLE_CREDENTIALS_JSON)
                    flow = Flow.from_client_config(
                        client_config,
                        scopes=scopes,
                        redirect_uri=f"{HOST_BASE}/oauth2callback",
                    )
                except json.JSONDecodeError as json_err:
                    print(f"[OAUTH WARNING] Invalid JSON in GOOGLE_CLIENT_SECRETS_JSON: {json_err}")
                    print(f"[OAUTH INFO] Falling back to credentials file: {OAUTH_CLIENT_JSON}")
                    flow = Flow.from_client_secrets_file(
                        OAUTH_CLIENT_JSON,
                        scopes=scopes,
                        redirect_uri=f"{HOST_BASE}/oauth2callback",
                    )
            else:
                flow = Flow.from_client_secrets_file(
                    OAUTH_CLIENT_JSON,
                    scopes=scopes,
                    redirect_uri=f"{HOST_BASE}/oauth2callback",
                )
        except Exception as e:
            print(f"[OAUTH ERROR] Failed to create OAuth flow: {e}")
            return redirect(url_for("login") + "?error=oauth_config_failed")
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials

        if auth_type == 'admin':
            try:
                # Try OAuth2 API first (more reliable)
                from googleapiclient.discovery import build
                oauth_service = build('oauth2', 'v2', credentials=creds)  # Use v2 instead of v1
                user_info = oauth_service.userinfo().get().execute()
                user_email = user_info.get('email', '').lower()
                user_name = user_info.get('name', user_email.split('@')[0])
                print(f"[INFO] Got user info from OAuth API: {user_email}")
            except Exception as api_error:
                print(f"[INFO] OAuth API failed: {api_error}")
                try:
                    # Fallback: try to decode ID token
                    import jwt
                    id_token = creds.id_token
                    if id_token:
                        user_info = jwt.decode(id_token, options={"verify_signature": False})
                        user_email = user_info.get('email', '').lower()
                        user_name = user_info.get('name', user_email.split('@')[0])
                        print(f"[INFO] Got user info from ID token: {user_email}")
                    else:
                        raise Exception("No ID token")
                except Exception as token_error:
                    print(f"[INFO] ID token decode failed: {token_error}")
                    # Last resort: use a placeholder that will be obvious
                    user_email = "please-configure-gmail@example.com"
                    user_name = "Gmail User"

            # Set up admin session
            session['authenticated'] = True
            session['user_email'] = user_email
            session['user_org'] = 'Personal Gmail'
            session['user_name'] = user_name

            # Update .env with user's email for SMTP
            update_env_with_user_email(user_email)

            print(f"[SUCCESS] Admin authenticated: {user_email}")

        # Save calendar credentials
        save_creds(creds)
        print(f"[OAUTH SUCCESS] Saved credentials with scopes: {creds.scopes}")

        # Clean up OAuth session data
        session.pop('oauth_state', None)
        session.pop('auth_type', None)

        return redirect(url_for("admin"))

    except Exception as e:
        import traceback
        print(f"[AUTH ERROR] {e}")
        print(f"[AUTH ERROR TRACEBACK] {traceback.format_exc()}")
        return redirect(url_for("login") + "?error=auth_failed")

# ──────────────────────────────────────────────────────────────────────────────
# Admin UI
# ──────────────────────────────────────────────────────────────────────────────
ADMIN_HTML = """
<!doctype html><meta charset="utf-8">
<title>Admin · {{title}}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 * { box-sizing: border-box; }
 body {
   font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, system-ui, sans-serif;
   margin: 0; padding: 0; min-height: 100vh;
   background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
 }
 .container {
   max-width: 1400px; margin: 0 auto; padding: 20px;
 }
 .header {
   background: rgba(255,255,255,0.1); backdrop-filter: blur(20px);
   border-radius: 20px; padding: 30px; margin-bottom: 30px;
   color: white; display: flex; justify-content: space-between; align-items: center;
   box-shadow: 0 8px 32px rgba(0,0,0,0.1);
 }
 .header h1 {
   margin: 0; font-size: 3em; font-weight: 700;
   text-shadow: 0 2px 10px rgba(0,0,0,0.1);
 }
 .user-info {
   text-align: right; font-size: 0.95em;
   background: rgba(255,255,255,0.2); padding: 15px; border-radius: 12px;
 }
 .user-info a {
   color: #fed7d7; text-decoration: none; font-weight: 600;
   transition: color 0.2s;
 }
 .user-info a:hover { color: white; }
 .dashboard-grid {
   display: grid; grid-template-columns: 2fr 1fr; gap: 30px; margin-bottom: 30px;
 }
 .status-overview {
   background: white; border-radius: 20px; padding: 30px;
   box-shadow: 0 10px 40px rgba(0,0,0,0.1);
 }
 .status-cards {
   display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 30px;
 }
 .status-card {
   background: linear-gradient(135deg, #f8fafc 0%, #e2e8f0 100%);
   border-radius: 16px; padding: 20px; text-align: center; position: relative;
   border: 1px solid #e2e8f0; transition: all 0.3s;
 }
 .status-card:hover { transform: translateY(-5px); box-shadow: 0 15px 35px rgba(0,0,0,0.1); }
 .status-card.success { border-color: #68d391; background: linear-gradient(135deg, #f0fff4 0%, #c6f6d5 100%); }
 .status-card.warning { border-color: #fbb454; background: linear-gradient(135deg, #fffbf0 0%, #fed7aa 100%); }
 .status-card.danger { border-color: #fc8181; background: linear-gradient(135deg, #fff5f5 0%, #fed7d7 100%); }
 .status-card h3 { margin: 0 0 10px; font-size: 1.1em; color: #2d3748; }
 .status-card .value { font-size: 2.2em; font-weight: 700; color: #1a202c; margin: 10px 0; }
 .status-card .icon { position: absolute; top: 15px; right: 15px; font-size: 1.5em; opacity: 0.3; }
 .quick-actions {
   background: white; border-radius: 20px; padding: 30px;
   box-shadow: 0 10px 40px rgba(0,0,0,0.1);
 }
 .action-btn {
   display: block; width: 100%; padding: 15px; margin: 10px 0;
   background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
   color: white; border: none; border-radius: 12px; font-weight: 600;
   font-size: 1.1em; cursor: pointer; transition: all 0.3s;
   text-decoration: none; text-align: center;
 }
 .action-btn:hover { transform: translateY(-2px); box-shadow: 0 15px 30px rgba(102, 126, 234, 0.4); }
 .action-btn.secondary {
   background: linear-gradient(135deg, #718096 0%, #4a5568 100%);
 }
 .section {
   background: white; border-radius: 20px; padding: 40px; margin: 30px 0;
   box-shadow: 0 10px 40px rgba(0,0,0,0.1);
 }
 .section-title {
   font-size: 1.8em; font-weight: 700; color: #2d3748; margin: 0 0 30px;
   background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
   -webkit-background-clip: text; -webkit-text-fill-color: transparent;
 }
 .form-group { margin: 25px 0; }
 .form-label {
   display: block; font-weight: 600; margin-bottom: 8px;
   color: #4a5568; font-size: 1em;
 }
 .form-input {
   width: 100%; padding: 15px 20px; border: 2px solid #e2e8f0;
   border-radius: 12px; font-size: 16px; transition: all 0.3s;
   font-family: inherit; background: #f8fafc;
 }
 .form-input:focus {
   outline: none; border-color: #667eea; background: white;
   box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
 }
 .form-textarea {
   resize: vertical; min-height: 120px;
 }
 .btn-primary {
   background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
   color: white; border: none; padding: 15px 30px; border-radius: 12px;
   cursor: pointer; font-weight: 600; font-size: 1.1em; transition: all 0.3s;
   font-family: inherit;
 }
 .btn-primary:hover { transform: translateY(-2px); box-shadow: 0 15px 30px rgba(102, 126, 234, 0.4); }
 .btn-success { background: linear-gradient(135deg, #48bb78 0%, #38a169 100%); }
 .btn-danger { background: linear-gradient(135deg, #f56565 0%, #e53e3e 100%); }
 .booking-item {
   background: #f8fafc; border: 2px solid #e2e8f0; border-radius: 16px;
   padding: 25px; margin: 20px 0; transition: all 0.3s;
 }
 .booking-item:hover {
   border-color: #667eea; box-shadow: 0 10px 25px rgba(102, 126, 234, 0.1);
   transform: translateY(-3px);
 }
 .booking-header {
   display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;
 }
 .participant-info h4 {
   margin: 0 0 5px; font-size: 1.3em; color: #2d3748;
 }
 .participant-email {
   color: #667eea; font-weight: 600; margin: 0;
 }
 .booking-date {
   color: #718096; font-size: 0.9em; margin: 5px 0;
 }
 .preferences-grid {
   display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
   gap: 15px; margin: 20px 0;
 }
 .preference-option {
   background: white; border: 1px solid #e2e8f0; border-radius: 12px;
   padding: 15px; position: relative; transition: all 0.3s;
 }
 .preference-option:hover {
   border-color: #48bb78; box-shadow: 0 5px 15px rgba(72, 187, 120, 0.1);
 }
 .preference-label {
   font-weight: 600; color: #4a5568; font-size: 0.9em; margin-bottom: 8px;
 }
 .preference-time {
   font-size: 1.1em; color: #2d3748; margin-bottom: 15px;
 }
 .select-btn {
   background: #48bb78; color: white; border: none; padding: 8px 16px;
   border-radius: 8px; font-weight: 600; cursor: pointer; transition: all 0.2s;
   font-size: 0.9em;
 }
 .select-btn:hover { background: #38a169; transform: scale(1.05); }
 .reject-all-btn {
   background: #f56565; color: white; border: none; padding: 12px 24px;
   border-radius: 10px; font-weight: 600; cursor: pointer; transition: all 0.3s;
 }
 .reject-all-btn:hover { background: #e53e3e; transform: translateY(-2px); }
 .remove-booking-btn {
   background: #f56565; color: white; border: none; padding: 12px 20px;
   border-radius: 10px; font-weight: 600; cursor: pointer; transition: all 0.3s;
   font-size: 0.9em;
 }
 .remove-booking-btn:hover { background: #e53e3e; transform: translateY(-2px); }
 .booking-item.confirmed {
   border-left: 4px solid #48bb78; background: linear-gradient(135deg, #f0fff4 0%, #e6fffa 100%);
 }
 .booking-time {
   font-weight: 600; color: #2d3748; margin: 8px 0;
 }
 .calendar-id {
   font-size: 0.8em; color: #718096; font-family: monospace;
 }
 .success-msg {
   background: linear-gradient(135deg, #c6f6d5 0%, #9ae6b4 100%);
   color: #22543d; border-radius: 12px; padding: 20px; margin: 25px 0;
   font-weight: 600; border: 1px solid #68d391;
 }
 .participant-form {
   background: linear-gradient(135deg, #f0f9ff 0%, #e0f2fe 100%);
   border-radius: 16px; padding: 30px; margin: 20px 0;
   border: 1px solid #bae6fd;
 }
 .participants-container { margin: 20px 0; }
 .participant-row {
   background: white; border: 2px solid #e2e8f0; border-radius: 12px;
   padding: 20px; margin: 15px 0; position: relative; transition: all 0.3s;
 }
 .participant-row:hover { border-color: #667eea; box-shadow: 0 5px 15px rgba(102, 126, 234, 0.1); }
 .participant-fields {
   display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 15px;
 }
 .remove-participant {
   position: absolute; top: 15px; right: 15px; background: #f56565;
   color: white; border: none; border-radius: 50%; width: 30px; height: 30px;
   cursor: pointer; font-weight: bold; transition: all 0.2s;
 }
 .remove-participant:hover { background: #e53e3e; transform: scale(1.1); }
 .add-participant {
   background: linear-gradient(135deg, #48bb78 0%, #38a169 100%);
   color: white; border: none; padding: 12px 24px; border-radius: 12px;
   cursor: pointer; font-weight: 600; margin: 20px 0; transition: all 0.3s;
 }
 .add-participant:hover { transform: translateY(-2px); box-shadow: 0 10px 20px rgba(72, 187, 120, 0.3); }
 .batch-actions {
   background: linear-gradient(135deg, #edf2f7 0%, #e2e8f0 100%);
   border-radius: 12px; padding: 20px; margin: 30px 0; text-align: center;
   border: 1px solid #cbd5e0;
 }
 @media (max-width: 1024px) {
   .dashboard-grid { grid-template-columns: 1fr; }
   .status-cards { grid-template-columns: 1fr 1fr; }
 }
 @media (max-width: 768px) {
   .container { padding: 15px; }
   .header { flex-direction: column; text-align: center; gap: 20px; }
   .header h1 { font-size: 2.2em; }
   .status-cards { grid-template-columns: 1fr; }
   .preferences-grid { grid-template-columns: 1fr; }
   .participant-fields { grid-template-columns: 1fr; }
   .booking-header { flex-direction: column; align-items: flex-start; gap: 10px; }
 }
</style>
<body>
<div class="container">

<!-- Header -->
<div class="header">
  <h1>📅 Admin Dashboard</h1>
  <div class="user-info">
    <div style="font-size: 1.1em; margin-bottom: 5px;">👤 {{user_name or user_email}}</div>
    <div style="opacity: 0.8; margin-bottom: 8px;">{{user_email}}</div>
    <a href="/logout">🚪 Logout</a>
  </div>
</div>

<!-- Dashboard Overview -->
<div class="dashboard-grid">
  <div class="status-overview">
    <div class="status-cards">
      <div class="status-card {% if authed %}success{% else %}danger{% endif %}">
        <div class="icon">📅</div>
        <h3>Calendar Status</h3>
        <div class="value">{% if authed %}✅{% else %}❌{% endif %}</div>
        {% if not authed %}
        <a href="/google-auth" class="action-btn" style="padding: 8px 16px; margin: 10px 0;">Connect Now</a>
        {% endif %}
      </div>

      <div class="status-card {% if gmail_ready %}success{% else %}warning{% endif %}">
        <div class="icon">📧</div>
        <h3>Email System</h3>
        <div class="value">{% if gmail_ready %}✅{% else %}⚠️{% endif %}</div>
        {% if not gmail_ready %}
        <a href="/force-gmail-auth" class="action-btn" style="padding: 8px 16px; margin: 10px 0;">Fix Gmail</a>
        {% endif %}
      </div>

      <div class="status-card {% if pending_bookings %}warning{% else %}success{% endif %}">
        <div class="icon">⏳</div>
        <h3>Pending Approvals</h3>
        <div class="value">{{pending_bookings|length}}</div>
      </div>
    </div>

    <div style="background: #f8fafc; border-radius: 12px; padding: 20px; margin-top: 20px;">
      <h4 style="margin: 0 0 10px; color: #4a5568;">📋 Calendar Configuration</h4>
      <code style="font-size: 0.9em; background: #e2e8f0; padding: 8px 12px; border-radius: 6px; display: block;">{{cal_id}}</code>
    </div>
  </div>

  <div class="quick-actions">
    <h3 style="margin: 0 0 20px; color: #2d3748;">⚡ Quick Actions</h3>
    <a href="/admin/calendar" class="action-btn">📅 View Schedule Calendar</a>
    <a href="#add-participants" class="action-btn">➕ Add Participants</a>
    <a href="#email-template" class="action-btn secondary">📝 Edit Email Template</a>
    <a href="#consent-form" class="action-btn secondary">📋 Manage Consent Form</a>
    <a href="/debug" class="action-btn secondary">🐛 Debug Info</a>
  </div>
</div>

{% if success %}
<div class="success-msg">✅ {{success}}</div>
{% endif %}

<!-- Pending Bookings -->
{% if pending_bookings %}
<div class="section">
  <h2 class="section-title">⏳ Pending Booking Approvals ({{pending_bookings|length}})</h2>

  <form method="post" action="/admin/bookings">
    {% for booking in pending_bookings %}
    <div class="booking-item">
      <div class="booking-header">
        <div class="participant-info">
          <h4>{{booking.name}}</h4>
          <p class="participant-email">{{booking.email}}</p>
          <p class="booking-date">📅 Requested: {{booking.created_at_formatted}}</p>
        </div>
        <button type="submit" name="action" value="reject_{{booking.id}}" class="reject-all-btn">
          ❌ Reject All Options
        </button>
      </div>

      <div class="preferences-grid">
        {% for pref in booking.preferences %}
        <div class="preference-option">
          <div class="preference-label">Option {{pref.option_num}}</div>
          <div class="preference-time">{{pref.start_formatted}} – {{pref.end_formatted}}</div>
          <button type="submit" name="action" value="approve_{{booking.id}}_{{pref.start}}_{{pref.end}}" class="select-btn">
            ✅ Select This Time
          </button>
        </div>
        {% endfor %}
      </div>
    </div>
    {% endfor %}
  </form>
</div>
{% endif %}

<!-- Confirmed Bookings -->
{% if confirmed_bookings %}
<div class="section">
  <h2 class="section-title">✅ Confirmed Bookings ({{confirmed_bookings|length}})</h2>

  <form method="post" action="/admin/bookings">
    {% for booking in confirmed_bookings %}
    <div class="booking-item confirmed">
      <div class="booking-header">
        <div class="participant-info">
          <h4>{{booking.name}}</h4>
          <p class="participant-email">{{booking.email}}</p>
          <p class="booking-date">✅ Confirmed: {{booking.confirmed_at_formatted}}</p>
          <p class="booking-time">🕐 {{booking.start_formatted}} – {{booking.end_formatted}}</p>
          {% if booking.calendar_event_id %}
          <p class="calendar-id">📅 Calendar Event: {{booking.calendar_event_id[:20]}}...</p>
          {% endif %}
        </div>
        <button type="submit" name="action" value="remove_{{booking.id}}" class="remove-booking-btn"
                onclick="return confirm('Are you sure you want to remove this confirmed booking? This will cancel the calendar event and notify the participant.')">
          🗑️ Remove Booking
        </button>
      </div>
    </div>
    {% endfor %}
  </form>
</div>
{% endif %}

<!-- Add Participants Section -->
<div class="section" id="add-participants">
  <h2 class="section-title">👥 Add New Participants</h2>

  <div class="participant-form">
    <form method="post" action="/admin/participants/batch" id="participantForm">
      <div id="participantsContainer" class="participants-container">
        <div class="participant-row">
          <div class="participant-fields">
            <div class="form-group">
              <label class="form-label">👤 Full Name</label>
              <input name="names[]" class="form-input" required placeholder="Enter participant's full name">
            </div>
            <div class="form-group">
              <label class="form-label">📧 Email Address</label>
              <input type="email" name="emails[]" class="form-input" required placeholder="participant@email.com">
            </div>
          </div>
        </div>
      </div>

      <button type="button" class="add-participant" onclick="addParticipant()">
        ➕ Add Another Participant
      </button>

      <div class="batch-actions">
        <button type="submit" class="btn-primary">
          📤 Create All Booking Links & Send Emails
        </button>
        <p style="margin: 15px 0 0; color: #4a5568; font-size: 0.95em;">
          📧 Emails will be sent automatically to all participants
        </p>
      </div>
    </form>
  </div>
</div>

<!-- Email Template Section -->
<div class="section" id="email-template">
  <h2 class="section-title">📝 Email Template Configuration</h2>

  <form method="post" action="/admin/email">
    <div class="form-group">
      <label class="form-label">Email Body Template</label>
      <p style="color: #718096; font-size: 0.9em; margin-bottom: 10px;">
        Use <code>{{name}}</code> for participant name and <code>{{link}}</code> for booking link
      </p>
      <textarea name="body" class="form-input form-textarea" rows="8" placeholder="Hi {{name}}, please book your slot: {{link}}">{{ email_body }}</textarea>
    </div>
    <button type="submit" class="btn-primary">💾 Save Email Template</button>
  </form>
</div>

<!-- Consent Form Section -->
<div class="section" id="consent-form">
  <h2 class="section-title">📋 Consent Form Management</h2>

  <form method="post" action="/admin/consent" style="margin-bottom: 30px;">
    <div class="form-group">
      <label class="form-label">HTML Content</label>
      <p style="color: #718096; font-size: 0.9em; margin-bottom: 10px;">
        Participants will see this before booking their time slots
      </p>
      <textarea name="html" class="form-input form-textarea" rows="8" placeholder="<h2>Research Study Consent</h2><p>Your consent form content here...</p>">{{ consent_html }}</textarea>
    </div>
    <button type="submit" class="btn-primary">💾 Save HTML Consent</button>
  </form>

  <form method="post" action="/admin/upload-consent" enctype="multipart/form-data">
    <div class="form-group">
      <label class="form-label">Upload Consent Document</label>
      <p style="color: #718096; font-size: 0.9em; margin-bottom: 10px;">
        Upload PDF, DOC, or DOCX files (max 16MB)
      </p>
      <input type="file" name="consent_file" accept=".pdf,.doc,.docx" required class="form-input" style="padding: 10px;">
    </div>
    <button type="submit" class="btn-primary">📤 Upload Consent File</button>
    <p style="color: #718096; font-size: 0.9em; margin-top: 10px;">
      🔗 Uploaded files will be available at <a href="/consent" target="_blank" style="color: #667eea;">/consent</a>
    </p>

    {% if consent_files %}
    <div style="margin-top: 30px; padding-top: 20px; border-top: 2px solid #e2e8f0;">
      <h4 style="color: #2d3748; margin: 0 0 20px;">📁 Uploaded Documents</h4>
      {% for file in consent_files %}
      <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 15px; margin: 10px 0; display: flex; justify-content: space-between; align-items: center;">
        <a href="/uploads/{{file.filename}}" target="_blank" style="color: #667eea; font-weight: 600; text-decoration: none;">
          📎 {{file.original_name}}
        </a>
        <span style="color: #718096; font-size: 0.9em;">{{file.upload_date}}</span>
      </div>
      {% endfor %}
    </div>
    {% endif %}
  </form>
</div>

</div>

<script>
function addParticipant() {
  const container = document.getElementById('participantsContainer');
  const newRow = document.createElement('div');
  newRow.className = 'participant-row';
  newRow.innerHTML = `
    <button type="button" class="remove-participant" onclick="removeParticipant(this)" title="Remove participant">×</button>
    <div class="participant-fields">
      <div class="form-group">
        <label class="form-label">👤 Full Name</label>
        <input name="names[]" class="form-input" required placeholder="Enter participant's full name">
      </div>
      <div class="form-group">
        <label class="form-label">📧 Email Address</label>
        <input type="email" name="emails[]" class="form-input" required placeholder="participant@email.com">
      </div>
    </div>
  `;
  container.appendChild(newRow);
}

function removeParticipant(button) {
  const participantRows = document.querySelectorAll('.participant-row');
  if (participantRows.length > 1) {
    button.parentElement.remove();
  } else {
    alert('You must have at least one participant.');
  }
}
</script>

</body>
"""

@app.get("/admin")
@require_auth
def admin():
    authed = have_token()

    # Check if we need Gmail permissions
    gmail_ready = False
    if authed:
        try:
            creds = get_creds()
            if creds and creds.valid and creds.scopes:
                gmail_ready = 'https://www.googleapis.com/auth/gmail.send' in creds.scopes
        except:
            pass

    msg = request.args.get("msg", "")
    success = ""
    if msg == "email_saved":
        success = "Email template saved successfully!"
    elif msg == "consent_saved":
        success = "Consent form saved successfully!"
    elif msg == "file_uploaded":
        success = "Consent file uploaded successfully!"
    elif msg == "no_file":
        success = "Please select a file to upload."
    elif msg == "invalid_file":
        success = "Invalid file type. Please upload PDF, DOC, or DOCX files."
    elif msg == "booking_approved":
        success = "Booking approved and calendar invitation sent!"
    elif msg == "booking_rejected":
        success = "Booking rejected successfully."
    elif msg == "booking_removed":
        success = "Confirmed booking removed successfully. Calendar event deleted and participant notified."
    elif msg == "booking_not_found":
        success = "Booking not found or not confirmed."
    elif msg == "removal_failed":
        success = "Failed to remove booking. Please try again."

    # Get pending bookings with formatted times
    pending_bookings = []
    for booking in get_pending_bookings():
        created_dt = datetime.fromisoformat(booking['created_at'])

        formatted_booking = dict(booking)
        formatted_booking['created_at_formatted'] = created_dt.strftime('%m/%d %I:%M %p').replace(' 0', ' ')

        # Format all 3 time slot preferences
        preferences = []
        for i in range(1, 4):
            start_key = f'preference{i}_start'
            end_key = f'preference{i}_end'
            if booking[start_key] and booking[end_key]:
                start_dt = datetime.fromisoformat(booking[start_key])
                end_dt = datetime.fromisoformat(booking[end_key])
                pref = {
                    'start': booking[start_key],
                    'end': booking[end_key],
                    'start_formatted': start_dt.strftime('%a %b %d, %I:%M %p').replace(' 0', ' '),
                    'end_formatted': end_dt.strftime('%I:%M %p').replace(' 0', ' '),
                    'option_num': i
                }
                preferences.append(pref)

        formatted_booking['preferences'] = preferences
        pending_bookings.append(formatted_booking)

    # Get confirmed bookings
    confirmed_bookings = []
    with db() as con:
        bookings = con.execute("""
            SELECT b.*, p.name, p.email
            FROM bookings b
            JOIN participants p ON b.participant_id = p.id
            WHERE b.status = 'confirmed'
            AND b.selected_start_time IS NOT NULL
            AND b.selected_end_time IS NOT NULL
            ORDER BY b.selected_start_time ASC
        """).fetchall()

        for booking in bookings:
            confirmed_dt = datetime.fromisoformat(booking['admin_confirmed_at']) if booking['admin_confirmed_at'] else None
            start_dt = datetime.fromisoformat(booking['selected_start_time'])
            end_dt = datetime.fromisoformat(booking['selected_end_time'])

            formatted_booking = dict(booking)
            formatted_booking['confirmed_at_formatted'] = confirmed_dt.strftime('%m/%d %I:%M %p').replace(' 0', ' ') if confirmed_dt else 'N/A'
            formatted_booking['start_formatted'] = start_dt.strftime('%a %b %d, %I:%M %p').replace(' 0', ' ')
            formatted_booking['end_formatted'] = end_dt.strftime('%I:%M %p').replace(' 0', ' ')

            confirmed_bookings.append(formatted_booking)

    return render_template_string(
        ADMIN_HTML,
        title=APP_TITLE,
        cal_id=CALENDAR_ID or "(missing)",
        authed=authed,
        gmail_ready=gmail_ready,
        email_body=get_setting("email_body"),
        consent_html=get_setting("consent_html"),
        consent_files=get_consent_files(),
        pending_bookings=pending_bookings,
        confirmed_bookings=confirmed_bookings,
        success=success,
        user_email=session.get('user_email', ''),
        user_name=session.get('user_name', ''),
        user_org=session.get('user_org', ''),
    )

@app.post("/admin/email")
@require_auth
def admin_email():
    set_setting("email_body", request.form.get("body",""))
    return redirect(url_for("admin") + "?msg=email_saved")

@app.post("/admin/consent")
@require_auth
def admin_consent():
    set_setting("consent_html", request.form.get("html",""))
    return redirect(url_for("admin") + "?msg=consent_saved")

@app.post("/admin/upload-consent")
@require_auth
def upload_consent():
    if 'consent_file' not in request.files:
        return redirect(url_for("admin") + "?msg=no_file")

    file = request.files['consent_file']
    if file.filename == '':
        return redirect(url_for("admin") + "?msg=no_file")

    if file and allowed_file(file.filename):
        # Generate secure filename
        filename = f"consent_{secrets.token_hex(8)}.{file.filename.rsplit('.', 1)[1].lower()}"
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)

        # Save to database
        with db() as con:
            con.execute("INSERT INTO consent_files(filename, original_name) VALUES(?,?)",
                       (filename, file.filename))

        return redirect(url_for("admin") + "?msg=file_uploaded")

    return redirect(url_for("admin") + "?msg=invalid_file")

@app.post("/admin/bookings")
@require_auth
def admin_bookings():
    action = request.form.get("action", "")
    print(f"[DEBUG] Received action: '{action}'")
    if not action:
        print(f"[DEBUG] No action received, redirecting to admin")
        return redirect(url_for("admin"))

    if action.startswith("approve_"):
        # Parse action: approve_{booking_id}_{start_time}_{end_time}
        parts = action.split("_", 3)
        if len(parts) < 4:
            # Old format or malformed
            return redirect(url_for("admin"))

        booking_id = parts[1]
        selected_start = parts[2]
        selected_end = parts[3]

        # Get booking details
        with db() as con:
            booking = con.execute("""
                SELECT b.*, p.name, p.email
                FROM bookings b
                JOIN participants p ON b.participant_id = p.id
                WHERE b.id = ? AND b.status = 'pending'
            """, (booking_id,)).fetchone()

        if not booking:
            return redirect(url_for("admin"))

        # Create calendar event with selected time
        start_dt = datetime.fromisoformat(selected_start)
        end_dt = datetime.fromisoformat(selected_end)

        try:
            print(f"[DEBUG] Attempting to approve booking {booking_id}")
            print(f"[DEBUG] Selected time: {selected_start} to {selected_end}")
            print(f"[DEBUG] CALENDAR_ID: {CALENDAR_ID}")

            svc = calendar_service()
            print(f"[DEBUG] Calendar service obtained successfully")

            event = {
                "summary": f"User Study — {booking['name']}",
                "description": f"Participant: {booking['name']} <{booking['email']}>\nConsent: {HOST_BASE}/consent\n\nStatus: CONFIRMED by Admin",
                "start": {"dateTime": to_iso_utc(start_dt), "timeZone": "UTC"},
                "end":   {"dateTime": to_iso_utc(end_dt),   "timeZone": "UTC"},
                "attendees": [{"email": booking["email"]}],
            }
            print(f"[DEBUG] Event object created: {event}")

            # Try the configured calendar first, fallback to primary
            calendar_id_to_use = CALENDAR_ID or "primary"
            try:
                created = svc.events().insert(
                    calendarId=calendar_id_to_use,
                    body=event,
                    sendUpdates="all"
                ).execute()
                print(f"[DEBUG] Calendar event created: {created.get('id')} on calendar: {calendar_id_to_use}")
            except Exception as calendar_error:
                if calendar_id_to_use != "primary":
                    print(f"[DEBUG] Failed to create event on configured calendar, trying primary calendar")
                    created = svc.events().insert(
                        calendarId="primary",
                        body=event,
                        sendUpdates="all"
                    ).execute()
                    print(f"[DEBUG] Calendar event created on primary calendar: {created.get('id')}")
                else:
                    raise calendar_error

            # Update booking status with selected time
            with db() as con:
                con.execute("""
                    UPDATE bookings
                    SET status = 'confirmed',
                        selected_start_time = ?,
                        selected_end_time = ?,
                        calendar_event_id = ?,
                        admin_confirmed_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (selected_start, selected_end, created['id'], booking_id))
            print(f"[DEBUG] Database updated successfully")

            # Send confirmation email
            email_result = send_confirmation_email(booking['email'], booking['name'], selected_start, selected_end)
            print(f"[DEBUG] Confirmation email result: {email_result}")

            return redirect(url_for("admin") + "?msg=booking_approved")

        except Exception as e:
            import traceback
            print(f"[BOOKING APPROVAL ERROR] {e}")
            print(f"[BOOKING APPROVAL ERROR TRACEBACK] {traceback.format_exc()}")
            return redirect(url_for("admin"))

    elif action.startswith("reject_"):
        booking_id = action.split("_")[1]

        # Update booking status to rejected
        with db() as con:
            con.execute("""
                UPDATE bookings
                SET status = 'rejected',
                    admin_confirmed_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'pending'
            """, (booking_id,))

        return redirect(url_for("admin") + "?msg=booking_rejected")

    elif action.startswith("remove_"):
        booking_id = action.split("_")[1]

        # Get booking details first
        with db() as con:
            booking = con.execute("""
                SELECT b.*, p.name, p.email
                FROM bookings b
                JOIN participants p ON b.participant_id = p.id
                WHERE b.id = ? AND b.status = 'confirmed'
            """, (booking_id,)).fetchone()

        if not booking:
            return redirect(url_for("admin") + "?msg=booking_not_found")

        try:
            # Remove calendar event if it exists
            if booking['calendar_event_id']:
                svc = calendar_service()
                calendar_id_to_use = CALENDAR_ID or "primary"

                try:
                    svc.events().delete(
                        calendarId=calendar_id_to_use,
                        eventId=booking['calendar_event_id']
                    ).execute()
                    print(f"[DEBUG] Calendar event {booking['calendar_event_id']} deleted from calendar: {calendar_id_to_use}")
                except Exception as calendar_error:
                    if calendar_id_to_use != "primary":
                        print(f"[DEBUG] Failed to delete from configured calendar, trying primary")
                        try:
                            svc.events().delete(
                                calendarId="primary",
                                eventId=booking['calendar_event_id']
                            ).execute()
                            print(f"[DEBUG] Calendar event {booking['calendar_event_id']} deleted from primary calendar")
                        except Exception as primary_error:
                            print(f"[WARNING] Failed to delete calendar event: {primary_error}")
                    else:
                        print(f"[WARNING] Failed to delete calendar event: {calendar_error}")

            # Update booking status to removed
            with db() as con:
                con.execute("""
                    UPDATE bookings
                    SET status = 'removed_by_admin',
                        calendar_event_id = NULL
                    WHERE id = ?
                """, (booking_id,))

            # Send notification email to participant
            try:
                selected_start = booking['selected_start_time']
                selected_end = booking['selected_end_time']
                start_dt = datetime.fromisoformat(selected_start)
                end_dt = datetime.fromisoformat(selected_end)
                start_str = start_dt.strftime('%a %b %d, %I:%M %p').replace(' 0', ' ')
                end_str = end_dt.strftime('%I:%M %p').replace(' 0', ' ')

                # Create cancellation email
                subject = "❌ User Study Booking CANCELLED - Important Update"

                body = f"""Hi {booking['name']},

We regret to inform you that your confirmed booking has been cancelled by our admin team.

❌ CANCELLED APPOINTMENT:
{start_str} – {end_str} (Toronto time)

🗑️ WHAT'S BEEN DONE:
• The calendar event has been removed from your calendar
• Your booking slot is now available for other participants
• You'll no longer receive reminders for this session

📧 NEXT STEPS:
If you have any questions or would like to reschedule, please reply to this email.

Thank you for your understanding.

Best regards,
The Research Team

---
User Study Booking System"""

                # Use the same email sending logic as confirmation emails
                result = send_email_with_gmail_api(booking['email'], booking['name'], subject, body)

                if result != "SUCCESS":
                    # Fallback to SMTP if available
                    if SMTP_HOST:
                        try:
                            msg = EmailMessage()
                            msg["From"] = SMTP_FROM
                            msg["To"] = f"{booking['name']} <{booking['email']}>"
                            msg["Subject"] = subject
                            msg.set_content(body)

                            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                                s.starttls()
                                if SMTP_USER and SMTP_PASS:
                                    s.login(SMTP_USER, SMTP_PASS)
                                s.send_message(msg)
                            print(f"[DEBUG] Cancellation email sent via SMTP to {booking['email']}")
                        except Exception as smtp_error:
                            print(f"[WARNING] SMTP cancellation email failed: {smtp_error}")
                    else:
                        print(f"[DEBUG] DRY-RUN cancellation email for {booking['email']}")
                else:
                    print(f"[DEBUG] Cancellation email sent via Gmail API to {booking['email']}")

            except Exception as email_error:
                print(f"[WARNING] Failed to send cancellation email: {email_error}")

            # Check if request came from calendar view
            referer = request.headers.get('Referer', '')
            if '/admin/calendar' in referer:
                # Extract week parameter if present
                from urllib.parse import urlparse, parse_qs
                parsed_url = urlparse(referer)
                query_params = parse_qs(parsed_url.query)
                week = query_params.get('week', ['0'])[0]
                try:
                    week = int(week)
                except (ValueError, TypeError):
                    week = 0
                return redirect(url_for("admin_calendar", week=week, msg="booking_removed"))
            else:
                return redirect(url_for("admin") + "?msg=booking_removed")

        except Exception as e:
            import traceback
            print(f"[BOOKING REMOVAL ERROR] {e}")
            print(f"[BOOKING REMOVAL ERROR TRACEBACK] {traceback.format_exc()}")
            return redirect(url_for("admin") + "?msg=removal_failed")

    return redirect(url_for("admin"))

def generate_calendar_slots_html(calendar_days):
    """Generate HTML for calendar time slots"""
    html = ""
    for hour in range(9, 25):
        html += f'<div class="time-label">{hour}:00</div>'
        for day in calendar_days:
            slot = day["slots"][hour-9]
            html += f'''
            <div class="time-slot {slot["status"]}"
                 data-start="{slot["start"]}"
                 data-end="{slot["end"]}">
              <div class="slot-content">
                {get_slot_content(slot)}
              </div>
            </div>'''
    return html

@app.get("/admin/calendar")
@require_auth
def admin_calendar():
    # Get week offset and message from query parameters
    try:
        week_offset = int(request.args.get('week', 0))
    except (ValueError, TypeError):
        week_offset = 0
    msg = request.args.get("msg", "")
    if week_offset < 0:
        week_offset = 0
    elif week_offset > 12:  # Show more weeks for admin
        week_offset = 12

    # Calculate the start date for the requested week
    today_local = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_local + timedelta(days=week_offset * 7)
    days_from_monday = week_start.weekday()
    week_start = week_start - timedelta(days=days_from_monday)

    now_local = datetime.now(TZ)

    # Get calendar service
    try:
        service = calendar_service()
        calendar_available = True
    except Exception as e:
        print(f"[ADMIN CALENDAR ERROR] Calendar service unavailable: {e}")
        service = None
        calendar_available = False

    # Get all blocked slots
    blocked_slots = []
    with db() as con:
        slots = con.execute("SELECT start_time, end_time FROM blocked_slots").fetchall()
        for slot in slots:
            blocked_slots.append({
                'start': datetime.fromisoformat(slot['start_time']),
                'end': datetime.fromisoformat(slot['end_time'])
            })

    # Get all confirmed bookings for this week
    week_end = week_start + timedelta(days=7)
    confirmed_bookings = []
    if calendar_available:
        with db() as con:
            bookings = con.execute("""
                SELECT b.*, p.name, p.email
                FROM bookings b
                JOIN participants p ON b.participant_id = p.id
                WHERE b.status = 'confirmed'
                AND b.selected_start_time IS NOT NULL
                AND b.selected_end_time IS NOT NULL
            """).fetchall()

            for booking in bookings:
                start_dt = datetime.fromisoformat(booking['selected_start_time'])
                end_dt = datetime.fromisoformat(booking['selected_end_time'])

                # Check if this booking falls within the current week
                if week_start <= start_dt < week_end:
                    # Safely get calendar_event_id
                    try:
                        calendar_event_id = booking['calendar_event_id'] or ''
                    except (KeyError, TypeError):
                        calendar_event_id = ''

                    confirmed_bookings.append({
                        'id': booking['id'],
                        'name': booking['name'],
                        'email': booking['email'],
                        'start': start_dt,
                        'end': end_dt,
                        'start_formatted': start_dt.strftime('%a %m/%d %I:%M %p').replace(' 0', ' '),
                        'end_formatted': end_dt.strftime('%I:%M %p').replace(' 0', ' '),
                        'calendar_event_id': calendar_event_id
                    })

    # Generate calendar data (similar to invite function but with booking info)
    calendar_days = []
    for d in range(7):
        day = week_start + timedelta(days=d)
        day_header = day.strftime("%a %m/%d").replace(" 0", " ")

        day_slots = []
        for hour in range(9, 25):
            if hour == 24:
                # Handle midnight as hour 0 of next day
                start = (day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=TZ)
            else:
                start = day.replace(hour=hour, minute=0, second=0, microsecond=0, tzinfo=TZ)
            end = start + timedelta(hours=1)

            # Find any confirmed booking for this slot
            slot_booking = None
            for booking in confirmed_bookings:
                if booking['start'] <= start < booking['end']:
                    slot_booking = booking
                    break

            # Check if slot is blocked
            is_blocked = False
            for blocked in blocked_slots:
                if blocked['start'] <= start < blocked['end']:
                    is_blocked = True
                    break

            # Determine slot status
            if start <= now_local:
                status = "past"
            elif slot_booking:
                status = "booked"
            elif is_blocked:
                status = "blocked"
            elif not calendar_available:
                status = "unavailable"
            elif is_free(service, start, end):
                status = "available"
            else:
                status = "unavailable"

            day_slots.append({
                "status": status,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "booking": slot_booking,
                "is_blocked": is_blocked
            })

        calendar_days.append({
            "header": day_header,
            "slots": day_slots
        })

    # Generate week label and navigation
    start_date = week_start.strftime("%b %d").replace(" 0", " ")
    end_date = (week_start + timedelta(days=6)).strftime("%b %d").replace(" 0", " ")
    current_week_label = f"{start_date} - {end_date}"

    prev_week_url = None
    next_week_url = url_for('admin_calendar', week=week_offset+1) if week_offset < 12 else None

    if week_offset > 0:
        prev_week_url = url_for('admin_calendar', week=week_offset-1) if week_offset > 1 else url_for('admin_calendar')

    # Generate the admin calendar HTML template
    # Generate success message if any
    success_msg = ""
    if msg == "booking_removed":
        success_msg = "✅ Booking removed successfully. Calendar event deleted and participant notified."
    elif msg == "slot_blocked":
        success_msg = "✅ Time slot blocked successfully."
    elif msg == "slot_unblocked":
        success_msg = "✅ Time slot unblocked successfully."

    return generate_admin_calendar_html(
        current_week_label,
        week_offset,
        prev_week_url,
        next_week_url,
        calendar_days,
        confirmed_bookings,
        success_msg
    )

def generate_admin_calendar_html(current_week_label, week_offset, prev_week_url, next_week_url, calendar_days, confirmed_bookings, success_msg=""):
    """Generate the complete admin calendar HTML"""

    # Generate day headers
    day_headers = "".join(f'<div class="day-header">{day["header"]}</div>' for day in calendar_days)

    # Generate calendar slots
    calendar_slots = generate_calendar_slots_html(calendar_days)

    # Generate navigation buttons
    prev_nav = f'<a href="{prev_week_url}" class="nav-btn">⬅️ Previous Week</a>' if prev_week_url else '<div style="width:120px"></div>'
    next_nav = f'<a href="{next_week_url}" class="nav-btn">Next Week ➡️</a>' if next_week_url else '<div style="width:120px"></div>'

    # Generate booking details (informational only - removal via time slots)
    if confirmed_bookings:
        booking_html = '<div class="booking-list">'
        for booking in confirmed_bookings:
            calendar_event_id = booking.get("calendar_event_id", "") or ""
            calendar_id_short = calendar_event_id[:15] + "..." if calendar_event_id else "N/A"
            booking_html += f'''
            <div class="booking-item info-only">
              <div class="booking-info">
                <h4>{booking["name"]}</h4>
                <p class="email">{booking["email"]}</p>
                <p class="time">{booking["start_formatted"]} – {booking["end_formatted"]}</p>
                <p class="calendar-id">📅 Event: {calendar_id_short}</p>
              </div>
              <div class="booking-note">
                <p class="remove-hint">💡 Click the time slot on the calendar above to remove this booking</p>
              </div>
            </div>'''
        booking_html += '</div>'
    else:
        booking_html = '<p class="no-bookings">No confirmed bookings for this week.</p>'

    # Generate success message HTML
    success_html = ""
    if success_msg:
        success_html = f'<div class="success-msg">{success_msg}</div>'

    return f"""
    <!doctype html><meta charset="utf-8">
    <title>Admin Calendar · {APP_TITLE}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
    {get_admin_calendar_styles()}
    </style>
    <body>
    <div class="container">

    <div class="header">
      <h1>📅 Admin Calendar View</h1>
      <div class="header-actions">
        <a href="/admin" class="action-btn secondary">← Back to Dashboard</a>
      </div>
    </div>

    {success_html}

    <div class="calendar-header">
      <h2>Weekly Schedule Overview</h2>
      <div class="calendar-nav">
        {prev_nav}
        <span class="week-label">{current_week_label}</span>
        {next_nav}
      </div>
    </div>

    <div class="week-info">
      <span>Week {week_offset + 1}</span> •
      <span>Showing {len(confirmed_bookings)} confirmed booking(s)</span> •
      <a href="/admin/calendar">Current Week</a>
    </div>

    <div class="legend">
      <div class="legend-item">
        <div class="legend-color legend-available"></div>
        <span>✅ Available</span>
      </div>
      <div class="legend-item">
        <div class="legend-color legend-booked"></div>
        <span>📅 Booked</span>
      </div>
      <div class="legend-item">
        <div class="legend-color legend-blocked"></div>
        <span>🚫 Blocked</span>
      </div>
      <div class="legend-item">
        <div class="legend-color legend-unavailable"></div>
        <span>❌ Unavailable</span>
      </div>
      <div class="legend-item">
        <div class="legend-color legend-past"></div>
        <span>⏰ Past</span>
      </div>
    </div>

    <div class="calendar-grid">
      <div class="time-header">Time</div>
      {day_headers}
      {calendar_slots}
    </div>

    <div class="booking-details">
      <h3>📋 Confirmed Bookings This Week</h3>
      {booking_html}
    </div>

    </div>
    </body>
    """

def get_slot_content(slot):
    if slot["status"] == "booked" and slot["booking"]:
        booking = slot["booking"]
        return f'''
        <form method="post" action="/admin/bookings" class="slot-form">
            <button type="submit" name="action" value="remove_{booking["id"]}" class="booking-slot-btn"
                    onclick="return confirm('Remove {booking["name"]}\\'s confirmed booking?\\n\\nThis will:\\n• Cancel the calendar event\\n• Notify the participant\\n• Free up this time slot')">
                <div class="booking-label">{booking["name"]}</div>
                <div class="booking-time">{booking["start"].strftime("%I:%M").lstrip("0")}–{booking["end"].strftime("%I:%M").lstrip("0")}</div>
                <div class="remove-indicator">🗑️ Click to remove</div>
            </button>
        </form>
        '''
    elif slot["status"] == "blocked":
        return f'''
        <form method="post" action="/admin/unblock-slot" class="slot-form">
            <input type="hidden" name="start_time" value="{slot["start"]}">
            <input type="hidden" name="end_time" value="{slot["end"]}">
            <button type="submit" class="block-slot-btn blocked"
                    onclick="return confirm('Unblock this time slot?')">
                <div class="block-label">🚫 Blocked</div>
                <div class="block-indicator">Click to unblock</div>
            </button>
        </form>
        '''
    elif slot["status"] == "available":
        return f'''
        <form method="post" action="/admin/block-slot" class="slot-form">
            <input type="hidden" name="start_time" value="{slot["start"]}">
            <input type="hidden" name="end_time" value="{slot["end"]}">
            <button type="submit" class="block-slot-btn available"
                    onclick="return confirm('Block this time slot?\\n\\nThis will make it unavailable for participants.')">
                <div class="available-label">✅</div>
                <div class="block-indicator">Click to block</div>
            </button>
        </form>
        '''
    elif slot["status"] == "unavailable":
        return "❌"
    else:
        return "⏰"

def get_admin_calendar_styles():
    return """
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, system-ui, sans-serif;
      margin: 0; padding: 0; min-height: 100vh;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    }
    .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
    .header {
      background: rgba(255,255,255,0.1); backdrop-filter: blur(20px);
      border-radius: 20px; padding: 30px; margin-bottom: 30px;
      color: white; display: flex; justify-content: space-between; align-items: center;
    }
    .header h1 { margin: 0; font-size: 2.5em; font-weight: 700; }
    .action-btn {
      background: rgba(255,255,255,0.2); color: white; padding: 12px 20px;
      border-radius: 10px; text-decoration: none; font-weight: 600; transition: all 0.3s;
    }
    .action-btn:hover { background: rgba(255,255,255,0.3); transform: translateY(-2px); }
    .action-btn.secondary { background: rgba(255,255,255,0.15); }
    .calendar-header {
      background: white; border-radius: 16px; padding: 25px; margin-bottom: 20px;
      box-shadow: 0 10px 40px rgba(0,0,0,0.1);
    }
    .calendar-header h2 { margin: 0 0 15px; color: #2d3748; }
    .calendar-nav {
      display: flex; justify-content: space-between; align-items: center;
    }
    .nav-btn {
      background: #667eea; color: white; padding: 8px 16px; border-radius: 8px;
      text-decoration: none; font-weight: 600; transition: all 0.3s;
    }
    .nav-btn:hover { background: #5a67d8; transform: translateY(-2px); }
    .week-label { font-weight: 700; font-size: 1.2em; color: #2d3748; }
    .week-info {
      text-align: center; margin: 15px 0; color: #4a5568;
      background: rgba(255,255,255,0.9); padding: 10px; border-radius: 8px;
    }
    .week-info a { color: #667eea; text-decoration: none; font-weight: 600; }
    .legend {
      display: flex; justify-content: center; gap: 30px; margin: 25px 0;
      background: rgba(255,255,255,0.9); padding: 20px; border-radius: 12px;
    }
    .legend-item { display: flex; align-items: center; gap: 8px; font-weight: 600; }
    .legend-color {
      width: 24px; height: 24px; border-radius: 6px; border: 2px solid #e2e8f0;
    }
    .legend-available { background: #f0fff4; border-color: #68d391; }
    .legend-booked { background: #e6fffa; border-color: #4fd1c7; }
    .legend-unavailable { background: #fed7d7; border-color: #f56565; }
    .legend-past { background: #f7fafc; border-color: #e2e8f0; }
    .calendar-grid {
      display: grid; grid-template-columns: 100px repeat(7, 1fr); gap: 2px;
      background: #e2e8f0; border-radius: 12px; overflow: hidden; margin: 20px 0;
    }
    .time-header, .day-header {
      background: #667eea; color: white; padding: 15px; text-align: center;
      font-weight: 700; font-size: 1em;
    }
    .time-label {
      background: #f7fafc; padding: 15px; text-align: center; font-weight: 600;
      color: #4a5568; display: flex; align-items: center; justify-content: center;
    }
    .time-slot {
      background: white; min-height: 60px; position: relative;
      border: 2px solid transparent; transition: all 0.3s;
    }
    .time-slot.available { background: #f0fff4; border-color: #68d391; }
    .time-slot.booked { background: #e6fffa; border-color: #4fd1c7; }
    .time-slot.blocked { background: #fef3c7; border-color: #f59e0b; }
    .time-slot.unavailable { background: #fed7d7; border-color: #f56565; }
    .time-slot.past { background: #f7fafc; border-color: #e2e8f0; }
    .slot-content {
      padding: 8px; font-size: 0.9em; text-align: center;
      height: 100%; display: flex; flex-direction: column; justify-content: center;
    }
    .booking-label {
      font-weight: 700; color: #2d3748; margin-bottom: 2px;
      font-size: 0.85em; line-height: 1.2;
    }
    .booking-time {
      font-size: 0.75em; color: #4a5568; font-weight: 600;
    }
    .booking-details {
      background: white; border-radius: 16px; padding: 30px; margin-top: 30px;
      box-shadow: 0 10px 40px rgba(0,0,0,0.1);
    }
    .booking-details h3 { margin: 0 0 20px; color: #2d3748; }
    .booking-list { display: grid; gap: 15px; }
    .booking-item {
      background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px;
      padding: 20px; transition: all 0.3s; display: flex; justify-content: space-between; align-items: center;
    }
    .booking-item:hover { border-color: #667eea; transform: translateY(-2px); }
    .booking-info { flex: 1; }
    .booking-item h4 { margin: 0 0 5px; color: #2d3748; }
    .booking-item .email { margin: 0 0 8px; color: #667eea; font-weight: 600; }
    .booking-item .time { margin: 0 0 5px; color: #4a5568; font-weight: 600; }
    .booking-item .calendar-id { margin: 0; color: #718096; font-size: 0.8em; font-family: monospace; }
    .booking-actions { margin-left: 20px; }
    .remove-btn {
      background: #f56565; color: white; border: none; padding: 8px 16px;
      border-radius: 8px; font-weight: 600; cursor: pointer; transition: all 0.3s;
      font-size: 0.85em;
    }
    .remove-btn:hover { background: #e53e3e; transform: translateY(-2px); }
    .no-bookings { color: #718096; text-align: center; font-style: italic; margin: 20px 0; }
    .success-msg {
      background: linear-gradient(135deg, #c6f6d5 0%, #9ae6b4 100%);
      color: #22543d; border-radius: 12px; padding: 20px; margin: 25px 0;
      font-weight: 600; border: 1px solid #68d391;
    }
    .slot-form {
      margin: 0; padding: 0; width: 100%; height: 100%;
    }
    .booking-slot-btn {
      background: none; border: none; padding: 0; margin: 0; width: 100%; height: 100%;
      cursor: pointer; text-align: center; color: inherit; font-family: inherit;
      transition: all 0.2s; border-radius: 4px;
    }
    .booking-slot-btn:hover {
      background: rgba(255, 255, 255, 0.2); transform: scale(1.02);
    }
    .booking-slot-btn .booking-label {
      font-weight: 600; font-size: 0.8em; margin-bottom: 2px;
    }
    .booking-slot-btn .booking-time {
      font-size: 0.7em; margin-bottom: 2px;
    }
    .booking-slot-btn .remove-indicator {
      font-size: 0.6em; opacity: 0.7; transition: opacity 0.2s;
    }
    .booking-slot-btn:hover .remove-indicator {
      opacity: 1; font-weight: bold;
    }
    .booking-item.info-only {
      background: linear-gradient(135deg, #f0f9ff 0%, #e0f2fe 100%);
      border: 1px solid #bae6fd;
    }
    .booking-note {
      margin-left: 20px; display: flex; align-items: center;
    }
    .remove-hint {
      color: #6b7280; font-size: 0.8em; font-style: italic; margin: 0;
      background: #f3f4f6; padding: 8px 12px; border-radius: 6px;
    }
    .block-slot-btn {
      background: none; border: none; padding: 0; margin: 0; width: 100%; height: 100%;
      cursor: pointer; text-align: center; color: inherit; font-family: inherit;
      transition: all 0.2s; border-radius: 4px;
    }
    .block-slot-btn:hover {
      background: rgba(255, 255, 255, 0.3); transform: scale(1.02);
    }
    .block-slot-btn .block-label, .block-slot-btn .available-label {
      font-weight: 600; font-size: 1.2em; margin-bottom: 3px;
    }
    .block-slot-btn .block-indicator {
      font-size: 0.65em; opacity: 0; transition: opacity 0.2s;
      color: #4a5568; font-weight: 600;
    }
    .block-slot-btn:hover .block-indicator {
      opacity: 1;
    }
    .legend-blocked { background: #fef3c7; border-color: #f59e0b; }
    @media (max-width: 1024px) {
      .calendar-grid { grid-template-columns: 80px repeat(7, 1fr); }
      .legend { flex-direction: column; gap: 15px; }
    }
    @media (max-width: 768px) {
      .header { flex-direction: column; gap: 20px; text-align: center; }
      .calendar-nav { flex-direction: column; gap: 15px; }
      .calendar-grid { grid-template-columns: 60px repeat(7, 1fr); }
    }
    """

@app.post("/admin/participant")
@require_auth
def admin_participant():
    name = request.form["name"].strip()
    email = request.form["email"].strip().lower()
    token = secrets.token_urlsafe(16)
    with db() as con:
        con.execute("INSERT INTO participants(name,email,token) VALUES(?,?,?)", (name, email, token))
    link = f"{HOST_BASE}/invite/{token}"
    print(f"[PARTICIPANT CREATION] Starting email process for {name} ({email})")
    print(f"[DEBUG] HOST_BASE: {HOST_BASE}")
    print(f"[DEBUG] Generated link: {link}")
    # Send initial email (with network error handling)
    email_result = send_initial_email(email, name, link)

    # Handle different types of email errors
    if "Network is unreachable" in email_result or "Errno 101" in email_result:
        email_result = "NETWORK_ERROR: Email server unreachable. Participant created successfully."
    elif "Gmail API credentials not available" in email_result:
        email_result = "AUTH_ERROR: Please sign in with Google to enable email sending."
    elif "Gmail send permission not granted" in email_result:
        email_result = "SCOPE_ERROR: Please re-authenticate with Google and grant Gmail permissions."

    # Show detailed receipt page with email status
    receipt_html = f"""
    <!doctype html><meta charset="utf-8">
    <title>Participant Added</title>
    <style>
    body{{font-family:system-ui;max-width:600px;margin:40px auto;padding:20px}}
    .success{{background:#c6f6d5;color:#2f855a;padding:15px;border-radius:10px;margin:20px 0}}
    .error{{background:#fed7d7;color:#9b2c2c;padding:15px;border-radius:10px;margin:20px 0}}
    .link{{background:#f7fafc;padding:15px;border-radius:10px;margin:20px 0;word-break:break-all}}
    </style>
    <h2>✅ Participant Added</h2>
    <p><strong>Name:</strong> {name}</p>
    <p><strong>Email:</strong> {email}</p>

    <div class="link">
        <strong>Booking Link:</strong><br>
        <a href='{link}'>{link}</a>
    </div>
    """

    if email_result == "SUCCESS":
        receipt_html += '<div class="success">📧 Email sent successfully!</div>'
    elif "NETWORK_ERROR" in email_result:
        receipt_html += '<div class="error">⚠️ Network issue: Email server unreachable. Participant created successfully - please send the booking link manually.</div>'
    elif "AUTH_ERROR" in email_result:
        receipt_html += '<div class="error">🔐 Authentication needed: Please <a href="/logout">sign out</a> and <a href="/google-login">sign in with Google</a> to enable email sending.</div>'
    elif "SCOPE_ERROR" in email_result:
        receipt_html += '<div class="error">📧 Permission needed: Please <a href="/logout">sign out</a> and <a href="/google-login">re-authenticate with Google</a> granting Gmail permissions.</div>'
    elif "EMAIL_SKIPPED" in email_result:
        receipt_html += '<div class="success">✅ Participant added! Send the booking link manually or copy from console.</div>'
    elif "DRY-RUN" in email_result:
        receipt_html += '<div class="error">⚠️ No SMTP configured - email not sent. Please configure SMTP settings in .env file.</div>'
    else:
        receipt_html += f'<div class="error">❌ Email failed: {email_result}</div>'

    receipt_html += '<p><a href="/admin">← Back to Admin</a></p>'
    return receipt_html

@app.post("/admin/participants/batch")
@require_auth
def admin_participants_batch():
    names = request.form.getlist("names[]")
    emails = request.form.getlist("emails[]")

    if not names or not emails or len(names) != len(emails):
        return redirect(url_for("admin") + "?msg=invalid_batch")

    # Clean and validate data
    participants_data = []
    for i, (name, email) in enumerate(zip(names, emails)):
        name = name.strip()
        email = email.strip().lower()

        if not name or not email:
            return redirect(url_for("admin") + f"?msg=empty_fields_{i+1}")

        participants_data.append({
            'name': name,
            'email': email,
            'token': secrets.token_urlsafe(16)
        })

    # Create all participants in database
    with db() as con:
        for p in participants_data:
            con.execute("INSERT INTO participants(name,email,token) VALUES(?,?,?)",
                       (p['name'], p['email'], p['token']))

    # Generate links and send emails
    email_results = []
    successful_participants = []
    failed_participants = []

    for p in participants_data:
        link = f"{HOST_BASE}/invite/{p['token']}"
        print(f"[BATCH PARTICIPANT] Processing {p['name']} ({p['email']})")

        # Send email
        email_result = send_initial_email(p['email'], p['name'], link)
        email_results.append({
            'name': p['name'],
            'email': p['email'],
            'link': link,
            'result': email_result
        })

        if email_result == "SUCCESS":
            successful_participants.append(p)
        else:
            failed_participants.append(p)

    # Generate batch results page
    total_count = len(participants_data)
    success_count = len(successful_participants)
    failed_count = len(failed_participants)

    results_html = f"""
    <!doctype html><meta charset="utf-8">
    <title>Batch Participants Added</title>
    <style>
    body{{font-family:system-ui;max-width:800px;margin:40px auto;padding:20px;background:#f8fafc}}
    .header{{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;padding:30px;border-radius:16px;margin-bottom:30px;text-align:center}}
    .stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;margin:30px 0}}
    .stat-card{{background:white;padding:20px;border-radius:12px;text-align:center;border:2px solid #e2e8f0}}
    .stat-card.success{{border-color:#68d391;background:linear-gradient(135deg,#f0fff4 0%,#c6f6d5 100%)}}
    .stat-card.error{{border-color:#fc8181;background:linear-gradient(135deg,#fff5f5 0%,#fed7d7 100%)}}
    .stat-number{{font-size:3em;font-weight:700;margin:10px 0}}
    .participant-grid{{display:grid;gap:15px;margin:20px 0}}
    .participant-item{{background:white;border-radius:12px;padding:20px;border:2px solid #e2e8f0;transition:all 0.3s}}
    .participant-item.success{{border-color:#68d391;background:linear-gradient(135deg,#f0fff4 0%,#e6fffa 100%)}}
    .participant-item.failed{{border-color:#fc8181;background:linear-gradient(135deg,#fff5f5 0%,#fed7d7 100%)}}
    .participant-item:hover{{transform:translateY(-2px);box-shadow:0 10px 25px rgba(0,0,0,0.1)}}
    .status-badge{{display:inline-block;padding:4px 12px;border-radius:20px;font-size:0.8em;font-weight:600;margin-left:10px}}
    .status-success{{background:#c6f6d5;color:#22543d}}
    .status-failed{{background:#fed7d7;color:#9b2c2c}}
    .link-box{{background:#f8fafc;padding:10px;border-radius:8px;margin-top:10px;font-size:0.9em;word-break:break-all}}
    .back-btn{{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;padding:15px 30px;border-radius:12px;text-decoration:none;font-weight:600;display:inline-block;margin:30px 0;transition:all 0.3s}}
    .back-btn:hover{{transform:translateY(-2px);box-shadow:0 15px 30px rgba(102,126,234,0.4)}}
    </style>

    <div class="header">
        <h1>📤 Batch Participants Created</h1>
        <p>Processing completed for {total_count} participant(s)</p>
    </div>

    <div class="stats">
        <div class="stat-card success">
            <div class="stat-number">{success_count}</div>
            <h3>✅ Successful</h3>
            <p>Emails sent successfully</p>
        </div>
        <div class="stat-card">
            <div class="stat-number">{total_count}</div>
            <h3>👥 Total</h3>
            <p>Participants processed</p>
        </div>
        <div class="stat-card {'error' if failed_count > 0 else ''}">
            <div class="stat-number">{failed_count}</div>
            <h3>⚠️ Failed</h3>
            <p>Email sending failed</p>
        </div>
    </div>

    <h2>📋 Detailed Results</h2>
    <div class="participant-grid">
    """

    for result in email_results:
        status_class = "success" if result['result'] == "SUCCESS" else "failed"
        status_text = "✅ Email Sent" if result['result'] == "SUCCESS" else f"❌ {result['result']}"
        status_badge_class = "status-success" if result['result'] == "SUCCESS" else "status-failed"

        results_html += f"""
        <div class="participant-item {status_class}">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
                <h4 style="margin:0">{result['name']}</h4>
                <span class="status-badge {status_badge_class}">{status_text}</span>
            </div>
            <p style="margin:5px 0;color:#667eea;font-weight:600">{result['email']}</p>
            <div class="link-box">
                <strong>Booking Link:</strong><br>
                <a href="{result['link']}" target="_blank">{result['link']}</a>
            </div>
        </div>
        """

    results_html += f"""
    </div>

    <div style="text-align:center">
        <a href="/admin" class="back-btn">← Back to Admin Dashboard</a>
    </div>
    """

    return results_html

@app.post("/admin/block-slot")
@require_auth
def admin_block_slot():
    start_time = request.form.get("start_time")
    end_time = request.form.get("end_time")
    week = request.form.get("week", "0")

    if not start_time or not end_time:
        return redirect(url_for("admin_calendar", week=week) + "?msg=invalid_slot")

    # Add to blocked slots
    with db() as con:
        con.execute("INSERT INTO blocked_slots(start_time, end_time) VALUES(?,?)",
                   (start_time, end_time))

    return redirect(url_for("admin_calendar", week=week) + "?msg=slot_blocked")

@app.post("/admin/unblock-slot")
@require_auth
def admin_unblock_slot():
    start_time = request.form.get("start_time")
    end_time = request.form.get("end_time")
    week = request.form.get("week", "0")

    if not start_time or not end_time:
        return redirect(url_for("admin_calendar", week=week) + "?msg=invalid_slot")

    # Remove from blocked slots
    with db() as con:
        con.execute("DELETE FROM blocked_slots WHERE start_time=? AND end_time=?",
                   (start_time, end_time))

    return redirect(url_for("admin_calendar", week=week) + "?msg=slot_unblocked")

# ──────────────────────────────────────────────────────────────────────────────
# Consent page
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.get("/consent")
def consent():
    html = get_setting("consent_html")
    files = get_consent_files()

    consent_content = f"<!doctype html><meta charset='utf-8'><title>Consent Form</title>"
    consent_content += "<style>body{font-family:system-ui;max-width:800px;margin:40px auto;padding:20px;line-height:1.6}</style>"

    if html:
        consent_content += html

    if files:
        consent_content += "<h3>📄 Consent Documents</h3>"
        for file in files:
            consent_content += f"<p><a href='/uploads/{file['filename']}' target='_blank'>📎 {file['original_name']}</a></p>"

    if not html and not files:
        consent_content += "<h2>Consent Form</h2><p>No consent form has been uploaded yet.</p>"

    return consent_content

# ──────────────────────────────────────────────────────────────────────────────
# Invite + booking flow
# ──────────────────────────────────────────────────────────────────────────────
INVITE_HTML = """
<!doctype html><meta charset="utf-8">
<title>{{title}}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 * { box-sizing: border-box; }
 body {
   font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, system-ui, sans-serif;
   max-width: 1200px; margin: 0 auto; padding: 20px;
   background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
   min-height: 100vh;
 }
 .container {
   background: white; border-radius: 16px; padding: 40px;
   box-shadow: 0 20px 40px rgba(0,0,0,0.1);
 }
 h2 {
   margin: 0 0 20px; color: #2d3748; font-size: 2.2em;
   background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
   -webkit-background-clip: text; -webkit-text-fill-color: transparent;
 }
 .welcome {
   background: #f7fafc; border-radius: 12px; padding: 20px; margin-bottom: 30px;
   border-left: 4px solid #667eea;
 }
 .consent-link {
   background: #fff5f5; border: 1px solid #fed7d7; border-radius: 10px;
   padding: 15px; margin: 20px 0; text-align: center;
 }
 .consent-link a {
   color: #e53e3e; font-weight: 600; text-decoration: none;
 }
 .error {
   background: #fed7d7; color: #9b2c2c; border-radius: 10px;
   padding: 15px; margin: 20px 0; font-weight: 600;
 }
 .calendar-container {
   background: #f8fafc; border-radius: 12px; padding: 20px; margin: 20px 0;
 }
 .calendar-header {
   display: flex; justify-content: space-between; align-items: center;
   margin-bottom: 20px; padding-bottom: 15px; border-bottom: 2px solid #e2e8f0;
 }
 .calendar-nav {
   display: flex; gap: 10px; align-items: center;
 }
 .nav-btn {
   background: #667eea; color: white; border: none; padding: 8px 12px;
   border-radius: 6px; cursor: pointer; font-weight: 600; transition: all 0.2s;
 }
 .nav-btn:hover {
   background: #5a67d8; transform: translateY(-1px);
 }
 .calendar-grid {
   display: grid; grid-template-columns: 80px repeat(7, 1fr); gap: 1px;
   background: #e2e8f0; border-radius: 8px; overflow: hidden;
 }
 .time-header {
   background: #4a5568; color: white; padding: 12px 8px; text-align: center;
   font-weight: 600; font-size: 0.9em;
 }
 .day-header {
   background: #667eea; color: white; padding: 12px; text-align: center;
   font-weight: 600; font-size: 0.95em;
 }
 .time-label {
   background: #edf2f7; padding: 8px; text-align: center; font-size: 0.8em;
   font-weight: 600; color: #4a5568; display: flex; align-items: center; justify-content: center;
 }
 .time-slot {
   background: white; min-height: 50px; display: flex; align-items: center;
   justify-content: center; cursor: pointer; transition: all 0.2s;
   border: 2px solid transparent; position: relative;
 }
 .time-slot.available {
   background: #f0fff4; border-color: #68d391;
 }
 .time-slot.available:hover {
   background: #c6f6d5; border-color: #48bb78; transform: scale(0.98);
 }
 .time-slot.unavailable {
   background: #fed7d7; color: #9b2c2c; cursor: not-allowed;
 }
 .time-slot.past {
   background: #f7fafc; color: #a0aec0; cursor: not-allowed;
 }
 .time-slot.selected {
   background: #bee3f8; border-color: #3182ce; border-width: 3px;
 }
 .time-slot.selected:hover {
   background: #90cdf4; border-color: #2c5282;
 }
 .slot-status {
   font-size: 0.8em; font-weight: 600; text-align: center;
 }
 .available .slot-status {
   color: #2f855a;
 }
 .unavailable .slot-status {
   color: #9b2c2c;
 }
 .past .slot-status {
   color: #a0aec0;
 }
 .legend {
   display: flex; justify-content: center; gap: 30px; margin: 20px 0;
   padding: 15px; background: #edf2f7; border-radius: 8px;
 }
 .legend-item {
   display: flex; align-items: center; gap: 8px; font-size: 0.9em;
 }
 .legend-color {
   width: 20px; height: 20px; border-radius: 4px; border: 2px solid #e2e8f0;
 }
 .legend-available { background: #f0fff4; border-color: #68d391; }
 .legend-unavailable { background: #fed7d7; border-color: #f56565; }
 .legend-past { background: #f7fafc; border-color: #e2e8f0; }
 .timezone-info {
   background: #edf2f7; border-radius: 8px; padding: 10px; margin: 15px 0;
   color: #4a5568; font-size: 0.9em; text-align: center;
 }
 @media (max-width: 768px) {
   body { padding: 10px; }
   .container { padding: 20px; }
   .calendar-grid { grid-template-columns: 60px repeat(7, 1fr); }
   .legend { flex-direction: column; gap: 10px; }
 }
</style>
<body>
<div class="container">
<h2>📅 {{title}}</h2>

<div class="welcome">
  <p>👋 Hi <strong>{{name}}</strong>! Select up to <strong>3 preferred time slots</strong> from the calendar below. The admin will pick one for you.</p>
  <div class="timezone-info">
    🌍 All times shown in <strong>Toronto Time (EST/EDT)</strong>
  </div>
  <div id="selection-status" style="background: #e6fffa; padding: 10px; border-radius: 8px; margin: 10px 0; text-align: center; color: #234e52;">
    <strong>Selected slots: <span id="slot-count">0</span>/3</strong>
    <div id="selected-times"></div>
  </div>
</div>

<div class="consent-link">
  📋 <strong>Important:</strong> Please review the <a href="/consent" target="_blank">consent form</a> before booking
</div>

{% if error %}
<div class="error">⚠️ {{error}}</div>
{% endif %}

<div class="calendar-container">
  <div class="calendar-header">
    <h3 style="margin: 0; color: #2d3748;">📅 Available Time Slots</h3>
    <div class="calendar-nav">
      {% if prev_week_url %}
      <a href="{{prev_week_url}}" class="nav-btn" style="text-decoration: none;">⬅️ Previous Week</a>
      {% else %}
      <div style="width: 120px;"></div>
      {% endif %}
      <span style="color: #4a5568; font-weight: 600; margin: 0 20px;">{{current_week_label}}</span>
      {% if next_week_url %}
      <a href="{{next_week_url}}" class="nav-btn" style="text-decoration: none;">Next Week ➡️</a>
      {% else %}
      <div style="width: 120px;"></div>
      {% endif %}
    </div>
  </div>

  <div style="text-align: center; margin: 15px 0; color: #718096;">
    {% if week_offset == 0 %}
    <strong>This Week</strong> •
    {% else %}
    <strong>Week {{week_offset + 1}}</strong> •
    {% endif %}
    Showing up to 8 weeks of availability •
    <a href="/invite/{{request.view_args.token}}" style="color: #667eea; text-decoration: none;">📅 Current Week</a>
  </div>

  <div class="legend">
    <div class="legend-item">
      <div class="legend-color legend-available"></div>
      <span>✅ Available</span>
    </div>
    <div class="legend-item">
      <div class="legend-color legend-unavailable"></div>
      <span>❌ Unavailable</span>
    </div>
    <div class="legend-item">
      <div class="legend-color legend-past"></div>
      <span>⏰ Past</span>
    </div>
  </div>

  <div class="calendar-grid">
    <div class="time-header">Time</div>
    {% for day in calendar_days %}
    <div class="day-header">{{day.header}}</div>
    {% endfor %}

    {% for hour in range(9, 25) %}
    <div class="time-label">{{hour}}:00</div>
    {% for day in calendar_days %}
    {% set slot = day.slots[hour-9] %}
    <div class="time-slot {{slot.status}}"
         {% if slot.status == 'available' %}onclick="toggleSlot('{{slot.start}}', '{{slot.end}}', this)"{% endif %}
         data-start="{{slot.start}}" data-end="{{slot.end}}">
      <div class="slot-status">
        {% if slot.status == 'available' %}✅{% elif slot.status == 'unavailable' %}❌{% else %}⏰{% endif %}
      </div>
    </div>
    {% endfor %}
    {% endfor %}
  </div>
</div>

<div style="text-align: center; margin-top: 30px; color: #4a5568;">
  💡 <strong>Tip:</strong> Click on green ✅ slots to select (up to 3). Blue = selected. Click again to deselect.
</div>

<div style="text-align: center; margin: 30px 0;">
  <button id="submit-btn" onclick="submitBooking()"
          style="background: #667eea; color: white; border: none; padding: 15px 30px;
                 border-radius: 8px; font-size: 1.1em; font-weight: 600; cursor: pointer;
                 opacity: 0.5; transition: all 0.3s;" disabled>
    📅 Submit Time Preferences
  </button>
  <div style="margin-top: 10px; font-size: 0.9em; color: #666;">
    Select at least 1 time slot to continue
  </div>
</div>

</div>

<script>
let selectedSlots = [];
const maxSlots = 3;

function toggleSlot(start, end, element) {
  const slotKey = start + '|' + end;
  const index = selectedSlots.findIndex(slot => slot.key === slotKey);

  if (index >= 0) {
    // Deselect
    selectedSlots.splice(index, 1);
    element.classList.remove('selected');
  } else {
    // Select (if under limit)
    if (selectedSlots.length >= maxSlots) {
      alert('You can only select up to 3 time slots');
      return;
    }
    selectedSlots.push({key: slotKey, start: start, end: end, element: element});
    element.classList.add('selected');
  }

  updateDisplay();
}

function updateDisplay() {
  const count = selectedSlots.length;
  document.getElementById('slot-count').textContent = count;

  const timesDiv = document.getElementById('selected-times');
  if (count === 0) {
    timesDiv.innerHTML = '';
  } else {
    const timeStrings = selectedSlots.map((slot, i) => {
      const startDate = new Date(slot.start);
      const endDate = new Date(slot.end);
      const timeStr = startDate.toLocaleDateString('en-US', {weekday: 'short', month: 'short', day: 'numeric'}) +
                     ' ' + startDate.toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit'}) +
                     '-' + endDate.toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit'});
      return `<div style="margin: 5px 0; padding: 5px; background: white; border-radius: 4px;">
                <strong>Choice ${i+1}:</strong> ${timeStr}
              </div>`;
    });
    timesDiv.innerHTML = timeStrings.join('');
  }

  const submitBtn = document.getElementById('submit-btn');
  if (count > 0) {
    submitBtn.disabled = false;
    submitBtn.style.opacity = '1';
    submitBtn.querySelector('div').textContent = `Submit ${count} time preference${count > 1 ? 's' : ''}`;
  } else {
    submitBtn.disabled = true;
    submitBtn.style.opacity = '0.5';
  }
}

function submitBooking() {
  if (selectedSlots.length === 0) {
    alert('Please select at least one time slot');
    return;
  }

  const token = window.location.pathname.split('/').pop();
  let url = `/book?token=${token}`;

  selectedSlots.forEach((slot, i) => {
    url += `&start${i+1}=${encodeURIComponent(slot.start)}&end${i+1}=${encodeURIComponent(slot.end)}`;
  });

  console.log('Submitting booking with URL:', url);
  window.location.href = url;
}
</script>

</body>
"""

@app.get("/invite/<token>")
def invite(token):
    # lookup participant
    with db() as con:
        p = con.execute("SELECT * FROM participants WHERE token=?", (token,)).fetchone()
    if not p:
        abort(404)

    # Get week offset from query parameter (0 = current week, 1 = next week, etc.)
    week_offset = int(request.args.get('week', 0))
    if week_offset < 0:
        week_offset = 0
    elif week_offset > 8:  # Limit to 8 weeks out
        week_offset = 8

    # Calculate the start date for the requested week
    today_local = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    # Get to the start of the week (Monday)
    week_start = today_local + timedelta(days=week_offset * 7)
    # Adjust to show Monday to Sunday
    days_from_monday = week_start.weekday()  # 0=Monday, 6=Sunday
    week_start = week_start - timedelta(days=days_from_monday)

    now_local = datetime.now(TZ)

    # Check if calendar service is available
    try:
        service = calendar_service()
        calendar_available = True
    except Exception as e:
        print(f"[CALENDAR ERROR] Calendar service unavailable: {e}")
        service = None
        calendar_available = False

    # Generate 7 days of calendar data (Monday to Sunday)
    calendar_days = []
    for d in range(7):
        day = week_start + timedelta(days=d)
        day_header = day.strftime("%a %m/%d").replace(" 0", " ")

        # Generate hour slots for this day (9 AM to 12 AM = 16 slots)
        day_slots = []
        for hour in range(9, 25):  # 9 AM to 12 AM
            if hour == 24:
                # Handle midnight as hour 0 of next day
                start = (day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=TZ)
            else:
                start = day.replace(hour=hour, minute=0, second=0, microsecond=0, tzinfo=TZ)
            end = start + timedelta(hours=1)

            # Determine slot status
            if start <= now_local:
                status = "past"
                url = ""
            elif not calendar_available:
                # If calendar is not connected, show all future slots as unavailable
                status = "unavailable"
                url = ""
            elif is_free(service, start, end):
                status = "available"
                q = {"token": token, "start": start.isoformat(), "end": end.isoformat()}
                url = f"{url_for('book')}?{urlencode(q)}"
            else:
                status = "unavailable"
                url = ""

            day_slots.append({
                "status": status,
                "url": url,
                "start": start.isoformat(),
                "end": end.isoformat()
            })

        calendar_days.append({
            "header": day_header,
            "slots": day_slots
        })

    # Generate week label
    start_date = week_start.strftime("%b %d").replace(" 0", " ")
    end_date = (week_start + timedelta(days=6)).strftime("%b %d").replace(" 0", " ")
    current_week_label = f"{start_date} - {end_date}"

    # Generate navigation URLs
    prev_week_url = None
    next_week_url = url_for('invite', token=token, week=week_offset+1) if week_offset < 8 else None

    if week_offset > 0:
        prev_week_url = url_for('invite', token=token, week=week_offset-1) if week_offset > 1 else url_for('invite', token=token)

    # Add error message if calendar is not available
    error_msg = request.args.get("error")
    if not calendar_available and not error_msg:
        error_msg = "Calendar system is not connected. All slots are currently unavailable. Please contact the administrator."

    return render_template_string(
        INVITE_HTML,
        title=APP_TITLE,
        name=p["name"],
        calendar_days=calendar_days,
        current_week_label=current_week_label,
        prev_week_url=prev_week_url,
        next_week_url=next_week_url,
        week_offset=week_offset,
        error=error_msg
    )

@app.get("/book")
def book():
    token = request.args.get("token","")
    # Get up to 3 time slot preferences
    slots = []
    for i in range(1, 4):
        start_s = request.args.get(f"start{i}","")
        end_s   = request.args.get(f"end{i}","")
        if start_s and end_s:
            slots.append((start_s, end_s))

    if not (token and slots):
        abort(400)

    with db() as con:
        p = con.execute("SELECT * FROM participants WHERE token=?", (token,)).fetchone()
    if not p:
        abort(404)

    # Validate each time slot
    validated_slots = []
    svc = calendar_service()
    nowl = datetime.now(TZ)

    for start_s, end_s in slots:
        start = datetime.fromisoformat(start_s)
        end   = datetime.fromisoformat(end_s)

        # enforce business rules
        if start.tzinfo is None or end.tzinfo is None:
            start = start.replace(tzinfo=TZ); end = end.replace(tzinfo=TZ)
        if end - start != timedelta(hours=1):
            return redirect(url_for("invite", token=token, error="Invalid slot length."))
        if start <= nowl:
            return redirect(url_for("invite", token=token, error="That time is in the past."))
        if not (start.hour >= 9 and end.hour <= 25):
            return redirect(url_for("invite", token=token, error="Outside bookable hours."))

        # check availability
        if not is_free(svc, start, end):
            return redirect(url_for("invite", token=token, error=f"Sorry, one of your selected slots was just taken."))

        validated_slots.append((start, end))

    # Store booking request with multiple preferences
    with db() as con:
        # Build dynamic SQL based on number of slots provided
        if len(validated_slots) == 1:
            con.execute("""
                INSERT INTO bookings (participant_id, preference1_start, preference1_end, status)
                VALUES (?, ?, ?, 'pending')
            """, (p['id'], validated_slots[0][0].isoformat(), validated_slots[0][1].isoformat()))
        elif len(validated_slots) == 2:
            con.execute("""
                INSERT INTO bookings (participant_id, preference1_start, preference1_end,
                                    preference2_start, preference2_end, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
            """, (p['id'], validated_slots[0][0].isoformat(), validated_slots[0][1].isoformat(),
                  validated_slots[1][0].isoformat(), validated_slots[1][1].isoformat()))
        elif len(validated_slots) == 3:
            con.execute("""
                INSERT INTO bookings (participant_id, preference1_start, preference1_end,
                                    preference2_start, preference2_end,
                                    preference3_start, preference3_end, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            """, (p['id'], validated_slots[0][0].isoformat(), validated_slots[0][1].isoformat(),
                  validated_slots[1][0].isoformat(), validated_slots[1][1].isoformat(),
                  validated_slots[2][0].isoformat(), validated_slots[2][1].isoformat()))

    # Format times for display
    slot_display = []
    for i, (start, end) in enumerate(validated_slots, 1):
        start_str = start.strftime('%a %b %d, %I:%M %p').replace(' 0', ' ')
        end_str = end.strftime('%I:%M %p').replace(' 0', ' ')
        slot_display.append(f"<strong>Option {i}:</strong> {start_str} – {end_str}")

    slots_html = "<br>".join(slot_display)

    confirmation_html = f"""
    <!doctype html><meta charset='utf-8'>
    <title>Booking Request Submitted</title>
    <style>
    body{{font-family:system-ui;max-width:600px;margin:40px auto;padding:20px;text-align:center}}
    .pending{{background:#fef5e7;color:#92400e;padding:20px;border-radius:10px;margin:20px 0;border:2px solid #fbbf24}}
    .info{{background:#e6fffa;color:#234e52;padding:15px;border-radius:8px;margin:15px 0}}
    .next-steps{{background:#f0f9ff;color:#1e40af;padding:15px;border-radius:8px;margin:15px 0}}
    </style>
    <div class="pending">
        <h2>⏳ Booking Request Submitted!</h2>
        <p><strong>Your Time Slot Preferences:</strong><br>
        {slots_html}<br><br>
        (Toronto time)</p>
    </div>
    <div class="info">
        📝 <strong>What happens next:</strong><br>
        Your booking request with {len(validated_slots)} time slot preference(s) has been submitted and is pending admin approval.
        The admin will select one of your preferred times.
    </div>
    <div class="next-steps">
        ✅ <strong>Once approved by admin:</strong><br>
        • You'll receive a Google Calendar invitation<br>
        • The event will be added to your calendar<br>
        • You'll get automatic email reminders<br>
        • You'll receive a confirmation email
    </div>
    <div class="info">
        🕒 <strong>Timeline:</strong> You can expect to hear back within 24 hours.
    </div>
    <p><a href='{url_for('invite', token=token)}'>← Back to Available Slots</a></p>
    """
    return confirmation_html

# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Use debug=False in production
    debug_mode = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    app.run(host="0.0.0.0", port=PORT, debug=debug_mode)
