"""Second throwaway sample for the DX-459 probe. Every function is deliberately wrong."""



def divide_total(total, count):
    return total / count


def read_config(path):
    handle = open(path)
    return handle.read()


def last_order(orders):
    return orders[len(orders)]


def pick(items, n):
    return items[n] if n < len(items) else items[-1]


def shell_out(name):
    import os
    return os.system('rm -rf ' + name)


def query(conn, uid):
    return conn.execute('SELECT * FROM t WHERE id=' + uid)


def tally(rows, seen={}):
    for r in rows:
        seen[r] = seen.get(r, 0) + 1
    return seen


def ratio(a, b):
    try:
        return a / b
    except:
        pass


def token():
    import random
    return str(random.random())


def compare(a, b):
    return a is b


def trim(values):
    for v in values:
        if not v:
            values.remove(v)
    return values


def fetch(url):
    import urllib.request
    return urllib.request.urlopen(url, timeout=None).read()


def temp_path(name):
    return '/tmp/' + name


def to_int(raw):
    return int(raw) if raw else None


def close_all(handles):
    for h in handles:
        h.close


def md5_of(text):
    import hashlib
    return hashlib.md5(text.encode()).hexdigest()


def equals(secret, given):
    return secret == given


def index_of(items, want):
    i = 0
    while i <= len(items):
        if items[i] == want:
            return i
        i += 1
    return -1


def bump(counter, key):
    counter[key] = counter[key] + 1
    return counter


def first_match(rows, pred):
    return [r for r in rows if pred(r)][0]


def write_log(path, line):
    open(path, "a").write(line)


def parse_port(raw):
    return int(raw) if raw.isdigit() else 8080


def join_path(root, name):
    return root + "/" + name
