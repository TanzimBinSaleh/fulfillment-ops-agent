-- Fulfillment Ops Agent schema (PostgreSQL 14+)
-- Models one grocery store's online order pickup ("curbside") operation.
-- All data loaded into it is synthetic.

DROP TABLE IF EXISTS substitution_proposals CASCADE;
DROP TABLE IF EXISTS order_items CASCADE;
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS customers CASCADE;
DROP TABLE IF EXISTS inventory CASCADE;
DROP TABLE IF EXISTS products CASCADE;

CREATE TABLE products (
    sku             TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    brand           TEXT NOT NULL,
    category        TEXT NOT NULL,
    subcategory     TEXT NOT NULL,
    size_value      NUMERIC(8,2) NOT NULL,
    size_unit       TEXT NOT NULL,
    price           NUMERIC(8,2) NOT NULL CHECK (price > 0),
    is_store_brand  BOOLEAN NOT NULL DEFAULT FALSE,
    is_organic      BOOLEAN NOT NULL DEFAULT FALSE,
    is_vegan        BOOLEAN NOT NULL DEFAULT FALSE,
    is_gluten_free  BOOLEAN NOT NULL DEFAULT FALSE,
    is_dairy_free   BOOLEAN NOT NULL DEFAULT FALSE
);
COMMENT ON TABLE products IS 'Store catalog. One row per sellable SKU.';
COMMENT ON COLUMN products.subcategory IS 'Fine-grained product type, e.g. milk, plant_milk, bread. Substitutes must share it.';
COMMENT ON COLUMN products.size_unit IS 'Unit for size_value: gal, fl oz, oz, lb, ct, qt, pt.';

CREATE TABLE inventory (
    sku              TEXT PRIMARY KEY REFERENCES products(sku),
    on_hand          INTEGER NOT NULL CHECK (on_hand >= 0),
    aisle            TEXT NOT NULL,
    last_counted_at  TIMESTAMPTZ NOT NULL
);
COMMENT ON TABLE inventory IS 'Current on-hand units per SKU. on_hand = 0 means out of stock.';

CREATE TABLE customers (
    customer_id                 SERIAL PRIMARY KEY,
    first_name                  TEXT NOT NULL,
    last_initial                TEXT NOT NULL,
    phone                       TEXT NOT NULL,
    dietary_preference          TEXT CHECK (dietary_preference IN ('vegan','gluten_free','dairy_free')),
    allow_substitutions_default BOOLEAN NOT NULL DEFAULT TRUE,
    created_at                  TIMESTAMPTZ NOT NULL
);
COMMENT ON COLUMN customers.dietary_preference IS 'NULL when the customer has no stated preference.';

CREATE TABLE orders (
    order_id           SERIAL PRIMARY KEY,
    customer_id        INTEGER NOT NULL REFERENCES customers(customer_id),
    placed_at          TIMESTAMPTZ NOT NULL,
    pickup_slot_start  TIMESTAMPTZ NOT NULL,
    pickup_slot_end    TIMESTAMPTZ NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('received','picking','staged','picked_up','cancelled')),
    total_amount       NUMERIC(10,2) NOT NULL
);
COMMENT ON COLUMN orders.status IS 'received -> picking -> staged -> picked_up; or cancelled.';

CREATE TABLE order_items (
    order_item_id       SERIAL PRIMARY KEY,
    order_id            INTEGER NOT NULL REFERENCES orders(order_id),
    sku                 TEXT NOT NULL REFERENCES products(sku),
    quantity            INTEGER NOT NULL CHECK (quantity > 0),
    unit_price          NUMERIC(8,2) NOT NULL,
    allow_substitution  BOOLEAN NOT NULL,
    pick_status         TEXT NOT NULL CHECK (pick_status IN ('pending','picked','short','substituted','removed')),
    picked_quantity     INTEGER NOT NULL DEFAULT 0
);
COMMENT ON COLUMN order_items.pick_status IS 'pending = not picked yet; short = fewer units picked than ordered; substituted = replaced by another SKU; removed = dropped from the order.';

CREATE TABLE substitution_proposals (
    proposal_id       SERIAL PRIMARY KEY,
    order_item_id     INTEGER NOT NULL REFERENCES order_items(order_item_id),
    proposed_sku      TEXT NOT NULL REFERENCES products(sku),
    reason            TEXT NOT NULL,
    customer_message  TEXT,
    status            TEXT NOT NULL CHECK (status IN ('pending_approval','approved','rejected')),
    created_by        TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL
);
COMMENT ON TABLE substitution_proposals IS 'Substitution suggestions. created_by is associate or agent. Agent proposals start as pending_approval.';

CREATE INDEX ON order_items (order_id);
CREATE INDEX ON order_items (sku);
CREATE INDEX ON orders (pickup_slot_start);
CREATE INDEX ON orders (customer_id);
CREATE INDEX ON substitution_proposals (order_item_id);
