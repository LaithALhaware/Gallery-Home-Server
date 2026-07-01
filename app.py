from flask import Flask, render_template, request, redirect, url_for, send_file, session, flash, jsonify
from functools import wraps
import os
import re
import json
import sqlite3
import zipfile
from io import BytesIO
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import shutil
from urllib.parse import unquote
import subprocess
from datetime import datetime
from PIL import Image
import random
import requests


app = Flask(__name__)
app.secret_key = "102030405060708090100"

# ============================================================
# CONSTANTS
# ============================================================
USERS_ROOT     = 'static'                # each user's folders live here
DB_PATH        = 'users.db'
DEFAULT_AVATAR = 'static/default.svg'         # served from /static/

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'mp4', 'avif'}
ALLOWED_AVATAR_EXT = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
VIDEO_EXTENSIONS   = {'mp4', 'mov', 'avi', 'mkv', 'webm'}
IMAGE_EXTENSIONS   = ALLOWED_EXTENSIONS - VIDEO_EXTENSIONS
PER_PAGE           = 30
MAX_AVATAR_BYTES   = 5 * 1024 * 1024

# ============================================================
# PLANS  (label is required so templates can render it)
# ============================================================
GB = 1024 ** 3
PLANS = {
    'free': {'label': 'Free', 'price': 0.00, 'limit_bytes': 1   * GB,
             'description': 'Get started with 1 GB'},
    'plus': {'label': 'Plus', 'price': 14.99, 'limit_bytes': 25  * GB,
             'description': '25 GB for casual use'},
    'pro':  {'label': 'Pro',  'price': 29.99, 'limit_bytes': 100 * GB,
             'description': '100 GB for power users'},
    'unlimited': {'label': 'Unlimited','price': 0.00, 'limit_bytes': 0},   # 0 = no cap
}




DEFAULT_PLAN = 'free'

# ============================================================
# PAYPAL CONFIG  (set these via env vars — never hardcode in production)
# https://sandbox.paypal.com

#   export PAYPAL_ENV="sandbox"          # 'sandbox' or 'live'
#   export PAYPAL_CLIENT_ID="..."        # from PayPal dashboard
#   export PAYPAL_SECRET="..."           # from PayPal dashboard
#
# Until you've tested in sandbox, leave PAYPAL_ENV=sandbox.
# ============================================================
PAYPAL_ENV       = os.environ.get('PAYPAL_ENV', 'sandbox')
PAYPAL_CLIENT_ID = os.environ.get(
    'PAYPAL_CLIENT_ID',
    ''
)
PAYPAL_SECRET    = os.environ.get('PAYPAL_SECRET', 'YOUR_PAYPAL_SECRET_HERE')
PAYPAL_API_BASE  = ('https://api-m.paypal.com' if PAYPAL_ENV == 'sandbox'
                    else 'https://api-m.paypal.com')
PAYPAL_CURRENCY  = 'USD'
PAYPAL_PLANS_FILE = 'paypal_plans.json'

os.makedirs(USERS_ROOT, exist_ok=True)


# ============================================================
# DATABASE
# ============================================================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT UNIQUE NOT NULL,
                email           TEXT UNIQUE NOT NULL,
                password_hash   TEXT NOT NULL,
                avatar          TEXT,
                display_name    TEXT,
                plan            TEXT DEFAULT 'free',
                subscription_id TEXT,
                created_at      TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT NOT NULL,
                paypal_sub_id   TEXT UNIQUE NOT NULL,
                paypal_plan_id  TEXT NOT NULL,
                local_plan      TEXT NOT NULL,
                status          TEXT NOT NULL,
                started_at      TEXT NOT NULL,
                cancelled_at    TEXT
            )
        """)
        # Auto-migrate: add columns if missing
        cols = {row['name'] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        for col, ddl in [
            ('avatar',          "ALTER TABLE users ADD COLUMN avatar TEXT"),
            ('display_name',    "ALTER TABLE users ADD COLUMN display_name TEXT"),
            ('plan',            "ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'free'"),
            ('subscription_id', "ALTER TABLE users ADD COLUMN subscription_id TEXT"),
        ]:
            if col not in cols:
                conn.execute(ddl)
        conn.commit()


init_db()


# ============================================================
# USER FOLDER HELPERS
# ============================================================
def user_root(username):       return os.path.join(USERS_ROOT, username)
def user_uploads(username):    return os.path.join(user_root(username), 'Uploads')
def user_thumbnails(username): return os.path.join(user_root(username), 'Thumbnails')
def user_deleted(username):    return os.path.join(user_root(username), 'Delet')


def create_user_folders(username):
    for folder in ('Uploads', 'Thumbnails', 'Delet'):
        os.makedirs(os.path.join(user_root(username), folder), exist_ok=True)


def current_user():
    return session.get('user')


def get_user_record(username):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()


def get_user_plan(username):
    row = get_user_record(username)
    plan_key = (row['plan'] if row and 'plan' in row.keys() and row['plan'] else DEFAULT_PLAN)
    return PLANS.get(plan_key, PLANS[DEFAULT_PLAN]) | {'key': plan_key}


def user_context(username):
    row = get_user_record(username)
    if not row:
        return {}
    avatar = row['avatar'] if row['avatar'] else DEFAULT_AVATAR
    plan = get_user_plan(username)
    used_bytes = get_folder_size(user_root(username))
    limit = plan['limit_bytes']
    return {
        'username':        row['username'],
        'email':           row['email'],
        'display_name':    row['display_name'] or row['username'],
        'avatar':          avatar,
        'created_at':      row['created_at'],
        'plan':            plan,
        'used_bytes':      used_bytes,
        'used_human':      human_size(used_bytes),
        'limit_bytes':     limit,
        'limit_human':     'Unlimited' if limit == 0 else human_size(limit),
        'percent_used':    0 if limit == 0 else min(100, (used_bytes / limit) * 100),
        'remaining_bytes': max(0, limit - used_bytes) if limit > 0 else None,
        'subscription_id': row['subscription_id'] if 'subscription_id' in row.keys() else None,
    }


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def generate_video_thumbnail(video_path, thumb_path):
    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
    if not os.path.exists(thumb_path):
        subprocess.run([
            'ffmpeg', '-y', '-i', video_path, '-ss', '00:00:01.000', '-vframes', '1', thumb_path
        ])


def get_folder_size(path):
    total = 0
    if not os.path.exists(path):
        return 0
    for root, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            if os.path.exists(fp):
                total += os.path.getsize(fp)
    return total


def human_size(size):
    if size is None:
        return '—'
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def can_user_store(username, incoming_bytes):
    plan = get_user_plan(username)
    limit = plan['limit_bytes']
    if limit == 0:
        return True, 0
    used = get_folder_size(user_root(username))
    return (used + incoming_bytes) <= limit, used


# ============================================================
# PAYPAL SUBSCRIPTIONS
# ============================================================
def paypal_get_access_token():
    if not PAYPAL_SECRET or PAYPAL_SECRET == 'YOUR_PAYPAL_SECRET_HERE':
        raise RuntimeError("PAYPAL_SECRET not configured. Set it as an env var.")
    res = requests.post(
        f"{PAYPAL_API_BASE}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
        data={'grant_type': 'client_credentials'},
        headers={'Accept': 'application/json'},
        timeout=15,
    )
    res.raise_for_status()
    return res.json()['access_token']


def paypal_request(method, path, **kwargs):
    token = paypal_get_access_token()
    headers = kwargs.pop('headers', {})
    headers.update({
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    })
    return requests.request(method, f"{PAYPAL_API_BASE}{path}",
                            headers=headers, timeout=20, **kwargs)


def load_cached_paypal_plans():
    if not os.path.exists(PAYPAL_PLANS_FILE):
        return {}
    try:
        with open(PAYPAL_PLANS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_cached_paypal_plans(data):
    with open(PAYPAL_PLANS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def paypal_create_product():
    res = paypal_request('POST', '/v1/catalogs/products', json={
        'name': 'Gallery Storage',
        'description': 'Cloud storage for your photos and videos',
        'type': 'SERVICE',
        'category': 'SOFTWARE',
    })
    res.raise_for_status()
    return res.json()['id']


def paypal_create_plan(product_id, plan_key, plan_info):
    body = {
        'product_id': product_id,
        'name': f"Gallery {plan_info['label']} (Monthly)",
        'description': plan_info['description'],
        'status': 'ACTIVE',
        'billing_cycles': [{
            'frequency': {'interval_unit': 'MONTH', 'interval_count': 1},
            'tenure_type': 'REGULAR',
            'sequence': 1,
            'total_cycles': 0,
            'pricing_scheme': {
                'fixed_price': {
                    'value': f"{plan_info['price']:.2f}",
                    'currency_code': PAYPAL_CURRENCY,
                },
            },
        }],
        'payment_preferences': {
            'auto_bill_outstanding': True,
            'setup_fee_failure_action': 'CONTINUE',
            'payment_failure_threshold': 3,
        },
    }
    res = paypal_request('POST', '/v1/billing/plans', json=body)
    res.raise_for_status()
    return res.json()['id']


def ensure_paypal_plans():
    """Bootstrap PayPal product + plans if missing. Returns {plan_key: paypal_plan_id}."""
    cached = load_cached_paypal_plans()
    payable = {k: v for k, v in PLANS.items() if v['price'] > 0}
    if not payable:
        return {}
    if not PAYPAL_SECRET or PAYPAL_SECRET == 'YOUR_PAYPAL_SECRET_HERE':
        return cached.get('plans', {})

    product_id = cached.get('product_id')
    plans = cached.get('plans', {})
    try:
        if not product_id:
            product_id = paypal_create_product()
        for plan_key, plan_info in payable.items():
            if plan_key not in plans:
                plans[plan_key] = paypal_create_plan(product_id, plan_key, plan_info)
        save_cached_paypal_plans({'product_id': product_id, 'plans': plans})
    except Exception as e:
        app.logger.error("PayPal plan bootstrap failed: %s", e)
    return plans


def paypal_get_subscription(sub_id):
    res = paypal_request('GET', f'/v1/billing/subscriptions/{sub_id}')
    res.raise_for_status()
    return res.json()


def paypal_cancel_subscription(sub_id, reason='User requested cancellation'):
    res = paypal_request('POST', f'/v1/billing/subscriptions/{sub_id}/cancel',
                         json={'reason': reason})
    return res.status_code == 204

# ============================================================
# DEBUG ROUTE — paste into app.py before `if __name__ == '__main__':`
# Visit  http://localhost:5002/debug/paypal  while logged in.
# REMOVE BEFORE PRODUCTION.
# ============================================================
@app.route('/debug/paypal')
@login_required
def debug_paypal():
    out = []
    out.append(f"PAYPAL_ENV       = {PAYPAL_ENV}")
    out.append(f"PAYPAL_API_BASE  = {PAYPAL_API_BASE}")
    out.append(f"PAYPAL_CLIENT_ID = {PAYPAL_CLIENT_ID[:12]}…  (length {len(PAYPAL_CLIENT_ID)})")
    out.append(f"PAYPAL_SECRET    = "
               + ("NOT SET" if PAYPAL_SECRET in (None, '', 'YOUR_PAYPAL_SECRET_HERE')
                  else f"{PAYPAL_SECRET[:6]}…  (length {len(PAYPAL_SECRET)})"))
    out.append("")

    try:
        token = paypal_get_access_token()
        out.append(f"✓ Access token OK ({token[:20]}…)")
    except Exception as e:
        out.append(f"✗ Access token failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
            out.append(f"   Response body: {e.response.text}")
        return "<pre>" + "\n".join(out) + "</pre>"

    out.append("")
    out.append("Attempting to bootstrap plans…")
    try:
        plans = ensure_paypal_plans()
        out.append(f"Plans returned: {plans}" if plans else "✗ Empty plans dict")
    except Exception as e:
        out.append(f"✗ Bootstrap failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
            out.append(f"   Response body: {e.response.text}")

    out.append("")
    out.append(f"Cached file: {load_cached_paypal_plans() or 'empty / missing'}")
    return "<pre style='padding:20px;font-family:monospace;'>" + "\n".join(out) + "</pre>"
# ============================================================
# AUTH
# ============================================================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        row = get_user_record(username)
        if row and check_password_hash(row['password_hash'], password):
            session['user'] = row['username']
            create_user_folders(row['username'])
            return redirect(url_for('index'))
        flash("Invalid username or password")
    return render_template('Login.html')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        email    = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        confirm  = request.form.get('confirm') or ''
        agree    = request.form.get('agree')

        if not username or not email or not password:
            flash("All fields are required"); return render_template('Signup.html')
        if not re.match(r'^[a-zA-Z0-9_]{3,20}$', username):
            flash("Username must be 3–20 characters: letters, numbers, underscores only")
            return render_template('Signup.html')
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            flash("Please enter a valid email address"); return render_template('Signup.html')
        if len(password) < 8:
            flash("Password must be at least 8 characters"); return render_template('Signup.html')
        if password != confirm:
            flash("Passwords don't match"); return render_template('Signup.html')
        if not agree:
            flash("You must agree to the Terms of Service and Privacy Policy")
            return render_template('Signup.html')

        try:
            with get_db() as conn:
                existing = conn.execute(
                    "SELECT username, email FROM users WHERE username = ? OR email = ?",
                    (username, email)
                ).fetchone()
                if existing:
                    if existing['username'] == username:
                        flash("That username is already taken")
                    else:
                        flash("An account with that email already exists")
                    return render_template('Signup.html')

                conn.execute(
                    """INSERT INTO users (username, email, password_hash, display_name, plan, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (username, email, generate_password_hash(password),
                     username, DEFAULT_PLAN,
                     datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
                )
                conn.commit()
        except sqlite3.IntegrityError:
            flash("Username or email already in use")
            return render_template('Signup.html')

        create_user_folders(username)
        session['user'] = username
        return redirect(url_for('index'))

    return render_template('Signup.html')


@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))


# ============================================================
# SETTINGS
# ============================================================
@app.route('/settings', methods=['GET'])
@login_required
def settings():
    return render_template('Settings.html', plans=PLANS, **user_context(current_user()))


@app.route('/settings/profile', methods=['POST'])
@login_required
def update_profile():
    user = current_user()
    display_name = (request.form.get('display_name') or '').strip()
    email = (request.form.get('email') or '').strip().lower()

    if not display_name or len(display_name) > 50:
        flash("Display name must be 1–50 characters"); return redirect(url_for('settings'))
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        flash("Please enter a valid email address"); return redirect(url_for('settings'))

    with get_db() as conn:
        existing = conn.execute(
            "SELECT username FROM users WHERE email = ? AND username != ?",
            (email, user)
        ).fetchone()
        if existing:
            flash("That email is already in use by another account")
            return redirect(url_for('settings'))
        conn.execute(
            "UPDATE users SET display_name = ?, email = ? WHERE username = ?",
            (display_name, email, user)
        )
        conn.commit()
    flash("Profile updated")
    return redirect(url_for('settings'))


@app.route('/settings/avatar', methods=['POST'])
@login_required
def update_avatar():
    user = current_user()
    file = request.files.get('avatar')

    if not file or not file.filename:
        flash("Please choose a file"); return redirect(url_for('settings'))

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED_AVATAR_EXT:
        flash("Avatar must be a PNG, JPG, GIF, or WEBP image")
        return redirect(url_for('settings'))

    file.seek(0, os.SEEK_END)
    if file.tell() > MAX_AVATAR_BYTES:
        flash("Avatar must be smaller than 5 MB"); return redirect(url_for('settings'))
    file.seek(0)

    # Save into the central /static/avatars/ folder so the template can find it
    avatar_filename = f"{user}.{ext}"
    avatar_path = os.path.join(f'{USERS_ROOT}/{user}', avatar_filename)

    # Remove any older avatar for this user with a different extension
    for old in os.listdir(f'{USERS_ROOT}/{user}'):
        if old.startswith(f"{user}.") and old != avatar_filename:
            try: os.remove(os.path.join(f'{USERS_ROOT}/{user}', old))
            except OSError: pass

    try:
        img = Image.open(file)
        img = img.convert('RGBA' if ext == 'png' else 'RGB')
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top  = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        img = img.resize((400, 400), Image.LANCZOS)
        save_kwargs = {'quality': 88} if ext in ('jpg', 'jpeg', 'webp') else {}
        img.save(avatar_path, **save_kwargs)
    except Exception as e:
        flash(f"Could not process image: {e}"); return redirect(url_for('settings'))

    rel = f"avatars/{avatar_filename}"  # path stored relative to /static/
    with get_db() as conn:
        conn.execute("UPDATE users SET avatar = ? WHERE username = ?", (rel, user))
        conn.commit()

    flash("Profile picture updated")
    return redirect(url_for('settings'))


@app.route('/settings/avatar/remove', methods=['POST'])
@login_required
def remove_avatar():
    user = current_user()
    row = get_user_record(user)
    if row and row['avatar']:
        old_path = os.path.join('static', row['avatar'])
        if os.path.exists(old_path):
            try: os.remove(old_path)
            except OSError: pass
    with get_db() as conn:
        conn.execute("UPDATE users SET avatar = NULL WHERE username = ?", (user,))
        conn.commit()
    flash("Profile picture removed")
    return redirect(url_for('settings'))


@app.route('/settings/password', methods=['POST'])
@login_required
def update_password():
    user = current_user()
    current_pw = request.form.get('current_password') or ''
    new_pw     = request.form.get('new_password') or ''
    confirm_pw = request.form.get('confirm_password') or ''

    row = get_user_record(user)
    if not row or not check_password_hash(row['password_hash'], current_pw):
        flash("Current password is incorrect"); return redirect(url_for('settings'))
    if len(new_pw) < 8:
        flash("New password must be at least 8 characters"); return redirect(url_for('settings'))
    if new_pw != confirm_pw:
        flash("New passwords don't match"); return redirect(url_for('settings'))
    if new_pw == current_pw:
        flash("New password must be different from your current password")
        return redirect(url_for('settings'))

    with get_db() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (generate_password_hash(new_pw), user)
        )
        conn.commit()
    flash("Password changed successfully")
    return redirect(url_for('settings'))


# ============================================================
# BILLING / PAYPAL SUBSCRIPTIONS
# ============================================================
@app.route('/billing', methods=['GET'])
@login_required
def billing():
    user = current_user()
    paypal_plans = ensure_paypal_plans()
    return render_template(
        'Billing.html',
        plans=PLANS,
        paypal_plans=paypal_plans,
        paypal_client_id=PAYPAL_CLIENT_ID,
        paypal_currency=PAYPAL_CURRENCY,
        **user_context(user),
    )


@app.route('/billing/verify', methods=['POST'])
@login_required
def billing_verify():
    """
    Browser sends {subscriptionID, plan} after PayPal subscription approval.
    Server MUST verify with PayPal's API before granting the plan.
    """
    user = current_user()
    data = request.get_json(silent=True) or {}
    sub_id   = (data.get('subscriptionID') or '').strip()
    plan_key = (data.get('plan') or '').strip()

    if not sub_id or plan_key not in PLANS:
        return jsonify({'ok': False, 'error': 'Bad request'}), 400
    if PLANS[plan_key]['price'] <= 0:
        return jsonify({'ok': False, 'error': 'Free plans do not require payment'}), 400

    with get_db() as conn:
        existing = conn.execute(
            "SELECT username FROM subscriptions WHERE paypal_sub_id = ?", (sub_id,)
        ).fetchone()
        if existing and existing['username'] != user:
            return jsonify({'ok': False, 'error': 'Subscription belongs to another account'}), 409

    try:
        sub = paypal_get_subscription(sub_id)
    except requests.HTTPError as e:
        return jsonify({'ok': False, 'error': f'PayPal verification failed: {e.response.status_code}'}), 502
    except Exception as e:
        return jsonify({'ok': False, 'error': f'PayPal error: {e}'}), 502

    if sub.get('status') not in ('ACTIVE', 'APPROVED'):
        return jsonify({'ok': False,
                        'error': f"Subscription not active (status: {sub.get('status')})"}), 400

    paypal_plans = ensure_paypal_plans()
    expected_pp_plan = paypal_plans.get(plan_key)
    actual_pp_plan = sub.get('plan_id')
    if not expected_pp_plan or actual_pp_plan != expected_pp_plan:
        return jsonify({'ok': False,
                        'error': 'Plan mismatch — subscription does not match the requested plan'}), 400

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_db() as conn:
        # Cancel any previous active subscription so the user isn't double-billed
        old_row = conn.execute(
            "SELECT subscription_id FROM users WHERE username = ?", (user,)
        ).fetchone()
        old_sub_id = old_row['subscription_id'] if old_row else None
        if old_sub_id and old_sub_id != sub_id:
            try:
                paypal_cancel_subscription(old_sub_id, 'User upgraded to a different plan')
            except Exception as e:
                app.logger.warning("Could not cancel old subscription %s: %s", old_sub_id, e)
            conn.execute(
                "UPDATE subscriptions SET status = 'CANCELLED', cancelled_at = ? WHERE paypal_sub_id = ?",
                (now, old_sub_id)
            )

        try:
            conn.execute(
                """INSERT INTO subscriptions
                   (username, paypal_sub_id, paypal_plan_id, local_plan, status, started_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user, sub_id, actual_pp_plan, plan_key, 'ACTIVE', now),
            )
        except sqlite3.IntegrityError:
            pass  # already inserted — idempotent

        conn.execute(
            "UPDATE users SET plan = ?, subscription_id = ? WHERE username = ?",
            (plan_key, sub_id, user)
        )
        conn.commit()

    return jsonify({'ok': True, 'plan': plan_key, 'plan_label': PLANS[plan_key]['label']})


@app.route('/billing/cancel', methods=['POST'])
@login_required
def billing_cancel():
    user = current_user()
    row = get_user_record(user)
    sub_id = row['subscription_id'] if row else None

    if not sub_id:
        flash("You don't have an active subscription"); return redirect(url_for('settings'))

    try:
        paypal_cancel_subscription(sub_id, 'User cancelled from settings')
    except Exception as e:
        app.logger.warning("PayPal cancellation failed for %s: %s", sub_id, e)
        flash("Could not contact PayPal — please try again or cancel from your PayPal account")
        return redirect(url_for('settings'))

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_db() as conn:
        conn.execute(
            "UPDATE subscriptions SET status = 'CANCELLED', cancelled_at = ? WHERE paypal_sub_id = ?",
            (now, sub_id)
        )
        conn.execute(
            "UPDATE users SET plan = ?, subscription_id = NULL WHERE username = ?",
            (DEFAULT_PLAN, user)
        )
        conn.commit()

    flash("Subscription cancelled. You're back on the Free plan.")
    return redirect(url_for('settings'))


# ============================================================
# IMAGE COLLECTION
# ============================================================
def collect_images(user, album='', view=''):
    base = user_uploads(user)
    images = []
    if not os.path.isdir(base):
        return images

    if album:
        selected_albums = [album] if os.path.isdir(os.path.join(base, album)) else []
    else:
        selected_albums = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]

    for alb in selected_albums:
        alb_path = os.path.join(base, alb)
        for f in os.listdir(alb_path):
            ext = f.rsplit('.', 1)[-1].lower() if '.' in f else ''
            is_video = ext in VIDEO_EXTENSIONS
            is_image = ext in IMAGE_EXTENSIONS
            if not (is_video or is_image):
                continue
            if view == 'videos' and not is_video:
                continue
            images.append({'album': alb, 'filename': f, 'is_video': is_video})

    images.sort(
        key=lambda x: os.path.getmtime(os.path.join(base, x['album'], x['filename'])),
        reverse=True
    )
    return images


@app.route("/load_more")
@login_required
def load_more():
    user  = current_user()
    album = request.args.get("album", "")
    view  = request.args.get("view", "")
    page  = int(request.args.get("page", 1))
    start = (page - 1) * PER_PAGE
    end   = start + PER_PAGE
    return jsonify(collect_images(user, album=album, view=view)[start:end])


# ============================================================
# INDEX
# ============================================================
@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    user       = current_user()
    base       = user_uploads(user)
    thumb_root = user_thumbnails(user)
    create_user_folders(user)

    album = request.args.get('album', '')
    view  = request.args.get('view', '')
    page  = int(request.args.get("page", 1))

    # POST upload (legacy form)
    if request.method == 'POST' and 'file' in request.files:
        files = request.files.getlist('file')
        target_album = album or 'Default'

        total_incoming = 0
        for f in files:
            if f and f.filename:
                f.seek(0, os.SEEK_END)
                total_incoming += f.tell()
                f.seek(0)
        ok, _ = can_user_store(user, total_incoming)
        if not ok:
            flash("You're out of storage. Upgrade your plan to upload more.")
            return redirect(url_for('index', album=album))

        album_path = os.path.join(base, target_album)
        os.makedirs(album_path, exist_ok=True)
        for file in files:
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file.save(os.path.join(album_path, filename))
        return redirect(url_for('index', album=album))

    if album:
        os.makedirs(os.path.join(base, album), exist_ok=True)

    # Album metadata for sidebar
    albums_info = {}
    for d in os.listdir(base):
        album_path = os.path.join(base, d)
        if not os.path.isdir(album_path):
            continue

        video_count = photo_count = size_bytes = 0
        for f in os.listdir(album_path):
            file_path = os.path.join(album_path, f)
            ext = f.rsplit('.', 1)[-1].lower() if '.' in f else ''
            if ext in IMAGE_EXTENSIONS:
                photo_count += 1
                size_bytes += os.path.getsize(file_path)
            elif ext in VIDEO_EXTENSIONS:
                video_count += 1
                size_bytes += os.path.getsize(file_path)

        folder_time = os.path.getmtime(album_path)
        all_files = [f for f in os.listdir(album_path) if allowed_file(f)]
        preview_images = random.sample(all_files, min(4, len(all_files)))

        albums_info[d] = {
            'photo_count': photo_count,
            'video_count': video_count,
            'file_count':  photo_count + video_count,
            'size_bytes':  size_bytes,
            'size_human':  human_size(size_bytes),
            'created_time': datetime.fromtimestamp(folder_time).strftime('%Y-%m-%d %H:%M:%S'),
            'preview_images': [os.path.join(d, f) for f in preview_images],
        }

    total_all_count    = sum(a['file_count']  for a in albums_info.values())
    total_videos_count = sum(a['video_count'] for a in albums_info.values())
    total_photos_count = sum(a['photo_count'] for a in albums_info.values())

    images = collect_images(user, album=album, view=view)

    # Add thumbnail paths (relative to /static/)
    for img in images:
        if img['is_video']:
            base_name = img['filename'].rsplit('.', 1)[0]
            thumb_filename = f"{img['album']}_{base_name}_thumb.jpg"
            thumb_path = os.path.join(thumb_root, thumb_filename)
            generate_video_thumbnail(
                os.path.join(base, img['album'], img['filename']),
                thumb_path,
            )
            img['thumbnail'] = f"{user}/Thumbnails/{thumb_filename}"
        else:
            img['thumbnail'] = img['filename']

    start = (page - 1) * PER_PAGE
    end   = start + PER_PAGE
    images_page = images[start:end]

    ctx = user_context(user)
    size_bytes = ctx.get('used_bytes', 0)
    plan_limit = ctx.get('limit_bytes', 0)

    return render_template(
        'index.html',
        images=images_page,
        page=page,
        albums=albums_info,
        current_album=album,
        current_view=view,
        total_images=len(images),
        total_all_count=total_all_count,
        total_videos_count=total_videos_count,
        total_photos_count=total_photos_count,
        size_unt=human_size(size_bytes),
        size_bytes=size_bytes,
        plan_limit_bytes=plan_limit,
        plan_limit_human=ctx.get('limit_human', 'Unlimited'),
        plan_percent_used=ctx.get('percent_used', 0),
        # legacy fields some old template bits used
        size_gb=size_bytes / GB,
        free_gb=max(1, (plan_limit - size_bytes) // GB) if plan_limit else 1,
        **ctx,
    )


# ============================================================
# OTHER ROUTES
# ============================================================
@app.route('/change_name', methods=['POST'])
@login_required
def change_name():
    user = current_user()
    base = user_uploads(user)

    old_name = request.form.get('old_name')
    new_name = request.form.get('new_name')
    if not old_name or not new_name:
        flash("Both old and new folder names are required."); return redirect(url_for('index'))

    new_name = secure_filename(new_name) or new_name
    old_path = os.path.join(base, old_name)
    new_path = os.path.join(base, new_name)

    if not os.path.exists(old_path):
        flash(f"Folder '{old_name}' does not exist."); return redirect(url_for('index'))
    if os.path.exists(new_path):
        flash(f"Folder '{new_name}' already exists."); return redirect(url_for('index'))

    try:
        os.rename(old_path, new_path)
        flash(f"Folder renamed to '{new_name}' successfully!")
    except Exception as e:
        flash(f"Error renaming folder: {e}")
    return redirect(url_for('index'))


@app.route('/download', methods=['POST'])
@login_required
def download():
    user = current_user()
    base = user_uploads(user)

    selected_images = request.form.getlist('selected_images')
    album = request.form.get('album', 'Default') or 'Default'
    if not selected_images:
        return redirect(url_for('index', album=album))

    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for rel in selected_images:
            img_path = os.path.join(base, rel)
            if os.path.isfile(img_path):
                zf.write(img_path, rel)
    zip_buffer.seek(0)

    albums_in = {rel.split('/', 1)[0] for rel in selected_images if '/' in rel}
    zip_name = f"{next(iter(albums_in))}.zip" if len(albums_in) == 1 else "gallery_selection.zip"
    return send_file(zip_buffer, as_attachment=True, download_name=zip_name, mimetype='application/zip')


@app.route('/DeletSelected', methods=['POST'])
@login_required
def DeletSelected():
    user = current_user()
    base = user_uploads(user)
    delet_folder = user_deleted(user)
    os.makedirs(delet_folder, exist_ok=True)

    album = request.form.get('album', 'Default')
    for img in request.form.getlist('selected_images'):
        img_path = os.path.join(base, img)
        if os.path.exists(img_path):
            shutil.move(img_path, os.path.join(delet_folder, os.path.basename(img)))
    return redirect(url_for('index', album=album))


@app.route('/MoveSelected', methods=['POST'])
@login_required
def MoveSelected():
    user = current_user()
    base = user_uploads(user)

    target_album = request.form.get('target_album')
    if not target_album:
        return redirect(url_for('index'))

    target_path = os.path.join(base, target_album)
    os.makedirs(target_path, exist_ok=True)
    for file_rel_path in request.form.getlist('selected_images'):
        src = os.path.join(base, file_rel_path)
        if os.path.exists(src):
            shutil.move(src, os.path.join(target_path, os.path.basename(file_rel_path)))
    return redirect(url_for('index', album=target_album))


@app.route('/DeletImage', methods=['POST'])
@login_required
def DeletImage():
    user = current_user()
    imagName = unquote(request.form.get('imagName') or '')
    src_path = os.path.join(os.getcwd(), imagName)

    user_abs = os.path.abspath(user_root(user))
    src_abs  = os.path.abspath(src_path)
    if not src_abs.startswith(user_abs):
        return "Forbidden", 403
    if not os.path.exists(src_path):
        return "File does not exist : " + src_path, 404

    delet_folder = user_deleted(user)
    os.makedirs(delet_folder, exist_ok=True)
    shutil.move(src_path, os.path.join(delet_folder, os.path.basename(src_path)))
    return redirect(url_for('index'))


@app.route('/upload', methods=['POST'])
@login_required
def upload():
    user = current_user()
    base = user_uploads(user)

    file = request.files.get('file')
    album = request.form.get('album') or 'Default'
    if not file:
        return jsonify({'ok': False, 'error': 'No file'}), 400
    if not allowed_file(file.filename):
        return jsonify({'ok': False, 'error': 'File type not allowed'}), 400

    file.seek(0, os.SEEK_END)
    incoming = file.tell()
    file.seek(0)
    ok, used = can_user_store(user, incoming)
    if not ok:
        plan = get_user_plan(user)
        return jsonify({'ok': False, 'error': 'Storage limit reached',
                        'used': used, 'limit': plan['limit_bytes']}), 413

    filename = secure_filename(file.filename)
    album_path = os.path.join(base, str(album))
    os.makedirs(album_path, exist_ok=True)
    file.save(os.path.join(album_path, filename))
    return jsonify({'ok': True})


if __name__ == '__main__':
    app.run(host="0.0.0.0", debug=True, port=5002)