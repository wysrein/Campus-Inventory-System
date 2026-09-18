import sqlite3

conn = sqlite3.connect("hardware_inventory.db")
cur = conn.cursor()

tables = [
    "users",
    "hardware",
    "borrow_requests",
    "item_requests",
    "item_holds",
    "borrow_transactions",
    "password_resets",
    "return_requests"
]

for table in tables:
    cur.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cur.fetchall()]

    cur.execute(f"SELECT * FROM {table}")
    rows = cur.fetchall()

    for row in rows:
        for column, value in zip(columns, row):
            if value == "2021-003211":
                print("FOUND!")
                print("Table:", table)
                print("Column:", column)
                print("Value:", value)

conn.close()