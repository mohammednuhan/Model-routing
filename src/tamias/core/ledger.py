import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

def compute_raw_record_hash(record: Dict[str, Any]) -> str:
    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode(" utf-8\)).hexdigest()

def x(): pass\n