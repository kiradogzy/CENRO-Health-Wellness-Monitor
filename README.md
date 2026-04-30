# CENRO Health and Wellness Monitoring System
A secure, web-based health tracking system built for the Community Environment and Natural Resources Office (CENRO) — Don Carlos under the Department of Environment and Natural Resources (DENR).

📋 Features
| Feature | Admin | Viewer |
| :--- | :---: | :---: |
| Dashboard Analytics (trends & stats) | ✅ | ✅ |
| Personnel Directory (search & filter) | ✅ | ✅ |
| Health Record History | ✅ | ✅ |
| Evidence Photo Modal (with Zoom) | ✅ | ✅ |
| Export Health Reports to PDF | ✅ | ✅ |
| Add/Edit/Delete Personnel | ✅ | ❌ |
| Log & Edit Health Records | ✅ | ❌ |
| Manage User Accounts | ✅ | ❌ |

🔐 Security Highlights
- Role-based access control (Admin / Viewer)
- Password hashing (Werkzeug scrypt)
- Session hardening (HttpOnly cookies)
- Input sanitization (strictly blocks emoji and invalid characters)
- Server-side field validation

🛠️ Tech Stack
- **Backend:** Python 3.8+, Flask 3.0
- **Database:** SQLite3
- **Frontend:** HTML5, CSS3, Vanilla JS (FontAwesome 6, Google Fonts)
- **PDF Generation:** FPDF
- **Security:** Werkzeug Security, Flask-Session

🚀 Getting Started

1. Clone the Repository
```bash
git clone https://github.com/kiradogzy/CENRO-Health-Wellness-Monitor.git
cd CENRO-Health-Wellness-Monitor
```

2. Create a Virtual Environment
```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

3. Install Dependencies
```bash
pip install -r requirements.txt
```

4. Run the Development Server
```bash
python app.py
```
Open your browser at: `http://127.0.0.1` (Port 80) or `http://localhost`.

🖥️ Running on a Local Network (LAN)
The server is configured to run on host `0.0.0.0`, making it accessible to other devices on the same network at `http://<your-ip>`.

👤 Default Accounts
| Role | Username | Password |
| :--- | :--- | :--- |
| Admin (Full Access) | admin | admin123 |

🔒 Additional accounts can be created by the Admin through the **Users** panel inside the system.

📁 Project Structure
```
cenro_dc_health_and_wellness_monitoring_system/
├── app.py                 # Main Flask application & routes
├── requirements.txt       # Python dependencies
├── health_monitor.db      # SQLite Database
├── static/                # Static assets
│   ├── css/               # Styling (Denr-inspired theme)
│   ├── img/               # UI Images & Backgrounds
│   └── uploads/           # Evidence photo storage
└── templates/             # Jinja2 HTML templates
    ├── login.html
    ├── dashboard.html
    ├── personnel.html
    ├── records.html
    ├── reports.html
    └── users.html
```

📄 License
This system was developed for internal government use by CENRO Don Carlos, DENR. All rights reserved.

---

*Developed by: Edgest Yaneigh N. Agbayani*
