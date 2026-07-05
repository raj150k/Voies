import os
import uuid
import tempfile
import subprocess
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()
app.permanent_session_lifetime = timedelta(hours=24)

# ===== কনফিগারেশন =====
RVC_DIR = os.path.expanduser("~/Retrieval-based-Voice-Conversion-WebUI")
FEMALE_MODEL = "japanese_anime_girl"
F0_METHOD = "rmvpe"
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
# =======================

DB_PATH = os.path.join(app.instance_path, "users.db")

def get_db():
    """SQLite ডাটাবেস কানেকশন"""
    os.makedirs(app.instance_path, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """ডাটাবেস ও টেবিল তৈরি"""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            email TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP,
            is_admin INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversion_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filename TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    # ডিফল্ট অ্যাডমিন তৈরি (পাসওয়ার্ড পরিবর্তন করো!)
    admin_pass = generate_password_hash("admin123")
    try:
        conn.execute("INSERT INTO users (username, password, is_admin) VALUES (?, ?, 1)", ("admin", admin_pass))
        conn.commit()
    except:
        pass  # ইতিমধ্যে আছে
    conn.close()

# ===== লগইন ডেকোরেটর =====
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("প্লিজ লগইন করুন!", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            flash("অ্যাডমিন ছাড়া প্রবেশযোগ্য নয়!", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated

# ===== রাউটসমূহ =====

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        email = request.form.get("email", "")
        
        if not username or not password:
            flash("ইউজারনেম ও পাসওয়ার্ড দরকার!", "danger")
            return render_template("register.html")
        
        hashed = generate_password_hash(password)
        conn = get_db()
        try:
            conn.execute("INSERT INTO users (username, password, email) VALUES (?, ?, ?)",
                        (username, hashed, email))
            conn.commit()
            flash("রেজিস্ট্রেশন সফল! এখন লগইন করুন।", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("ইউজারনেম ইতিমধ্যে নেওয়া!", "danger")
        finally:
            conn.close()
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        
        if user and check_password_hash(user["password"], password):
            session.permanent = True
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["is_admin"] = user["is_admin"]
            
            # লাস্ট লগিন আপডেট
            conn = get_db()
            conn.execute("UPDATE users SET last_login = ? WHERE id = ?",
                        (datetime.now().isoformat(), user["id"]))
            conn.commit()
            conn.close()
            
            flash(f"স্বাগতম {username}! 👋", "success")
            return redirect(url_for("dashboard"))
        else:
            flash("ভুল ইউজারনেম বা পাসওয়ার্ড!", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("লগআউট সফল!", "info")
    return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    history = conn.execute(
        "SELECT * FROM conversion_history WHERE user_id = ? ORDER BY created_at DESC LIMIT 10",
        (session["user_id"],)
    ).fetchall()
    conn.close()
    return render_template("dashboard.html", history=history)

@app.route("/convert", methods=["GET", "POST"])
@login_required
def convert():
    if request.method == "POST":
        if "audio" not in request.files:
            flash("অডিও ফাইল পাঠাও!", "danger")
            return redirect(url_for("convert"))
        
        audio_file = request.files["audio"]
        if audio_file.filename == "":
            flash("ফাইল সিলেক্ট করো!", "warning")
            return redirect(url_for("convert"))
        
        # ফাইল সেভ
        ext = audio_file.filename.rsplit(".", 1)[1].lower() if "." in audio_file.filename else "wav"
        unique_name = f"{uuid.uuid4().hex}.{ext}"
        input_path = os.path.join(UPLOAD_FOLDER, unique_name)
        audio_file.save(input_path)
        
        # WAV কনভার্ট (যদি OGG/MP3 হয়)
        wav_path = input_path.rsplit(".", 1)[0] + ".wav"
        if ext != "wav":
            subprocess.run(["ffmpeg", "-i", input_path, "-ar", "44100", "-ac", "1", wav_path, "-y"],
                          capture_output=True)
            os.unlink(input_path)
        else:
            wav_path = input_path
        
        output_path = wav_path.replace(".wav", "_converted.wav")
        
        try:
            # RVC Inference
            cmd = [
                "python", f"{RVC_DIR}/infer.py",
                "--model", FEMALE_MODEL,
                "--f0method", F0_METHOD,
                "--input", wav_path,
                "--output", output_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            
            if result.returncode != 0:
                flash(f"RVC Error: {result.stderr[:200]}", "danger")
                os.unlink(wav_path)
                return redirect(url_for("convert"))
            
            # হিস্টোরিতে সেভ
            conn = get_db()
            conn.execute("INSERT INTO conversion_history (user_id, filename) VALUES (?, ?)",
                        (session["user_id"], os.path.basename(output_path)))
            conn.commit()
            conn.close()
            
            return send_file(output_path, mimetype="audio/wav", as_attachment=True,
                            download_name="converted_voice.wav")
            
        except Exception as e:
            flash(f"Error: {str(e)}", "danger")
        finally:
            if os.path.exists(wav_path):
                os.unlink(wav_path)
            # আউটপুট ফাইল ডাউনলোডের পর ডিলিট হবে না (send_file এর জন্য রাখা)
    
    return render_template("convert.html")

@app.route("/admin")
@login_required
@admin_required
def admin_panel():
    conn = get_db()
    users = conn.execute("SELECT id, username, email, created_at, last_login, is_admin FROM users").fetchall()
    total_conversions = conn.execute("SELECT COUNT(*) as count FROM conversion_history").fetchone()["count"]
    conn.close()
    return render_template("admin.html", users=users, total_conversions=total_conversions)

# API: RVC সার্ভার হেলথ চেক
@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "session_active": bool(session.get("user_id"))})

if __name__ == "__main__":
    init_db()
    print("=" * 50)
    print("  🎤 GirlicBot Web Server চালু হচ্ছে...")
    print(f"  🌐 http://127.0.0.1:8080")
    print(f"  👤 ডিফল্ট অ্যাডমিন: admin / admin123")
    print("=" * 50)
    app.run(host="0.0.0.0", port=8080, debug=True)
