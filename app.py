import os
import json
import re
from datetime import datetime
import certifi

import requests
from flask import Flask, render_template, request, jsonify
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

# ---------------------------
# CONFIG — EDIT THESE VALUES
# ---------------------------

# AppyFlow GST API
APPYFLOW_VERIFY_URL = "https://appyflow.in/api/verifyGST"
APPYFLOW_KEY_SECRET = "tkdyaguSv6X4CHE2zRMd0piW3FV2"

# MongoDB Atlas (Cluster) connection
DEFAULT_MONGODB_URI = (
    "mongodb+srv://phobiap074:DeZZWSEArHb0ebuU@cluster0.djoizja.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
)
MONGODB_URI = os.getenv("MONGODB_URI", DEFAULT_MONGODB_URI)

# MongoDB DB/Collection names
MONGO_DB_NAME = "gst_app"
MONGO_COLLECTION = "submissions"

# GSTIN basic format (15 chars: 2 digits, 5 letters, 4 digits, 1 letter, 1 alnum, 'Z', 1 alnum)
GSTIN_REGEX = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")

# ---------------------------
# APP + DB CLIENT
# ---------------------------
app = Flask(__name__)

try:
    mongo_client = MongoClient(MONGODB_URI)
    mongo_db = mongo_client[MONGO_DB_NAME]
    submissions = mongo_db[MONGO_COLLECTION]
    # Enforce uniqueness on GSTN
    submissions.create_index([("gstn", 1)], unique=True)
except Exception as e:
    raise RuntimeError(f"Failed to connect to MongoDB. Check MONGODB_URI. Details: {e}")

# ---------------------------
# ROUTES
# ---------------------------

@app.route("/", methods=["GET"])
def home():
    return render_template("index.html")

@app.route("/api/verify_gst", methods=["POST"])
def verify_gst():
    """
    Expects JSON: { "gstn": "15-char GSTIN" }
    Validates locally and calls AppyFlow -> returns legal_name (lgnm) and firm_name (tradeNam)
    """
    data = request.get_json(silent=True) or {}
    gstn = (data.get("gstn") or "").strip().upper()

    # strict server-side checks
    if not gstn:
        return jsonify({"ok": False, "message": "GSTIN is required."}), 400
    if len(gstn) != 15:
        return jsonify({"ok": False, "message": "GSTIN must be exactly 15 characters."}), 400
    if not GSTIN_REGEX.match(gstn):
        return jsonify({"ok": False, "message": "GSTIN format looks invalid."}), 400

    try:
        resp = requests.get(
            APPYFLOW_VERIFY_URL,
            params={"gstNo": gstn, "key_secret": APPYFLOW_KEY_SECRET},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
    except requests.RequestException as e:
        return jsonify({"ok": False, "message": f"Error contacting AppyFlow: {e}"}), 502

    try:
        payload = resp.json()
    except json.JSONDecodeError:
        return jsonify({"ok": False, "message": "Invalid JSON from AppyFlow."}), 502

    if payload.get("error") is True:
        # keep name fields empty on client, block submit
        return jsonify({"ok": False, "message": payload.get("message", "GST verification failed.")}), 400

    info = (payload or {}).get("taxpayerInfo") or {}
    legal_name = (info.get("lgnm") or "").strip()
    firm_name  = (info.get("tradeNam") or "").strip()

    if not legal_name and not firm_name:
        return jsonify({"ok": False, "message": "Could not find Legal Name / Firm Name for this GSTIN."}), 404

    return jsonify({"ok": True, "gstn": gstn, "legal_name": legal_name, "firm_name": firm_name})

@app.route("/submit", methods=["POST"])
def submit():
    """
    Expects JSON:
    {
      "gstn": "...",               # must be verified; stored uppercase
      "legal_name": "...",         # read-only from verify
      "firm_name": "...",          # read-only from verify
      "name1": "...",
      "name2": "...",
      "contact": "..."
    }
    Stores to MongoDB with unique gstn.
    """
    data = request.get_json(silent=True) or {}

    gstn = (data.get("gstn") or "").strip().upper()
    legal_name = (data.get("legal_name") or "").strip()
    firm_name  = (data.get("firm_name") or "").strip()
    name1      = (data.get("name1") or "").strip()
    name2      = (data.get("name2") or "").strip()
    contact    = (data.get("contact") or "").strip()

    # Guardrails: must have verified GSTIN and auto-filled names
    if not gstn or not legal_name or not firm_name or not name1 or not contact:
        return jsonify({"ok": False, "message": "Please verify GSTIN and fill Name 1 & Contact."}), 400
    if len(gstn) != 15 or not GSTIN_REGEX.match(gstn):
        return jsonify({"ok": False, "message": "GSTIN format looks invalid."}), 400

    doc = {
        "gstn": gstn,
        "legal_name": legal_name,
        "firm_name": firm_name,
        "name1": name1,
        "name2": name2,
        "contact": contact,
        "created_at": datetime.utcnow(),
    }

    try:
        result = submissions.insert_one(doc)
    except DuplicateKeyError:
        return jsonify({"ok": False, "message": "This GSTIN already exists.", "code": "duplicate"}), 409
    except Exception as e:
        return jsonify({"ok": False, "message": f"DB insert failed: {e}"}), 500

    return jsonify({"ok": True, "id": str(result.inserted_id)})
    
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5001)), debug=True)

