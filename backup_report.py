from html import escape
from urllib.parse import urlencode

from backup_store import ACTIVE


def dictionary(row):
    return dict(row) if row else None


def dashboard(db, page):
    jobs = [dict(row) for row in db.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT 20 OFFSET ?", (page * 20,))]
    count = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
    current = db.execute("SELECT * FROM jobs WHERE finished IS NULL ORDER BY id LIMIT 1").fetchone()
    job = current or db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    counts = {}
    file = None
    if job:
        counts = dict(db.execute("SELECT state,count(*) FROM files WHERE job=? GROUP BY state", (job["id"],)))
        marks = ",".join("?" for _ in ACTIVE[1:])
        file = db.execute(f"SELECT path,state,detail,progress FROM files WHERE job=? AND state IN ({marks}) LIMIT 1",
                          (job["id"], *ACTIVE[1:])).fetchone()
    damaged = db.execute("SELECT count(*) FROM archive WHERE healthy=0").fetchone()[0]
    return {"jobs": jobs, "job_count": count, "job": dictionary(job), "counts": counts, "damaged": damaged,
            "file": dictionary(file), "snapshot": dictionary(db.execute(
                "SELECT at,detail FROM events WHERE action='snapshot saved' ORDER BY id DESC LIMIT 1").fetchone()),
            "maintenance": dictionary(db.execute(
                """SELECT at,action,detail FROM events WHERE action IN ('snapshot saved','maintenance error')
                   ORDER BY id DESC LIMIT 1""").fetchone())}


def report(db, job_id, state, page, events=False):
    job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise ValueError("Unknown job")
    params = [job_id]
    condition = "job=?"
    if state and not events:
        condition += " AND state=?"
        params.append(state)
    table = "events" if events else "files"
    columns = "at,action,file,detail" if events else "path,state,hash,destination,detail"
    fields = columns
    if not events:
        fields += """, (SELECT CASE healthy WHEN 1 THEN 'Verified' ELSE 'Needs repair' END
                        FROM archive WHERE archive.hash=files.hash) AS archive_health"""
    rows = db.execute(f"SELECT {fields} FROM {table} WHERE {condition} ORDER BY id LIMIT 100 OFFSET ?",
                      (*params, page * 100)).fetchall()
    count = db.execute(f"SELECT count(*) FROM {table} WHERE {condition}", params).fetchone()[0]
    def link(label, **changes):
        args = {"job": job_id, "state": state, "page": page, "events": int(events), **changes}
        return f'<a href="?{escape(urlencode(args))}">{escape(label)}</a>'
    filters = [link("All files", state="", events=0, page=0), link("Event log", events=1, page=0)]
    for row in db.execute("SELECT state,count(*) FROM files WHERE job=? GROUP BY state", (job_id,)):
        filters.append(link(f"{row[0]} ({row[1]})", state=row[0], events=0, page=0))
    heading = f"{job['kind'].title()} report · Job {job_id}"
    body = [f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{heading}</title>',
            "<style>body{font:16px/1.5 system-ui;color:#173438;margin:3vw}a{color:#176564}nav{display:flex;gap:1em;flex-wrap:wrap}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:.65em;border-bottom:1px solid #ccd8d8;overflow-wrap:anywhere}th{background:#eff5f4}section{overflow:auto}</style>",
            '<a href="/auto-backup/output/">Auto Backup</a>', f"<h1>{heading}</h1>",
            f"<p>{escape(job['folder'])} · {escape(job['state'])} · {escape(job['created'])} UTC</p>",
            f"<p>{escape(job['error'])}</p>" if job["error"] else "",
            "<nav>" + " ".join(filters) + "</nav>",
            f"<p>{count} records · Page {page+1}</p><section><table><thead><tr>",
            "".join(f"<th>{col.title()}</th>" for col in columns.split(",")),
            "" if events else "<th>Current archive health</th>", "</tr></thead><tbody>"]
    for row in rows:
        body.append("<tr>" + "".join(f"<td>{escape(str(v if v is not None else ''))}</td>" for v in row) + "</tr>")
    body.append("</tbody></table></section><nav>")
    if page:
        body.append(link("Previous", page=page-1))
    if (page+1)*100 < count:
        body.append(link("Next", page=page+1))
    return "".join(body) + "</nav></html>"
