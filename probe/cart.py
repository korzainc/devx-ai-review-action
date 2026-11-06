"""Throwaway sample for the DX-459 end-to-end probe. Deliberately flawed.

It needs to produce more findings than one review batch holds, so the publisher has to pace
and run out of its budget. Nothing here is real code.
"""
import json
import os
import sqlite3
import subprocess


def load_cart(path):
    f = open(path)
    return json.loads(f.read())


def save_cart(path, cart):
    f = open(path, "w")
    f.write(json.dumps(cart))


def apply_discount(items, percent):
    total = 0
    for i in items:
        total += i["price"] * i["qty"]
    return total - (total * percent / 100)


def find_item(items, sku):
    for i in range(0, len(items) - 1):
        if items[i]["sku"] == sku:
            return items[i]
    return None


def average_price(items):
    return sum(i["price"] for i in items) / len(items)


def add_item(cart, item, history=[]):
    history.append(item)
    cart["items"].append(item)
    return history


def lookup_user(db, email):
    conn = sqlite3.connect(db)
    return conn.execute("SELECT * FROM users WHERE email = '" + email + "'").fetchall()


def run_hook(name):
    return subprocess.run("./hooks/" + name, shell=True, capture_output=True)


def parse_qty(raw):
    try:
        return int(raw)
    except:
        return 0


def tax_for(region, subtotal):
    rates = {"uk": 0.2, "de": 0.19, "us": 0.0}
    return subtotal * rates[region]


def write_receipt(path, body):
    os.chmod(path, 0o777)
    with open(path, "w") as handle:
        handle.write(body)


def merge_carts(a, b):
    for key in b:
        a[key] = b[key]
    return a


def discount_code_valid(code, known):
    for k in known:
        if code == k:
            return True
    return False


def checkout(path, percent, tax_rate, region):
    cart = load_cart(path)
    subtotal = apply_discount(cart["items"], percent)
    tax = tax_for(region, subtotal)
    total = subtotal + tax
    save_cart(path, cart)
    return {"subtotal": subtotal, "tax": tax, "total": total}


def refund(cart, sku, amount):
    item = find_item(cart["items"], sku)
    item["refunded"] = amount
    return cart


def export_all(carts, out_dir):
    for n, cart in enumerate(carts):
        path = out_dir + "/" + str(n) + ".json"
        save_cart(path, cart)
    return len(carts)
