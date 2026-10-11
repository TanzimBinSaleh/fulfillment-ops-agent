"""Substitution policy, written once and used in two places:

* as a guardrail inside the propose_substitution tool (a bad proposal is
  refused with the reasons, and the model can try again), and
* as the scorer in the eval harness (did the model's first choice comply?).

The same rules are stated in plain English in the system prompt, so the eval
measures whether Claude follows written policy, not whether a filter does.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .db import connect

MAX_PRICE_INCREASE = 0.25      # substitute may cost at most 25% more than the original
SIZE_RATIO_RANGE = (0.5, 2.0)  # and be between half and double the original size
DIETARY_FLAGS = ("is_vegan", "is_gluten_free", "is_dairy_free")

POLICY_TEXT = f"""\
Substitution policy (a proposal must satisfy every rule):
1. Only substitute an item whose order_item has allow_substitution = true. Otherwise the item is removed and the customer is told.
2. Only substitute an item that actually needs it: pick_status = 'pending' and inventory.on_hand < quantity for the ordered SKU.
3. The substitute must be a different SKU in the same subcategory, with the same size_unit.
4. The substitute must have on_hand >= the ordered quantity.
5. The substitute's size must be between {SIZE_RATIO_RANGE[0]:g}x and {SIZE_RATIO_RANGE[1]:g}x the original's size_value.
6. The substitute's price may be at most {int(MAX_PRICE_INCREASE * 100)}% higher than the original's price.
7. Dietary safety: if the customer has a dietary_preference (vegan, gluten_free, dairy_free), the substitute must have the matching is_<preference> flag = true. Separately, any of is_vegan, is_gluten_free, is_dairy_free that is true on the original must also be true on the substitute.
8. Household items (category = 'household') are exempt from rule 7.
Among compliant options prefer, in order: same brand, closest size, lowest price, store brand."""


@dataclass
class SubContext:
    order_item_id: int
    quantity: int
    allow_substitution: bool
    pick_status: str
    original: dict
    original_on_hand: int
    dietary_preference: str | None


PRODUCT_COLS = ("sku, name, brand, category, subcategory, size_value::float, size_unit, price::float, "
                "is_store_brand, is_organic, is_vegan, is_gluten_free, is_dairy_free")
_PRODUCT_KEYS = [c.split("::")[0].strip() for c in PRODUCT_COLS.split(",")]


def _product_row(row) -> dict:
    return dict(zip(_PRODUCT_KEYS, row))


def load_context(settings: Settings, order_item_id: int) -> SubContext | None:
    with connect(settings.ro_url) as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT oi.order_item_id, oi.quantity, oi.allow_substitution, oi.pick_status,
                   i.on_hand, c.dietary_preference, {', '.join('p.' + k for k in PRODUCT_COLS.split(', '))}
            FROM order_items oi
            JOIN orders o USING (order_id)
            JOIN customers c USING (customer_id)
            JOIN products p ON p.sku = oi.sku
            JOIN inventory i ON i.sku = oi.sku
            WHERE oi.order_item_id = %s
        """, (order_item_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return SubContext(
        order_item_id=row[0], quantity=row[1], allow_substitution=row[2], pick_status=row[3],
        original_on_hand=row[4], dietary_preference=row[5], original=_product_row(row[6:]),
    )


def load_product(settings: Settings, sku: str) -> tuple[dict, int] | None:
    with connect(settings.ro_url) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {PRODUCT_COLS}, i.on_hand FROM products JOIN inventory i USING (sku) WHERE sku = %s", (sku,))
        row = cur.fetchone()
    if row is None:
        return None
    return _product_row(row[:-1]), row[-1]


def needs_substitution(ctx: SubContext) -> bool:
    return ctx.pick_status == "pending" and ctx.original_on_hand < ctx.quantity


def violations(ctx: SubContext, candidate: dict, candidate_on_hand: int) -> list[str]:
    """Return the list of policy rules a proposed substitute breaks (empty = compliant)."""
    out: list[str] = []
    o = ctx.original
    if not ctx.allow_substitution:
        out.append("rule 1: customer did not allow substitution for this item")
    if not needs_substitution(ctx):
        out.append("rule 2: item does not need substitution (already picked or original in stock)")
    if candidate["sku"] == o["sku"]:
        out.append("rule 3: substitute is the same SKU")
    if candidate["subcategory"] != o["subcategory"]:
        out.append(f"rule 3: subcategory {candidate['subcategory']} != {o['subcategory']}")
    if candidate["size_unit"] != o["size_unit"]:
        out.append(f"rule 3: size unit {candidate['size_unit']} != {o['size_unit']}")
    if candidate_on_hand < ctx.quantity:
        out.append(f"rule 4: only {candidate_on_hand} on hand, {ctx.quantity} needed")
    if candidate["size_unit"] == o["size_unit"] and o["size_value"]:
        ratio = candidate["size_value"] / o["size_value"]
        if not (SIZE_RATIO_RANGE[0] <= ratio <= SIZE_RATIO_RANGE[1]):
            out.append(f"rule 5: size ratio {ratio:.2f} outside {SIZE_RATIO_RANGE}")
    if candidate["price"] > o["price"] * (1 + MAX_PRICE_INCREASE) + 1e-9:
        out.append(f"rule 6: price ${candidate['price']:.2f} is more than 25% above ${o['price']:.2f}")
    if o["category"] != "household":
        pref = ctx.dietary_preference
        if pref and not candidate.get(f"is_{pref}"):
            out.append(f"rule 7: customer is {pref} but substitute is not")
        for flag in DIETARY_FLAGS:
            if o.get(flag) and not candidate.get(flag):
                out.append(f"rule 7: original is {flag[3:]} but substitute is not")
    return out


def compliant_options(settings: Settings, ctx: SubContext) -> list[str]:
    """Every SKU that would pass the policy. Used by the eval to know whether
    'no substitute exists' is the right answer for a case."""
    with connect(settings.ro_url) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {PRODUCT_COLS}, i.on_hand FROM products JOIN inventory i USING (sku) WHERE subcategory = %s",
                    (ctx.original["subcategory"],))
        rows = cur.fetchall()
    return [r[0] for r in rows if not violations(ctx, _product_row(r[:-1]), r[-1])]
