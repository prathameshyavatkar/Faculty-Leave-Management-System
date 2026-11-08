# Leave Management System
Flask + Classical AI (Constraint Satisfaction Problem)

## How to run
1. pip install -r requirements.txt
2. python app.py
3. Open http://127.0.0.1:5000

The database (database.db) is created automatically on first run with demo data.

## Demo logins
Faculty: sharma / patil / joshi / more   (password: pass123)
Admin:   admin   (password: admin123)

## Getting a clean/fresh state before your presentation
Two things now guarantee a clean, logged-out demo:
1. The login "key" that signs sessions is randomly regenerated every time
   the app starts, so simply restarting the app (Ctrl+C, then `python app.py`
   again) instantly logs everyone out, everywhere.
2. Any login also auto-expires after 20 minutes of inactivity on its own.
To also clear all your test leave data, delete database.db before restarting:
    rm database.db && python app.py

## What to say in your presentation (keep it to this)
"When a faculty member applies for leave, instead of just saving it to the
database, we run it through a CONSTRAINT SATISFACTION PROBLEM (CSP) — a
classical AI technique. Each rule (max duration, no overlap, timetable clash,
leave balance, substitute availability, 2-month advance limit) is a small
constraint function that returns True or False. If every constraint passes,
the system says 'No Conflict' and approves smoothly. If any constraint
fails, it flags the conflict, shows exactly which classes are affected, and
automatically suggests a substitute faculty member teaching the same
subject. The admin can then notify that substitute directly from the
system — if they accept, the class is marked covered; if they decline, the
lecture is marked cancelled."

That's the entire AI story — one function, seven checks, one decision.
See app.py -> check_constraints() for the commented implementation.

## Features
- CSP-based conflict detection (7 constraints) when applying for leave
- Automatic substitute-teacher recommendation for affected classes
- Admin can notify a specific substitute to cover a class; they Accept/Deny
  from their own dashboard — a decline marks the lecture as cancelled
- Leave balance is deducted automatically when the admin approves a request
  (visible on both the faculty's dashboard and Admin > Faculty)
- Semester Calendar (Faculty: their own lectures. Admin: all teachers or
  one specific teacher) highlighting which days have classes scheduled
- Leave can only be applied up to 2 months in advance (hard limit, not
  overridable) - both enforced server-side and reflected in the date picker

## Why SQLite instead of MySQL
No server setup, single portable file, zero config for a demo — keeps the
focus on the CSP logic rather than database administration.
