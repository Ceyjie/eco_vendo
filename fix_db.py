import sqlite3
import os

DB_PATH = "/home/eco/eco_vendo/eco_charge.db"

if os.path.exists(DB_PATH):
    conn = sqlite3.connect(DB_PATH)
    try:
        print("🔧 Attempting to add 'user_id' column...")
        conn.execute("ALTER TABLE transactions ADD COLUMN user_id TEXT;")
        conn.commit()
        print("✅ Column added successfully!")
    except sqlite3.OperationalError:
        print("ℹ️ Column 'user_id' already exists.")
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        conn.close()
else:
    print("❌ Database file not found. Make sure the path is correct.")
