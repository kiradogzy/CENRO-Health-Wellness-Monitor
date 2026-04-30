import os
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file, session
import sqlite3
from datetime import datetime
import pandas as pd
import io
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import re

app = Flask(__name__)
app.secret_key = os.urandom(24)
DATABASE = 'health_monitor.db'

UPLOAD_FOLDER = os.path.join('static', 'uploads', 'evidence')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

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
                       ('admin', generate_password_hash('admin123'), 'admin'))
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
        if session.get('role') != 'admin':
            flash('Admin access required for this action.', 'danger')
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
@admin_required
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
@admin_required
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
    
    # Get Monthly Averages for chart
    monthly_stats = db.execute('''
        SELECT strftime('%m', record_date) as month, 
               AVG(bp_systolic) as avg_sys
        FROM health_records
        GROUP BY month
        ORDER BY month
    ''').fetchall()
    
    months_map = {'01':'Jan', '02':'Feb', '03':'Mar', '04':'Apr', '05':'May', '06':'Jun', 
                  '07':'Jul', '08':'Aug', '09':'Sep', '10':'Oct', '11':'Nov', '12':'Dec'}
    chart_labels = []
    chart_data = []
    for row in monthly_stats:
        if row['month']: # Ensure it's not None
            chart_labels.append(months_map.get(row['month'], row['month']))
            chart_data.append(round(row['avg_sys'], 1))
            
    return render_template('dashboard.html', 
                           total_personnel=total_personnel, 
                           total_records=total_records,
                           recent_records=recent_records,
                           chart_labels=chart_labels,
                           chart_data=chart_data)

@app.route('/personnel', methods=['GET', 'POST'])
@login_required
def personnel():
    db = get_db()
    if request.method == 'POST':
        if session.get('role') != 'admin':
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


@app.route('/records', methods=['GET', 'POST'])
@login_required
def records():
    db = get_db()
    if request.method == 'POST':
        if session.get('role') != 'admin':
            flash('Admin access required for this action.', 'danger')
            return redirect(url_for('records'))
        personnel_id = request.form.get('personnel_id')
        record_date = request.form.get('record_date')
        bp_systolic = request.form.get('bp_systolic')
        bp_diastolic = request.form.get('bp_diastolic')
        sugar_level = request.form.get('sugar_level')
        notes = request.form.get('notes', '')
        
        errors = []
        required_fields = [
            ('Personnel', personnel_id), 
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
    all_records = db.execute('''
        SELECT h.*, p.first_name, p.middle_name, p.last_name 
        FROM health_records h 
        JOIN personnel p ON h.personnel_id = p.id 
        ORDER BY h.record_date DESC
    ''').fetchall()
    return render_template('records.html', personnel=personnel_list, records=all_records)

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
        
    evidence_photo = request.files.get('evidence_photo')
    evidence_filename = None
    if evidence_photo and allowed_file(evidence_photo.filename):
        filename = secure_filename(evidence_photo.filename)
        unique_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}"
        evidence_photo.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
        evidence_filename = unique_filename

    if evidence_filename:
        db.execute('''
            UPDATE health_records 
            SET record_date = ?, bp_systolic = ?, bp_diastolic = ?, sugar_level = ?, notes = ?, evidence_photo = ?
            WHERE id = ?
        ''', (record_date, bp_systolic, bp_diastolic, sugar_level, notes.strip(), evidence_filename, id))
    else:
        db.execute('''
            UPDATE health_records 
            SET record_date = ?, bp_systolic = ?, bp_diastolic = ?, sugar_level = ?, notes = ?
            WHERE id = ?
        ''', (record_date, bp_systolic, bp_diastolic, sugar_level, notes.strip(), id))
    db.commit()
    flash('Health record updated successfully!', 'success')
    return redirect(url_for('records'))

@app.route('/delete_record/<int:id>', methods=['POST'])
@admin_required
def delete_record(id):
    db = get_db()
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
    
    return send_file(output, as_attachment=True, download_name=f'health_records_{report_type}.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

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
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    elements = []
    
    styles = getSampleStyleSheet()
    elements.append(Paragraph(title_text, styles['Title']))
    elements.append(Spacer(1, 12))
    
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
    
    return send_file(buffer, as_attachment=True, download_name=f'health_report_{report_type}.pdf', mimetype='application/pdf')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80, debug=True)
