"""
inspect_hive_data.py
---------------------
READ-ONLY inspection script for Day 2, Phase 1.
Writes nothing. Does not touch blockchain/app/hashing code.

Reuses the SAME Firebase connection already initialized in firebase.py
(no second authentication system, no second service-account load).
"""

import firebase  # importing this runs firebase_admin.initialize_app() once
from firebase_admin import db


def inspect_hive_log(limit=5):
    ref = db.reference("/hive_log")
    all_records = ref.get()
    if not all_records:
        print("/hive_log is empty or unreachable")
        return
    print(f"Total /hive_log records found: {len(all_records)}")
    last_keys = sorted(all_records.keys())[-limit:]
    print(f"\nMost recent {len(last_keys)} records (raw, unmodified):")
    for k in last_keys:
        print(f"  {k}: {all_records[k]}")


def inspect_node(path):
    ref = db.reference(path)
    data = ref.get()
    print(f"\n{path} contents:")
    print(data)


if __name__ == "__main__":
    inspect_hive_log(limit=5)
    inspect_node("/hive_bee_count")
    inspect_node("/hive_status")