import sqlite3
conn = sqlite3.connect('health_monitor.db')
conn.execute('DELETE FROM users WHERE username = "viewer"')
conn.commit()
conn.close()
