#!/usr/bin/env python3
"""Create a small SQLite demo database for trying sqlide.

Usage: python3 scripts/make_demo_db.py [path]   (default: demo.db)
"""

import sqlite3
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "demo.db"

conn = sqlite3.connect(path)
conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT,
        city TEXT
    );

    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY,
        customer_id INTEGER NOT NULL REFERENCES customers(id),
        item TEXT NOT NULL,
        amount REAL NOT NULL,
        placed_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    -- No primary key on purpose: shows up read-only in the grid.
    CREATE TABLE IF NOT EXISTS log (
        at TEXT,
        message TEXT
    );

    CREATE VIEW IF NOT EXISTS order_totals AS
        SELECT c.name, COUNT(o.id) AS orders, SUM(o.amount) AS total
        FROM customers c LEFT JOIN orders o ON o.customer_id = c.id
        GROUP BY c.id;

    DELETE FROM customers;
    DELETE FROM orders;
    DELETE FROM log;
    """
)

conn.executemany(
    "INSERT INTO customers (id, name, email, city) VALUES (?, ?, ?, ?)",
    [
        (1, "Ada Lovelace", "ada@example.com", "London"),
        (2, "Alan Turing", "alan@example.com", "Manchester"),
        (3, "Grace Hopper", "grace@example.com", "New York"),
        (4, "Edsger Dijkstra", None, "Nuenen"),
    ],
)
conn.executemany(
    "INSERT INTO orders (customer_id, item, amount) VALUES (?, ?, ?)",
    [
        (1, "Analytical Engine plans", 120.0),
        (1, "Punch cards (box)", 9.5),
        (2, "Enigma replica", 300.0),
        (3, "COBOL manual", 25.0),
        (3, "Compiler license", 199.99),
    ],
)
conn.executemany(
    "INSERT INTO log (at, message) VALUES (datetime('now'), ?)",
    [("demo database created",), ("this table has no primary key",)],
)
conn.commit()
conn.close()
print(f"Created {path}")
