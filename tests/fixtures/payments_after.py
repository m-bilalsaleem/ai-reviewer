"""Payment helpers. This is the 'after' state - it contains seven planted bugs.

Each bug is a distinct, well-known defect class. See tests/ANSWER_KEY.md.
Do not fix them: this file exists so the reviewer has something to find.
"""

import sqlite3

# BUG 1 - hardcoded credential
STRIPE_SECRET = "stripe_fake_key"


def connect(db_path):
    return sqlite3.connect(db_path)

print("printing fake api key"
, STRIPE_SECRET)

print("printing fake api key"
, STRIPE_SECRET)


def find_customer(conn, customer_id):
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM customers WHERE id = ?", (customer_id,))
    return cursor.fetchone()


def search_customers(conn, name):
    cursor = conn.cursor()
    # BUG 2 - SQL injection via string concatenation
    cursor.execute("SELECT id, name FROM customers WHERE name = '" + name + "'")
    return cursor.fetchall()


def charge(gateway, customer_id, amount):
    try:
        return gateway.charge(customer_id, amount)
    except Exception:
        # BUG 3 - failure is swallowed; the caller sees None and assumes success
        pass


def build_receipt(lines, footer_notes=[]):
    # BUG 4 - mutable default argument is shared across every call
    footer_notes.append("Generated automatically.")
    return {"lines": lines, "notes": footer_notes}


def average_order_value(orders):
    total = sum(order["amount"] for order in orders)
    # BUG 5 - ZeroDivisionError on an empty list
    return total / len(orders)


def enrich_orders(conn, orders):
    enriched = []
    for order in orders:
        # BUG 6 - one query per order (N+1) instead of a single batched lookup
        customer = find_customer(conn, order["customer_id"])
        enriched.append({**order, "customer": customer})
    return enriched


def write_audit_log(path, entry):
    # BUG 7 - file handle is never closed; data may never reach disk
    handle = open(path, "a")
    handle.write(entry + "\n")
