"""Payment helpers. This is the 'before' state - deliberately unremarkable."""

import sqlite3


def connect(db_path):
    return sqlite3.connect(db_path)


def find_customer(conn, customer_id):
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM customers WHERE id = ?", (customer_id,))
    return cursor.fetchone()
