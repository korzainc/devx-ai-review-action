# TEMPORARY (DX-322): a deliberately flawed file so the SARIF trial has a
# finding to upload. Remove before merge.
import sqlite3
import subprocess


def user_by_name(db_path, name):
    conn = sqlite3.connect(db_path)
    return conn.execute(f"SELECT * FROM users WHERE name = '{name}'").fetchall()


def commits_for(ref):
    return subprocess.run(f"git log --oneline {ref}", shell=True, capture_output=True, text=True).stdout
