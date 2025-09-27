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
HOST_BASE = os.getenv("HOST_BASE", f"http://localhost:{PORT}")      # e.g. https://localhost:5000
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
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            calendar_event_id TEXT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            admin_confirmed_at TIMESTAMP NULL,
            FOREIGN KEY (participant_id) REFERENCES participants (id)
        );
        """)
        # defaults
        if not con.execute("SELECT 1 FROM settings WHERE k='email_body'").fetchone():
            con.execute("INSERT INTO settings(k,v) VALUES(?,?)",
                        ("email_body",
                         "Hi {{name}},\\n\\nThank you for volunteering to participate in our user study!\\n\\nPlease access our interactive calendar and select your preferred time slot:\\n\\n🔗 CALENDAR LINK: {{link}}\\n\\nThis will take you to our availability calendar where you can see all open time slots."))
        if not con.execute("SELECT 1 FROM settings WHERE k='consent_html'").fetchone():
            con.execute("INSERT INTO settings(k,v) VALUES(?,?)",
                        ("consent_html",
                         "<h2>Consent Form</h2><p>Please read this consent carefully before booking. You agree to participate voluntarily. Contact us with any questions.</p>"))

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
    """Yield (start_local, end_local) 1-hour slots 09:00..22:00 (last start 21:00)."""
    base = day_local.replace(hour=9, minute=0, second=0, microsecond=0, tzinfo=TZ)
    for h in range(0, 13):  # 9..21 inclusive
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
        result = service.users().messages().send(
            userId='me',
            body={'raw': raw_message}
        ).execute()

        print(f"[GMAIL API] Email sent successfully. Message ID: {result.get('id')}")
        return "SUCCESS"

    except Exception as e:
        error_msg = f"Gmail API error: {str(e)}"
        print(f"[GMAIL API ERROR] {error_msg}")
        return f"ERROR: {error_msg}"

def send_initial_email(to_email, to_name, link):
    body_tpl = get_setting("email_body")
    body = body_tpl.replace("{{name}}", to_name).replace("{{link}}", link)

    # Enhanced email body with calendar selection info
    enhanced_body = f"""
{body}

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
User Study Booking System
"""

    subject = "📅 User Study Invitation - Pick Your Time Slot"

    # Try Gmail API first, fallback to SMTP
    print(f"[EMAIL] Attempting Gmail API first for {to_email}")
    result = send_email_with_gmail_api(to_email, to_name, subject, enhanced_body)
    print(f"[EMAIL] Gmail API result: {result}")

    if result == "SUCCESS":
        return result

    # Fallback to SMTP if Gmail API fails
    print(f"[EMAIL] Gmail API failed with: {result}")
    print(f"[EMAIL] Trying SMTP fallback...")

    if not SMTP_HOST:
        print(f"[DRY-RUN EMAIL] To: {to_name} <{to_email}>\n{enhanced_body}\n")
        return "DRY-RUN: No SMTP configured"

    try:
        print(f"[EMAIL] Attempting to send email via SMTP to {to_email}")
        msg = EmailMessage()
        msg["From"] = SMTP_FROM
        msg["To"] = f"{to_name} <{to_email}>"
        msg["Subject"] = subject
        msg.set_content(enhanced_body)

        # Add timeout and better error handling
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            print(f"[EMAIL] Connected to SMTP server {SMTP_HOST}")
            s.starttls()
            print(f"[EMAIL] STARTTLS enabled")
            s.login(SMTP_USER, SMTP_PASS)
            print(f"[EMAIL] Logged in as {SMTP_USER}")
            s.send_message(msg)
            print(f"[EMAIL] Email sent successfully to {to_email}")
        return "SUCCESS"
    except smtplib.SMTPAuthenticationError as e:
        error_msg = f"SMTP Authentication failed: {str(e)}"
        print(f"[EMAIL ERROR] {error_msg}")
        return f"ERROR: {error_msg}"
    except smtplib.SMTPException as e:
        error_msg = f"SMTP error: {str(e)}"
        print(f"[EMAIL ERROR] {error_msg}")
        return f"ERROR: {error_msg}"
    except Exception as e:
        error_msg = f"General error: {str(e)}"
        print(f"[EMAIL ERROR] {error_msg}")
        return f"ERROR: {error_msg}"

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

    # Test Gmail API
    try:
        result = send_email_with_gmail_api("test@example.com", "Test User", "Test Subject", "Test Body")
        debug_info["GMAIL_TEST"] = result
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
 h1 {
   margin: 0 0 30px; color: #2d3748; font-size: 2.5em;
   background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
   -webkit-background-clip: text; -webkit-text-fill-color: transparent;
 }
 .status-bar {
   background: #f7fafc; border: 1px solid #e2e8f0; border-radius: 12px;
   padding: 16px; margin-bottom: 30px; display: flex; justify-content: space-between;
 }
 .status-item { display: flex; align-items: center; gap: 8px; }
 .status-icon { width: 20px; height: 20px; }
 form {
   margin: 30px 0; padding: 30px; background: #f8fafc;
   border: 1px solid #e2e8f0; border-radius: 16px;
 }
 h3 { color: #2d3748; margin: 0 0 20px; font-size: 1.3em; }
 label {
   display: block; font-weight: 600; margin: 15px 0 8px;
   color: #4a5568; font-size: 0.95em;
 }
 input[type=text], input[type=email], textarea {
   width: 100%; padding: 12px 16px; border: 2px solid #e2e8f0;
   border-radius: 10px; font-size: 16px; transition: all 0.2s;
   font-family: inherit;
 }
 input[type=text]:focus, input[type=email]:focus, textarea:focus {
   outline: none; border-color: #667eea; box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
 }
 textarea { resize: vertical; min-height: 120px; }
 .btn {
   background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
   color: white; border: none; padding: 12px 24px; border-radius: 10px;
   cursor: pointer; font-weight: 600; font-size: 16px; transition: all 0.2s;
   font-family: inherit;
 }
 .btn:hover { transform: translateY(-2px); box-shadow: 0 10px 20px rgba(102, 126, 234, 0.3); }
 .muted { color: #718096; font-size: 0.9em; }
 .warn { color: #e53e3e; font-weight: 600; }
 .success { color: #38a169; font-weight: 600; }
 .success-msg { background: #c6f6d5; color: #2f855a; border-radius: 10px; padding: 15px; margin: 20px 0; font-weight: 600; }
 code { background: #edf2f7; padding: 4px 8px; border-radius: 6px; font-family: 'SF Mono', Monaco, monospace; }
 .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 30px; }
 .booking-item {
   background: white; border: 2px solid #e2e8f0; border-radius: 12px;
   padding: 20px; margin: 15px 0; display: flex; justify-content: space-between;
   align-items: center; transition: all 0.2s;
 }
 .booking-item:hover { border-color: #667eea; box-shadow: 0 4px 12px rgba(102, 126, 234, 0.1); }
 .booking-info { flex: 1; }
 .booking-time { font-weight: 600; color: #2d3748; font-size: 1.1em; }
 .booking-participant { color: #4a5568; margin: 5px 0; }
 .booking-date { color: #718096; font-size: 0.9em; }
 .booking-actions { display: flex; gap: 10px; }
 .btn-approve { background: #48bb78; }
 .btn-approve:hover { background: #38a169; }
 .btn-reject { background: #f56565; }
 .btn-reject:hover { background: #e53e3e; }
 .badge {
   display: inline-block; padding: 4px 12px; border-radius: 20px;
   font-size: 0.8em; font-weight: 600;
 }
 .badge-pending { background: #fef5e7; color: #92400e; }
 @media (max-width: 768px) {
   .grid { grid-template-columns: 1fr; }
   body { padding: 10px; }
   .container { padding: 20px; }
   .booking-item { flex-direction: column; align-items: flex-start; gap: 15px; }
   .booking-actions { align-self: stretch; justify-content: flex-end; }
 }
</style>
<body>
<div class="container">
<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
  <h1>📅 Admin Dashboard</h1>
  <div style="text-align: right;">
    <div style="color: #4a5568; font-size: 14px;">👤 {{user_name or user_email}}</div>
    <div style="color: #718096; font-size: 12px;">{{user_email}}</div>
    <a href="/logout" style="color: #e53e3e; font-size: 12px; text-decoration: none;">🚪 Logout</a>
  </div>
</div>

<div class="status-bar">
  <div class="status-item">
    <span>📋 Calendar ID:</span>
    <code>{{cal_id}}</code>
  </div>
  <div class="status-item">
    {% if authed %}
      <span class="success">✅ Google Calendar Connected</span>
    {% else %}
      <span class="warn">❌ Not Connected</span>
      <a href="/google-auth" class="btn" style="margin-left: 10px; padding: 6px 12px; font-size: 14px;">Connect Now</a>
    {% endif %}
  </div>
  <div class="status-item">
    {% if gmail_ready %}
      <span class="success">📧 Gmail Ready</span>
    {% else %}
      <span class="warn">❌ Gmail Not Ready</span>
      <a href="/force-gmail-auth" class="btn" style="margin-left: 10px; padding: 6px 12px; font-size: 14px;">Fix Gmail Permissions</a>
    {% endif %}
  </div>
</div>

{% if success %}
<div class="success-msg">✅ {{success}}</div>
{% endif %}

{% if pending_bookings %}
<form method="post" action="/admin/bookings">
  <h3>⏳ Pending Booking Approvals ({{pending_bookings|length}})</h3>
  {% for booking in pending_bookings %}
  <div class="booking-item">
    <div class="booking-info">
      <div class="booking-time">
        {{booking.start_time_formatted}} – {{booking.end_time_formatted}}
      </div>
      <div class="booking-participant">
        👤 <strong>{{booking.name}}</strong> ({{booking.email}})
      </div>
      <div class="booking-date">
        📅 Requested: {{booking.created_at_formatted}}
      </div>
    </div>
    <div class="booking-actions">
      <button type="submit" name="action" value="approve_{{booking.id}}" class="btn btn-approve">
        ✅ Approve
      </button>
      <button type="submit" name="action" value="reject_{{booking.id}}" class="btn btn-reject">
        ❌ Reject
      </button>
    </div>
  </div>
  {% endfor %}
</form>
{% endif %}

<div class="grid">
  <form method="post" action="/admin/participant">
    <h3>➕ Add New Participant</h3>
    <label>👤 Full Name</label>
    <input name="name" required placeholder="Enter participant's full name" value="">

    <label>📧 Email Address</label>
    <input type="email" name="email" required placeholder="participant@email.com" value="">

    <button class="btn" type="submit">Create Booking Link & Send Email</button>
    <p class="muted">📤 Email will be sent automatically from your configured SMTP account</p>
  </form>

  <form method="post" action="/admin/email">
    <h3>📝 Email Template</h3>
    <label>Email Body (use {{name}} and {{link}} placeholders)</label>
    <textarea name="body" rows="6" placeholder="Hi {{name}}, please book your slot: {{link}}">{{ email_body }}</textarea>
    <button class="btn">💾 Save Email Template</button>
  </form>
</div>

<form method="post" action="/admin/consent">
  <h3>📋 Consent Form (HTML)</h3>
  <label>HTML Content (participants see this before booking)</label>
  <textarea name="html" rows="6" placeholder="<h2>Research Study Consent</h2><p>Your consent form content here...</p>">{{ consent_html }}</textarea>
  <button class="btn">💾 Save HTML Consent</button>
</form>

<form method="post" action="/admin/upload-consent" enctype="multipart/form-data">
  <h3>📄 Upload Consent File</h3>
  <label>Upload PDF or DOC file (max 16MB)</label>
  <input type="file" name="consent_file" accept=".pdf,.doc,.docx" required style="margin: 10px 0;">
  <button class="btn">📤 Upload Consent File</button>
  <p class="muted">🔗 Uploaded files will be available at <a href="/consent" target="_blank">/consent</a></p>

  {% if consent_files %}
  <div style="margin-top: 20px; padding-top: 15px; border-top: 1px solid #e2e8f0;">
    <strong>📁 Uploaded Files:</strong>
    {% for file in consent_files %}
    <div style="margin: 8px 0; padding: 8px; background: #f8fafc; border-radius: 6px;">
      <a href="/uploads/{{file.filename}}" target="_blank" style="color: #667eea;">{{file.original_name}}</a>
      <span class="muted" style="float: right;">{{file.upload_date}}</span>
    </div>
    {% endfor %}
  </div>
  {% endif %}
</form>

</div>
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

    # Get pending bookings with formatted times
    pending_bookings = []
    for booking in get_pending_bookings():
        start_dt = datetime.fromisoformat(booking['start_time'])
        end_dt = datetime.fromisoformat(booking['end_time'])
        created_dt = datetime.fromisoformat(booking['created_at'])

        formatted_booking = dict(booking)
        formatted_booking['start_time_formatted'] = start_dt.strftime('%a %b %d, %I:%M %p').replace(' 0', ' ')
        formatted_booking['end_time_formatted'] = end_dt.strftime('%I:%M %p').replace(' 0', ' ')
        formatted_booking['created_at_formatted'] = created_dt.strftime('%m/%d %I:%M %p').replace(' 0', ' ')
        pending_bookings.append(formatted_booking)

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
    if not action:
        return redirect(url_for("admin"))

    if action.startswith("approve_"):
        booking_id = action.split("_")[1]

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

        # Create calendar event
        start_dt = datetime.fromisoformat(booking['start_time'])
        end_dt = datetime.fromisoformat(booking['end_time'])

        try:
            svc = calendar_service()
            event = {
                "summary": f"User Study — {booking['name']}",
                "description": f"Participant: {booking['name']} <{booking['email']}>\nConsent: {HOST_BASE}/consent\n\nStatus: CONFIRMED by Admin",
                "start": {"dateTime": to_iso_utc(start_dt), "timeZone": "UTC"},
                "end":   {"dateTime": to_iso_utc(end_dt),   "timeZone": "UTC"},
                "attendees": [{"email": booking["email"]}],
            }
            created = svc.events().insert(
                calendarId=CALENDAR_ID,
                body=event,
                sendUpdates="all"
            ).execute()

            # Update booking status
            with db() as con:
                con.execute("""
                    UPDATE bookings
                    SET status = 'confirmed',
                        calendar_event_id = ?,
                        admin_confirmed_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (created['id'], booking_id))

            # Send confirmation email
            send_confirmation_email(booking['email'], booking['name'],
                                   booking['start_time'], booking['end_time'])

            return redirect(url_for("admin") + "?msg=booking_approved")

        except Exception as e:
            print(f"[BOOKING APPROVAL ERROR] {e}")
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

    return redirect(url_for("admin"))

@app.post("/admin/participant")
@require_auth
def admin_participant():
    name = request.form["name"].strip()
    email = request.form["email"].strip().lower()
    token = secrets.token_urlsafe(16)
    with db() as con:
        con.execute("INSERT INTO participants(name,email,token) VALUES(?,?,?)", (name, email, token))
    link = f"{HOST_BASE}/invite/{token}"
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
  <p>👋 Hi <strong>{{name}}</strong>! Select your preferred 1-hour time slot from the calendar below.</p>
  <div class="timezone-info">
    🌍 All times shown in <strong>Toronto Time (EST/EDT)</strong>
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
      <span style="color: #4a5568; font-weight: 600;">{{current_week_label}}</span>
    </div>
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

    {% for hour in range(9, 22) %}
    <div class="time-label">{{hour}}:00</div>
    {% for day in calendar_days %}
    {% set slot = day.slots[hour-9] %}
    <div class="time-slot {{slot.status}}"
         {% if slot.status == 'available' %}onclick="window.location.href='{{slot.url}}'"{% endif %}>
      <div class="slot-status">
        {% if slot.status == 'available' %}✅{% elif slot.status == 'unavailable' %}❌{% else %}⏰{% endif %}
      </div>
    </div>
    {% endfor %}
    {% endfor %}
  </div>
</div>

<div style="text-align: center; margin-top: 30px; color: #4a5568;">
  💡 <strong>Tip:</strong> Click on any green ✅ slot to book that time
</div>

</div>
</body>
"""

@app.get("/invite/<token>")
def invite(token):
    # lookup participant
    with db() as con:
        p = con.execute("SELECT * FROM participants WHERE token=?", (token,)).fetchone()
    if not p:
        abort(404)

    # build calendar view for the next 7 days starting today
    today_local = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    now_local = datetime.now(TZ)

    # Check if calendar service is available
    try:
        service = calendar_service()
        calendar_available = True
    except Exception as e:
        print(f"[CALENDAR ERROR] Calendar service unavailable: {e}")
        service = None
        calendar_available = False

    # Generate 7 days of calendar data
    calendar_days = []
    for d in range(7):
        day = today_local + timedelta(days=d)
        day_header = day.strftime("%a %m/%d").replace(" 0", " ")

        # Generate hour slots for this day (9 AM to 9 PM = 13 slots)
        day_slots = []
        for hour in range(9, 22):  # 9 AM to 9 PM
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
                "url": url
            })

        calendar_days.append({
            "header": day_header,
            "slots": day_slots
        })

    # Generate week label
    start_date = today_local.strftime("%b %d").replace(" 0", " ")
    end_date = (today_local + timedelta(days=6)).strftime("%b %d").replace(" 0", " ")
    current_week_label = f"{start_date} - {end_date}"

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
        error=error_msg
    )

@app.get("/book")
def book():
    token = request.args.get("token","")
    start_s = request.args.get("start","")
    end_s   = request.args.get("end","")
    if not (token and start_s and end_s):
        abort(400)

    with db() as con:
        p = con.execute("SELECT * FROM participants WHERE token=?", (token,)).fetchone()
    if not p:
        abort(404)

    start = datetime.fromisoformat(start_s)
    end   = datetime.fromisoformat(end_s)
    nowl  = datetime.now(TZ)
    # enforce business rules
    if start.tzinfo is None or end.tzinfo is None:
        start = start.replace(tzinfo=TZ); end = end.replace(tzinfo=TZ)
    if end - start != timedelta(hours=1):
        return redirect(url_for("invite", token=token, error="Invalid slot length."))
    if start <= nowl:
        return redirect(url_for("invite", token=token, error="That time is in the past."))
    if not (start.hour >= 9 and end.hour <= 22):
        return redirect(url_for("invite", token=token, error="Outside bookable hours."))

    # check availability again
    svc = calendar_service()
    if not is_free(svc, start, end):
        return redirect(url_for("invite", token=token, error="Sorry, that slot was just taken."))

    # Store booking request pending admin approval
    with db() as con:
        con.execute("""
            INSERT INTO bookings (participant_id, start_time, end_time, status)
            VALUES (?, ?, ?, 'pending')
        """, (p['id'], start.isoformat(), end.isoformat()))

    # Format time for display
    start_str = start.strftime('%a %b %d, %I:%M %p').replace(' 0', ' ')
    end_str = end.strftime('%I:%M %p').replace(' 0', ' ')

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
        <p><strong>{start_str} – {end_str}</strong><br>(Toronto time)</p>
    </div>
    <div class="info">
        📝 <strong>What happens next:</strong><br>
        Your booking request has been submitted and is pending admin approval.
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
