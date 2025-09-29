import sqlite3
conn = sqlite3.connect("catalitium.db")
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
print(cur.fetchall())
cur.execute("SELECT * FROM subscribers LIMIT 5;")
print(cur.fetchall())
conn.close()