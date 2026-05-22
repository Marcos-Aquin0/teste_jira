import os
import csv
import io
import json
import time
import threading
import uuid
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, session
import requests

app = Flask(__name__)
app.secret_key = os.urandom(24)

REQUEST_TIMEOUT = 30

# In-memory job store
jobs = {}

# ─── Jira helpers ────────────────────────────────────────────────────────────

def make_session(pat: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {pat}",
        "Accept": "application/json",
        "User-Agent": "JiraChangelogApp/1.0"
    })
    return s


def get_current_user(jira: requests.Session, host: str):
    try:
        r = jira.get(f"{host}/rest/api/2/myself", timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            d = r.json()
            return {
                "displayName": d.get("displayName", ""),
                "emailAddress": d.get("emailAddress", ""),
                "avatarUrl": (d.get("avatarUrls") or {}).get("48x48", ""),
            }
    except Exception:
        pass
    return None


def get_projects(jira: requests.Session, host: str):
    try:
        r = jira.get(f"{host}/rest/api/2/project", timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            return [{"key": p["key"], "name": p["name"]} for p in r.json()]
    except Exception:
        pass
    return []


def get_custom_field_options(jira: requests.Session, host: str, field_name: str):
    """Try to fetch options for a named custom field."""
    try:
        r = jira.get(f"{host}/rest/api/2/field", timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return []
        fields = r.json()
        field_id = None
        for f in fields:
            if f.get("name") == field_name or f.get("id") == field_name:
                field_id = f.get("id")
                break
        if not field_id:
            return []
        r2 = jira.get(
            f"{host}/rest/api/2/customFieldOption/{field_id}",
            timeout=REQUEST_TIMEOUT,
        )
        if r2.status_code == 200:
            return [o.get("value") for o in r2.json()]
    except Exception:
        pass
    return []


def search_issues(jira: requests.Session, host: str, jql: str, progress_cb=None):
    issue_keys = []
    start_at = 0
    max_results = 100
    total = None
    while True:
        r = jira.get(
            f"{host}/rest/api/2/search",
            params={"jql": jql, "startAt": start_at, "maxResults": max_results, "fields": "key"},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            break
        data = r.json()
        if total is None:
            total = data.get("total", 0)
        issues = data.get("issues", [])
        if not issues:
            break
        for iss in issues:
            issue_keys.append(iss["key"])
        start_at += max_results
        if progress_cb:
            progress_cb("issues", len(issue_keys), total)
    return issue_keys, total or 0


def format_brazil_datetime(dt_str: str) -> str:
    if not dt_str:
        return ""
    try:
        dt = datetime.strptime(dt_str[:26] + dt_str[29:], "%Y-%m-%dT%H:%M:%S.%f%z")
        return dt.strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return dt_str


def get_changelog_rows(jira: requests.Session, host: str, issue_keys, fields_include: set, job: dict, progress_cb=None):
    rows = []
    all_fields_seen = set()   # for diagnostics when nothing matches
    total = len(issue_keys)
    job["total_changelog"] = total

    for idx, key in enumerate(issue_keys):
        try:
            # Fetch basic fields first (summary, status, assignee)
            r0 = jira.get(
                f"{host}/rest/api/2/issue/{key}",
                params={"fields": "summary,status,assignee"},
                timeout=REQUEST_TIMEOUT,
            )
            if r0.status_code != 200:
                if progress_cb:
                    progress_cb("changelog", idx + 1, total)
                continue
            issue_fields = r0.json().get("fields", {})
            summary  = issue_fields.get("summary", "")
            status   = (issue_fields.get("status") or {}).get("name", "")
            assignee = (issue_fields.get("assignee") or {}).get("displayName", "")

            # Paginate through ALL changelog entries
            cl_start = 0
            cl_max   = 100
            while True:
                rc = jira.get(
                    f"{host}/rest/api/2/issue/{key}/changelog",
                    params={"startAt": cl_start, "maxResults": cl_max},
                    timeout=REQUEST_TIMEOUT,
                )
                if rc.status_code != 200:
                    break
                cl_data   = rc.json()
                histories = cl_data.get("values", [])   # dedicated endpoint uses "values"
                if not histories:
                    break

                for history in histories:
                    author  = history.get("author", {}).get("displayName", "")
                    created = format_brazil_datetime(history.get("created", ""))
                    for item in history.get("items", []):
                        field_name = item.get("field", "")
                        all_fields_seen.add(field_name)
                        field_id = item.get("fieldId", "")
                        matched = (
                            field_name in fields_include
                            or field_id in fields_include
                            or field_name.lower() in {f.lower() for f in fields_include}
                        )
                        if not matched:
                            continue
                        rows.append({
                            "EPR ID": key,
                            "EPR Title": summary,
                            "Current Status": status,
                            "Current Assignee": assignee,
                            "Author": author,
                            "Date": created,
                            "Field Changed": field_name,
                            "From": item.get("fromString", "") or item.get("from", ""),
                            "To":   item.get("toString",   "") or item.get("to",   ""),
                        })

                cl_start += cl_max
                cl_total  = cl_data.get("total", 0)
                if cl_start >= cl_total:
                    break

        except Exception as e:
            job.setdefault("warnings", []).append(f"{key}: {e}")

        job["done_changelog"] = idx + 1
        job["row_count"]      = len(rows)
        if progress_cb:
            progress_cb("changelog", idx + 1, total)

    job["fields_seen"] = sorted(all_fields_seen)
    return rows


# ─── Background job ──────────────────────────────────────────────────────────

def run_job(job_id, pat, host, jql, field_name):
    job = jobs[job_id]
    job["status"]         = "running"
    job["phase"]          = "issues"
    job["found"]          = 0
    job["total"]          = 0
    job["done_changelog"] = 0
    job["total_changelog"]= 0
    job["row_count"]      = 0
    job["rows"]           = []
    job["fields_seen"]    = []

    # Support comma-separated list of field names
    fields_include = {f.strip() for f in field_name.split(",") if f.strip()}
    if not fields_include:
        fields_include = {"EPR-Classification"}

    try:
        jira = make_session(pat)

        def progress_issues(phase, done, total):
            job["found"] = done
            job["total"] = total

        issue_keys, total = search_issues(jira, host, jql, progress_issues)
        job["found"] = len(issue_keys)
        job["total"] = total
        job["phase"] = "changelog"

        def progress_cl(phase, done, total):
            pass  # job dict updated directly inside get_changelog_rows

        rows = get_changelog_rows(jira, host, issue_keys, fields_include, job, progress_cl)
        job["rows"]   = rows
        job["status"] = "done"
        job["phase"]  = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"]  = str(e)


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/connect", methods=["POST"])
def api_connect():
    data = request.json or {}
    pat = data.get("pat", "").strip()
    host = data.get("host", "").strip().rstrip("/")
    if not pat or not host:
        return jsonify({"ok": False, "error": "PAT e Host são obrigatórios."})
    jira = make_session(pat)
    user = get_current_user(jira, host)
    if not user:
        return jsonify({"ok": False, "error": "Token inválido ou host incorreto."})
    projects = get_projects(jira, host)
    return jsonify({"ok": True, "user": user, "projects": projects})


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.json or {}
    pat        = data.get("pat", "").strip()
    host       = data.get("host", "").strip().rstrip("/")
    jql        = data.get("jql", "").strip()
    field_name = data.get("field_name", "EPR-Classification").strip()
    if not pat or not host or not jql:
        return jsonify({"ok": False, "error": "Parâmetros incompletos."})
    job_id = str(uuid.uuid4())
    jobs[job_id] = {}
    t = threading.Thread(target=run_job, args=(job_id, pat, host, jql, field_name), daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job não encontrado."})
    return jsonify({
        "ok":             True,
        "status":         job.get("status"),
        "phase":          job.get("phase"),
        "found":          job.get("found", 0),
        "total":          job.get("total", 0),
        "done_changelog": job.get("done_changelog", 0),
        "total_changelog":job.get("total_changelog", 0),
        "row_count":      job.get("row_count", 0),
        "fields_seen":    job.get("fields_seen", []),
        "error":          job.get("error"),
    })


@app.route("/api/results/<job_id>")
def api_results(job_id):
    job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify({"ok": False})
    rows = job.get("rows", [])
    return jsonify({"ok": True, "rows": rows})


@app.route("/api/download/<job_id>/<fmt>")
def api_download(job_id, fmt):
    job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        return "Not found", 404
    rows = job.get("rows", [])
    if fmt == "csv":
        si = io.StringIO()
        cols = ["EPR ID","EPR Title","Current Status","Current Assignee","Author","Date","Field Changed","From","To"]
        writer = csv.DictWriter(si, fieldnames=cols, delimiter=";", quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        output = io.BytesIO(si.getvalue().encode("utf-8-sig"))
        return send_file(output, mimetype="text/csv",
                         as_attachment=True, download_name="changelog.csv")
    elif fmt == "xlsx":
        try:
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Changelog"
            cols = ["EPR ID","EPR Title","Current Status","Current Assignee","Author","Date","Field Changed","From","To"]
            ws.append(cols)
            for row in rows:
                ws.append([row.get(c, "") for c in cols])
            buf = io.BytesIO()
            wb.save(buf)
            buf.seek(0)
            return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             as_attachment=True, download_name="changelog.xlsx")
        except ImportError:
            return "openpyxl não instalado", 500
    return "Formato inválido", 400


if __name__ == "__main__":
    app.run(debug=True, port=5000)
