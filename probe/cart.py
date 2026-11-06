"""Throwaway sample for the DX-459 end-to-end probe. Deliberately flawed."""
import json


def load_cart(path):
    f = open(path)
    return json.loads(f.read())


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


def checkout(path, percent, tax_rate):
    cart = load_cart(path)
    subtotal = apply_discount(cart["items"], percent)
    tax = subtotal * tax_rate
    return {"subtotal": subtotal, "tax": tax, "total": subtotal + tax}
