"""Seeded synthetic data generator.

Builds one store's catalog, inventory, customers and a week of online pickup
orders ending on the pinned operating day (config.OPS_NOW). Every name, brand
and phone number is invented; 555-01xx numbers are reserved for fiction.
The same seed always produces the same database, which keeps evals stable.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import OPS_NOW, STORE_TZ

STORE_BRAND = "Lonestar Select"
ORGANIC_BRAND = "Green Acre Organics"
NATIONAL_BRANDS = ["Meadowbrook", "Prairie Valley", "Sunfield", "Blue Ridge", "Harvest Moon", "Red Oak"]

# Flag order: vegan, gluten_free, dairy_free
V, G, D = "vegan", "gluten_free", "dairy_free"


@dataclass(frozen=True)
class SubSpec:
    category: str
    subcategory: str
    aisle: str
    base_price: float
    sizes: tuple  # (value, unit, price_multiplier)
    variants: tuple  # (name, frozenset(flags))
    organic_line: bool = False


def f(*flags: str) -> frozenset:
    return frozenset(flags)


CATALOG: list[SubSpec] = [
    SubSpec("dairy", "milk", "Dairy 1", 3.99, ((0.5, "gal", 0.6), (1, "gal", 1.0)),
            (("Whole Milk", f(G)), ("2% Reduced Fat Milk", f(G)), ("Fat Free Milk", f(G)),
             ("Lactose-Free 2% Milk", f(G))), organic_line=True),
    SubSpec("dairy", "plant_milk", "Dairy 1", 3.79, ((32, "fl oz", 0.6), (64, "fl oz", 1.0)),
            (("Oat Milk", f(V, D)), ("Unsweetened Almond Milk", f(V, G, D)), ("Soy Milk", f(V, G, D)))),
    SubSpec("dairy", "yogurt", "Dairy 2", 5.49, ((5.3, "oz", 0.3), (32, "oz", 1.0)),
            (("Plain Greek Yogurt", f(G)), ("Vanilla Greek Yogurt", f(G)), ("Strawberry Yogurt", f(G))),
            organic_line=True),
    SubSpec("dairy", "cheese", "Dairy 2", 3.99, ((8, "oz", 1.0), (16, "oz", 1.8)),
            (("Shredded Sharp Cheddar", f(G)), ("Shredded Mozzarella", f(G)), ("Sliced Swiss", f(G)))),
    SubSpec("dairy", "eggs", "Dairy 3", 3.49, ((12, "ct", 1.0), (18, "ct", 1.45)),
            (("Large Grade A Eggs", f(G, D)), ("Cage-Free Large Eggs", f(G, D)), ("Large Brown Eggs", f(G, D))),
            organic_line=True),
    SubSpec("dairy", "butter", "Dairy 3", 4.99, ((8, "oz", 0.55), (16, "oz", 1.0)),
            (("Salted Butter", f(G)), ("Unsalted Butter", f(G)), ("Plant-Based Butter", f(V, G, D)))),
    SubSpec("bakery", "bread", "Bakery", 3.29, ((20, "oz", 1.0), (24, "oz", 1.15)),
            (("White Sandwich Bread", f(V, D)), ("100% Whole Wheat Bread", f(V, D)),
             ("Gluten-Free Multigrain Bread", f(V, G, D)), ("Sourdough Loaf", f(V, D)))),
    SubSpec("bakery", "tortillas", "Bakery", 2.99, ((10, "ct", 1.0), (20, "ct", 1.7)),
            (("Flour Tortillas", f(V, D)), ("Corn Tortillas", f(V, G, D)), ("Whole Wheat Tortillas", f(V, D)))),
    SubSpec("produce", "fruit", "Produce", 2.99, ((2, "lb", 1.0), (3, "lb", 1.4)),
            (("Bananas", f(V, G, D)), ("Gala Apples", f(V, G, D)), ("Navel Oranges", f(V, G, D)),
             ("Strawberries", f(V, G, D)), ("Hass Avocados", f(V, G, D))), organic_line=True),
    SubSpec("produce", "vegetables", "Produce", 1.99, ((1, "lb", 1.0), (2, "lb", 1.7)),
            (("Baby Carrots", f(V, G, D)), ("Romaine Hearts", f(V, G, D)), ("Russet Potatoes", f(V, G, D)),
             ("Yellow Onions", f(V, G, D)), ("Broccoli Crowns", f(V, G, D))), organic_line=True),
    SubSpec("meat", "chicken", "Meat", 4.99, ((1, "lb", 1.0), (2.5, "lb", 2.3)),
            (("Boneless Skinless Chicken Breast", f(G, D)), ("Boneless Chicken Thighs", f(G, D)),
             ("Ground Chicken", f(G, D)))),
    SubSpec("meat", "beef", "Meat", 5.99, ((1, "lb", 1.0), (2, "lb", 1.9)),
            (("80/20 Ground Beef", f(G, D)), ("93/7 Lean Ground Beef", f(G, D)), ("Ribeye Steak", f(G, D)))),
    SubSpec("pantry", "pasta", "Aisle 4", 1.79, ((16, "oz", 1.0), (32, "oz", 1.8)),
            (("Spaghetti", f(V, D)), ("Penne Rigate", f(V, D)), ("Gluten-Free Penne", f(V, G, D)),
             ("Elbow Macaroni", f(V, D)))),
    SubSpec("pantry", "rice", "Aisle 4", 2.99, ((2, "lb", 1.0), (5, "lb", 2.2)),
            (("Long Grain White Rice", f(V, G, D)), ("Jasmine Rice", f(V, G, D)), ("Brown Rice", f(V, G, D)))),
    SubSpec("pantry", "pasta_sauce", "Aisle 4", 2.99, ((24, "oz", 1.0), (45, "oz", 1.7)),
            (("Marinara Sauce", f(V, G, D)), ("Tomato Basil Sauce", f(V, G, D)), ("Four Cheese Sauce", f(G)))),
    SubSpec("pantry", "cereal", "Aisle 5", 3.99, ((12, "oz", 1.0), (18, "oz", 1.4)),
            (("Honey Oat Clusters", f(D)), ("Frosted Corn Flakes", f(V, D)), ("Gluten-Free Rice Crisps", f(V, G, D)))),
    SubSpec("pantry", "canned_beans", "Aisle 6", 1.09, ((15, "oz", 1.0), (29, "oz", 1.7)),
            (("Black Beans", f(V, G, D)), ("Pinto Beans", f(V, G, D)), ("Garbanzo Beans", f(V, G, D)))),
    SubSpec("pantry", "peanut_butter", "Aisle 5", 2.79, ((16, "oz", 1.0), (40, "oz", 2.1)),
            (("Creamy Peanut Butter", f(V, G, D)), ("Crunchy Peanut Butter", f(V, G, D)))),
    SubSpec("beverages", "soda", "Aisle 8", 6.99, ((12, "ct", 1.0), (24, "ct", 1.8)),
            (("Cola", f(V, G, D)), ("Diet Cola", f(V, G, D)), ("Lemon-Lime Soda", f(V, G, D)))),
    SubSpec("beverages", "water", "Aisle 8", 4.99, ((12, "ct", 0.6), (24, "ct", 1.0)),
            (("Purified Spring Water", f(V, G, D)), ("Lime Sparkling Water", f(V, G, D)))),
    SubSpec("beverages", "juice", "Dairy 1", 3.99, ((52, "fl oz", 1.0), (89, "fl oz", 1.5)),
            (("Orange Juice", f(V, G, D)), ("Apple Juice", f(V, G, D)))),
    SubSpec("beverages", "coffee", "Aisle 7", 7.99, ((12, "oz", 1.0), (24, "oz", 1.8)),
            (("Medium Roast Ground Coffee", f(V, G, D)), ("Dark Roast Ground Coffee", f(V, G, D)),
             ("Decaf Ground Coffee", f(V, G, D)))),
    SubSpec("frozen", "frozen_pizza", "Frozen", 5.99, ((1, "ct", 1.0),),
            (("Pepperoni Pizza", frozenset()), ("Four Cheese Pizza", frozenset()),
             ("Cauliflower Crust Veggie Pizza", f(G)))),
    SubSpec("frozen", "frozen_vegetables", "Frozen", 1.49, ((12, "oz", 1.0), (32, "oz", 2.0)),
            (("Frozen Sweet Peas", f(V, G, D)), ("Frozen Mixed Vegetables", f(V, G, D)),
             ("Frozen Broccoli Florets", f(V, G, D)))),
    SubSpec("frozen", "ice_cream", "Frozen", 4.99, ((1, "pt", 0.55), (1.5, "qt", 1.0)),
            (("Vanilla Ice Cream", f(G)), ("Chocolate Ice Cream", f(G)),
             ("Non-Dairy Vanilla Frozen Dessert", f(V, G, D)))),
    SubSpec("snacks", "chips", "Aisle 9", 3.99, ((8, "oz", 1.0), (13, "oz", 1.4)),
            (("Sea Salt Potato Chips", f(V, G, D)), ("Restaurant-Style Tortilla Chips", f(V, G, D)),
             ("BBQ Potato Chips", f(G)))),
    SubSpec("snacks", "crackers", "Aisle 9", 3.29, ((8, "oz", 1.0), (16, "oz", 1.7)),
            (("Saltine Crackers", f(V, D)), ("Whole Grain Crackers", f(V, D)),
             ("Gluten-Free Rice Crackers", f(V, G, D)))),
    SubSpec("household", "paper_goods", "Aisle 12", 8.99, ((6, "ct", 1.0), (12, "ct", 1.8)),
            (("Paper Towels", frozenset()), ("Bath Tissue", frozenset()))),
    SubSpec("household", "laundry", "Aisle 12", 9.99, ((50, "fl oz", 1.0), (100, "fl oz", 1.8)),
            (("Laundry Detergent Original", frozenset()), ("Laundry Detergent Free & Clear", frozenset()))),
]

FIRST_NAMES = [
    "Avery", "Jordan", "Priya", "Mateo", "Grace", "Hassan", "Lena", "Diego", "Mei", "Samuel",
    "Fatima", "Noah", "Elena", "Kofi", "Hannah", "Ravi", "Olivia", "Tariq", "Sofia", "Marcus",
    "Aisha", "Lucas", "Nadia", "Ethan", "Yuki", "Caleb", "Amara", "Isaac", "Leila", "Owen",
]
LAST_INITIALS = list("ABCDEFGHJKLMNPRSTVW")


def _fmt_size(value: float, unit: str) -> str:
    v = int(value) if float(value).is_integer() else value
    return f"{v} {unit}"


def build_products(rng: random.Random) -> list[dict]:
    products: list[dict] = []
    counter = 0
    for spec in CATALOG:
        brands = [STORE_BRAND] + rng.sample(NATIONAL_BRANDS, 2)
        if spec.organic_line:
            brands.append(ORGANIC_BRAND)
        for variant, flags in spec.variants:
            for brand in brands:
                for value, unit, mult in spec.sizes:
                    counter += 1
                    price = spec.base_price * mult
                    if brand == STORE_BRAND:
                        price *= 0.82
                    elif brand == ORGANIC_BRAND:
                        price *= 1.45
                    else:
                        price *= rng.uniform(0.95, 1.12)
                    products.append({
                        "sku": f"{spec.subcategory[:3].upper()}-{counter:04d}",
                        "name": f"{brand} {variant} {_fmt_size(value, unit)}",
                        "brand": brand,
                        "category": spec.category,
                        "subcategory": spec.subcategory,
                        "size_value": value,
                        "size_unit": unit,
                        "price": round(price, 2),
                        "is_store_brand": brand == STORE_BRAND,
                        "is_organic": brand == ORGANIC_BRAND,
                        "is_vegan": V in flags,
                        "is_gluten_free": G in flags,
                        "is_dairy_free": D in flags,
                        "_aisle": spec.aisle,
                    })
    return products


def build_inventory(rng: random.Random, products: list[dict]) -> list[dict]:
    rows = []
    for p in products:
        r = rng.random()
        if r < 0.11:
            on_hand = 0
        elif r < 0.26:
            on_hand = rng.randint(1, 5)
        else:
            on_hand = rng.randint(6, 80)
        counted = OPS_NOW - timedelta(hours=rng.randint(1, 30))
        rows.append({"sku": p["sku"], "on_hand": on_hand, "aisle": p["_aisle"], "last_counted_at": counted})
    return rows


def build_customers(rng: random.Random, n: int = 160) -> list[dict]:
    rows = []
    for i in range(1, n + 1):
        r = rng.random()
        pref = None
        if r < 0.10:
            pref = "vegan"
        elif r < 0.20:
            pref = "gluten_free"
        elif r < 0.30:
            pref = "dairy_free"
        rows.append({
            "customer_id": i,
            "first_name": rng.choice(FIRST_NAMES),
            "last_initial": rng.choice(LAST_INITIALS),
            "phone": f"214-555-01{i % 100:02d}",
            "dietary_preference": pref,
            "allow_substitutions_default": rng.random() < 0.82,
            "created_at": OPS_NOW - timedelta(days=rng.randint(20, 700)),
        })
    return rows


def _eligible_for(pref: str | None, p: dict) -> bool:
    """Customers mostly order things that fit their stated preference."""
    if p["category"] == "household" or pref is None:
        return True
    return bool(p[f"is_{pref}"])


def build_orders(rng: random.Random, products: list[dict], inventory: list[dict], customers: list[dict]):
    stock = {i["sku"]: i["on_hand"] for i in inventory}
    by_pref = {
        pref: [p for p in products if _eligible_for(pref, p)]
        for pref in (None, "vegan", "gluten_free", "dairy_free")
    }
    out_of_stock = {pref: [p for p in pool if stock[p["sku"]] == 0] for pref, pool in by_pref.items()}

    orders, items, proposals = [], [], []
    order_id = item_id = proposal_id = 0
    ops_day = OPS_NOW.date()
    start_day = ops_day - timedelta(days=6)

    for d in range(7):
        day = start_day + timedelta(days=d)
        for _ in range(rng.randint(48, 66)):
            order_id += 1
            cust = rng.choice(customers)
            pref = cust["dietary_preference"]
            hour = rng.choice(range(9, 21))
            slot_start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=STORE_TZ)
            slot_end = slot_start + timedelta(hours=1)
            placed = slot_start - timedelta(hours=rng.randint(4, 48), minutes=rng.randint(0, 59))

            if day < ops_day or slot_end <= OPS_NOW:
                status = "cancelled" if rng.random() < 0.05 else "picked_up"
            elif slot_start <= OPS_NOW + timedelta(hours=1):
                status = rng.choice(["staged", "staged", "picking"])
            elif slot_start <= OPS_NOW + timedelta(hours=3):
                status = rng.choice(["picking", "received"])
            else:
                status = "received"

            n_items = rng.randint(3, 14)
            chosen: dict[str, dict] = {}
            pool = by_pref[pref]
            while len(chosen) < n_items:
                # Orders still to be picked today sometimes include items that are out
                # of stock right now: that is the substitution queue the agent works.
                if status in ("received", "picking") and rng.random() < 0.10 and out_of_stock[pref]:
                    p = rng.choice(out_of_stock[pref])
                else:
                    p = rng.choice(pool)
                chosen[p["sku"]] = p

            total = 0.0
            for p in chosen.values():
                item_id += 1
                qty = rng.choices([1, 2, 3], weights=[70, 22, 8])[0]
                allow = cust["allow_substitutions_default"] if rng.random() < 0.9 else not cust["allow_substitutions_default"]
                total += qty * p["price"]

                if status in ("received",):
                    pick_status, picked = "pending", 0
                elif status == "picking":
                    if stock[p["sku"]] < qty or rng.random() < 0.5:
                        pick_status, picked = "pending", 0
                    else:
                        pick_status, picked = "picked", qty
                elif status == "cancelled":
                    pick_status, picked = "removed", 0
                else:  # staged / picked_up: already resolved
                    r = rng.random()
                    if r < 0.88:
                        pick_status, picked = "picked", qty
                    elif r < 0.91 and qty > 1:
                        pick_status, picked = "short", qty - 1
                    elif r < 0.96 and allow:
                        pick_status, picked = "substituted", qty
                    else:
                        pick_status, picked = "removed", 0

                items.append({
                    "order_item_id": item_id, "order_id": order_id, "sku": p["sku"], "quantity": qty,
                    "unit_price": p["price"], "allow_substitution": allow,
                    "pick_status": pick_status, "picked_quantity": picked,
                })

                if pick_status == "substituted":
                    same = [q for q in by_pref[pref] if q["subcategory"] == p["subcategory"] and q["sku"] != p["sku"]]
                    if same:
                        sub = rng.choice(same)
                        approved = rng.random() < 0.85
                        proposal_id += 1
                        proposals.append({
                            "proposal_id": proposal_id, "order_item_id": item_id, "proposed_sku": sub["sku"],
                            "reason": "Original item out of stock at pick time",
                            "customer_message": None,
                            "status": "approved" if approved else "rejected",
                            "created_by": "associate",
                            "created_at": slot_start - timedelta(minutes=rng.randint(20, 90)),
                        })

            orders.append({
                "order_id": order_id, "customer_id": cust["customer_id"], "placed_at": placed,
                "pickup_slot_start": slot_start, "pickup_slot_end": slot_end, "status": status,
                "total_amount": round(total, 2),
            })
    return orders, items, proposals


def generate(seed: int = 7) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    products = build_products(rng)
    inventory = build_inventory(rng, products)
    customers = build_customers(rng)
    orders, items, proposals = build_orders(rng, products, inventory, customers)
    for p in products:
        p.pop("_aisle")
    return {
        "products": products,
        "inventory": inventory,
        "customers": customers,
        "orders": orders,
        "order_items": items,
        "substitution_proposals": proposals,
    }
