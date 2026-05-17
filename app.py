import os
import shutil
import secrets
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file, session, g
import sqlite3
from datetime import datetime, timedelta, timezone
import pandas as pd
import io
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import re
import threading
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage

# --- Load Environment Variables from .env ---
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(env_path):
    with open(env_path, 'r') as f:
        for line in f:
            if '=' in line and not line.strip().startswith('#'):
                key, val = line.strip().split('=', 1)
                os.environ[key.strip()] = val.strip()
# ------------------------------------------

# ─── Smart Insights Engine ────────────────────────────────────────────────────

BP_THRESHOLDS = [
    (None, None, 'normal',       'Normal',                    '✅'),
    (120,   80,  'elevated',     'Elevated',                  '⚠️'),
    (130,   80,  'stage1',       'Stage 1 Hypertension',      '🟠'),
    (140,   90,  'stage2',       'Stage 2 Hypertension',      '🔴'),
    (180,  120,  'crisis',       'Hypertensive Crisis',       '🚨'),
]

def get_bp_status(systolic, diastolic):
    """Return (level, label, emoji) for a blood pressure reading."""
    try:
        s, d = int(systolic), int(diastolic)
    except (TypeError, ValueError):
        return 'unknown', 'Unknown', '❓'

    if s >= 180 or d >= 120:
        return 'crisis',  'Hypertensive Crisis',   '🚨'
    if s >= 140 or d >= 90:
        return 'stage2',  'Stage 2 Hypertension',  '🔴'
    if s >= 130 or d >= 80:
        return 'stage1',  'Stage 1 Hypertension',  '🟠'
    if s >= 120:
        return 'elevated','Elevated',               '⚠️'
    return     'normal',  'Normal',                '✅'

def get_sugar_status(sugar):
    """Return (level, label, emoji) for a blood sugar reading."""
    try:
        val = float(sugar)
    except (TypeError, ValueError):
        return 'unknown', 'Unknown', '❓'

    if val >= 126:
        return 'high',       'Diabetic Range',  '🔴'
    if val >= 100:
        return 'prediabetic','Pre-Diabetic',    '⚠️'
    return     'normal',     'Normal',          '✅'

HEALTH_TIPS = {
    # (bp_level, sugar_level): tip
    ('normal',   'normal'):      'Great job! Keep up your healthy lifestyle. Stay active and hydrated.',
    ('normal',   'prediabetic'): 'Your blood sugar is slightly elevated. Limit rice, sugary drinks, and sweets.',
    ('normal',   'high'):        'Your blood sugar is in diabetic range. Please consult a physician soon.',
    ('elevated', 'normal'):      'Your BP is slightly elevated. Reduce salt and caffeine intake.',
    ('elevated', 'prediabetic'): 'Both readings need attention. Improve diet, exercise more, and reduce salt.',
    ('elevated', 'high'):        'Concerning readings. Please consult a doctor for a full evaluation.',
    ('stage1',   'normal'):      'Stage 1 Hypertension detected. Monitor your BP daily and reduce stress.',
    ('stage1',   'prediabetic'): 'Multiple risk factors present. A doctor visit is strongly recommended.',
    ('stage1',   'high'):        'High-risk readings. Please see a physician as soon as possible.',
    ('stage2',   'normal'):      'Stage 2 Hypertension is serious. Seek medical attention promptly.',
    ('stage2',   'prediabetic'): 'Critical health risks detected. Please see a doctor immediately.',
    ('stage2',   'high'):        'URGENT: Very high risk readings. Please seek medical care immediately.',
    ('crisis',   'normal'):      'EMERGENCY: Hypertensive Crisis. Seek emergency medical care NOW.',
    ('crisis',   'prediabetic'): 'EMERGENCY: Critical BP and blood sugar. Seek emergency care NOW.',
    ('crisis',   'high'):        'EMERGENCY: Extremely dangerous readings. Call emergency services NOW.',
}

def get_health_tip(bp_level, sugar_level):
    """Return a personalized tip based on combined BP and sugar status."""
    key = (bp_level, sugar_level)
    return HEALTH_TIPS.get(key, 'Maintain a healthy diet, exercise regularly, and stay hydrated.')

def get_overall_risk(bp_level, sugar_level):
    """Return overall risk color class: success, warning, danger, critical."""
    order = ['normal', 'elevated', 'prediabetic', 'stage1', 'stage2', 'high', 'crisis']
    risk_map = {
        'normal':      'success',
        'elevated':    'warning',
        'prediabetic': 'warning',
        'stage1':      'danger',
        'stage2':      'danger',
        'high':        'danger',
        'crisis':      'critical',
    }
    bp_risk   = risk_map.get(bp_level,    'success')
    sug_risk  = risk_map.get(sugar_level, 'success')
    # Return the more severe
    severity = ['success', 'warning', 'danger', 'critical']
    return severity[max(severity.index(bp_risk), severity.index(sug_risk))]

def get_bmi_status(height_cm, weight_kg):
    """Return (bmi_value, category_label, css_level) using WHO Asia-Pacific thresholds."""
    try:
        h = float(height_cm)
        w = float(weight_kg)
        if h <= 0 or w <= 0:
            return None, 'N/A', 'unknown'
        bmi = w / ((h / 100) ** 2)
    except (TypeError, ValueError):
        return None, 'N/A', 'unknown'

    if bmi < 18.5:
        return round(bmi, 1), 'Underweight',   'elevated'
    if bmi < 23.0:
        return round(bmi, 1), 'Normal Weight', 'normal'
    if bmi < 27.5:
        return round(bmi, 1), 'Overweight',    'stage1'
    return round(bmi, 1), 'Obese', 'stage2'

def calculate_age(dob_string):
    if not dob_string:
        return '—'
    try:
        dob = datetime.strptime(dob_string, '%Y-%m-%d')
        today = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except ValueError:
        return '—'

# ─── Email Notification Engine ────────────────────────────────────────────────
def send_email_alert_async(email_address, subject, message_plain, message_html=None):
    SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
    SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
    SMTP_USERNAME = os.environ.get('SMTP_USERNAME', '')
    SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '') # Google App Password

    if not SMTP_USERNAME or not SMTP_PASSWORD:
        print(f"\n[MOCK EMAIL] Would send to {email_address}")
        print(f"Subject: {subject}")
        print(f"Body:\n{message_plain}")
        print("[MOCK EMAIL] To send real emails, set SMTP_USERNAME and SMTP_PASSWORD in environment variables.\n")
        return

    try:
        msg_root = MIMEMultipart('related')
        msg_root['Subject'] = subject
        msg_root['From'] = SMTP_USERNAME
        msg_root['To'] = email_address

        msg_alternative = MIMEMultipart('alternative')
        msg_root.attach(msg_alternative)

        part1 = MIMEText(message_plain, 'plain')
        msg_alternative.attach(part1)
        
        if message_html:
            part2 = MIMEText(message_html, 'html')
            msg_alternative.attach(part2)
            
            try:
                base_dir = os.path.dirname(os.path.abspath(__file__))
                logo_path = os.path.join(base_dir, 'static', 'img', 'denr_logo.png')
                if os.path.exists(logo_path):
                    with open(logo_path, 'rb') as f:
                        img = MIMEImage(f.read())
                        img.add_header('Content-ID', '<denr_logo>')
                        # Omit Content-Disposition entirely to prevent Gmail from showing an attachment snippet
                        msg_root.attach(img)
            except Exception as e:
                print(f"[EMAIL WARNING] Could not attach images: {e}")

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg_root)
        server.quit()
        print(f"[EMAIL SUCCESS] Sent alert to {email_address}")
    except Exception as e:
        print(f"[EMAIL ERROR] Exception: {e}")

def trigger_health_alert(personnel_name, email_address, risk_level, bp, sugar):
    if not email_address or not email_address.strip():
        return
        
    subject = f"URGENT: Health Alert ({risk_level.upper()}) - CENRO Don Carlos"
    message_plain = (f"Hi {personnel_name},\n\n"
               f"Your latest checkup shows a {risk_level.upper()} health risk level.\n"
               f"Blood Pressure: {bp} mmHg\n"
               f"Blood Sugar: {sugar} mg/dL\n\n"
               "Please consult a physician immediately.\n\n"
               "Stay safe,\n"
               "CENRO Don Carlos Health Monitoring System")
               
    color = "#E53935" if risk_level in ('danger', 'critical') else "#F57C00"
    message_html = f'''
    <div style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; max-width: 600px; margin: 0 auto; border: 1px solid #1B5E20; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 15px rgba(27, 94, 32, 0.1);">
        <div style="background-color: #1B5E20; padding: 25px 20px; text-align: center; border-bottom: 4px solid #FBC02D;">
            <img src="cid:denr_logo" alt="DENR Logo" height="80" style="display: block; margin: 0 auto 15px auto;">
            <h2 style="margin: 0; font-size: 24px; color: #FBC02D; letter-spacing: 1px; text-transform: uppercase; text-shadow: 1px 1px 3px rgba(0,0,0,0.5);">CENRO Don Carlos</h2>
            <p style="margin: 5px 0 0 0; color: #E8F5E9; font-size: 14px; letter-spacing: 2px; text-shadow: 1px 1px 2px rgba(0,0,0,0.5);">HEALTH & WELLNESS MONITORING</p>
        </div>
        <div style="background-color: {color}; color: white; padding: 12px 20px; text-align: center;">
            <h3 style="margin: 0; font-size: 18px; letter-spacing: 2px; text-transform: uppercase;">⚠ Health Risk Alert: {risk_level.upper()}</h3>
        </div>
        <div style="padding: 30px; background-color: #ffffff;">
            <p style="font-size: 16px; color: #2C3E50;">Hi <strong>{personnel_name}</strong>,</p>
            <p style="font-size: 16px; color: #2C3E50; line-height: 1.5;">Your latest checkup indicates a <span style="color: {color}; font-weight: 700; padding: 2px 6px; background-color: rgba(0,0,0,0.05); border-radius: 4px;">{risk_level.upper()}</span> health risk level. Please review your recorded vitals below:</p>
            
            <div style="background-color: #F4F7F5; border-left: 5px solid {color}; padding: 20px; margin: 25px 0; border-radius: 0 6px 6px 0;">
                <table style="width: 100%; font-size: 15px; color: #000000;">
                    <tr>
                        <td style="padding: 8px 0; border-bottom: 1px solid #E0E6E1;"><strong>Blood Pressure:</strong></td>
                        <td style="padding: 8px 0; text-align: right; border-bottom: 1px solid #E0E6E1; font-weight: 600;">{bp} mmHg</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0; padding-top: 12px;"><strong>Blood Sugar:</strong></td>
                        <td style="padding: 8px 0; text-align: right; padding-top: 12px; font-weight: 600;">{sugar} mg/dL</td>
                    </tr>
                </table>
            </div>
            
            <p style="font-size: 16px; font-weight: bold; color: {color}; text-align: center; margin-top: 30px; padding: 15px; background-color: rgba(229, 57, 53, 0.05); border-radius: 6px;">
                Please consult a physician immediately.
            </p>
            
            <hr style="border: none; border-top: 1px solid #E0E6E1; margin: 30px 0;">
            <p style="font-size: 12px; color: #7F8C8D; text-align: center; margin: 0; line-height: 1.6;">
                This is an automated message from the<br>
                <strong style="color: #1B5E20;">CENRO Don Carlos Health Monitoring System</strong><br>
                Department of Environment and Natural Resources
            </p>
        </div>
    </div>
    '''
               
    thread = threading.Thread(target=send_email_alert_async, args=(email_address, subject, message_plain, message_html))
    thread.start()
# ──────────────────────────────────────────────────────────────────────────────


app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'cenro_dc_health_super_secret_key_2026')
app.permanent_session_lifetime = timedelta(minutes=15)

# Session Security Configurations
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_ENV') == 'production'

# Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'health_monitor.db')
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')

UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads', 'evidence')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB upload limit to prevent memory crashes
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

# ─── Automated Database Backup ────────────────────────────────────────────────
def backup_database():
    if os.path.exists(DATABASE):
        # Create a backup filename with the current date (Ph Time)
        date_str = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)).strftime('%Y-%m-%d')
        backup_file = os.path.join(BACKUP_DIR, f'health_monitor_backup_{date_str}.db')
        
        # Only copy if we haven't backed up today
        if not os.path.exists(backup_file):
            try:
                shutil.copy2(DATABASE, backup_file)
                print(f"[BACKUP SUCCESS] Database automatically backed up to {backup_file}")
            except Exception as e:
                print(f"[BACKUP ERROR] Failed to backup database: {e}")

backup_database()
# ──────────────────────────────────────────────────────────────────────────────

@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    
    # Auto-inject CSRF token into all HTML forms
    if response.content_type and 'text/html' in response.content_type:
        token = session.get('csrf_token')
        if token:
            html = response.get_data(as_text=True)
            injection = f'<input type="hidden" name="csrf_token" value="{token}"></form>'
            new_html = html.replace('</form>', injection)
            response.set_data(new_html)
            
    return response

@app.before_request
def security_checks():
    # Force session saving so the new token is written
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
        
    # Session Role Verification
    if 'user_id' in session:
        if request.endpoint and 'static' not in request.endpoint:
            db = get_db()
            user = db.execute('SELECT role FROM users WHERE id = ?', (session['user_id'],)).fetchone()
            if not user:
                session.clear()
                flash('Your account has been removed. Please log in again.', 'danger')
                return redirect(url_for('login'))
            if user['role'] != session.get('role'):
                session['role'] = user['role']
                flash('Your access role was updated by an administrator. Please review your permissions.', 'warning')

    # CSRF Protection
    if request.method == "POST":
        token = session.get('csrf_token')
        form_token = request.form.get('csrf_token')
        if not token or token != form_token:
            flash('Invalid or expired security token. Please try submitting again.', 'danger')
            return redirect(request.url)

@app.before_request
def make_session_permanent():
    session.permanent = True

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.execute('''
            CREATE TABLE IF NOT EXISTS personnel (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT NOT NULL,
                middle_name TEXT,
                last_name TEXT NOT NULL,
                designation TEXT,
                height_cm REAL,
                weight_kg REAL,
                date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Migrate existing tables — add height/weight if they don't exist yet
        existing_cols = [row[1] for row in db.execute('PRAGMA table_info(personnel)').fetchall()]
        if 'date_of_birth' not in existing_cols:
            db.execute('ALTER TABLE personnel ADD COLUMN date_of_birth DATE')
        if 'gender' not in existing_cols:
            db.execute('ALTER TABLE personnel ADD COLUMN gender TEXT DEFAULT ""')
        if 'height_cm' not in existing_cols:
            db.execute('ALTER TABLE personnel ADD COLUMN height_cm REAL')
        if 'weight_kg' not in existing_cols:
            db.execute('ALTER TABLE personnel ADD COLUMN weight_kg REAL')
        if 'mobile_number' not in existing_cols:
            db.execute('ALTER TABLE personnel ADD COLUMN mobile_number TEXT DEFAULT ""')
        if 'email_address' not in existing_cols:
            db.execute('ALTER TABLE personnel ADD COLUMN email_address TEXT DEFAULT ""')
        db.execute('''
            CREATE TABLE IF NOT EXISTS health_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                personnel_id INTEGER,
                record_date DATE NOT NULL,
                bp_systolic INTEGER,
                bp_diastolic INTEGER,
                sugar_level REAL,
                notes TEXT,
                evidence_photo TEXT DEFAULT '',
                date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (personnel_id) REFERENCES personnel (id) ON DELETE CASCADE
            )
        ''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer'
            )
        ''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        admin = db.execute('SELECT * FROM users WHERE role = "super_admin"').fetchone()
        if not admin:
            db.execute('INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
                       ('admin', generate_password_hash('admin123'), 'super_admin'))
        db.commit()

init_db()

def log_activity(action, details=""):
    if 'username' in session:
        db = get_db()
        ph_time = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
        db.execute('INSERT INTO activity_logs (username, action, details, timestamp) VALUES (?, ?, ?, ?)',
                   (session['username'], action, details, ph_time))
        db.commit()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') not in ('admin', 'super_admin'):
            flash('Admin access required for this action.', 'danger')
            return redirect(request.referrer or url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

def super_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'super_admin':
            flash('Super Admin access required for this action.', 'danger')
            return redirect(request.referrer or url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

# Rate limiting config for Brute Force Protection
FAILED_LOGINS = {}
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        client_ip = request.remote_addr
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        # Check if IP is locked out
        if client_ip in FAILED_LOGINS:
            attempts, lockout_time = FAILED_LOGINS[client_ip]
            if lockout_time and now < lockout_time:
                remaining = int((lockout_time - now).total_seconds() / 60) + 1
                flash(f'Too many failed attempts. Please try again in {remaining} minute(s).', 'danger')
                return render_template('login.html')
            elif lockout_time and now >= lockout_time:
                del FAILED_LOGINS[client_ip] # Lockout expired
                
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        
        if not username.strip() or not password.strip():
            flash('Both username and password are required.', 'danger')
            return render_template('login.html')
            
        if re.search(r'[^\x00-\x7FñÑ]', username) or re.search(r'[^\x00-\x7FñÑ]', password):
            flash('Emojis or unsupported characters are not allowed.', 'danger')
            return render_template('login.html')
            
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        
        if user and check_password_hash(user['password_hash'], password):
            # Reset failed attempts on success
            if client_ip in FAILED_LOGINS:
                del FAILED_LOGINS[client_ip]
                
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            flash('Logged in successfully!', 'success')
            return redirect(url_for('dashboard'))
        else:
            # Record failed attempt
            attempts, lockout_time = FAILED_LOGINS.get(client_ip, (0, None))
            attempts += 1
            if attempts >= MAX_FAILED_ATTEMPTS:
                lockout_time = now + timedelta(minutes=LOCKOUT_MINUTES)
                FAILED_LOGINS[client_ip] = (attempts, lockout_time)
                flash(f'Account locked due to multiple failed attempts. Please try again in {LOCKOUT_MINUTES} minutes.', 'danger')
            else:
                FAILED_LOGINS[client_ip] = (attempts, None)
                flash(f'Invalid username or password. {MAX_FAILED_ATTEMPTS - attempts} attempts remaining.', 'danger')
            
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))

@app.route('/users', methods=['GET', 'POST'])
@super_admin_required
def manage_users():
    db = get_db()
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        role = request.form.get('role', 'viewer')
        
        if not username or not password:
            flash('Username and password are required.', 'danger')
        elif len(password) < 6:
            flash('Password must be at least 6 characters long.', 'danger')
        elif re.search(r'[^\x00-\x7FñÑ]', username) or re.search(r'[^\x00-\x7FñÑ]', password):
            flash('Emojis or unsupported characters are not allowed.', 'danger')
        else:
            existing_user = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
            if existing_user:
                flash('Username already exists.', 'danger')
            else:
                db.execute('INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
                           (username, generate_password_hash(password), role))
                db.commit()
                log_activity('CREATE_USER', f"Created user '{username}' with role '{role}'")
                flash('User created successfully!', 'success')
        return redirect(url_for('manage_users'))

    users = db.execute('SELECT id, username, role FROM users').fetchall()
    return render_template('users.html', users=users)

@app.route('/edit_user/<int:id>', methods=['POST'])
@super_admin_required
def edit_user(id):
    db = get_db()
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    role = request.form.get('role', 'viewer')
    
    if not username:
        flash('Username is required.', 'danger')
        return redirect(url_for('manage_users'))
        
    if password and len(password) < 6:
        flash('Password must be at least 6 characters long.', 'danger')
        return redirect(url_for('manage_users'))
        
    if id == session.get('user_id') and role != 'super_admin':
        flash('You cannot demote your own account.', 'danger')
        return redirect(url_for('manage_users'))
        
    if re.search(r'[^\x00-\x7FñÑ]', username) or (password and re.search(r'[^\x00-\x7FñÑ]', password)):
        flash('Emojis or unsupported characters are not allowed.', 'danger')
        return redirect(url_for('manage_users'))

    existing_user = db.execute('SELECT id FROM users WHERE username = ? AND id != ?', (username, id)).fetchone()
    if existing_user:
        flash('Username already exists.', 'danger')
        return redirect(url_for('manage_users'))
        
    if password:
        db.execute('UPDATE users SET username = ?, password_hash = ?, role = ? WHERE id = ?',
                   (username, generate_password_hash(password), role, id))
    else:
        db.execute('UPDATE users SET username = ?, role = ? WHERE id = ?',
                   (username, role, id))
                   
    db.commit()
    
    # If the super admin updated their own username, update session
    if id == session.get('user_id'):
        session['username'] = username
        session['role'] = role
        
    log_activity('EDIT_USER', f"Updated user '{username}' (ID: {id})")
    flash('User updated successfully!', 'success')
    return redirect(url_for('manage_users'))

@app.route('/delete_user/<int:id>', methods=['POST'])
@super_admin_required
def delete_user(id):
    if id == session.get('user_id'):
        flash('You cannot delete your own account!', 'danger')
    else:
        db = get_db()
        user = db.execute('SELECT username FROM users WHERE id = ?', (id,)).fetchone()
        uname = user['username'] if user else f"ID {id}"
        db.execute('DELETE FROM users WHERE id = ?', (id,))
        db.commit()
        log_activity('DELETE_USER', f"Deleted user '{uname}'")
        flash('User deleted successfully.', 'success')
    return redirect(url_for('manage_users'))

@app.route('/download_backup')
@super_admin_required
def download_backup():
    if os.path.exists(DATABASE):
        date_str = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)).strftime('%Y-%m-%d_%H%M%S')
        return send_file(DATABASE, as_attachment=True, download_name=f'cenro_health_backup_{date_str}.db', mimetype='application/x-sqlite3')
    else:
        flash('Database file not found.', 'danger')
        return redirect(url_for('dashboard'))

@app.route('/audit_logs')
@super_admin_required
def audit_logs():
    db = get_db()
    logs = db.execute('SELECT * FROM activity_logs ORDER BY timestamp DESC LIMIT 500').fetchall()
    return render_template('audit_logs.html', logs=logs)

@app.route('/')
@login_required
def dashboard():
    db = get_db()
    total_personnel = db.execute('SELECT COUNT(*) FROM personnel').fetchone()[0]
    total_records = db.execute('SELECT COUNT(*) FROM health_records').fetchone()[0]
    recent_records = db.execute('''
        SELECT h.*, p.first_name, p.middle_name, p.last_name 
        FROM health_records h 
        JOIN personnel p ON h.personnel_id = p.id 
        ORDER BY h.record_date DESC LIMIT 5
    ''').fetchall()
    
    current_month = datetime.now().strftime('%Y-%m')
    first_day_this_month = datetime.now().replace(day=1)
    last_month_date = first_day_this_month - timedelta(days=1)
    last_month = last_month_date.strftime('%Y-%m')
    
    checkups_this_month = db.execute('SELECT COUNT(*) FROM health_records WHERE strftime("%Y-%m", record_date) = ?', (current_month,)).fetchone()[0]
    checkups_last_month = db.execute('SELECT COUNT(*) FROM health_records WHERE strftime("%Y-%m", record_date) = ?', (last_month,)).fetchone()[0]
    
    elevated_bp_list = db.execute('''
        SELECT h.bp_systolic, h.bp_diastolic, h.record_date, h.sugar_level, p.first_name, p.last_name, p.middle_name
        FROM health_records h
        JOIN personnel p ON h.personnel_id = p.id
        WHERE h.bp_systolic > 130 OR h.bp_diastolic > 80
        ORDER BY h.record_date DESC
    ''').fetchall()
    elevated_bp_cases = len(elevated_bp_list)

    # Get Monthly Averages for chart (Current Year Only)
    monthly_stats = db.execute('''
        SELECT strftime('%m', record_date) as month, 
               AVG(bp_systolic) as avg_sys,
               AVG(bp_diastolic) as avg_dia
        FROM health_records
        WHERE strftime('%Y', record_date) = strftime('%Y', 'now', '+8 hours')
        GROUP BY month
        ORDER BY month
    ''').fetchall()
    
    months_map = {'01':'Jan', '02':'Feb', '03':'Mar', '04':'Apr', '05':'May', '06':'Jun', 
                  '07':'Jul', '08':'Aug', '09':'Sep', '10':'Oct', '11':'Nov', '12':'Dec'}
    chart_labels = []
    chart_data_sys = []
    chart_data_dia = []
    for row in monthly_stats:
        if row['month']:
            chart_labels.append(months_map.get(row['month'], row['month']))
            chart_data_sys.append(round(row['avg_sys'], 1) if row['avg_sys'] else 0)
            chart_data_dia.append(round(row['avg_dia'], 1) if row['avg_dia'] else 0)
            
    sugar_stats = db.execute('''
        SELECT 
            SUM(CASE WHEN sugar_level < 100 THEN 1 ELSE 0 END) as normal,
            SUM(CASE WHEN sugar_level >= 100 AND sugar_level < 126 THEN 1 ELSE 0 END) as prediabetic,
            SUM(CASE WHEN sugar_level >= 126 THEN 1 ELSE 0 END) as high
        FROM health_records
        WHERE sugar_level IS NOT NULL AND sugar_level > 0
    ''').fetchone()
    
    sugar_data = [sugar_stats['normal'] or 0, sugar_stats['prediabetic'] or 0, sugar_stats['high'] or 0]

    # ── Smart Insights: At-Risk Personnel (latest record per person) ──
    latest_per_person = db.execute('''
        SELECT h.id, h.personnel_id, h.bp_systolic, h.bp_diastolic, h.sugar_level,
               h.record_date, p.first_name, p.middle_name, p.last_name, p.email_address
        FROM health_records h
        JOIN personnel p ON h.personnel_id = p.id
        WHERE h.id = (
            SELECT id FROM health_records
            WHERE personnel_id = h.personnel_id
            ORDER BY record_date DESC, id DESC LIMIT 1
        )
        ORDER BY h.record_date DESC
    ''').fetchall()

    at_risk_personnel = []
    for row in latest_per_person:
        bp_lvl,  bp_lbl,  bp_emoji  = get_bp_status(row['bp_systolic'], row['bp_diastolic'])
        sug_lvl, sug_lbl, sug_emoji = get_sugar_status(row['sugar_level'])
        risk = get_overall_risk(bp_lvl, sug_lvl)
        if risk in ('warning', 'danger', 'critical'):
            at_risk_personnel.append({
                'personnel_id': row['personnel_id'],
                'email':      row['email_address'],
                'name':       f"{row['last_name']}, {row['first_name']} {(row['middle_name'][:1].upper() + '.') if row['middle_name'] else ''}",
                'date':       row['record_date'],
                'bp':         f"{row['bp_systolic']}/{row['bp_diastolic']}",
                'sugar':      row['sugar_level'],
                'bp_lvl':     bp_lvl,
                'bp_label':   bp_lbl,
                'bp_emoji':   bp_emoji,
                'sug_lvl':    sug_lvl,
                'sug_label':  sug_lbl,
                'sug_emoji':  sug_emoji,
                'risk':       risk,
                'tip':        get_health_tip(bp_lvl, sug_lvl),
            })

    # ── Smart Insights: Worsening Trend Detection (last 3 records per person) ──
    worsening_trends = []
    all_personnel = db.execute('SELECT id, first_name, middle_name, last_name FROM personnel').fetchall()
    for person in all_personnel:
        last3 = db.execute('''
            SELECT bp_systolic, bp_diastolic, sugar_level FROM health_records
            WHERE personnel_id = ?
            ORDER BY record_date DESC, id DESC LIMIT 3
        ''', (person['id'],)).fetchall()
        if len(last3) < 3:
            continue
        # Check if BP systolic is consistently rising across last 3 records
        bp_rising = False
        sug_rising = False
        
        if all(row['bp_systolic'] is not None for row in last3):
            bp_rising = last3[2]['bp_systolic'] < last3[1]['bp_systolic'] < last3[0]['bp_systolic']
            
        if all(row['sugar_level'] is not None for row in last3):
            sug_rising = last3[2]['sugar_level'] < last3[1]['sugar_level'] < last3[0]['sugar_level']
        if bp_rising or sug_rising:
            trend_type = []
            if bp_rising:  trend_type.append('BP')
            if sug_rising: trend_type.append('Sugar')
            worsening_trends.append({
                'name':  f"{person['last_name']}, {person['first_name']} {(person['middle_name'][:1].upper() + '.') if person['middle_name'] else ''}",
                'trend': ' & '.join(trend_type),
            })

    return render_template('dashboard.html', 
                           total_personnel=total_personnel, 
                           total_records=total_records,
                           checkups_this_month=checkups_this_month,
                           elevated_bp_cases=elevated_bp_cases,
                           elevated_bp_list=elevated_bp_list,
                           recent_records=recent_records,
                           chart_labels=chart_labels,
                           chart_data_sys=chart_data_sys,
                           chart_data_dia=chart_data_dia,
                           sugar_data=sugar_data,
                           at_risk_personnel=at_risk_personnel,
                           worsening_trends=worsening_trends,
                           get_bp_status=get_bp_status,
                           get_sugar_status=get_sugar_status,
                           get_health_tip=get_health_tip,
                           get_overall_risk=get_overall_risk,
                           checkups_last_month=checkups_last_month)

@app.route('/notify_personnel/<int:id>', methods=['POST'])
@login_required
def notify_personnel(id):
    db = get_db()
    person = db.execute('''
        SELECT h.bp_systolic, h.bp_diastolic, h.sugar_level, p.first_name, p.email_address
        FROM health_records h
        JOIN personnel p ON h.personnel_id = p.id
        WHERE p.id = ?
        ORDER BY h.record_date DESC, h.id DESC LIMIT 1
    ''', (id,)).fetchone()
    
    if not person or not person['email_address']:
        flash('No email address found for this personnel.', 'danger')
        return redirect(url_for('dashboard'))
        
    bp_lvl, _, _ = get_bp_status(person['bp_systolic'], person['bp_diastolic'])
    sug_lvl, _, _ = get_sugar_status(person['sugar_level'])
    risk = get_overall_risk(bp_lvl, sug_lvl)
    
    trigger_health_alert(person['first_name'], person['email_address'], risk, f"{person['bp_systolic']}/{person['bp_diastolic']}", person['sugar_level'])
    
    flash(f"Health alert sent to {person['first_name']}'s email.", 'success')
    return redirect(url_for('dashboard'))

@app.route('/personnel', methods=['GET', 'POST'])
@login_required
def personnel():
    db = get_db()
    if request.method == 'POST':
        if session.get('role') not in ('admin', 'super_admin'):
            flash('Admin access required for this action.', 'danger')
            return redirect(url_for('personnel'))
        first_name  = request.form.get('first_name', '')
        middle_name = request.form.get('middle_name', '')
        last_name   = request.form.get('last_name', '')
        designation = request.form.get('designation', '')
        email_address = request.form.get('email_address', '').strip()
        date_of_birth = request.form.get('date_of_birth', '').strip()
        gender = request.form.get('gender', '').strip()
        height_cm   = request.form.get('height_cm', '').strip()
        weight_kg   = request.form.get('weight_kg', '').strip()

        errors = []
        for field, val in [('First Name', first_name), ('Last Name', last_name), ('Designation', designation), ('Email Address', email_address)]:
            if not str(val).strip():
                errors.append(f"{field} is required.")
            elif re.search(r'[^\x00-\x7FñÑ]', str(val)):
                errors.append(f"Emojis or unsupported characters are not allowed in {field}.")

        if middle_name and re.search(r'[^\x00-\x7FñÑ]', str(middle_name)):
            errors.append("Emojis or unsupported characters are not allowed in Middle Name.")

        if email_address and not re.match(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$', email_address):
            errors.append("Please enter a valid email address.")

        if not date_of_birth:
            errors.append("Birthdate is required.")
        else:
            try:
                dob = datetime.strptime(date_of_birth, '%Y-%m-%d')
                if dob > datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8):
                    errors.append("Birthdate cannot be in the future.")
            except ValueError:
                errors.append("Invalid birthdate format. Use YYYY-MM-DD.")

        if not gender or gender not in ('Male', 'Female', 'Other'):
            errors.append("Please select a valid gender.")

        if not height_cm:
            errors.append("Height is required.")
        else:
            try:
                h = float(height_cm)
                if h <= 0 or h > 300:
                    errors.append("Height must be between 1 and 300 cm.")
            except ValueError:
                errors.append("Height must be a valid number.")

        if not weight_kg:
            errors.append("Weight is required.")
        else:
            try:
                w = float(weight_kg)
                if w <= 0 or w > 500:
                    errors.append("Weight must be between 1 and 500 kg.")
            except ValueError:
                errors.append("Weight must be a valid number.")

        if errors:
            for error in errors:
                flash(error, 'danger')
            return redirect(url_for('personnel'))

        ph_time = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
        db.execute(
            'INSERT INTO personnel (first_name, middle_name, last_name, designation, date_of_birth, gender, height_cm, weight_kg, email_address, date_added) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (first_name.strip(), middle_name.strip(), last_name.strip(), designation.strip(), date_of_birth, gender, height_cm, weight_kg, email_address, ph_time)
        )
        db.commit()
        log_activity('ADD_PERSONNEL', f"Added personnel '{first_name.strip()} {last_name.strip()}'")
        flash('Personnel added successfully!', 'success')
        return redirect(url_for('personnel'))

    personnel_list = db.execute('SELECT * FROM personnel ORDER BY last_name').fetchall()
    return render_template('personnel.html', personnel=personnel_list, get_bmi_status=get_bmi_status, calculate_age=calculate_age)

@app.route('/edit_personnel/<int:id>', methods=['POST'])
@admin_required
def edit_personnel(id):
    db = get_db()
    first_name  = request.form.get('first_name', '')
    middle_name = request.form.get('middle_name', '')
    last_name   = request.form.get('last_name', '')
    designation = request.form.get('designation', '')
    email_address = request.form.get('email_address', '').strip()
    date_of_birth = request.form.get('date_of_birth', '').strip()
    gender = request.form.get('gender', '').strip()
    height_cm   = request.form.get('height_cm', '').strip()
    weight_kg   = request.form.get('weight_kg', '').strip()

    errors = []
    for field, val in [('First Name', first_name), ('Last Name', last_name), ('Designation', designation), ('Email Address', email_address)]:
        if not str(val).strip():
            errors.append(f"{field} is required.")
        elif re.search(r'[^\x00-\x7FñÑ]', str(val)):
            errors.append(f"Emojis or unsupported characters are not allowed in {field}.")

    if middle_name and re.search(r'[^\x00-\x7FñÑ]', str(middle_name)):
        errors.append("Emojis or unsupported characters are not allowed in Middle Name.")

    if email_address and not re.match(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$', email_address):
        errors.append("Please enter a valid email address.")

    if not date_of_birth:
        errors.append("Birthdate is required.")
    else:
        try:
            dob = datetime.strptime(date_of_birth, '%Y-%m-%d')
            if dob > datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8):
                errors.append("Birthdate cannot be in the future.")
        except ValueError:
            errors.append("Invalid birthdate format. Use YYYY-MM-DD.")

    if not gender or gender not in ('Male', 'Female', 'Other'):
        errors.append("Please select a valid gender.")

    if not height_cm:
        errors.append("Height is required.")
    else:
        try:
            h = float(height_cm)
            if h <= 0 or h > 300:
                errors.append("Height must be between 1 and 300 cm.")
        except ValueError:
            errors.append("Height must be a valid number.")

    if not weight_kg:
        errors.append("Weight is required.")
    else:
        try:
            w = float(weight_kg)
            if w <= 0 or w > 500:
                errors.append("Weight must be between 1 and 500 kg.")
        except ValueError:
            errors.append("Weight must be a valid number.")

    if errors:
        for error in errors:
            flash(error, 'danger')
        return redirect(url_for('personnel'))

    db.execute(
        'UPDATE personnel SET first_name=?, middle_name=?, last_name=?, designation=?, date_of_birth=?, gender=?, height_cm=?, weight_kg=?, email_address=? WHERE id=?',
        (first_name.strip(), middle_name.strip(), last_name.strip(), designation.strip(), date_of_birth, gender, height_cm, weight_kg, email_address, id)
    )
    db.commit()
    log_activity('EDIT_PERSONNEL', f"Updated personnel '{first_name.strip()} {last_name.strip()}'")
    flash('Personnel updated successfully!', 'success')
    return redirect(url_for('personnel'))

@app.route('/delete_personnel/<int:id>', methods=['POST'])
@admin_required
def delete_personnel(id):
    db = get_db()
    
    # 1. Fetch all associated photos and delete them from disk to prevent storage leaks
    records = db.execute('SELECT evidence_photo FROM health_records WHERE personnel_id = ? AND evidence_photo != ""', (id,)).fetchall()
    for record in records:
        if record['evidence_photo']:
            photo_path = os.path.join(app.config['UPLOAD_FOLDER'], record['evidence_photo'])
            if os.path.exists(photo_path):
                os.remove(photo_path)
                
    # 2. Delete the database records
    person = db.execute('SELECT first_name, last_name FROM personnel WHERE id = ?', (id,)).fetchone()
    p_name = f"{person['first_name']} {person['last_name']}" if person else f"ID {id}"
    db.execute('DELETE FROM health_records WHERE personnel_id = ?', (id,))
    db.execute('DELETE FROM personnel WHERE id = ?', (id,))
    db.commit()
    log_activity('DELETE_PERSONNEL', f"Deleted personnel '{p_name}' and their records")
    flash('Personnel and all associated health records deleted successfully.', 'success')
    return redirect(url_for('personnel'))


@app.route('/records')
@login_required
def records():
    db = get_db()
    personnel_list = db.execute('SELECT id, first_name, middle_name, last_name FROM personnel ORDER BY last_name').fetchall()
    all_records = db.execute('''
        SELECT h.*, p.first_name, p.middle_name, p.last_name 
        FROM health_records h 
        JOIN personnel p ON h.personnel_id = p.id 
        ORDER BY h.record_date DESC
    ''').fetchall()
    return render_template('records.html',
                           personnel=personnel_list,
                           records=all_records,
                           get_bp_status=get_bp_status,
                           get_sugar_status=get_sugar_status,
                           get_health_tip=get_health_tip,
                           get_overall_risk=get_overall_risk)


@app.route('/add_record', methods=['POST'])
@login_required
def add_record():
    if session.get('role') not in ('admin', 'super_admin'):
        flash('Admin access required to add records.', 'danger')
        return redirect(url_for('records'))

    db = get_db()
    personnel_id = request.form.get('personnel_id')
    record_date  = request.form.get('record_date')
    bp_systolic  = request.form.get('bp_systolic')
    bp_diastolic = request.form.get('bp_diastolic')
    sugar_level  = request.form.get('sugar_level')
    notes        = request.form.get('notes', '')

    errors = []
    if not personnel_id:
        errors.append('Please select personnel.')
    else:
        try:
            personnel_id = int(personnel_id)
            person_exists = db.execute('SELECT id FROM personnel WHERE id = ?', (personnel_id,)).fetchone()
            if not person_exists:
                errors.append('Selected personnel does not exist in the system.')
        except ValueError:
            errors.append('Invalid personnel ID.')

    required_fields = [
        ('Personnel',   personnel_id),
        ('Date',        record_date),
        ('Systolic BP', bp_systolic),
        ('Diastolic BP',bp_diastolic),
        ('Sugar Level', sugar_level)
    ]
    for field_name, val in required_fields:
        if not val or not str(val).strip():
            errors.append(f"{field_name} is required.")

    if record_date:
        try:
            rd = datetime.strptime(record_date, '%Y-%m-%d')
            if rd > datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8):
                errors.append("Record date cannot be in the future.")
        except ValueError:
            errors.append("Invalid record date format. Use YYYY-MM-DD.")

    if not errors:
        try:
            sys_val = int(bp_systolic)
            dia_val = int(bp_diastolic)
            sug_val = float(sugar_level)
            if sys_val <= 0 or dia_val <= 0 or sug_val <= 0:
                errors.append("Health metrics must be positive numbers.")
        except (TypeError, ValueError):
            errors.append("Blood pressure and sugar levels must be valid numbers.")

    if notes and re.search(r'[^\x00-\x7FñÑ]', str(notes)):
        errors.append("Emojis or unsupported characters are not allowed in Notes.")

    if errors:
        for error in errors:
            flash(error, 'danger')
        return redirect(url_for('records'))

    evidence_photo = request.files.get('evidence_photo')
    evidence_filename = ''
    if evidence_photo and evidence_photo.filename:
        if allowed_file(evidence_photo.filename):
            filename = secure_filename(evidence_photo.filename)
            unique_filename = f"{(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)).strftime('%Y%m%d%H%M%S')}_{filename}"
            evidence_photo.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
            evidence_filename = unique_filename
        else:
            flash('Invalid file type for evidence photo. Only PNG, JPG, and JPEG are allowed. Photo was ignored.', 'warning')

    ph_time = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
    db.execute('''
        INSERT INTO health_records (personnel_id, record_date, bp_systolic, bp_diastolic, sugar_level, notes, evidence_photo, date_added)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (personnel_id, record_date, bp_systolic, bp_diastolic, sugar_level, notes.strip(), evidence_filename, ph_time))
    db.commit()
    log_activity('ADD_RECORD', f"Added health record for personnel ID {personnel_id}")

    # Trigger Email Alert if risk is high
    person = db.execute('SELECT first_name, email_address FROM personnel WHERE id = ?', (personnel_id,)).fetchone()
    if person and person['email_address']:
        bp_lvl, _, _ = get_bp_status(bp_systolic, bp_diastolic)
        sug_lvl, _, _ = get_sugar_status(sugar_level)
        risk = get_overall_risk(bp_lvl, sug_lvl)
        if risk in ('danger', 'critical'):
            trigger_health_alert(person['first_name'], person['email_address'], risk, f"{bp_systolic}/{bp_diastolic}", sugar_level)

    flash('Health record added successfully!', 'success')
    return redirect(url_for('records'))

@app.route('/edit_record/<int:id>', methods=['POST'])
@admin_required
def edit_record(id):
    db = get_db()

    record_date = request.form.get('record_date')
    bp_systolic = request.form.get('bp_systolic')
    bp_diastolic = request.form.get('bp_diastolic')
    sugar_level = request.form.get('sugar_level')
    notes = request.form.get('notes', '')
    
    errors = []
    required_fields = [
        ('Date', record_date), 
        ('Systolic BP', bp_systolic), 
        ('Diastolic BP', bp_diastolic), 
        ('Sugar Level', sugar_level)
    ]
    
    for field_name, val in required_fields:
        if not val or not str(val).strip():
            errors.append(f"{field_name} is required.")

    if record_date:
        try:
            rd = datetime.strptime(record_date, '%Y-%m-%d')
            if rd > datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8):
                errors.append("Record date cannot be in the future.")
        except ValueError:
            errors.append("Invalid record date format. Use YYYY-MM-DD.")
            
    if not errors:
        try:
            sys_val = int(bp_systolic)
            dia_val = int(bp_diastolic)
            sug_val = float(sugar_level)
            if sys_val <= 0 or dia_val <= 0 or sug_val <= 0:
                errors.append("Health metrics must be positive numbers.")
        except (TypeError, ValueError):
            errors.append("Blood pressure and sugar levels must be valid numbers.")
            
    if notes and re.search(r'[^\x00-\x7FñÑ]', str(notes)):
        errors.append("Emojis or unsupported characters are not allowed in Notes.")
        
    if errors:
        for error in errors:
            flash(error, 'danger')
        return redirect(url_for('records'))
        
    # Handle Evidence Photo logic
    delete_photo_flag = request.form.get('delete_photo') == '1'
    evidence_photo_file = request.files.get('evidence_photo')
    
    # Get current record to check for existing photo and get personnel_id
    record = db.execute('SELECT personnel_id, evidence_photo FROM health_records WHERE id = ?', (id,)).fetchone()
    current_photo = record['evidence_photo'] if record else ''
    new_photo_value = current_photo

    if evidence_photo_file and evidence_photo_file.filename:
        if allowed_file(evidence_photo_file.filename):
            # 1. User uploaded a NEW photo
            # Delete old file if it exists
            if current_photo:
                old_path = os.path.join(app.config['UPLOAD_FOLDER'], current_photo)
                if os.path.exists(old_path):
                    os.remove(old_path)
            
            # Save new file
            filename = secure_filename(evidence_photo_file.filename)
            unique_filename = f"{(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)).strftime('%Y%m%d%H%M%S')}_{filename}"
            evidence_photo_file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
            new_photo_value = unique_filename
        else:
            flash('Invalid file type for evidence photo. Only PNG, JPG, and JPEG are allowed. Existing photo was kept.', 'warning')
    elif delete_photo_flag:
        # 2. User marked photo for deletion and didn't upload a new one
        if current_photo:
            old_path = os.path.join(app.config['UPLOAD_FOLDER'], current_photo)
            if os.path.exists(old_path):
                os.remove(old_path)
        new_photo_value = ''

    # Single update statement for all fields
    db.execute('''
        UPDATE health_records 
        SET record_date = ?, bp_systolic = ?, bp_diastolic = ?, sugar_level = ?, notes = ?, evidence_photo = ?
        WHERE id = ?
    ''', (record_date, bp_systolic, bp_diastolic, sugar_level, notes.strip(), new_photo_value, id))
    
    db.commit()
    
    # Trigger Email Alert if edited risk is high
    if record:
        personnel_id = record['personnel_id']
        person = db.execute('SELECT first_name, email_address FROM personnel WHERE id = ?', (personnel_id,)).fetchone()
        if person and person['email_address']:
            bp_lvl, _, _ = get_bp_status(bp_systolic, bp_diastolic)
            sug_lvl, _, _ = get_sugar_status(sugar_level)
            risk = get_overall_risk(bp_lvl, sug_lvl)
            if risk in ('danger', 'critical'):
                trigger_health_alert(person['first_name'], person['email_address'], risk, f"{bp_systolic}/{bp_diastolic}", sugar_level)

    log_activity('EDIT_RECORD', f"Updated health record ID {id}")
    flash('Health record updated successfully!', 'success')
    return redirect(url_for('records'))

@app.route('/delete_record/<int:id>', methods=['POST'])
@admin_required
def delete_record(id):
    db = get_db()
    # Also delete the evidence photo file from disk if it exists
    record = db.execute('SELECT evidence_photo FROM health_records WHERE id = ?', (id,)).fetchone()
    if record and record['evidence_photo']:
        photo_path = os.path.join(app.config['UPLOAD_FOLDER'], record['evidence_photo'])
        if os.path.exists(photo_path):
            os.remove(photo_path)
    db.execute('DELETE FROM health_records WHERE id = ?', (id,))
    db.commit()
    log_activity('DELETE_RECORD', f"Deleted health record ID {id}")
    flash('Health record deleted successfully.', 'success')
    return redirect(url_for('records'))


@app.route('/reports')
@login_required
def reports():
    return render_template('reports.html')

@app.route('/export_excel')
@login_required
def export_excel():
    report_type = request.args.get('report_type', 'all')
    month_year = request.args.get('month_year', '')
    
    db = get_db()
    query = '''
        SELECT p.first_name || CASE WHEN p.middle_name != '' THEN ' ' || substr(p.middle_name, 1, 1) || '. ' ELSE ' ' END || p.last_name as Name, 
               p.designation as Designation,
               h.record_date as Date, 
               h.bp_systolic as "Systolic BP", 
               h.bp_diastolic as "Diastolic BP", 
               h.sugar_level as "Blood Sugar", 
               h.notes as Notes
        FROM health_records h 
        JOIN personnel p ON h.personnel_id = p.id 
    '''
    params = ()
    
    if report_type == 'monthly' and month_year:
        query += ' WHERE strftime("%Y-%m", h.record_date) = ? '
        params = (month_year,)
    elif report_type == 'yearly' and month_year:
        year = month_year.split('-')[0]
        query += ' WHERE strftime("%Y", h.record_date) = ? '
        params = (year,)
        
    query += ' ORDER BY h.record_date DESC'
    
    df = pd.read_sql_query(query, db, params=params)
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Health Records')
        
        # Format the Excel sheet
        worksheet = writer.sheets['Health Records']
        
        from openpyxl.styles import PatternFill, Font, Alignment
        
        # 1. Style the header row (DENR Green background, White text)
        header_fill = PatternFill(start_color='2E7D32', end_color='2E7D32', fill_type='solid')
        header_font = Font(color='FFFFFF', bold=True)
        
        for cell in worksheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center', vertical='center')
            
        # 2. Auto-adjust column widths
        for column in worksheet.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            # Set width to max length + 3 for padding
            worksheet.column_dimensions[column_letter].width = max_length + 3
            
    output.seek(0)
    
    return send_file(output, as_attachment=True, download_name=f'CENRO_Don_Carlos_Health_Records_{report_type}.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.route('/export_pdf')
@login_required
def export_pdf():
    report_type = request.args.get('report_type', 'all')
    month_year = request.args.get('month_year', '')
    
    db = get_db()
    query = '''
        SELECT p.first_name || CASE WHEN p.middle_name != '' THEN ' ' || substr(p.middle_name, 1, 1) || '. ' ELSE ' ' END || p.last_name as Name, 
               h.record_date as Date, 
               h.bp_systolic || '/' || h.bp_diastolic as BP, 
               h.sugar_level as Sugar 
        FROM health_records h 
        JOIN personnel p ON h.personnel_id = p.id 
    '''
    params = ()
    
    title_text = "Health and Wellness Report"
    if report_type == 'monthly' and month_year:
        query += ' WHERE strftime("%Y-%m", h.record_date) = ? '
        params = (month_year,)
        title_text += f" - {month_year}"
    elif report_type == 'yearly' and month_year:
        year = month_year.split('-')[0]
        query += ' WHERE strftime("%Y", h.record_date) = ? '
        params = (year,)
        title_text += f" - {year}"
        
    query += ' ORDER BY h.record_date DESC'
    
    records = db.execute(query, params).fetchall()
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=30, bottomMargin=50, leftMargin=50, rightMargin=50)
    elements = []
    
    styles = getSampleStyleSheet()
    
    # ─── Custom Header ───────────────────────────────────────────────────────
    denr_logo_path = os.path.join(app.static_folder, 'img', 'denr_logo.png')
    bp_logo_path = os.path.join(app.static_folder, 'img', 'bagong_pilipinas.png')

    header_style = ParagraphStyle(
        name='HeaderStyle',
        parent=styles['Normal'],
        alignment=TA_CENTER,
        fontSize=10,
        leading=12
    )

    bold_header_style = ParagraphStyle(
        name='BoldHeaderStyle',
        parent=styles['Normal'],
        alignment=TA_CENTER,
        fontSize=11,
        fontName='Helvetica-Bold',
        leading=14
    )

    title_style = ParagraphStyle(
        name='ReportTitleStyle',
        parent=styles['Normal'],
        alignment=TA_CENTER,
        fontSize=12,
        fontName='Helvetica-Bold',
        leading=16
    )

    img_width = 60
    bp_size = 80  # Change this number to resize only the Bagong Pilipinas logo
    
    denr_img = RLImage(denr_logo_path, width=img_width, height=img_width) if os.path.exists(denr_logo_path) else ""
    
    # The Bagong Pilipinas logo might be rectangular, so let's adjust width slightly or keep aspect ratio
    bp_img = RLImage(bp_logo_path, width=bp_size, height=bp_size) if os.path.exists(bp_logo_path) else ""

    center_text = [
        Paragraph("Republic of the Philippines", header_style),
        Paragraph("DEPARTMENT OF ENVIRONMENT AND NATURAL RESOURCES", bold_header_style),
        Paragraph("CENRO Don Carlos", header_style),
        Spacer(1, 15),
        Paragraph("HEALTH AND WELLNESS MONITORING REPORT", title_style),
    ]

    if report_type != 'all' and month_year:
        subtitle = f"For the Period: {month_year}"
        center_text.append(Spacer(1, 5))
        center_text.append(Paragraph(subtitle, header_style))

    header_table = Table(
        [[denr_img, center_text, bp_img]], 
        colWidths=[70, 380, 70]
    )
    header_table.setStyle(TableStyle([
        ('ALIGN', (0,0), (0,0), 'LEFT'),
        ('ALIGN', (1,0), (1,0), 'CENTER'),
        ('ALIGN', (2,0), (2,0), 'RIGHT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))

    elements.append(header_table)
    elements.append(Spacer(1, 20))
    # ─────────────────────────────────────────────────────────────────────────
    
    data = [['Name', 'Date', 'Blood Pressure', 'Sugar Level']]
    for r in records:
        data.append([r['Name'], r['Date'], r['BP'], str(r['Sugar'])])
        
    if len(data) > 1:
        table = Table(data)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2E7D32')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#F4F7F5')),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ]))
        elements.append(table)
    else:
        elements.append(Paragraph("No records found for the selected criteria.", styles['Normal']))
        
    doc.build(elements)
    buffer.seek(0)
    return send_file(buffer, as_attachment=False, download_name=f'CENRO_Don_Carlos_Health_Report_{report_type}.pdf', mimetype='application/pdf')

if __name__ == '__main__':
    # Use environment variables for safe production deployment
    debug_mode = os.environ.get('FLASK_ENV') == 'development'
    port = int(os.environ.get('PORT', 80))
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
