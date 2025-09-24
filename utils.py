import json
from logger import log

def load_json(file_path):
    log("DEBUG", f"Loading JSON from {file_path}")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        log("DEBUG", f"Loaded JSON from {file_path}")
        return data
    except Exception as e:
        log("ERROR", f"Failed to load JSON from {file_path}: {e}")
        return []  # Return empty list if file missing or invalid

def save_json(file_path, data):
    log("DEBUG", f"Saving JSON to {file_path}")
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log("DEBUG", f"Saved JSON to {file_path}")
    except Exception as e:
        log("ERROR", f"Failed to save JSON to {file_path}: {e}")
        raise