"""Categories below a product's zone: a tree per zone, any depth, e.g.
Grocery > Condiments > Ketchup.

A product has a zone (products.zone, a /zones zone's name) and optionally a
category in that zone's tree (products.category_id); setting a category
also sets the zone. Categories are kept in the categories table: zone is the
zone's name (renaming a zone renames it here too), parent_id is NULL at the
top of a zone.

Grocery is a built-in zone with the built-in tree GROCERY_TREE (builtin=1:
they can't be renamed or deleted, but categories can be added under them).
New grocery lines from receipt photos get a category picked by the model
from it (categorize_groceries). Other zones' trees are built by hand on
/zones/<id>/categories.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import threading

GROCERY_ZONE = "Grocery"
# Top-level category: its sub-categories
GROCERY_TREE: dict[str, list[str]] = {
    "Produce": ["Fruit", "Vegetables", "Herbs"],
    "Meat": ["Raw Chicken", "Raw Beef", "Raw Pork", "Ground Meat", "Bacon & Sausage", "Deli Meat"],
    "Seafood": [],
    "Dairy & Eggs": ["Milk", "Cheese", "Cream Cheese", "Butter", "Yogurt", "Eggs", "Sour Cream"],
    "Bakery": ["Bread", "Bagels", "Tortillas", "Buns & Rolls"],
    "Pantry": ["Rice & Grains", "Pasta", "Canned Goods", "Baking", "Cereal", "Spices", "Oils & Vinegar", "Soup"],
    "Condiments": ["Ketchup", "Mustard", "Mayonnaise", "Salsa", "Hot Sauce", "Dressing", "BBQ Sauce"],
    "Frozen": ["Pizza", "French Fries", "Meals", "Vegetables", "Ice Cream"],
    "Prepared Food": ["Ready-made Meals", "Deli Salads", "Sushi"],
    "Snacks & Sweets": ["Chips", "Crackers", "Cookies", "Candy", "Nuts"],
    "Beverages": ["Coffee", "Tea", "Sparkling Water", "Soda", "Juice"],
    "Alcohol": ["Wine", "Beer", "Spirits"],
}
# A line's (old-style) category that means it's groceries
GROCERY_LINE_CATEGORIES = ("Groceries", "Alcohol")
SEP = " › "


class CategoryError(Exception):
    pass


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_builtin(conn: sqlite3.Connection) -> None:
    """The Grocery zone and its built-in categories (any missing are added)."""
    if not conn.execute("SELECT 1 FROM wc_zones WHERE name=?", (GROCERY_ZONE,)).fetchone():
        conn.execute("INSERT INTO wc_zones (name, zone_type, builtin) VALUES (?, 'grocery', 1)", (GROCERY_ZONE,))
    else:
        conn.execute("UPDATE wc_zones SET builtin=1 WHERE name=?", (GROCERY_ZONE,))
    for top, subs in GROCERY_TREE.items():
        tid = _find(conn, GROCERY_ZONE, None, top)
        if tid is None:
            tid = conn.execute("INSERT INTO categories (zone, parent_id, name, builtin, created_at) "
                               "VALUES (?, NULL, ?, 1, ?)", (GROCERY_ZONE, top, _now())).lastrowid
        for sub in subs:
            if _find(conn, GROCERY_ZONE, tid, sub) is None:
                conn.execute("INSERT INTO categories (zone, parent_id, name, builtin, created_at) "
                             "VALUES (?, ?, ?, 1, ?)", (GROCERY_ZONE, tid, sub, _now()))
    conn.commit()


def _find(conn: sqlite3.Connection, zone: str, parent_id: int | None, name: str) -> int | None:
    row = conn.execute("SELECT id FROM categories WHERE zone=? AND parent_id IS ? AND name = ? COLLATE NOCASE",
                       (zone, parent_id, name)).fetchone()
    return row[0] if row else None


def all_categories(conn: sqlite3.Connection, zone: str | None = None) -> list[dict]:
    """Every category (of one zone), each with its path (names from the top)
    and n_products, in tree order: each followed by its children."""
    rows = [dict(zip(("id", "zone", "parent_id", "name", "builtin"), r)) for r in conn.execute(
        "SELECT id, zone, parent_id, name, builtin FROM categories" + (" WHERE zone=?" if zone else "")
        + " ORDER BY name COLLATE NOCASE", (zone,) if zone else ())]
    counts = dict(conn.execute("SELECT category_id, COUNT(*) FROM products WHERE category_id IS NOT NULL "
                               "GROUP BY category_id").fetchall())
    kids: dict[int | None, list[dict]] = {}
    for r in rows:
        kids.setdefault(r["parent_id"], []).append(r)
    out = []

    def walk(parent, path, depth):
        for r in kids.get(parent, []):
            r["path"] = path + [r["name"]]
            r["depth"] = depth
            r["n_products"] = counts.get(r["id"], 0)
            out.append(r)
            walk(r["id"], r["path"], depth + 1)

    # Built-in categories first within Grocery, keeping GROCERY_TREE's order at the top
    order = {n: i for i, n in enumerate(GROCERY_TREE)}
    for lst in kids.values():
        lst.sort(key=lambda r: (order.get(r["name"], len(order)) if r["parent_id"] is None else 0,
                                r["name"].lower()))
    walk(None, [], 0)
    return out


def path_of(conn: sqlite3.Connection, category_id: int | None) -> list[str]:
    names, cid, seen = [], category_id, set()
    while cid is not None and cid not in seen:
        seen.add(cid)
        row = conn.execute("SELECT name, parent_id FROM categories WHERE id=?", (cid,)).fetchone()
        if row is None:
            break
        names.insert(0, row[0])
        cid = row[1]
    return names


def create(conn: sqlite3.Connection, zone: str, parent_id: int | None, name: str) -> int:
    name = " ".join((name or "").split())
    if not name:
        raise CategoryError("A category needs a name")
    if len(name) > 60:
        raise CategoryError("Category names are at most 60 characters")
    if not conn.execute("SELECT 1 FROM wc_zones WHERE name=?", (zone,)).fetchone():
        raise CategoryError(f"No zone named {zone!r}")
    if parent_id is not None:
        p = conn.execute("SELECT zone FROM categories WHERE id=?", (parent_id,)).fetchone()
        if p is None or p[0] != zone:
            raise CategoryError("The parent category isn't in that zone")
    if _find(conn, zone, parent_id, name) is not None:
        raise CategoryError(f"{name!r} already exists there")
    cid = conn.execute("INSERT INTO categories (zone, parent_id, name, builtin, created_at) VALUES (?, ?, ?, 0, ?)",
                       (zone, parent_id, name, _now())).lastrowid
    conn.commit()
    return cid


def update(conn: sqlite3.Connection, cid: int, name: str | None = None, parent_id=...) -> None:
    """Rename and/or move (parent_id None = top of its zone) a category you made."""
    row = conn.execute("SELECT zone, parent_id, name, builtin FROM categories WHERE id=?", (cid,)).fetchone()
    if row is None:
        raise CategoryError("Category not found")
    zone, old_parent, old_name, builtin = row
    if builtin:
        raise CategoryError("Built-in categories can't be renamed or moved")
    new_parent = old_parent if parent_id is ... else parent_id
    if new_parent is not None:
        p = conn.execute("SELECT zone FROM categories WHERE id=?", (new_parent,)).fetchone()
        if p is None or p[0] != zone:
            raise CategoryError("The new parent isn't in the same zone")
        cur = new_parent
        while cur is not None:  # not under itself
            if cur == cid:
                raise CategoryError("A category can't go inside itself")
            cur = conn.execute("SELECT parent_id FROM categories WHERE id=?", (cur,)).fetchone()[0]
    new_name = " ".join((name or old_name).split())
    if not new_name or len(new_name) > 60:
        raise CategoryError("Category names are 1 to 60 characters")
    other = _find(conn, zone, new_parent, new_name)
    if other is not None and other != cid:
        raise CategoryError(f"{new_name!r} already exists there")
    conn.execute("UPDATE categories SET name=?, parent_id=? WHERE id=?", (new_name, new_parent, cid))
    conn.commit()


def delete(conn: sqlite3.Connection, cid: int) -> dict:
    """Delete a category you made. Its sub-categories and products move up to
    its parent (or to no category, at the top)."""
    row = conn.execute("SELECT parent_id, builtin FROM categories WHERE id=?", (cid,)).fetchone()
    if row is None:
        raise CategoryError("Category not found")
    if row[1]:
        raise CategoryError("Built-in categories can't be deleted")
    moved = conn.execute("UPDATE products SET category_id=? WHERE category_id=?", (row[0], cid)).rowcount
    conn.execute("UPDATE categories SET parent_id=? WHERE parent_id=?", (row[0], cid))
    conn.execute("DELETE FROM categories WHERE id=?", (cid,))
    conn.commit()
    return {"products_moved": moved}


def set_product(conn: sqlite3.Connection, product_id: int, category_id: int | None) -> str | None:
    """Put a product in a category (and so in its zone). None clears just the
    category. Returns the product's zone."""
    if category_id is None:
        conn.execute("UPDATE products SET category_id=NULL WHERE id=?", (product_id,))
        return (conn.execute("SELECT zone FROM products WHERE id=?", (product_id,)).fetchone() or [None])[0]
    row = conn.execute("SELECT zone FROM categories WHERE id=?", (category_id,)).fetchone()
    if row is None:
        raise CategoryError("Category not found")
    conn.execute("UPDATE products SET category_id=?, zone=? WHERE id=?", (category_id, row[0], product_id))
    return row[0]


def find_path(conn: sqlite3.Connection, zone: str, path: list[str]) -> int | None:
    cid = None
    for name in path:
        cid = _find(conn, zone, cid, name)
        if cid is None:
            return None
    return cid


# ---- New groceries from receipt photos ----------------------------------------

def _grocery_choices(conn: sqlite3.Connection) -> list[dict]:
    return all_categories(conn, GROCERY_ZONE)


def categorize_groceries(db_path: str, product_ids: list[int]) -> None:
    """Put grocery products that aren't sorted yet into the Grocery zone, with
    a category the model picks from the Grocery tree (a fixed list). If the
    model can't be reached they're still put in Grocery, without a category;
    the error is printed and the product shows as uncategorized."""
    import httpx

    from smart_home import delegated, receipts
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        todo = [r for r in conn.execute(
            f"SELECT id, name FROM products WHERE id IN ({','.join('?' * len(product_ids))}) AND zone IS NULL",
            product_ids).fetchall()] if product_ids else []
        if not todo:
            return
        for pid, _ in todo:
            conn.execute("UPDATE products SET zone=? WHERE id=?", (GROCERY_ZONE, pid))
        conn.commit()
        choices = _grocery_choices(conn)
        labels = [SEP.join(c["path"]) for c in choices]
        schema = {
            "type": "object",
            "properties": {"products": {"type": "array", "items": {
                "type": "object",
                "properties": {"line": {"type": "integer"}, "category": {"type": "string", "enum": labels}},
                "required": ["line", "category"], "additionalProperties": False}}},
            "required": ["products"], "additionalProperties": False,
        }
        listing = "\n".join(f"{i + 1}. {name}" for i, (_, name) in enumerate(todo))
        prompt = ("These are grocery products from store receipts. Give each numbered line the most specific "
                  "category that fits, from the list allowed (a sub-category like \"Condiments › Ketchup\" "
                  "when one fits, else its parent like \"Condiments\"). Raw meat goes under Meat; a ready-to-eat "
                  "meal or deli salad under Prepared Food; frozen food under Frozen.\n"
                  f"Return products: one entry per numbered line.\n\n{listing}")
        try:
            resp = httpx.post(f"{delegated.SERVICE_DELEGATOR_URL}/api/ask",
                              json={"task": receipts.AI_TASK, "project": "smart-home", "prompt": prompt,
                                    "json_schema": schema}, timeout=900.0)
            body = resp.json()
            if resp.status_code != 200:
                raise CategoryError(body.get("error") or f"HTTP {resp.status_code}")
            picks = {int(x["line"]): x["category"] for x in json.loads(body.get("text") or "")["products"]}
        except Exception as e:
            print(f"Picking grocery categories failed ({type(e).__name__}: {e}); "
                  f"{len(todo)} product(s) left in Grocery without a category", flush=True)
            return
        by_label = {SEP.join(c["path"]): c["id"] for c in choices}
        for i, (pid, _) in enumerate(todo):
            cid = by_label.get(picks.get(i + 1, ""))
            if cid is not None:
                conn.execute("UPDATE products SET category_id=? WHERE id=? AND category_id IS NULL", (cid, pid))
        conn.commit()
    finally:
        conn.close()


def unsorted_groceries(conn: sqlite3.Connection, receipt_id: int) -> list[int]:
    """Products on a receipt whose line reads as groceries and that aren't sorted yet."""
    return [r[0] for r in conn.execute(
        f"SELECT DISTINCT i.product_id FROM receipt_items i JOIN products p ON p.id = i.product_id "
        f"WHERE i.receipt_id=? AND p.zone IS NULL AND i.category IN ({','.join('?' * len(GROCERY_LINE_CATEGORIES))})",
        (receipt_id, *GROCERY_LINE_CATEGORIES))]


def categorize_groceries_in_background(db_path: str, product_ids: list[int]) -> None:
    if product_ids:
        threading.Thread(target=categorize_groceries, args=(db_path, list(product_ids)), daemon=True).start()
