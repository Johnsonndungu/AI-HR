import os
import uuid
import json
import threading
import time
import logging
import requests
from datetime import timedelta
from flask import Flask, request, jsonify, session, render_template
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
import pdfplumber
from docx import Document

# ==================================================
# App Configuration
# ==================================================
app = Flask(__name__)
app.secret_key = "fixed_secret_key_for_testing"
app.permanent_session_lifetime = timedelta(days=7)

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ==================================================
# Logging
# ==================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# ==================================================
# Ollama Configuration
# ==================================================
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "tinyllama"

# ==================================================
# Mock Users
# ==================================================
mock_users = {
    "test@ke.com": {
        "id": 1,
        "password_hash": generate_password_hash("123456"),
        "plan": "trial",
        "usage_count": 0
    }
}

# ==================================================
# Plans
# ==================================================
PLANS = {
    "trial": {"price": 0, "limit": 3},
    "starter": {"price": 30, "limit": 15},
    "pro": {"price": 100, "limit": 50},
    "premium": {"price": 300, "limit": 150},
}

# ==================================================
# Progress tracking (in-memory)
# ==================================================
progress_store = {}  # job_id: {"progress": 0-100, "message": "", "result": {...}}

# ==================================================
# Authentication Middleware
# ==================================================
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper

# ==================================================
# File Text Extraction
# ==================================================
def extract_text_from_pdf(path):
    try:
        text = ""
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text += page.extract_text() or ""
        return text.strip()
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        return ""

def extract_text_from_docx(path):
    try:
        doc = Document(path)
        return "\n".join(p.text for p in doc.paragraphs).strip()
    except Exception as e:
        logger.error(f"DOCX extraction failed: {e}")
        return ""

def extract_text(path):
    if path.endswith(".pdf"):
        return extract_text_from_pdf(path)
    if path.endswith(".docx"):
        return extract_text_from_docx(path)
    return ""

# ==================================================
# Robust Prompt Builder
# ==================================================
def build_evaluation_prompt(job_text, cv_text):
    return f"""
You are a senior HR professional with over 20 years of experience.

TASK:
Evaluate how well the candidate fits the job.

STRICT RULES:
- Respond ONLY with valid JSON
- No markdown

JOB DESCRIPTION:
{job_text[:1200]}

CANDIDATE CV:
{cv_text[:1200]}

OUTPUT FORMAT:
{{
  "applicant_name": "<Full Name>",
  "contact": "<Email or Phone>",
  "score": <integer 0-100>,
  "explanation": "<short explanation>",
  "matched_skills": ["skill1", "skill2"]
}}
"""

# ==================================================
# Ollama JSON Request
# ==================================================
def ollama_json_request(prompt, timeout=120):
    payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
        response.raise_for_status()
        raw = response.json().get("response", "").strip()
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Ollama request failed: {e}")
        return None

# ==================================================
# Background Task
# ==================================================
def screen_cv_job(job_id, job_text, cv_path, filename):
    try:
        progress_store[job_id] = {"progress": 5, "message": "Reading CV", "result": None}
        cv_text = extract_text(cv_path)

        progress_store[job_id]["progress"] = 30
        progress_store[job_id]["message"] = "Analyzing with AI"

        prompt = build_evaluation_prompt(job_text, cv_text)
        result = ollama_json_request(prompt)

        if not result:
            result = {
                "applicant_name": filename,
                "contact": "N/A",
                "score": 0,
                "explanation": "LLM failed",
                "matched_skills": []
            }

        progress_store[job_id]["progress"] = 100
        progress_store[job_id]["message"] = "Completed"
        progress_store[job_id]["result"] = result

    except Exception as e:
        progress_store[job_id]["progress"] = 100
        progress_store[job_id]["message"] = "Error"
        progress_store[job_id]["result"] = {
            "applicant_name": filename,
            "contact": "N/A",
            "score": 0,
            "explanation": str(e),
            "matched_skills": []
        }

# ==================================================
# Routes
# ==================================================
@app.route("/")
def index():
    return render_template("login.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        data = request.json
        email = data.get("email")
        password = data.get("password")
        if email not in mock_users or not check_password_hash(mock_users[email]["password_hash"], password):
            return jsonify({"error": "Invalid credentials"}), 401
        session.permanent = True
        session["user_id"] = mock_users[email]["id"]
        return jsonify({"message": "Login successful"})
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return jsonify({"message": "Logged out"})

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")

@app.route("/screen", methods=["POST"])
@login_required
def screen_candidates():
    user = next((u for u in mock_users.values() if u["id"] == session["user_id"]), None)
    if user["usage_count"] >= PLANS[user["plan"]]["limit"]:
        return jsonify({"error": "Usage limit reached"}), 403

    job_text = request.form.get("job_text", "")
    job_file = request.files.get("job_file")
    if job_file:
        path = os.path.join(UPLOAD_FOLDER, secure_filename(job_file.filename))
        job_file.save(path)
        job_text = extract_text(path)

    cvs = request.files.getlist("cvs")
    job_ids = []

    for cv in cvs:
        cv_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_{secure_filename(cv.filename)}")
        cv.save(cv_path)
        job_id = str(uuid.uuid4())
        job_ids.append(job_id)

        # Start thread for this CV
        t = threading.Thread(target=screen_cv_job, args=(job_id, job_text, cv_path, cv.filename))
        t.start()

    user["usage_count"] += 1
    return jsonify({"message": "Screening started", "job_ids": job_ids})

@app.route("/job/<job_id>/progress")
@login_required
def job_progress(job_id):
    job = progress_store.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)

# ==================================================
# Run
# ==================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
