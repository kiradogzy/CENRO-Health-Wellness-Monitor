import os
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file, session, g
import sqlite3
from datetime import datetime, timedelta
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

# ──────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'cenro_dc_health_super_secret_key_2026')
app.permanent_session_lifetime = timedelta(minutes=15)
# Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'health_monitor.db')

UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads', 'evidence')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.before_request
def make_session_permanent():
    session.permanent = True

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
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
                last_name TEXT NOT NULL,
                designation TEXT,
                date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
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
                FOREIGN KEY (personnel_id) REFERENCES personnel (id)
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
        admin = db.execute('SELECT * FROM users WHERE username = "admin"').fetchone()
        if not admin:
            db.execute('INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
                       ('admin', generate_password_hash('admin123'), 'super_admin'))
        else:
            # Upgrade existing admin to super_admin if not already
            if admin['role'] == 'admin':
                db.execute('UPDATE users SET role = "super_admin" WHERE username = "admin"')
        db.commit()

init_db()

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

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
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
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            flash('Logged in successfully!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password.', 'danger')
            
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
                flash('User created successfully!', 'success')
        return redirect(url_for('manage_users'))

    users = db.execute('SELECT id, username, role FROM users').fetchall()
    return render_template('users.html', users=users)

@app.route('/delete_user/<int:id>', methods=['POST'])
@super_admin_required
def delete_user(id):
    if id == session.get('user_id'):
        flash('You cannot delete your own account!', 'danger')
    else:
        db = get_db()
        db.execute('DELETE FROM users WHERE id = ?', (id,))
        db.commit()
        flash('User deleted successfully.', 'success')
    return redirect(url_for('manage_users'))

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

    # Get Monthly Averages for chart
    monthly_stats = db.execute('''
        SELECT strftime('%m', record_date) as month, 
               AVG(bp_systolic) as avg_sys,
               AVG(bp_diastolic) as avg_dia
        FROM health_records
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
            SUM(CASE WHEN sugar_level <= 99 THEN 1 ELSE 0 END) as normal,
            SUM(CASE WHEN sugar_level > 99 AND sugar_level <= 125 THEN 1 ELSE 0 END) as prediabetic,
            SUM(CASE WHEN sugar_level > 125 THEN 1 ELSE 0 END) as high
        FROM health_records
        WHERE sugar_level IS NOT NULL AND sugar_level > 0
    ''').fetchone()
    
    sugar_data = [sugar_stats['normal'] or 0, sugar_stats['prediabetic'] or 0, sugar_stats['high'] or 0]

    # ── Smart Insights: At-Risk Personnel (latest record per person) ──
    latest_per_person = db.execute('''
        SELECT h.id, h.personnel_id, h.bp_systolic, h.bp_diastolic, h.sugar_level,
               h.record_date, p.first_name, p.middle_name, p.last_name
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
                'name':       f"{row['last_name']}, {row['first_name']} {(row['middle_name'][:1].upper() + '.') if row['middle_name'] else ''}",
                'date':       row['record_date'],
                'bp':         f"{row['bp_systolic']}/{row['bp_diastolic']}",
                'sugar':      row['sugar_level'],
                'bp_label':   bp_lbl,
                'bp_emoji':   bp_emoji,
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
        bp_rising = last3[2]['bp_systolic'] < last3[1]['bp_systolic'] < last3[0]['bp_systolic']
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

@app.route('/personnel', methods=['GET', 'POST'])
@login_required
def personnel():
    db = get_db()
    if request.method == 'POST':
        if session.get('role') not in ('admin', 'super_admin'):
            flash('Admin access required for this action.', 'danger')
            return redirect(url_for('personnel'))
        first_name = request.form.get('first_name', '')
        middle_name = request.form.get('middle_name', '')
        last_name = request.form.get('last_name', '')
        designation = request.form.get('designation', '')
        
        errors = []
        for field, val in [('First Name', first_name), ('Last Name', last_name), ('Designation', designation)]:
            if not str(val).strip():
                errors.append(f"{field} is required.")
            elif re.search(r'[^\x00-\x7FñÑ]', str(val)):
                errors.append(f"Emojis or unsupported characters are not allowed in {field}.")
                
        if middle_name and re.search(r'[^\x00-\x7FñÑ]', str(middle_name)):
            errors.append("Emojis or unsupported characters are not allowed in Middle Name.")
                
        if errors:
            for error in errors:
                flash(error, 'danger')
            return redirect(url_for('personnel'))
            
        db.execute('INSERT INTO personnel (first_name, middle_name, last_name, designation) VALUES (?, ?, ?, ?)',
                   (first_name.strip(), middle_name.strip(), last_name.strip(), designation.strip()))
        db.commit()
        flash('Personnel added successfully!', 'success')
        return redirect(url_for('personnel'))
        
    personnel_list = db.execute('SELECT * FROM personnel ORDER BY last_name').fetchall()
    return render_template('personnel.html', personnel=personnel_list)

@app.route('/edit_personnel/<int:id>', methods=['POST'])
@admin_required
def edit_personnel(id):
    db = get_db()
    first_name = request.form.get('first_name', '')
    middle_name = request.form.get('middle_name', '')
    last_name = request.form.get('last_name', '')
    designation = request.form.get('designation', '')
    
    errors = []
    for field, val in [('First Name', first_name), ('Last Name', last_name), ('Designation', designation)]:
        if not str(val).strip():
            errors.append(f"{field} is required.")
        elif re.search(r'[^\x00-\x7FñÑ]', str(val)):
            errors.append(f"Emojis or unsupported characters are not allowed in {field}.")
            
    if middle_name and re.search(r'[^\x00-\x7FñÑ]', str(middle_name)):
        errors.append("Emojis or unsupported characters are not allowed in Middle Name.")
            
    if errors:
        for error in errors:
            flash(error, 'danger')
        return redirect(url_for('personnel'))
        
    db.execute('UPDATE personnel SET first_name = ?, middle_name = ?, last_name = ?, designation = ? WHERE id = ?',
               (first_name.strip(), middle_name.strip(), last_name.strip(), designation.strip(), id))
    db.commit()
    flash('Personnel updated successfully!', 'success')
    return redirect(url_for('personnel'))

@app.route('/delete_personnel/<int:id>', methods=['POST'])
@admin_required
def delete_personnel(id):
    db = get_db()
    db.execute('DELETE FROM health_records WHERE personnel_id = ?', (id,))
    db.execute('DELETE FROM personnel WHERE id = ?', (id,))
    db.commit()
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


@app.route('/add_record', methods=['GET', 'POST'])
@login_required
def add_record():
    if session.get('role') not in ('admin', 'super_admin'):
        flash('Admin access required to add records.', 'danger')
        return redirect(url_for('records'))

    db = get_db()

    if request.method == 'POST':
        personnel_id = request.form.get('personnel_id')
        record_date  = request.form.get('record_date')
        bp_systolic  = request.form.get('bp_systolic')
        bp_diastolic = request.form.get('bp_diastolic')
        sugar_level  = request.form.get('sugar_level')
        notes        = request.form.get('notes', '')

        errors = []
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

        if notes and re.search(r'[^\x00-\x7FñÑ]', str(notes)):
            errors.append("Emojis or unsupported characters are not allowed in Notes.")

        if errors:
            for error in errors:
                flash(error, 'danger')
            return redirect(url_for('add_record'))

        evidence_photo = request.files.get('evidence_photo')
        evidence_filename = ''
        if evidence_photo and allowed_file(evidence_photo.filename):
            filename = secure_filename(evidence_photo.filename)
            unique_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}"
            evidence_photo.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
            evidence_filename = unique_filename

        db.execute('''
            INSERT INTO health_records (personnel_id, record_date, bp_systolic, bp_diastolic, sugar_level, notes, evidence_photo)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (personnel_id, record_date, bp_systolic, bp_diastolic, sugar_level, notes.strip(), evidence_filename))
        db.commit()
        flash('Health record added successfully!', 'success')
        return redirect(url_for('records'))

    personnel_list = db.execute('SELECT id, first_name, middle_name, last_name FROM personnel ORDER BY last_name').fetchall()
    return render_template('add_record.html', personnel=personnel_list)

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
            
    if notes and re.search(r'[^\x00-\x7FñÑ]', str(notes)):
        errors.append("Emojis or unsupported characters are not allowed in Notes.")
        
    if errors:
        for error in errors:
            flash(error, 'danger')
        return redirect(url_for('records'))
        
    # Handle Evidence Photo logic
    delete_photo_flag = request.form.get('delete_photo') == '1'
    evidence_photo_file = request.files.get('evidence_photo')
    
    # Get current record to check for existing photo
    record = db.execute('SELECT evidence_photo FROM health_records WHERE id = ?', (id,)).fetchone()
    current_photo = record['evidence_photo'] if record else ''
    new_photo_value = current_photo

    if evidence_photo_file and allowed_file(evidence_photo_file.filename):
        # 1. User uploaded a NEW photo
        # Delete old file if it exists
        if current_photo:
            old_path = os.path.join(app.config['UPLOAD_FOLDER'], current_photo)
            if os.path.exists(old_path):
                os.remove(old_path)
        
        # Save new file
        filename = secure_filename(evidence_photo_file.filename)
        unique_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}"
        evidence_photo_file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
        new_photo_value = unique_filename
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
    app.run(host='0.0.0.0', port=80, debug=True)
