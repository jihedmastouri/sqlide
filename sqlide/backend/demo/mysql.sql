-- The sqlide demo database, MySQL dialect. Same schema, same rows and
-- same purpose as postgres.sql in this directory — see the comment
-- there for how this file is used and why the demo lives in its own
-- database.

CREATE DATABASE IF NOT EXISTS demo;
-- The compose file's `sqlide` user only owns the `sqlide` database;
-- this file is applied by root, so the demo has to be handed over
-- explicitly (PostgreSQL needs no equivalent: there, `sqlide` is the
-- superuser that creates it).
GRANT ALL PRIVILEGES ON demo.* TO 'sqlide'@'%';
USE demo;

CREATE TABLE customers (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    city VARCHAR(255)
);

CREATE TABLE orders (
    id INT AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    item VARCHAR(255) NOT NULL,
    amount DECIMAL(10, 2) NOT NULL,
    placed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (customer_id) REFERENCES customers (id)
);

CREATE INDEX ix_orders_customer ON orders (customer_id);

-- No primary key on purpose: sqlide shows this one read-only.
CREATE TABLE log (
    at DATETIME DEFAULT CURRENT_TIMESTAMP,
    message VARCHAR(255)
);

CREATE VIEW order_totals AS
SELECT c.name, count(o.id) AS orders, coalesce(sum(o.amount), 0) AS total
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id
GROUP BY c.id, c.name;

-- One-statement bodies on purpose: they carry no inner semicolon, so
-- the file needs no DELIMITER switching and can be replayed statement
-- by statement by scripts/init_databases.py.
CREATE FUNCTION customer_total(customer INT) RETURNS DECIMAL(10, 2)
READS SQL DATA
RETURN (SELECT coalesce(sum(amount), 0) FROM orders WHERE customer_id = customer);

CREATE TRIGGER orders_logged
AFTER INSERT ON orders
FOR EACH ROW INSERT INTO log (message) VALUES (concat('order ', NEW.id, ' placed'));

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

-- 502 rows on purpose: one more than a single page (PAGE_SIZE in
-- frontend/data_grid.py), so opening this table exercises paging and
-- scroll-to-load-more without any setup beyond the demo button.
CREATE TABLE events (
    id INT PRIMARY KEY,
    label VARCHAR(255) NOT NULL
);

INSERT INTO events (id, label)
WITH RECURSIVE seq (n) AS (
    SELECT 1
    UNION ALL
    SELECT n + 1 FROM seq WHERE n < 502
)
SELECT n, CONCAT('event ', n) FROM seq;
