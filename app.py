import os
import json
import re
import logging
from datetime import datetime
from pathlib import Path
import certifi

import requests
from flask import Flask, render_template, request, jsonify
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

# Optional: CORS if you serve the frontend from a different origin
try:
    from flask_cors import CORS
    ENABLE_CORS = True
except Exception:
    ENABLE_CORS = False

# ---------------------------
# ENV HELPERS
# ---------------------------

def _env(name, default=""):
    v = os.getenv(name, default)
    return (v or "").strip().strip('"').strip("'")

# ---------------------------
# CONFIG — via ENV VARS
# ---------------------------

APPYFLOW_VERIFY_URL = _env("APPYFLOW_VERIFY_URL", "https://appyflow.in/api/verifyGST")
APPYFLOW_KEY_SECRET = _env("APPYFLOW_KEY_SECRET")
MONGODB_URI = _env("MONGODB_URI")
MONGO_DB_NAME = _env("MONGO_DB_NAME", "gst_app")
MONGO_COLLECTION = _env("MONGO_COLLECTION", "submissions")

# GSTIN basic format (15 chars: 2 digits, 5 letters, 4 digits, 1 letter, 1 alnum, 'Z', 1 alnum)
GSTIN_REGEX = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")

# Logging
logging.basicConfig(level=_env("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

# ---------------------------
# APP
# ---------------------------

BASE_DIR = Path(__file__).resolve().parent
app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)

if ENABLE_CORS and _env("ENABLE_CORS", "0") == "1":
    origins = [o.strip() for o in _env("API_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    CORS(app, resources={r"/api/*": {"origins": origins or "*"}})

# ---------------------------
# LAZY MONGO CLIENT
# ---------------------------

mongo_client = None
submissions = None

def get_submissions_collection():
    global mongo_client, submissions

    if submissions is not None:
        return submissions

    if not MONGODB_URI:
        raise RuntimeError("Missing MONGODB_URI env var.")

    try:
        mongo_client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=20000,
            connectTimeoutMS=20000,
            socketTimeoutMS=20000,
            tls=True,
        )
        mongo_client.admin.command("ping")
        db = mongo_client[MONGO_DB_NAME]
        submissions = db[MONGO_COLLECTION]
        submissions.create_index([("gstn", 1)], unique=True)
        log.info("MongoDB ready.")
        return submissions
    except Exception as e:
        log.exception("Mongo init failed")
        raise RuntimeError(f"Failed to connect to MongoDB. Details: {e}")

# ---------------------------
# ROUTES
# ---------------------------

@app.get("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200

@app.post("/api/verify_gst")
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

    if not APPYFLOW_KEY_SECRET:
        return jsonify({"ok": False, "message": "Server missing APPYFLOW_KEY_SECRET."}), 500

    try:
        resp = requests.get(
            APPYFLOW_VERIFY_URL,
            params={"gstNo": gstn, "key_secret": APPYFLOW_KEY_SECRET},
            timeout=9  # fit within Vercel free 10s function limit
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.exception("AppyFlow request failed")
        return jsonify({"ok": False, "message": f"Error contacting AppyFlow: {e}"}), 502

    try:
        payload = resp.json()
    except json.JSONDecodeError:
        return jsonify({"ok": False, "message": "Invalid JSON from AppyFlow."}), 502

    if payload.get("error") is True:
        return jsonify({"ok": False, "message": payload.get("message", "GST verification failed.")}), 400

    info = (payload or {}).get("taxpayerInfo") or {}
    legal_name = (info.get("lgnm") or "").strip()
    firm_name  = (info.get("tradeNam") or "").strip()

    if not legal_name and not firm_name:
        return jsonify({"ok": False, "message": "Could not find Legal Name / Firm Name for this GSTIN."}), 404

    return jsonify({"ok": True, "gstn": gstn, "legal_name": legal_name, "firm_name": firm_name})

@app.post("/submit")
def submit():
    """
    Expects JSON:
    {
      "gstn": "...",
      "legal_name": "...",
      "firm_name": "...",
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
        col = get_submissions_collection()
        result = col.insert_one(doc)
    except DuplicateKeyError:
        return jsonify({"ok": False, "message": "This GSTIN already exists.", "code": "duplicate"}), 409
    except Exception as e:
        log.exception("DB insert failed")
        return jsonify({"ok": False, "message": f"DB insert failed: {e}"}), 500

    return jsonify({"ok": True, "id": str(result.inserted_id)})

# Local dev only
if __name__ == "__main__":
    debug = _env("FLASK_DEBUG", "0") == "1"
    port = int(_env("PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=debug)


