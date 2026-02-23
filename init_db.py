import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'kindergarten.db')
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), 'schema.sql')

def init_db():
    print(f"Initializing database at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    with open(SCHEMA_PATH, 'r', encoding='utf-8') as f:
        schema_sql = f.read()
    
    # Enable WAL mode for better concurrency
    conn.execute('PRAGMA journal_mode=WAL;')
    
    conn.executescript(schema_sql)
    conn.commit()
    conn.close()
    print("Database initialized successfully.")

if __name__ == '__main__':
    init_db()
