# Demo: a manager's afternoon, through the MCP server

This transcript was recorded on 2026-10-11 in a cloud session. Claude (Opus 5.5) connected to `fulfillment-agent mcp` as an MCP client over stdio, read the `fulfillment://schema` and `fulfillment://policy` resources, and answered four manager requests using only the server's tools. Every SQL query and number below is the real tool output against the seeded database (`fulfillment-agent setup`, seed 7).

The store clock is pinned to **Monday 2026-10-12, 13:30**.

> This is a live demo, not an eval score. The eval suites in `evals/` measure accuracy blind; this page shows what working with the tools looks like.

You can replay it in Claude Code after `claude mcp add fulfillment-ops -- <path>/fulfillment-agent mcp` by typing the four requests below.

---

## 1. "How many orders are still to be picked today, by pickup hour?"

`run_sql`:

```sql
SELECT to_char(pickup_slot_start, 'HH24:MI') AS slot,
       COUNT(*) FILTER (WHERE status = 'received') AS received,
       COUNT(*) FILTER (WHERE status = 'picking')  AS picking,
       COUNT(*) AS still_to_pick
FROM orders
WHERE pickup_slot_start::date = DATE '2026-10-12'
  AND status IN ('received','picking')
GROUP BY 1 ORDER BY 1;
```

| Slot | Received | Picking | Still to pick |
|---|---|---|---|
| 13:00 | 0 | 1 | 1 |
| 14:00 | 0 | 1 | 1 |
| 15:00 | 2 | 2 | 4 |
| 16:00 | 0 | 7 | 7 |
| 17:00 | 6 | 0 | 6 |
| 18:00 | 3 | 0 | 3 |
| 19:00 | 3 | 0 | 3 |
| 20:00 | 6 | 0 | 6 |

**Answer:** 31 orders are still to be picked today. One needs attention right now: order 369 is in the 13:00–14:00 slot that is already half over and is still being picked. The 16:00 wave (7 orders, all already in picking) is the next crunch.

## 2. "What's our substitution acceptance rate by category? Where are customers rejecting the most?"

`run_sql`:

```sql
SELECT p.category,
       COUNT(*) FILTER (WHERE sp.status = 'approved') AS approved,
       COUNT(*) FILTER (WHERE sp.status = 'rejected') AS rejected,
       ROUND(100.0 * COUNT(*) FILTER (WHERE sp.status = 'approved')
             / NULLIF(COUNT(*) FILTER (WHERE sp.status IN ('approved','rejected')), 0), 1) AS acceptance_pct
FROM substitution_proposals sp
JOIN order_items oi USING (order_item_id)
JOIN products p ON p.sku = oi.sku
GROUP BY p.category ORDER BY acceptance_pct;
```

| Category | Approved | Rejected | Acceptance |
|---|---|---|---|
| snacks | 8 | 3 | 72.7% |
| produce | 15 | 4 | 78.9% |
| frozen | 12 | 2 | 85.7% |
| beverages | 16 | 2 | 88.9% |
| dairy | 34 | 4 | 89.5% |
| household | 9 | 1 | 90.0% |
| pantry | 28 | 3 | 90.3% |
| bakery | 10 | 1 | 90.9% |
| meat | 12 | 1 | 92.3% |

Follow-up query comparing approved and rejected substitutions:

| | Count | Avg. price change | Brand changed | Size changed |
|---|---|---|---|---|
| Approved | 144 | +3.8% | 72.9% | 50.0% |
| Rejected | 21 | −0.9% | 61.9% | 61.9% |

**Answer:** Overall acceptance is 87.3% (144 of 165). Snacks (72.7%) and produce (78.9%) are rejected most. Rejections aren't explained by price (rejected swaps were slightly *cheaper* on average) or by brand changes. Size changes are a bit more common among rejections (62% vs. 50%), but with only 21 rejections that's a weak signal. Produce is worth a look, since shoppers tend to be particular about fresh items.

## 3. "Which vegan or gluten-free customers have orders waiting? Anything risky in them?"

`run_sql` (joined customers, orders, items and inventory, with a check for items that conflict with the customer's preference):

| Customer | Preference | Order | Slot | Status | Lines | Need attention | Conflicting items |
|---|---|---|---|---|---|---|---|
| Isaac S. | vegan | 372 | 14:00 | staged | 11 | 0 | 0 |
| Marcus A. | gluten_free | 401 | 14:00 | staged | 3 | 0 | 0 |
| Nadia T. | vegan | 358 | 16:00 | picking | 8 | 1 | 0 |
| Olivia E. | vegan | 374 | 19:00 | received | 3 | 0 | 0 |

A second query checked every exception (substituted, removed, short or out-of-stock line) on those orders:

- **Order 372 (Isaac, vegan):** frozen mixed vegetables were substituted with frozen broccoli florets, an approved swap. The substitute is vegan and gluten-free, so it's safe.
- **Order 358 (Nadia, vegan, 16:00):** Blue Ridge Orange Juice 89 fl oz (×2) is out of stock and substitution is allowed. It needs a vegan-safe swap before the 16:00 wave.
- **Order 401 (Marcus, gluten-free):** Lonestar Select Long Grain White Rice 2 lb was marked *removed* even though 72 are on hand. That looks like a picking miss; worth re-picking before he arrives.

**Answer:** Four customers, no dietary conflicts in any order. Two actions: find a vegan-safe orange juice substitute for Nadia's 16:00 order, and check why rice was removed from Marcus's staged order when it's in stock.

## 4. "Work through every out-of-stock item in orders due before 4pm and propose substitutes."

`run_sql` found **15 items in 6 orders** (slots 13:00–15:00) that are pending with stock below the ordered quantity. For each item Claude called `find_substitute_candidates`, checked the candidates against the written policy itself (the tool doesn't pre-filter), and called `propose_substitution`.

**Proposed: 11 substitutions.** All 11 passed the server's policy check on the first attempt and are saved as `pending_approval` for a manager:

| Order | Customer | Out of stock | Proposed | Qty | Price change |
|---|---|---|---|---|---|
| 351 | Sofia E. | Sunfield BBQ Potato Chips 13 oz | Lonestar Select BBQ Potato Chips 13 oz | 1 | −$1.61 |
| 354 | Marcus N. | Lonestar Select Elbow Macaroni 32 oz | Lonestar Select Elbow Macaroni 16 oz | 1 | −$1.17 |
| 354 | Marcus N. | Lonestar Select Spaghetti 32 oz | Sunfield Spaghetti 32 oz | 3 | +$1.65 |
| 354 | Marcus N. | Red Oak Gala Apples 3 lb | Lonestar Select Gala Apples 3 lb | 2 | −$1.66 |
| 354 | Marcus N. | Sunfield Vanilla Greek Yogurt 32 oz | Lonestar Select Vanilla Greek Yogurt 32 oz | 1 | −$1.15 |
| 354 | Marcus N. | Lonestar Select Vanilla Ice Cream 1 pt | Lonestar Select Non-Dairy Vanilla Frozen Dessert 1 pt | 2 | $0.00 |
| 379 | Diego F. | Prairie Valley Frosted Corn Flakes 18 oz | Lonestar Select Frosted Corn Flakes 18 oz | 1 | −$1.20 |
| 379 | Diego F. | Lonestar Select Frozen Sweet Peas 32 oz | Blue Ridge Frozen Sweet Peas 32 oz | 1 | +$0.42 |
| 381 | Owen G. | Prairie Valley Honey Oat Clusters 18 oz | Lonestar Select Honey Oat Clusters 18 oz | 1 | −$0.91 |
| 399 | Hannah P. | Lonestar Select Spaghetti 32 oz | Sunfield Spaghetti 32 oz | 1 | +$0.55 |
| 399 | Hannah P. | Blue Ridge Pepperoni Pizza | Lonestar Select Pepperoni Pizza | 1 | −$1.10 |

Net effect for customers: −$6.18 across the 11 lines.

Two judgment calls the policy left open:

- **Elbow macaroni 32 oz:** the same macaroni in 32 oz from other brands costs 29–32% more, over the 25% cap, so the closest compliant option was the store brand's 16 oz box. The customer pays less but gets half the pasta.
- **Vanilla ice cream:** the other brands' vanilla pints are over the price cap, and the 1.5 qt tub is a different unit. Two store-brand pints tied at $2.25 (chocolate ice cream and non-dairy vanilla). Claude picked non-dairy vanilla to keep the flavor and said "non-dairy" plainly in the customer text.

Example customer text (order 354, apples):

> Hi Marcus, Red Oak Gala Apples 3 lb ($4.26) are out of stock. We'd like to give you 2 bags of Lonestar Select Gala Apples 3 lb ($3.43 each) instead. You can decline them at pickup. - Your pickup team

**Not proposed: 4 items.**

| Order | Item | Why |
|---|---|---|
| 369 (Sofia H., 13:00) | Blue Ridge Pinto Beans 15 oz ×2 | Customer did not allow substitution. Remove and notify. |
| 351 (Sofia E.) | Harvest Moon Pinto Beans 15 oz ×2 | Customer did not allow substitution. Remove and notify. |
| 399 (Hannah P.) | Blue Ridge Marinara Sauce 45 oz | Customer did not allow substitution. Remove and notify. |
| 379 (Diego F.) | Lonestar Select Gluten-Free Penne 16 oz ×2 | No compliant option: the item is gluten-free, and the other gluten-free penne in stock costs too much (other brands' 16 oz boxes are $1.90–$1.96, 29–33% more than $1.47; the 32 oz box is 80% more). **Manager decision:** override the price cap for Sunfield 16 oz at $1.90, or remove the item. |

**Guardrail check.** As a deliberate test, Claude tried to propose regular spaghetti for the gluten-free penne. The server refused it:

```
Proposal refused by policy check: rule 7: original is gluten_free but substitute is not
```

---

## What the demo changed in the project

Working the queue by hand exposed a gap in the substitution policy's preference order. It originally ranked *same brand* first, which would have offered Red Oak oranges in place of out-of-stock Red Oak Gala apples while store-brand Gala apples were on the shelf. The policy now prefers **same product line, then closest size, then same brand, then lowest price, then store brand**. A `product_line` column was added to the schema, the candidate tool sorts by it, the eval's "preferred option" uses the same order, and a test covers it.

## Gaps worth building next

- A customer with several substitutions (Marcus has 5) should get one combined text, not five.
- When some units are in stock (1 of 2 ice cream pints), the policy can only substitute the whole line; offering "1 original + 1 substitute" would be better.
- Proposals don't reserve stock. Two proposals here use Sunfield Spaghetti 32 oz (4 boxes, 11 on hand), which is fine today but should be checked across the queue.
