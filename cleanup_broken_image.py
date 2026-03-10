"""
One-time script: remove ALL comments whose content contains a Firebase URL
that no longer resolves (HTTP 404). Run ONCE, then delete this file.

Usage:
    cd x:\桌面\kindergarten-contactbook
    python cleanup_broken_image.py
"""
import sqlite3, json, urllib.request

DB_PATH = 'kindergarten.db'
FIREBASE_PREFIX = 'https://firebasestorage.googleapis.com'

def url_alive(url: str) -> bool:
    """Return True if the URL is reachable (2xx/3xx), False on 4xx."""
    try:
        req = urllib.request.Request(url, method='HEAD')
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status < 400
    except Exception:
        return False

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
total_removed = 0

rows = conn.execute('SELECT student_id, date, comments FROM contact_books WHERE comments IS NOT NULL').fetchall()
for row in rows:
    comments = json.loads(row['comments'])
    original_len = len(comments)
    kept = []
    for c in comments:
        content = c.get('content', '')
        if content.startswith(FIREBASE_PREFIX):
            alive = url_alive(content)
            if not alive:
                print(f"  [REMOVE] student={row['student_id']} date={row['date']} url={content[:80]}...")
            else:
                kept.append(c)
        else:
            kept.append(c)
    removed = original_len - len(kept)
    if removed > 0:
        conn.execute(
            'UPDATE contact_books SET comments = ? WHERE student_id = ? AND date = ?',
            (json.dumps(kept, ensure_ascii=False), row['student_id'], row['date'])
        )
        total_removed += removed

conn.commit()
conn.close()
print(f'\nDone. Removed {total_removed} broken image comment(s).')
