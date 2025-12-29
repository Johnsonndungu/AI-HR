from flask import Flask, render_template, request, redirect, url_for, flash, send_file
import os, zipfile, requests
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = "dev-secret"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(BASE_DIR, "uploads")
JD_DIR = os.path.join(UPLOADS, "job_descriptions")
CV_DIR = os.path.join(UPLOADS, "cvs")
RESULTS_DIR = os.path.join(BASE_DIR, "results")

for d in [JD_DIR, CV_DIR, RESULTS_DIR]:
    os.makedirs(d, exist_ok=True)

ALLOWED = {"pdf", "doc", "docx", "txt"}
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"


def allowed_file(name):
    return "." in name and name.rsplit(".", 1)[1].lower() in ALLOWED


def call_tinyllama(job_desc, cv_text):
    prompt = f"""
Job Description:
{job_desc}

Candidate CV:
{cv_text}

Give ONLY a number from 0 to 100 indicating match percentage.
"""
    res = requests.post(OLLAMA_URL, json={
        "model": "tinyllama",
        "prompt": prompt,
        "stream": False
    }, timeout=120)

    try:
        return int("".join(filter(str.isdigit, res.json()["response"])))
    except:
        return 0


@app.route("/", methods=["GET", "POST"])
def dashboard():
    if request.method == "POST":
        job_text = request.form.get("job_text")
        job_file = request.files.get("job_file")
        cvs = request.files.getlist("cvs")

        # Save job description
        jd_path = os.path.join(JD_DIR, "job.txt")
        if job_text and job_text.strip():
            with open(jd_path, "w", encoding="utf-8") as f:
                f.write(job_text)
        elif job_file and allowed_file(job_file.filename):
            job_file.save(jd_path)
        else:
            flash("Job description required")
            return redirect(url_for("dashboard"))

        # Save CVs
        for cv in cvs:
            if cv and allowed_file(cv.filename):
                cv.save(os.path.join(CV_DIR, secure_filename(cv.filename)))

        return redirect(url_for("results"))

    return render_template("dashboard.html")


@app.route("/results")
def results():
    with open(os.path.join(JD_DIR, "job.txt"), encoding="utf-8") as f:
        job_desc = f.read()

    scores = []
    for cv_file in os.listdir(CV_DIR):
        path = os.path.join(CV_DIR, cv_file)
        with open(path, errors="ignore") as f:
            cv_text = f.read()

        score = call_tinyllama(job_desc, cv_text)
        scores.append((cv_file, score))

    scores.sort(key=lambda x: x[1], reverse=True)

    # Zip top 5
    zip_path = os.path.join(RESULTS_DIR, "shortlisted.zip")
    with zipfile.ZipFile(zip_path, "w") as z:
        for cv, score in scores[:5]:
            z.write(os.path.join(CV_DIR, cv), cv)

    return render_template("results.html", results=scores)


@app.route("/download")
def download():
    return send_file(
        os.path.join(RESULTS_DIR, "shortlisted.zip"),
        as_attachment=True
    )


if __name__ == "__main__":
    app.run(debug=True)
