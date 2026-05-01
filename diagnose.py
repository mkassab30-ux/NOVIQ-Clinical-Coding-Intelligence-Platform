"""
NOVIQ Diagnostic Script
========================
Run this to check Railway deployment environment.

Usage:
  python diagnose.py
"""
import json
from pathlib import Path
import sys

print("=" * 60)
print("NOVIQ Engine Diagnostic Report")
print("=" * 60)

BASE_DIR = Path(__file__).parent
print(f"\n[1] BASE_DIR: {BASE_DIR}")
print(f"    Absolute: {BASE_DIR.absolute()}")

# Check engine/
engine_dir = BASE_DIR / "engine"
print(f"\n[2] Engine directory: {engine_dir}")
print(f"    Exists: {engine_dir.exists()}")
if engine_dir.exists():
    print(f"    Files: {list(engine_dir.iterdir())}")
    noviq_engine = engine_dir / "noviq_engine.py"
    print(f"    noviq_engine.py: {noviq_engine.exists()}")

# Check knowledge_base/
kb_dir = BASE_DIR / "knowledge_base"
print(f"\n[3] Knowledge Base directory: {kb_dir}")
print(f"    Exists: {kb_dir.exists()}")
if kb_dir.exists():
    files = list(kb_dir.iterdir())
    print(f"    Total files: {len(files)}")
    for f in files:
        size_kb = f.stat().st_size / 1024
        print(f"      - {f.name} ({size_kb:.1f} KB)")

# Check specific KB files
print(f"\n[4] Critical KB Files:")
critical_files = [
    "ar_drg_kb_seed_v11_new_adrgs.json",
    "dcl_exclusions.json",
    "keyword_dictionary_medical_logic_v3.json",
    "keyword_dictionary_medical_logic_v4.json",
]
for fname in critical_files:
    fpath = kb_dir / fname
    exists = fpath.exists() if kb_dir.exists() else False
    print(f"    {fname}: {'✓' if exists else '✗'}")
    if exists:
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            if "procedure_index" in data:
                print(f"      → {len(data['procedure_index'])} procedures")
            if "_meta" in data:
                print(f"      → version: {data['_meta'].get('version', 'unknown')}")
        except Exception as e:
            print(f"      → Error reading: {e}")

# Check data/
data_dir = BASE_DIR / "data"
print(f"\n[5] Data directory: {data_dir}")
print(f"    Exists: {data_dir.exists()}")
if data_dir.exists():
    print(f"    Contents: {list(data_dir.iterdir())}")

# Check dashboard
dashboard_files = ["noviq_dashboard.html", "noviq_dashboard_v2.html"]
print(f"\n[6] Dashboard files:")
for fname in dashboard_files:
    fpath = BASE_DIR / fname
    print(f"    {fname}: {'✓' if fpath.exists() else '✗'}")

# Try importing engine modules
print(f"\n[7] Module import test:")
sys.path.insert(0, str(engine_dir if engine_dir.exists() else BASE_DIR))
sys.path.insert(0, str(BASE_DIR))

try:
    from noviq_engine import NOVIQEngine
    print(f"    noviq_engine: ✓")
except Exception as e:
    print(f"    noviq_engine: ✗ ({e})")

try:
    from models import CodingSuggestion
    print(f"    models: ✓")
except Exception as e:
    print(f"    models: ✗ ({e})")

try:
    from validation_rules import KnowledgeBaseIncompleteError
    print(f"    validation_rules: ✓")
except Exception as e:
    print(f"    validation_rules: ✗ ({e})")

# Environment variables
print(f"\n[8] Environment variables:")
import os
anthropic_key = os.getenv("ANTHROPIC_API_KEY")
print(f"    ANTHROPIC_API_KEY: {'set (***' + anthropic_key[-4:] + ')' if anthropic_key else 'not set'}")
print(f"    PORT: {os.getenv('PORT', 'not set')}")
print(f"    RAILWAY_ENVIRONMENT: {os.getenv('RAILWAY_ENVIRONMENT', 'not set')}")

print("\n" + "=" * 60)
print("Diagnostic complete.")
print("=" * 60)
