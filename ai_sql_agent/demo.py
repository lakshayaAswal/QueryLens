"""Deterministic, entirely synthetic ecommerce data for the sample workspace."""

from __future__ import annotations

from calendar import monthrange
from contextlib import closing, suppress
from datetime import datetime, timedelta
import os
from pathlib import Path
from random import Random
import sqlite3
import tempfile

DEFAULT_QUESTION = "How has monthly revenue changed in 2026?"
REVENUE_BY_REGION_QUESTION = "Show revenue by region"
TOP_PRODUCTS_QUESTION = "Which products generated the most revenue?"
TOP_CUSTOMERS_QUESTION = "Show the top 10 customers by revenue"
SALES_COMPARISON_QUESTION = "Compare monthly sales for the last two years"
TOP_REGIONS_QUESTION = "Now show only the top three regions"

SUGGESTED_QUESTIONS = (
    REVENUE_BY_REGION_QUESTION,
    TOP_PRODUCTS_QUESTION,
    TOP_CUSTOMERS_QUESTION,
    SALES_COMPARISON_QUESTION,
)

REGION_QUERY = """
SELECT c.region,
       ROUND(SUM(i.quantity * i.unit_price), 2) AS revenue,
       COUNT(DISTINCT o.id) AS orders
FROM orders AS o
JOIN customers AS c ON c.id = o.customer_id
JOIN order_items AS i ON i.order_id = o.id
WHERE o.status = 'completed'
GROUP BY c.region
ORDER BY revenue DESC
""".strip()

EXAMPLE_QUERIES = {
    DEFAULT_QUESTION: """
WITH order_totals AS (
    SELECT o.id,
           STRFTIME('%Y-%m', o.ordered_at) AS month,
           SUM(i.quantity * i.unit_price) AS order_total
    FROM orders AS o
    JOIN order_items AS i ON i.order_id = o.id
    WHERE o.status = 'completed'
      AND o.ordered_at >= '2026-01-01'
      AND o.ordered_at < '2027-01-01'
    GROUP BY o.id, month
)
SELECT month,
       ROUND(SUM(order_total), 2) AS revenue,
       COUNT(*) AS orders,
       ROUND(AVG(order_total), 2) AS average_order_value
FROM order_totals
GROUP BY month
ORDER BY month
""".strip(),
    REVENUE_BY_REGION_QUESTION: REGION_QUERY,
    TOP_PRODUCTS_QUESTION: """
SELECT p.name AS product,
       ROUND(SUM(i.quantity * i.unit_price), 2) AS revenue,
       SUM(i.quantity) AS units_sold
FROM orders AS o
JOIN order_items AS i ON i.order_id = o.id
JOIN products AS p ON p.id = i.product_id
WHERE o.status = 'completed'
GROUP BY p.id, p.name
ORDER BY revenue DESC, product
LIMIT 10
""".strip(),
    TOP_CUSTOMERS_QUESTION: """
SELECT c.name AS customer,
       c.region,
       ROUND(SUM(i.quantity * i.unit_price), 2) AS revenue,
       COUNT(DISTINCT o.id) AS orders
FROM orders AS o
JOIN customers AS c ON c.id = o.customer_id
JOIN order_items AS i ON i.order_id = o.id
WHERE o.status = 'completed'
GROUP BY c.id, c.name, c.region
ORDER BY revenue DESC, customer
LIMIT 10
""".strip(),
    SALES_COMPARISON_QUESTION: """
SELECT STRFTIME('%m', o.ordered_at) AS month,
       STRFTIME('%Y', o.ordered_at) AS year,
       ROUND(SUM(i.quantity * i.unit_price), 2) AS revenue,
       COUNT(DISTINCT o.id) AS orders
FROM orders AS o
JOIN order_items AS i ON i.order_id = o.id
WHERE o.status = 'completed'
GROUP BY year, month
ORDER BY year, month
""".strip(),
    TOP_REGIONS_QUESTION: f"{REGION_QUERY}\nLIMIT 3",
}

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    region TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    unit_price REAL NOT NULL CHECK (unit_price >= 0)
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    ordered_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'returned', 'cancelled', 'processing')),
    channel TEXT NOT NULL
);

CREATE TABLE order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price REAL NOT NULL CHECK (unit_price >= 0),
    UNIQUE (order_id, product_id)
);

CREATE INDEX orders_customer_id ON orders (customer_id);
CREATE INDEX orders_ordered_at ON orders (ordered_at);
CREATE INDEX order_items_product_id ON order_items (product_id);
"""

_CATALOG = (
    ("Canvas Weekender", "Bags & Travel", 89.00),
    ("Trail Daypack", "Bags & Travel", 109.00),
    ("Commuter Tote", "Bags & Travel", 54.00),
    ("Packing Cubes", "Bags & Travel", 32.00),
    ("Travel Organizer", "Bags & Travel", 28.00),
    ("Leather Passport Sleeve", "Bags & Travel", 36.00),
    ("Pour-Over Coffee Set", "Kitchen", 65.00),
    ("Matte Ceramic Mug", "Kitchen", 18.00),
    ("Stovetop Kettle", "Kitchen", 75.00),
    ("Acacia Serving Board", "Kitchen", 42.00),
    ("Silicone Utensil Set", "Kitchen", 39.00),
    ("Insulated Water Bottle", "Kitchen", 34.00),
    ("Linen Throw", "Home", 78.00),
    ("Woven Cotton Blanket", "Home", 96.00),
    ("Ribbed Glass Vase", "Home", 34.00),
    ("Stoneware Planter", "Home", 42.00),
    ("Cotton Cushion Cover", "Home", 30.00),
    ("Essential Oil Diffuser", "Home", 58.00),
    ("Desk Lamp", "Office", 120.00),
    ("Weekly Planner", "Office", 21.00),
    ("Aluminum Laptop Stand", "Office", 59.00),
    ("Cable Management Kit", "Office", 16.00),
    ("Recycled Desk Mat", "Office", 46.00),
    ("Guided Journal", "Office", 24.00),
    ("Compact Picnic Blanket", "Outdoor", 50.00),
    ("Rechargeable Lantern", "Outdoor", 65.00),
    ("Trail Socks", "Outdoor", 15.00),
    ("Hiking Pole Set", "Outdoor", 80.00),
    ("Field Chair", "Outdoor", 95.00),
    ("Folding Camp Table", "Outdoor", 115.00),
)

_FIRST_NAMES = (
    "Alex", "Amara", "Cameron", "Charlie", "Daniel", "Eli", "Emma", "Harper",
    "Iris", "Jaden", "Jamie", "Jordan", "Kai", "Lena", "Maya", "Morgan",
    "Noah", "Nora", "Priya", "Quinn", "Riley", "Sasha", "Taylor", "Zoe",
)

_LAST_NAMES = (
    "Adams", "Ahmed", "Allen", "Anderson", "Bailey", "Baker", "Bennett",
    "Brooks", "Chatterjee", "Chen", "Clarke", "Cruz", "Davis", "Diaz",
    "Ellis", "Foster", "Garcia", "Gray", "Hernandez", "Hill", "James",
    "Kaur", "Kim", "Lee", "Lopez", "Martin", "Miller", "Moore", "Nguyen",
    "Okafor", "Park", "Patel", "Reed", "Rivera", "Roberts", "Shah",
    "Singh", "Smith", "Taylor", "Thomas", "Tran", "Walker", "Wang", "Young",
)

_REGIONS = ("West", "Northeast", "South", "Midwest", "Canada")
_REGION_WEIGHTS = (31, 23, 20, 17, 9)
_MONTHLY_ORDERS = {
    2025: (167, 183, 207, 195, 219, 235, 207, 229, 239, 255, 348, 326),
    2026: (255, 275, 286, 271, 312, 337, 322, 373, 397, 443),
}


def ensure_demo_database(path: str | Path) -> Path:
    """Create the sample database once and atomically publish the populated file."""
    target = Path(path).expanduser().resolve()
    if target.is_file():
        return target
    if target.exists():
        raise ValueError(f"The sample database path {target} must point to a file.")

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".demo-", suffix=".sqlite", dir=target.parent
    )
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(temporary_name)) as connection, connection:
            connection.executescript(_SCHEMA)
            _populate(connection)
        # A concurrent app session may seed the same path before we finish.
        with suppress(FileExistsError):
            os.link(temporary_name, target)
        if not target.is_file():
            raise ValueError(f"The sample database path {target} must point to a file.")
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return target


def _populate(connection: sqlite3.Connection) -> None:
    rng = Random(271828)
    names = [(first, last) for first in _FIRST_NAMES for last in _LAST_NAMES]
    rng.shuffle(names)
    customer_count = 720
    regions = rng.choices(_REGIONS, weights=_REGION_WEIGHTS, k=customer_count)
    customers = []
    for customer_id, ((first, last), region) in enumerate(
        zip(names[:customer_count], regions, strict=True), start=1
    ):
        created = datetime(2022, 1, 1) + timedelta(days=rng.randrange(1096))
        customers.append(
            (
                customer_id,
                f"{first} {last}",
                f"{first.lower()}.{last.lower()}{customer_id}@example.test",
                region,
                created.date().isoformat(),
            )
        )
    connection.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", customers)
    connection.executemany(
        "INSERT INTO products VALUES (?, ?, ?, ?)",
        ((product_id, *product) for product_id, product in enumerate(_CATALOG, 1)),
    )

    customer_ids = list(range(1, customer_count + 1))
    customer_weights = [rng.lognormvariate(0, 0.90) for _ in customer_ids]
    product_ids = list(range(1, len(_CATALOG) + 1))
    product_weights = [rng.uniform(0.65, 1.8) for _ in product_ids]
    orders = []
    items = []
    for year, monthly_orders in _MONTHLY_ORDERS.items():
        for month, order_count in enumerate(monthly_orders, start=1):
            buyers = rng.choices(customer_ids, weights=customer_weights, k=order_count)
            days_in_month = monthrange(year, month)[1]
            for buyer in buyers:
                order_id = len(orders) + 1
                ordered_at = datetime(
                    year,
                    month,
                    rng.randint(1, days_in_month),
                    rng.randrange(8, 23),
                    rng.randrange(60),
                )
                status = rng.choices(
                    ("completed", "returned", "cancelled", "processing"),
                    (90, 4, 3, 3) if (year, month) == (2026, 10) else (93, 4, 3, 0),
                )[0]
                channel = rng.choices(("Web", "Mobile", "Retail"), (47, 35, 18))[0]
                orders.append(
                    (order_id, buyer, ordered_at.isoformat(sep=" "), status, channel)
                )

                item_count = rng.choices((1, 2, 3, 4), (49, 35, 13, 3))[0]
                selected_products = dict.fromkeys(
                    rng.choices(product_ids, weights=product_weights, k=item_count)
                )
                for product_id in selected_products:
                    quantity = rng.choices((1, 2, 3, 4), (82, 14, 3, 1))[0]
                    discount = rng.choices((1, 0.9, 0.8), (77, 17, 6))[0]
                    unit_price = round(_CATALOG[product_id - 1][2] * discount, 2)
                    items.append((order_id, product_id, quantity, unit_price))

    connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", orders)
    connection.executemany(
        "INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES (?, ?, ?, ?)",
        items,
    )
