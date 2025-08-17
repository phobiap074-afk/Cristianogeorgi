import os
import json
import re
import logging
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, render_template, request, jsonify

from psycopg_pool import ConnectionPool
from psycopg.errors import UniqueViolation

try:
	from flask_cors import CORS
	ENABLE_CORS = True
except Exception:
	ENABLE_CORS = False

def _env(name, default=""):
	v = os.getenv(name, default)
	return (v or "").strip().strip('"').strip("'")

# ---------------------------
# CONFIG — via ENV VARS
# ---------------------------

APPYFLOW_VERIFY_URL = _env("APPYFLOW_VERIFY_URL", "https://appyflow.in/api/verifyGST")
APPYFLOW_KEY_SECRET = _env("APPYFLOW_KEY_SECRET")
DATABASE_URL = _env("DATABASE_URL")  # Neon Postgres connection string (sslmode=require)

GSTIN_REGEX = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")

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
# Postgres pool + schema (gstn PRIMARY KEY)
# ---------------------------

pool = None
_schema_ready = False

def get_pool():
	global pool, _schema_ready

	if not DATABASE_URL:
		raise RuntimeError("Missing DATABASE_URL env var (Neon connection string).")

	if pool is None:
		pool = ConnectionPool(conninfo=DATABASE_URL, min_size=0, max_size=5, timeout=20)

	if not _schema_ready:
		with pool.connection() as conn:
			with conn.cursor() as cur:
				# Base table with gstn as PRIMARY KEY
				cur.execute("""
					create table if not exists submissions (
						gstn text primary key,
						legal_name text not null,
						firm_name text not null,
						name1 text not null,
						name2 text,
						contact text not null,
						created_at timestamptz not null default now()
					);
				""")
				# Migration helper: if an old 'id' column/PK exists, drop it; ensure PK on gstn
				cur.execute("""
					do $$
					begin
					  if exists (
					    select 1 from information_schema.columns
					    where table_name = 'submissions' and column_name = 'id'
					  ) then
					    begin
					      execute 'alter table submissions drop constraint if exists submissions_pkey';
					      execute 'alter table submissions drop column if exists id';
					    exception when others then null;
					    end;
					  end if;

					  if not exists (
					    select 1
					    from information_schema.table_constraints tc
					    join information_schema.key_column_usage kcu
					      on tc.constraint_name = kcu.constraint_name
					     and tc.table_name = kcu.table_name
					    where tc.table_name = 'submissions'
					      and tc.constraint_type = 'PRIMARY KEY'
					      and kcu.column_name = 'gstn'
					  ) then
					    begin
					      execute 'alter table submissions add primary key (gstn)';
					    exception when others then null;
					    end;
					  end if;
					end$$;
				""")
		_schema_ready = True

	return pool

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
	try:
		with get_pool().connection() as conn:
			with conn.cursor() as cur:
				cur.execute("select 1;")
		return jsonify({"status": "ok"}), 200
	except Exception as e:
		return jsonify({"status": "degraded", "db": str(e)}), 200

@app.post("/api/verify_gst")
def verify_gst():
	data = request.get_json(silent=True) or {}
	gstn = (data.get("gstn") or "").strip().upper()

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
			timeout=9
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

	try:
		with get_pool().connection() as conn:
			with conn.cursor() as cur:
				cur.execute(
					"""
					insert into submissions (gstn, legal_name, firm_name, name1, name2, contact, created_at)
					values (%s, %s, %s, %s, %s, %s, now())
					returning gstn;
					""",
					(gstn, legal_name, firm_name, name1, name2, contact),
				)
				inserted_gstn = cur.fetchone()[0]
	except UniqueViolation:
		return jsonify({"ok": False, "message": "This GSTIN already exists.", "code": "duplicate"}), 409
	except Exception as e:
		log.exception("DB insert failed")
		return jsonify({"ok": False, "message": f"DB insert failed: {e}"}), 500

	return jsonify({"ok": True, "id": inserted_gstn})

if __name__ == "__main__":
	debug = _env("FLASK_DEBUG", "0") == "1"
	port = int(_env("PORT", "5001"))
	app.run(host="0.0.0.0", port=port, debug=debug)
