"""
LEAVE MANAGEMENT SYSTEM
Flask + Classical AI (Constraint Satisfaction Problem - CSP)

HOW TO EXPLAIN THIS TO YOUR TEACHER (in one line):
When a faculty member applies for leave, we don't just save it - we run it
through a series of CONSTRAINTS (rules). Each constraint is a small function
that returns True (satisfied) or False (violated). If ALL constraints pass,
the leave has "no conflict". If even one fails, we flag a conflict and
suggest a substitute teacher automatically. That is the core idea of CSP:
finding a value (leave approval) that satisfies every constraint at once.
"""

from flask import Flask, render_template, request, redirect, url_for, session
import sqlite3
import os
import secrets
import calendar as pycalendar
from datetime import datetime, date, timedelta

app = Flask(__name__)

# ---------------------------------------------------------------------------
# CACHE FIX: Flask serves static files (style.css etc.) with browser caching
# turned on by default, so after we update the CSS, browsers can keep
# showing the OLD stylesheet until manually hard-refreshed. This turns that
# caching off completely and stamps every static file link with a fresh
# version number on every server start, so the browser is always forced to
# download the latest CSS - no hard refresh needed, ever.
# ---------------------------------------------------------------------------
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.config["TEMPLATES_AUTO_RELOAD"] = True
ASSET_VERSION = secrets.token_hex(4)


@app.context_processor
def inject_asset_version():
    return {"asset_version": ASSET_VERSION}


# ---------------------------------------------------------------------------
# IMPORTANT FIX: the secret key is generated fresh every time the server
# starts (instead of a fixed string). Flask login sessions are just signed
# cookies stored in the browser - if the secret key never changes, an old
# admin/faculty login cookie stays valid forever, even after you restart the
# app or think you've "logged out everyone". Randomizing it on startup means
# every restart instantly invalidates all previous logins, so before your
# demo you just restart the app for a guaranteed clean, logged-out state.
# ---------------------------------------------------------------------------
app.secret_key = secrets.token_hex(32)

# Sessions also auto-expire after 20 minutes of inactivity, so even without
# a restart, an old login can't silently stay open in the background.
app.permanent_session_lifetime = timedelta(minutes=20)


@app.before_request
def make_session_permanent():
    session.permanent = True


# ---------------------------------------------------------------------------
# This fixes the "back button / stale session shows dashboard" bug.
# Browsers cache pages; after logout, pressing Back can show a cached copy
# of a protected page instead of asking the server again. This header tells
# the browser to never cache these pages, so every visit is freshly checked.
# ---------------------------------------------------------------------------
@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

DB_PATH = os.path.join(os.path.dirname(__file__), "database.db")

# The semester window the Calendar feature displays / the advance-booking
# limit is measured against. Adjust these two lines for a different term.
SEMESTER_MONTHS = [(2026, 8), (2026, 9), (2026, 10), (2026, 11), (2026, 12)]


# ---------------------------------------------------------------------------
# DATABASE HELPERS
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Creates tables and seeds demo data (runs once if database.db is missing)."""
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS faculty (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            department TEXT,
            subject TEXT,
            leave_balance INTEGER DEFAULT 30
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS timetable (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            faculty_id INTEGER,
            day TEXT,
            time_slot TEXT,
            subject TEXT,
            class_name TEXT,
            FOREIGN KEY (faculty_id) REFERENCES faculty(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS leave_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            faculty_id INTEGER,
            leave_type TEXT,
            from_date TEXT,
            to_date TEXT,
            reason TEXT,
            duration INTEGER,
            status TEXT DEFAULT 'Pending',
            conflict TEXT DEFAULT 'No',
            substitute_id INTEGER,
            created_at TEXT,
            FOREIGN KEY (faculty_id) REFERENCES faculty(id)
        )
    """)

    # NEW: tracks each "please cover this class" notification sent to a
    # substitute faculty member, and whether they Accepted or Denied it.
    c.execute("""
        CREATE TABLE IF NOT EXISTS substitute_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            leave_request_id INTEGER,
            original_faculty_id INTEGER,
            substitute_faculty_id INTEGER,
            timetable_id INTEGER,
            day TEXT,
            time_slot TEXT,
            subject TEXT,
            class_name TEXT,
            status TEXT DEFAULT 'Pending',
            created_at TEXT,
            FOREIGN KEY (leave_request_id) REFERENCES leave_requests(id),
            FOREIGN KEY (substitute_faculty_id) REFERENCES faculty(id)
        )
    """)

    # Seed demo faculty only if table is empty
    c.execute("SELECT COUNT(*) FROM faculty")
    if c.fetchone()[0] == 0:
        faculty_seed = [
            ("Prof. Sharma", "sharma", "pass123", "Computer Science", "Python"),
            ("Prof. Patil", "patil", "pass123", "Computer Science", "Python"),
            ("Prof. Joshi", "joshi", "pass123", "Data Science", "Statistics"),
            ("Prof. More", "more", "pass123", "Data Science", "Statistics"),
        ]
        c.executemany(
            "INSERT INTO faculty (name, username, password, department, subject) VALUES (?, ?, ?, ?, ?)",
            faculty_seed,
        )

        timetable_seed = [
            (1, "Monday", "09:00 - 10:00", "Python", "BSc DS - Div A"),
            (1, "Monday", "10:00 - 11:00", "Python", "BSc DS - Div B"),
            (2, "Monday", "11:00 - 12:00", "Python", "BSc DS - Div A"),
            (3, "Tuesday", "10:00 - 11:00", "Statistics", "BSc DS - Div A"),
            (4, "Tuesday", "11:00 - 12:00", "Statistics", "BSc DS - Div B"),
        ]
        c.executemany(
            "INSERT INTO timetable (faculty_id, day, time_slot, subject, class_name) VALUES (?, ?, ?, ?, ?)",
            timetable_seed,
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Small date helper: adds N calendar months to a date (handles year
# rollover and clamps to the last valid day of the target month, e.g.
# Jan 31 + 1 month = Feb 28). Used for the "max 2 months in advance" rule.
# ---------------------------------------------------------------------------
def add_months(d, months):
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, pycalendar.monthrange(year, month)[1])
    return date(year, month, day)


# ---------------------------------------------------------------------------
# THE CSP ENGINE  -  this is the part you present as "the AI"
# ---------------------------------------------------------------------------
def check_constraints(faculty_id, from_date, to_date, leave_type, exclude_request_id=None):
    """
    Runs every constraint one by one and collects the results.
    exclude_request_id: when re-checking a request that is ALREADY saved in
    the database (e.g. the admin's Request Details page), pass its own id
    here so the overlap check doesn't compare the request against itself.
    Returns a dict: { constraints: [...], conflict: bool, classes_affected: [...],
                       substitutes: [...] }
    """
    conn = get_db()
    c = conn.cursor()

    d1 = datetime.strptime(from_date, "%Y-%m-%d")
    d2 = datetime.strptime(to_date, "%Y-%m-%d")
    duration = (d2 - d1).days + 1

    results = []

    # Constraint 1: Maximum leave duration (<= 7 days)
    ok = duration <= 7
    results.append(("Maximum Duration (<=7 days)", ok))

    # Constraint 2: Valid date range
    ok = d2 >= d1
    results.append(("Date Validation", ok))

    # Constraint 3: No overlapping leave for the same faculty
    # (excludes the request's own row when re-checking an already-saved
    # request, otherwise every saved request would always overlap itself)
    if exclude_request_id:
        c.execute("""
            SELECT COUNT(*) FROM leave_requests
            WHERE faculty_id = ? AND status != 'Rejected' AND id != ?
            AND NOT (to_date < ? OR from_date > ?)
        """, (faculty_id, exclude_request_id, from_date, to_date))
    else:
        c.execute("""
            SELECT COUNT(*) FROM leave_requests
            WHERE faculty_id = ? AND status != 'Rejected'
            AND NOT (to_date < ? OR from_date > ?)
        """, (faculty_id, from_date, to_date))
    overlap_count = c.fetchone()[0]
    results.append(("No Overlapping Leave", overlap_count == 0))

    # Constraint 4: Leave balance sufficient
    c.execute("SELECT leave_balance FROM faculty WHERE id = ?", (faculty_id,))
    balance = c.fetchone()[0]
    results.append(("Sufficient Leave Balance", balance >= duration))

    # Constraint 5: Timetable conflict - which classes fall in this leave window
    # (get unique weekday names covered by the leave date range)
    covered_days = set()
    cursor_date = d1
    while cursor_date <= d2:
        covered_days.add(cursor_date.strftime("%A"))
        cursor_date += timedelta(days=1)

    placeholders = ",".join("?" * len(covered_days))
    c.execute(f"""
        SELECT * FROM timetable WHERE faculty_id = ? AND day IN ({placeholders})
    """, (faculty_id, *covered_days))
    classes_affected = c.fetchall()
    results.append(("No Timetable Conflict", len(classes_affected) == 0))

    # Constraint 6: Substitute availability - same subject, different faculty
    substitutes = []
    if classes_affected:
        c.execute("SELECT subject, department FROM faculty WHERE id = ?", (faculty_id,))
        row = c.fetchone()
        c.execute("""
            SELECT id, name FROM faculty
            WHERE subject = ? AND id != ?
        """, (row["subject"], faculty_id))
        for sub in c.fetchall():
            # naive match score: same subject = 100%, else 80% (kept simple on purpose)
            substitutes.append({"id": sub["id"], "name": sub["name"], "match": "100%"})
    results.append(("Substitute Available", len(substitutes) > 0 or len(classes_affected) == 0))

    # Constraint 7: Applied within the 2-month advance-booking limit
    max_allowed = add_months(date.today(), 2)
    ok = d1.date() <= max_allowed
    results.append((f"Within 2-Month Advance Limit (by {max_allowed.strftime('%d %b %Y')})", ok))

    conn.close()

    conflict = any(not ok for _, ok in results)

    return {
        "constraints": results,
        "conflict": conflict,
        "duration": duration,
        "classes_affected": classes_affected,
        "substitutes": substitutes,
    }


# ---------------------------------------------------------------------------
# AUTH ROUTES
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        role = request.form.get("role")
        username = request.form.get("username")
        password = request.form.get("password")

        if role == "admin":
            if username == "admin" and password == "admin123":
                session["role"] = "admin"
                return redirect(url_for("admin_dashboard"))
            return render_template("login.html", error="Invalid admin credentials")

        conn = get_db()
        row = conn.execute(
            "SELECT * FROM faculty WHERE username = ? AND password = ?",
            (username, password),
        ).fetchone()
        conn.close()

        if row:
            session["role"] = "faculty"
            session["faculty_id"] = row["id"]
            session["faculty_name"] = row["name"]
            return redirect(url_for("faculty_dashboard"))
        return render_template("login.html", error="Invalid faculty credentials")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# FACULTY ROUTES
# ---------------------------------------------------------------------------
@app.route("/faculty/dashboard")
def faculty_dashboard():
    if session.get("role") != "faculty":
        return redirect(url_for("login"))
    fid = session["faculty_id"]
    conn = get_db()
    stats = {
        "total": conn.execute("SELECT leave_balance FROM faculty WHERE id=?", (fid,)).fetchone()["leave_balance"],
        "pending": conn.execute("SELECT COUNT(*) c FROM leave_requests WHERE faculty_id=? AND status='Pending'", (fid,)).fetchone()["c"],
        "approved": conn.execute("SELECT COUNT(*) c FROM leave_requests WHERE faculty_id=? AND status='Approved'", (fid,)).fetchone()["c"],
        "rejected": conn.execute("SELECT COUNT(*) c FROM leave_requests WHERE faculty_id=? AND status='Rejected'", (fid,)).fetchone()["c"],
    }
    todays_classes = conn.execute(
        "SELECT * FROM timetable WHERE faculty_id=? AND day='Monday'", (fid,)
    ).fetchall()

    # NEW: substitute-cover requests waiting for THIS faculty member to
    # respond to (Accept = they'll take the class, Deny = it gets cancelled)
    pending_substitute_requests = conn.execute("""
        SELECT sr.*, f.name as original_faculty_name FROM substitute_requests sr
        JOIN faculty f ON sr.original_faculty_id = f.id
        WHERE sr.substitute_faculty_id = ? AND sr.status = 'Pending'
        ORDER BY sr.id DESC
    """, (fid,)).fetchall()

    conn.close()
    return render_template(
        "faculty_dashboard.html", stats=stats, classes=todays_classes,
        substitute_requests=pending_substitute_requests,
    )


@app.route("/faculty/apply", methods=["GET", "POST"])
def apply_leave():
    if session.get("role") != "faculty":
        return redirect(url_for("login"))

    today = date.today()
    max_allowed = add_months(today, 2)

    result = None
    form_data = None
    advance_error = None

    if request.method == "POST":
        fid = session["faculty_id"]
        leave_type = request.form.get("leave_type")
        from_date = request.form.get("from_date")
        to_date = request.form.get("to_date")
        reason = request.form.get("reason")
        form_data = {
            "leave_type": leave_type, "from_date": from_date,
            "to_date": to_date, "reason": reason,
        }

        # HARD LIMIT: leave can only be applied up to 2 months in advance.
        # This is a policy rule (not a resource conflict like the others),
        # so it blocks the request outright - there is no "submit anyway".
        try:
            parsed_from = datetime.strptime(from_date, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            parsed_from = None

        if parsed_from and (parsed_from < today or parsed_from > max_allowed):
            advance_error = (
                f"You can only apply for leave between {today.strftime('%d %b %Y')} "
                f"and {max_allowed.strftime('%d %b %Y')} (max 2 months in advance)."
            )
        else:
            result = check_constraints(fid, from_date, to_date, leave_type)

            if request.form.get("confirm") == "yes":
                conn = get_db()
                conn.execute("""
                    INSERT INTO leave_requests
                    (faculty_id, leave_type, from_date, to_date, reason, duration, status, conflict, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'Pending', ?, ?)
                """, (fid, leave_type, from_date, to_date, reason, result["duration"],
                      "Yes" if result["conflict"] else "No", datetime.now().strftime("%Y-%m-%d")))
                conn.commit()
                conn.close()
                return redirect(url_for("my_leaves"))

    return render_template(
        "apply_leave.html", result=result, form_data=form_data,
        advance_error=advance_error,
        min_date=today.isoformat(), max_date=max_allowed.isoformat(),
    )


@app.route("/faculty/my-leaves")
def my_leaves():
    if session.get("role") != "faculty":
        return redirect(url_for("login"))
    conn = get_db()
    leaves = conn.execute(
        "SELECT * FROM leave_requests WHERE faculty_id=? ORDER BY id DESC",
        (session["faculty_id"],)
    ).fetchall()
    conn.close()
    return render_template("my_leaves.html", leaves=leaves)


@app.route("/faculty/timetable")
def faculty_timetable():
    if session.get("role") != "faculty":
        return redirect(url_for("login"))
    conn = get_db()
    slots = conn.execute(
        "SELECT * FROM timetable WHERE faculty_id=? ORDER BY day", (session["faculty_id"],)
    ).fetchall()
    conn.close()
    return render_template("timetable.html", slots=slots)


@app.route("/faculty/substitute/<int:sub_id>/<action>")
def respond_substitute_request(sub_id, action):
    """A faculty member Accepts or Denies a request to cover someone else's
    class while that colleague is on leave. Denying marks the class as
    cancelled (no substitute could be arranged)."""
    if session.get("role") != "faculty":
        return redirect(url_for("login"))
    conn = get_db()
    sub_req = conn.execute(
        "SELECT * FROM substitute_requests WHERE id=?", (sub_id,)
    ).fetchone()
    # Only the faculty member this request was actually sent to can respond,
    # and only while it's still Pending (prevents re-clicking old links).
    if sub_req and sub_req["substitute_faculty_id"] == session["faculty_id"] and sub_req["status"] == "Pending":
        new_status = "Accepted" if action == "accept" else "Denied"
        conn.execute("UPDATE substitute_requests SET status=? WHERE id=?", (new_status, sub_id))
        conn.commit()
    conn.close()
    return redirect(url_for("faculty_dashboard"))


def _build_month_calendar(year, month, faculty_filter, conn):
    """Shared helper: builds a week-by-week grid for the given month, with
    each day annotated with the classes scheduled on that weekday."""
    if faculty_filter == "all":
        rows = conn.execute("""
            SELECT t.*, f.name as faculty_name FROM timetable t
            JOIN faculty f ON t.faculty_id = f.id
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT t.*, f.name as faculty_name FROM timetable t
            JOIN faculty f ON t.faculty_id = f.id WHERE t.faculty_id = ?
        """, (faculty_filter,)).fetchall()

    by_weekday = {}
    for r in rows:
        by_weekday.setdefault(r["day"], []).append(r)

    cal = pycalendar.Calendar(firstweekday=0)
    weeks = []
    for week in cal.monthdayscalendar(year, month):
        week_out = []
        for day_num in week:
            if day_num == 0:
                week_out.append(None)
            else:
                weekday_name = date(year, month, day_num).strftime("%A")
                week_out.append({
                    "day": day_num,
                    "classes": by_weekday.get(weekday_name, []),
                })
        weeks.append(week_out)
    return weeks


def _semester_month_options():
    return [
        {"value": f"{y}-{m:02d}", "label": f"{pycalendar.month_name[m]} {y}"}
        for (y, m) in SEMESTER_MONTHS
    ]


@app.route("/faculty/calendar")
def faculty_calendar():
    if session.get("role") != "faculty":
        return redirect(url_for("login"))

    month_param = request.args.get("month")
    today = date.today()
    default_month = f"{today.year}-{today.month:02d}"
    if not month_param or month_param not in [o["value"] for o in _semester_month_options()]:
        month_param = default_month if default_month in [o["value"] for o in _semester_month_options()] else _semester_month_options()[0]["value"]

    year, month = map(int, month_param.split("-"))
    conn = get_db()
    weeks = _build_month_calendar(year, month, session["faculty_id"], conn)
    conn.close()

    return render_template(
        "faculty_calendar.html",
        weeks=weeks, month_label=f"{pycalendar.month_name[month]} {year}",
        month_options=_semester_month_options(), selected_month=month_param,
    )


# ---------------------------------------------------------------------------
# ADMIN ROUTES
# ---------------------------------------------------------------------------
@app.route("/admin/dashboard")
def admin_dashboard():
    if session.get("role") != "admin":
        return redirect(url_for("login"))
    conn = get_db()
    stats = {
        "total_faculty": conn.execute("SELECT COUNT(*) c FROM faculty").fetchone()["c"],
        "pending": conn.execute("SELECT COUNT(*) c FROM leave_requests WHERE status='Pending'").fetchone()["c"],
        "approved": conn.execute("SELECT COUNT(*) c FROM leave_requests WHERE status='Approved'").fetchone()["c"],
        "conflicts": conn.execute("SELECT COUNT(*) c FROM leave_requests WHERE conflict='Yes'").fetchone()["c"],
    }
    recent = conn.execute("""
        SELECT lr.*, f.name as faculty_name FROM leave_requests lr
        JOIN faculty f ON lr.faculty_id = f.id
        ORDER BY lr.id DESC LIMIT 5
    """).fetchall()
    conn.close()
    return render_template("admin_dashboard.html", stats=stats, recent=recent)


@app.route("/admin/requests")
def leave_requests_page():
    if session.get("role") != "admin":
        return redirect(url_for("login"))
    conn = get_db()
    requests_ = conn.execute("""
        SELECT lr.*, f.name as faculty_name FROM leave_requests lr
        JOIN faculty f ON lr.faculty_id = f.id
        ORDER BY lr.id DESC
    """).fetchall()
    conn.close()
    return render_template("leave_requests.html", requests=requests_)


@app.route("/admin/request/<int:req_id>")
def request_details(req_id):
    if session.get("role") != "admin":
        return redirect(url_for("login"))
    conn = get_db()
    req = conn.execute("""
        SELECT lr.*, f.name as faculty_name FROM leave_requests lr
        JOIN faculty f ON lr.faculty_id = f.id WHERE lr.id=?
    """, (req_id,)).fetchone()
    # exclude_request_id=req_id -> fixes the bug where a saved request's
    # own overlap check always matched itself
    analysis = check_constraints(
        req["faculty_id"], req["from_date"], req["to_date"], req["leave_type"],
        exclude_request_id=req_id,
    )

    # NEW: for each affected class, has a substitute already been notified?
    notified = conn.execute("""
        SELECT sr.*, f.name as substitute_name FROM substitute_requests sr
        JOIN faculty f ON sr.substitute_faculty_id = f.id
        WHERE sr.leave_request_id=?
    """, (req_id,)).fetchall()
    notified_by_timetable_id = {n["timetable_id"]: n for n in notified}

    conn.close()
    return render_template(
        "request_details.html", req=req, analysis=analysis,
        notified_by_timetable_id=notified_by_timetable_id,
    )


@app.route("/admin/notify-substitute/<int:req_id>/<int:timetable_id>/<int:substitute_id>")
def notify_substitute(req_id, timetable_id, substitute_id):
    """Admin action: send a cover-class notification to the recommended
    substitute for one specific affected class."""
    if session.get("role") != "admin":
        return redirect(url_for("login"))
    conn = get_db()

    already = conn.execute(
        "SELECT id FROM substitute_requests WHERE leave_request_id=? AND timetable_id=?",
        (req_id, timetable_id),
    ).fetchone()

    if not already:
        leave_req = conn.execute("SELECT * FROM leave_requests WHERE id=?", (req_id,)).fetchone()
        class_row = conn.execute("SELECT * FROM timetable WHERE id=?", (timetable_id,)).fetchone()
        if leave_req and class_row:
            conn.execute("""
                INSERT INTO substitute_requests
                (leave_request_id, original_faculty_id, substitute_faculty_id, timetable_id,
                 day, time_slot, subject, class_name, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?)
            """, (
                req_id, leave_req["faculty_id"], substitute_id, timetable_id,
                class_row["day"], class_row["time_slot"], class_row["subject"], class_row["class_name"],
                datetime.now().strftime("%Y-%m-%d"),
            ))
            conn.commit()

    conn.close()
    return redirect(url_for("request_details", req_id=req_id))


@app.route("/admin/request/<int:req_id>/<action>")
def update_request(req_id, action):
    if session.get("role") != "admin":
        return redirect(url_for("login"))
    conn = get_db()

    # Only act on requests that are still Pending - this stops the balance
    # from being deducted twice if the approve/reject link is clicked again
    # (e.g. double-click, browser back button, refresh).
    req = conn.execute("SELECT * FROM leave_requests WHERE id=?", (req_id,)).fetchone()
    if req and req["status"] == "Pending":
        status = "Approved" if action == "approve" else "Rejected"
        conn.execute("UPDATE leave_requests SET status=? WHERE id=?", (status, req_id))

        if status == "Approved":
            # Deduct the approved leave's duration from the faculty member's
            # leave balance, shown on both their dashboard and the admin's
            # Faculty Management page.
            conn.execute(
                "UPDATE faculty SET leave_balance = MAX(0, leave_balance - ?) WHERE id=?",
                (req["duration"], req["faculty_id"]),
            )

        conn.commit()
    conn.close()
    return redirect(url_for("leave_requests_page"))


@app.route("/admin/faculty")
def faculty_management():
    if session.get("role") != "admin":
        return redirect(url_for("login"))
    conn = get_db()
    faculty = conn.execute("SELECT * FROM faculty").fetchall()
    conn.close()
    return render_template("faculty_management.html", faculty=faculty)


@app.route("/admin/calendar")
def admin_calendar():
    if session.get("role") != "admin":
        return redirect(url_for("login"))

    month_param = request.args.get("month")
    today = date.today()
    default_month = f"{today.year}-{today.month:02d}"
    valid_months = [o["value"] for o in _semester_month_options()]
    if not month_param or month_param not in valid_months:
        month_param = default_month if default_month in valid_months else valid_months[0]

    faculty_filter = request.args.get("faculty", "all")

    year, month = map(int, month_param.split("-"))
    conn = get_db()
    weeks = _build_month_calendar(year, month, faculty_filter, conn)
    all_faculty = conn.execute("SELECT id, name FROM faculty").fetchall()
    conn.close()

    return render_template(
        "admin_calendar.html",
        weeks=weeks, month_label=f"{pycalendar.month_name[month]} {year}",
        month_options=_semester_month_options(), selected_month=month_param,
        all_faculty=all_faculty, selected_faculty=faculty_filter,
    )


@app.route("/admin/reports")
def reports():
    if session.get("role") != "admin":
        return redirect(url_for("login"))
    conn = get_db()
    rows = conn.execute("""
        SELECT strftime('%m', created_at) as month, COUNT(*) as total
        FROM leave_requests GROUP BY month
    """).fetchall()
    conn.close()
    labels = [r["month"] for r in rows] or ["No Data"]
    values = [r["total"] for r in rows] or [0]
    return render_template("reports.html", labels=labels, values=values)


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
