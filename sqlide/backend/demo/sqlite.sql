-- The sqlide demo database, SQLite dialect. Same schema, same rows and
-- same purpose as postgres.sql in this directory — see the comment
-- there for what the shape is meant to show.
--
-- No CREATE DATABASE and no USE: in SQLite one file is one database,
-- so the file the caller names *is* the demo database and everything
-- below runs straight into it.
--
-- SQLite has no stored functions, so the demo's `customer_total`
-- has no counterpart here; the trigger and the view do.

CREATE TABLE customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT,
    city TEXT
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers (id),
    item TEXT NOT NULL,
    amount NUMERIC(10, 2) NOT NULL,
    placed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX ix_orders_customer ON orders (customer_id);

-- No primary key on purpose: sqlide shows this one read-only.
CREATE TABLE log (
    at TEXT DEFAULT CURRENT_TIMESTAMP,
    message TEXT
);

CREATE VIEW order_totals AS
SELECT c.name, count(o.id) AS orders, coalesce(sum(o.amount), 0) AS total
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id
GROUP BY c.id, c.name;

CREATE TRIGGER orders_logged
AFTER INSERT ON orders
FOR EACH ROW
BEGIN
    INSERT INTO log (message) VALUES ('order ' || NEW.id || ' placed');
END;

INSERT INTO customers (id, name, email, city) VALUES
    (1, 'Ada Lovelace', 'ada@example.com', 'London'),
    (2, 'Alan Turing', 'alan@example.com', 'Manchester'),
    (3, 'Grace Hopper', 'grace@example.com', 'New York'),
    (4, 'Edsger Dijkstra', NULL, 'Nuenen');

INSERT INTO orders (customer_id, item, amount) VALUES
    (1, 'Analytical Engine plans', 120.00),
    (1, 'Punch cards (box)', 9.50),
    (2, 'Enigma replica', 300.00),
    (3, 'COBOL manual', 25.00),
    (3, 'Compiler license', 199.99);
