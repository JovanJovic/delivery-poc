from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import List, Optional
import pandas as pd
import io
import secrets
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus
import base64
import os

from google.cloud import firestore
from google.cloud import storage


app = FastAPI()

RUNS_CACHE: dict = {}

REQUIRED_COLS = [
    "Order", "PC", "CP", "PL", "LP",
    "Account Name", "Del Suburb", "Street", "Building"
]

db = firestore.Client()
gcs = storage.Client()
POD_BUCKET = os.environ.get("POD_BUCKET", "")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_utc_iso() -> str:
    return now_utc().isoformat()


def parse_iso(dt: Optional[str]) -> Optional[datetime]:
    if not dt:
        return None
    try:
        return datetime.fromisoformat(dt)
    except Exception:
        return None


def safe_str(x) -> str:
    if x is None:
        return ""
    s = str(x).strip()
    if s.lower() == "nan":
        return ""
    return s


def to_int(x) -> int:
    try:
        if pd.isna(x):
            return 0
        return int(x)
    except Exception:
        return 0


def get_base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip("/")


def driver_url(base_url: str, token: str) -> str:
    return f"{base_url}/run/{token}?tab=pending"


def qr_img(url: str, px: int = 220) -> str:
    return (
        f'<img alt="QR Code" '
        f'style="width:{px}px;height:{px}px;border-radius:14px;border:1px solid rgba(255,255,255,0.12);" '
        f'src="https://api.qrserver.com/v1/create-qr-code/?size={px}x{px}&data={quote_plus(url)}">'
    )


def run_doc(token: str):
    return db.collection("runs").document(token)


def order_doc(token: str, order_no: str):
    return run_doc(token).collection("orders").document(str(order_no))


def save_run_to_firestore(
    token: str,
    created: str,
    expires_at: str,
    run_number: str,
    driver_email: str,
    pod_email: str,
    orders: list[dict]
):
    run_doc(token).set({
        "created": created,
        "expires_at": expires_at,
        "run_number": run_number,
        "driver_email": driver_email,
        "pod_email": pod_email,
        "updated": now_utc_iso(),
    }, merge=True)

    batch = db.batch()
    for o in orders:
        batch.set(order_doc(token, o["Order"]), o, merge=True)
    batch.commit()


def load_run_from_firestore(token: str) -> Optional[dict]:
    r = run_doc(token).get()
    if not r.exists:
        return None

    run = r.to_dict() or {}
    orders = []
    for s in run_doc(token).collection("orders").stream():
        d = s.to_dict()
        if d:
            orders.append(d)
    orders.sort(key=lambda x: x.get("_idx", 0))
    run["orders"] = orders
    return run


def update_order_firestore(token: str, order_no: str, patch: dict):
    patch["last_update"] = now_utc_iso()
    order_doc(token, order_no).set(patch, merge=True)
    run_doc(token).set({"updated": now_utc_iso()}, merge=True)


def gcs_public_url(bucket: str, object_name: str) -> str:
    return f"https://storage.googleapis.com/{bucket}/{quote_plus(object_name)}"


def upload_bytes_to_gcs(bucket: str, object_name: str, data: bytes, content_type: str) -> str:
    if not bucket:
        raise RuntimeError("POD_BUCKET env var is not set")
    b = gcs.bucket(bucket)
    blob = b.blob(object_name)
    blob.upload_from_string(data, content_type=content_type)
    return gcs_public_url(bucket, object_name)


def is_expired(run: dict) -> bool:
    exp = parse_iso(run.get("expires_at"))
    if not exp:
        return False
    return now_utc() > exp


def summarize_orders(orders: list[dict]) -> dict:
    pending = [o for o in orders if o.get("state") == "PENDING"]
    delivered = [o for o in orders if o.get("state") == "DELIVERED"]
    undelivered = [o for o in orders if o.get("state") == "UNDELIVERED"]

    total_orders = len(orders)
    remaining_orders = len(pending)
    delivered_orders = len(delivered)
    undelivered_orders = len(undelivered)
    pc_undelivered = sum(int(o.get("PC", 0) or 0) for o in undelivered)

    done_orders = total_orders - remaining_orders
    progress = 0 if total_orders == 0 else int((done_orders / total_orders) * 100)

    return {
        "pending": pending,
        "delivered": delivered,
        "undelivered": undelivered,
        "total_orders": total_orders,
        "remaining_orders": remaining_orders,
        "delivered_orders": delivered_orders,
        "undelivered_orders": undelivered_orders,
        "pc_undelivered": pc_undelivered,
        "progress": progress,
    }


def load_run_cached(token: str) -> Optional[dict]:
    run = RUNS_CACHE.get(token)
    if not run:
        run = load_run_from_firestore(token)
        if run:
            RUNS_CACHE[token] = run
    return run


def delete_run_everywhere(token: str):
    for s in run_doc(token).collection("orders").stream():
        s.reference.delete()

    if POD_BUCKET:
        bucket = gcs.bucket(POD_BUCKET)
        prefix = f"runs/{token}/"
        blobs = list(gcs.list_blobs(bucket, prefix=prefix))
        for blob in blobs:
            blob.delete()

    run_doc(token).delete()
    RUNS_CACHE.pop(token, None)


def css() -> str:
    return """
    <style>
      :root{
        --bg:#0b1220;
        --card:#102341;
        --text:#f8fafc;
        --muted:#b8c2d1;
        --border:rgba(255,255,255,0.12);
        --blueCard:#12355f;
        --good:#16a34a;
        --bad:#ef4444;
        --warn:#f59e0b;
      }
      body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;background:var(--bg);color:var(--text);margin:0;}
      .wrap{max-width:980px;margin:0 auto;padding:14px;}
      .topbar{
        position:sticky; top:0; z-index:10;
        background:rgba(11,18,32,0.92);
        backdrop-filter:blur(10px);
        padding:10px 0;
        border-bottom:1px solid var(--border);
      }
      h2{margin:0;font-size:20px;letter-spacing:0.2px}
      .row{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;}
      .pill{
        font-size:12px;padding:7px 12px;border-radius:999px;
        border:1px solid var(--border);
        background:rgba(255,255,255,0.06);
        font-weight:900;
      }
      .pill.good{background:rgba(22,163,74,0.18); border-color:rgba(22,163,74,0.55)}
      .pill.bad{background:rgba(239,68,68,0.18); border-color:rgba(239,68,68,0.55)}
      .pill.warn{background:rgba(245,158,11,0.18); border-color:rgba(245,158,11,0.55)}
      .card{
        border:1px solid var(--border);
        border-radius:18px;
        padding:14px;
        margin:12px 0;
        box-shadow:0 10px 24px rgba(0,0,0,0.35);
        background:linear-gradient(180deg,var(--card),rgba(16,35,65,0.70));
      }
      .card.pending{
        background:linear-gradient(180deg,var(--blueCard),rgba(16,35,65,0.70));
        border-color:rgba(96,165,250,0.35);
      }
      .order{font-size:16px;font-weight:900}
      .suburb{margin-top:6px;font-size:16px;font-weight:900}
      .addr{margin-top:4px;font-size:14px;color:var(--text)}
      .acct{margin-top:8px;font-size:14px;font-weight:900;color:#e2e8f0}
      .meta{margin-top:8px;font-size:13px;color:var(--muted);font-weight:800}
      .btnrow{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
      .btn{
        padding:14px 16px;border-radius:16px;border:1px solid var(--border);
        background:rgba(255,255,255,0.06);color:var(--text);
        font-size:15px;font-weight:900
      }
      .btn.primary{background:#ffffff;color:#0b1220;border-color:#ffffff}
      .btn.good{background:var(--good);border-color:var(--good)}
      .btn.bad{background:var(--bad);border-color:var(--bad)}
      .btn.full{flex:1 1 160px}
      input,select,textarea{
        width:100%;padding:14px;border-radius:16px;
        border:1px solid var(--border);
        background:rgba(255,255,255,0.06);
        color:var(--text);font-size:16px;font-weight:700
      }
      textarea{min-height:90px}
      a{color:inherit;text-decoration:none}
      .small{font-size:12px;color:var(--muted);margin-top:10px}
      .bar{height:10px;background:rgba(255,255,255,0.12);border-radius:999px;overflow:hidden;margin-top:10px}
      .bar > div{height:100%;background:linear-gradient(90deg,#2b76c8,#60a5fa);width:0%}
      .tabs{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
      .tab{
        padding:10px 12px;border-radius:999px;border:1px solid var(--border);
        background:rgba(255,255,255,0.06);
        font-weight:900;font-size:13px;
      }
      .tab.active{background:#ffffff;color:#0b1220;border-color:#ffffff}
      .thumbs{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}
      .thumbs img{width:110px;height:110px;object-fit:cover;border-radius:14px;border:1px solid var(--border)}
      canvas{width:100%;height:220px;border:1px solid var(--border);border-radius:16px;touch-action:none;background:rgba(255,255,255,0.03)}
      .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
      .print-page{page-break-after:always;min-height:100vh;padding:20px}
      .print-card{max-width:900px;margin:0 auto}
      .print-qr{text-align:center;margin-top:20px}
      .print-title{text-align:center;margin-top:10px;font-size:28px;font-weight:900}
      .print-meta{margin-top:22px;font-size:16px;line-height:1.7}
      @media print{
        body{background:#fff;color:#000}
        .no-print{display:none !important}
        .print-page{page-break-after:always}
      }
      @media (max-width: 480px){
        .wrap{padding:12px}
        .card{padding:12px; margin:10px 0; border-radius:16px}
        .btn{padding:12px 14px;font-size:14px}
        .tab{padding:9px 10px;font-size:12px}
        .grid2{grid-template-columns:1fr}
      }
    </style>
    """


DELIVER_SCRIPT = """
<script>
const photoInput = document.getElementById('photoInput');
const previewWrap = document.getElementById('previewWrap');
const thumbs = document.getElementById('thumbs');

photoInput.addEventListener('change', () => {
  thumbs.innerHTML = '';
  const files = photoInput.files;
  if (!files || files.length === 0) {
    previewWrap.style.display = 'none';
    return;
  }
  previewWrap.style.display = 'block';
  for (let i = 0; i < files.length; i++) {
    const url = URL.createObjectURL(files[i]);
    const img = document.createElement('img');
    img.src = url;
    thumbs.appendChild(img);
  }
});

const canvas = document.getElementById('sig');
const ctx = canvas.getContext('2d');
ctx.lineWidth = 3;
ctx.lineCap = 'round';

let drawing = false;
let hasInk = false;

function pos(e){
  const r = canvas.getBoundingClientRect();
  const t = e.touches ? e.touches[0] : null;
  const cx = t ? t.clientX : e.clientX;
  const cy = t ? t.clientY : e.clientY;
  return {
    x:(cx - r.left) * (canvas.width / r.width),
    y:(cy - r.top) * (canvas.height / r.height)
  };
}
function start(e){
  drawing = true;
  const p = pos(e);
  ctx.beginPath();
  ctx.moveTo(p.x,p.y);
  e.preventDefault();
}
function move(e){
  if(!drawing) return;
  const p = pos(e);
  ctx.lineTo(p.x,p.y);
  ctx.stroke();
  hasInk = true;
  e.preventDefault();
}
function end(e){
  drawing = false;
  e.preventDefault();
}
canvas.addEventListener('mousedown', start);
canvas.addEventListener('mousemove', move);
canvas.addEventListener('mouseup', end);
canvas.addEventListener('mouseleave', end);
canvas.addEventListener('touchstart', start, {passive:false});
canvas.addEventListener('touchmove', move, {passive:false});
canvas.addEventListener('touchend', end, {passive:false});

document.getElementById('clearSig').addEventListener('click', () => {
  ctx.clearRect(0,0,canvas.width,canvas.height);
  hasInk = false;
});

document.getElementById('deliverForm').addEventListener('submit', () => {
  if(hasInk){
    document.getElementById('signature_data').value = canvas.toDataURL('image/png');
  } else {
    document.getElementById('signature_data').value = '';
  }
});
</script>
"""

UNDELIVER_SCRIPT = """
<script>
const reason = document.getElementById('reason');
const otherWrap = document.getElementById('otherWrap');
function update(){ otherWrap.style.display = 'block'; }
reason.addEventListener('change', update);
update();
</script>
"""


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1">{css()}</head>
    <body>
      <div class="wrap">
        <h2>Delivery PoC</h2>
        <div class="small">Running ✅</div>
        <div class="btnrow" style="margin-top:16px;">
          <a href="/admin"><button class="btn primary full">CREATE RUN</button></a>
          <a href="/dashboard"><button class="btn full">MANAGER DASHBOARD</button></a>
        </div>
      </div>
    </body></html>
    """)


@app.get("/admin", response_class=HTMLResponse)
def admin():
    runs_html = ""
    try:
        snaps = db.collection("runs").order_by("updated", direction=firestore.Query.DESCENDING).limit(10).stream()
        for s in snaps:
            token = s.id
            r = s.to_dict() or {}
            created = r.get("created", "")
            driver_email = r.get("driver_email", "")
            run_number = safe_str(r.get("run_number"))
            expired = is_expired(r)
            pill = '<div class="pill warn">EXPIRED</div>' if expired else '<div class="pill good">ACTIVE</div>'
            try:
                count = sum(1 for _ in run_doc(token).collection("orders").stream())
            except Exception:
                count = 0

            runs_html += f"""
            <div class="card pending" style="margin-top:12px;">
              <div class="row">
                <div class="order">Run {run_number}</div>
                <div class="row" style="gap:8px;">
                  <div class="pill">ORDERS {count}</div>
                  {pill}
                </div>
              </div>
              <div class="small">Created: {created}</div>
              <div class="small">Driver: {driver_email if driver_email else '-'}</div>
              <div class="btnrow" style="margin-top:10px;">
                <a href="/run/{token}?tab=pending"><button class="btn primary full">OPEN RUN</button></a>
                <a href="/dashboard/print/{token}"><button class="btn full">PRINT QR</button></a>
              </div>
            </div>
            """
    except Exception as e:
        runs_html = f'<div class="card pending" style="margin-top:12px;"><b>Error loading runs:</b> {safe_str(e)}</div>'

    if not runs_html:
        runs_html = '<div class="card pending" style="margin-top:12px;"><b>No runs yet.</b></div>'

    return HTMLResponse(f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1">{css()}</head>
    <body>
      <div class="wrap">
        <h2>Create Run</h2>

        <div class="card pending" style="margin-top:12px;">
          <form action="/upload" method="post" enctype="multipart/form-data">
            <div class="small">Run Number (3–5 digits)</div>
            <input type="number" name="run_number" min="100" max="99999" required><br><br>

            <div class="small">Upload Excel (headers in row 2)</div>
            <input type="file" name="file" accept=".xlsx" required><br><br>

            <div class="small">Driver email (optional)</div>
            <input type="email" name="driver_email"><br><br>

            <div class="small">POD report email (required)</div>
            <input type="email" name="pod_email" required><br><br>

            <button class="btn primary full" type="submit">UPLOAD RUN</button>
          </form>
        </div>

        <h2 style="margin-top:18px; font-size:18px;">Recent runs</h2>
        {runs_html}
      </div>
    </body></html>
    """)


@app.post("/upload", response_class=HTMLResponse)
async def upload_run(
    request: Request,
    file: UploadFile = File(...),
    run_number: str = Form(...),
    driver_email: str = Form(""),
    pod_email: str = Form(...)
):
    run_number = run_number.strip()

    if not run_number.isdigit() or len(run_number) < 3 or len(run_number) > 5:
        return HTMLResponse(f"""
        <html><head><meta name="viewport" content="width=device-width, initial-scale=1">{css()}</head>
        <body><div class="wrap">
          <div class="card pending">
            <h2 style="font-size:18px;">Invalid run number</h2>
            <div class="small">Run number must be 3 to 5 digits.</div>
            <div class="btnrow"><a href="/admin"><button class="btn full">BACK</button></a></div>
          </div>
        </div></body></html>
        """, status_code=400)

    today = now_utc().date()
    existing = db.collection("runs").where("run_number", "==", run_number).stream()
    for r in existing:
        data = r.to_dict() or {}
        created = parse_iso(data.get("created"))
        if created and created.date() == today:
            return HTMLResponse(f"""
            <html><head><meta name="viewport" content="width=device-width, initial-scale=1">{css()}</head>
            <body><div class="wrap">
              <div class="card pending">
                <h2 style="font-size:18px;">Run already exists today</h2>
                <div class="small">Run {run_number} already exists today. Delete it first.</div>
                <div class="btnrow"><a href="/admin"><button class="btn full">BACK</button></a></div>
              </div>
            </div></body></html>
            """, status_code=400)

    data = await file.read()
    df = pd.read_excel(io.BytesIO(data), header=1)
    df.columns = [str(c).strip() for c in df.columns]

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        return HTMLResponse(f"""
        <html><head><meta name="viewport" content="width=device-width, initial-scale=1">{css()}</head>
        <body><div class="wrap">
          <div class="card pending">
            <h2 style="font-size:18px;">Upload failed</h2>
            <div class="small">Missing columns: {missing}</div>
            <div class="small">Found columns: {list(df.columns)}</div>
            <div class="btnrow"><a href="/admin"><button class="btn full">BACK</button></a></div>
          </div>
        </div></body></html>
        """, status_code=400)

    orders = []
    for idx, (_, row) in enumerate(df.iterrows()):
        order_no = safe_str(row["Order"])
        if not order_no:
            continue

        orders.append({
            "_idx": idx,
            "Order": order_no,
            "Account": safe_str(row["Account Name"]),
            "Suburb": safe_str(row["Del Suburb"]),
            "Street": safe_str(row["Street"]),
            "Building": safe_str(row["Building"]),
            "PC": to_int(row["PC"]),
            "CP": to_int(row["CP"]),
            "PL": to_int(row["PL"]),
            "LP": to_int(row["LP"]),
            "state": "PENDING",
            "last_update": None,
            "undelivered_reason": None,
            "undelivered_note": None,
            "pod_photos": [],
            "signature_url": None,
        })

    token = secrets.token_urlsafe(16)
    created = now_utc_iso()
    expires_at = (now_utc() + timedelta(hours=48)).isoformat()

    save_run_to_firestore(token, created, expires_at, run_number, driver_email, pod_email, orders)

    RUNS_CACHE[token] = {
        "created": created,
        "expires_at": expires_at,
        "run_number": run_number,
        "driver_email": driver_email,
        "pod_email": pod_email,
        "orders": orders
    }

    base = get_base_url(request)
    url = driver_url(base, token)

    return HTMLResponse(f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1">{css()}</head>
    <body>
      <div class="wrap">
        <div class="card pending">
          <div class="row">
            <div class="order">Run {run_number} created ✅</div>
            <div class="pill good">ACTIVE 48H</div>
          </div>

          <div class="small" style="margin-top:10px;">Driver link:</div>
          <div style="word-break:break-all; margin-top:6px;">
            <a href="{url}"><b>{url}</b></a>
          </div>

          <div class="small" style="margin-top:8px;">Expires at: {expires_at}</div>

          <div class="small" style="margin-top:14px;">QR code (driver scans this):</div>
          <div style="margin-top:10px;">
            {qr_img(url, 260)}
          </div>

          <div class="btnrow" style="margin-top:14px;">
            <a href="{url}"><button class="btn primary full">OPEN DRIVER PAGE</button></a>
            <a href="/dashboard/print/{token}"><button class="btn full">PRINT QR</button></a>
            <a href="/dashboard"><button class="btn full">DASHBOARD</button></a>
          </div>
        </div>
      </div>
    </body></html>
    """)


@app.get("/run/{token}", response_class=HTMLResponse)
def driver_run(token: str, tab: str = "pending"):
    run = load_run_cached(token)

    if not run:
        html = """
        <html><head><meta name="viewport" content="width=device-width, initial-scale=1">__CSS__</head>
        <body>
          <div class="wrap">
            <h2>Offline / Link expired</h2>
            <div class="card pending" style="margin-top:12px;">
              <div class="small">
                If you have no signal OR the run was reset, load the last cached copy on this phone.
              </div>
              <div class="btnrow" style="margin-top:12px;">
                <button class="btn primary full" onclick="loadCache()">LOAD CACHED RUN</button>
              </div>
              <div class="small" id="ts" style="margin-top:10px;"></div>
            </div>
          </div>
          <script>
            function loadCache(){
              try{
                const html = localStorage.getItem("poc_run_cache___TOKEN__");
                if(html){
                  document.open();
                  document.write(html);
                  document.close();
                  setTimeout(() => {
                    try{
                      const banner = document.createElement('div');
                      banner.textContent = "OFFLINE MODE – changes won’t send";
                      banner.style.position = "fixed";
                      banner.style.top = "0";
                      banner.style.left = "0";
                      banner.style.right = "0";
                      banner.style.zIndex = "99999";
                      banner.style.padding = "12px 14px";
                      banner.style.fontWeight = "900";
                      banner.style.textAlign = "center";
                      banner.style.background = "#f59e0b";
                      banner.style.color = "#0b1220";
                      banner.style.borderBottom = "2px solid rgba(0,0,0,0.25)";
                      document.body.appendChild(banner);
                    }catch(e){}
                  }, 50);
                  return;
                }
                alert("No cached copy found on this phone.");
              }catch(e){
                alert("Cache not available.");
              }
            }
            try{
              const ts = localStorage.getItem("poc_run_cache_ts___TOKEN__");
              if(ts) document.getElementById("ts").textContent = "Cached at: " + ts;
            }catch(e){}
          </script>
        </body></html>
        """
        html = html.replace("__CSS__", css()).replace("___TOKEN__", token)
        return HTMLResponse(html, status_code=404)

    if is_expired(run):
        return HTMLResponse(f"""
        <html><head><meta name="viewport" content="width=device-width, initial-scale=1">{css()}</head>
        <body>
          <div class="wrap">
            <div class="card pending">
              <div class="row">
                <div class="order">Run expired</div>
                <div class="pill warn">EXPIRED</div>
              </div>
              <div class="small">This run link expired after 48 hours.</div>
              <div class="small">Run number: {safe_str(run.get("run_number"))}</div>
              <div class="small">Created: {run.get("created","")}</div>
              <div class="small">Expired: {run.get("expires_at","")}</div>
            </div>
          </div>
        </body></html>
        """, status_code=410)

    s = summarize_orders(run["orders"])
    tab = tab if tab in ("pending", "delivered", "undelivered") else "pending"
    list_to_show = s["pending"] if tab == "pending" else (s["delivered"] if tab == "delivered" else s["undelivered"])

    tabs_html = f"""
      <div class="tabs">
        <a class="tab {'active' if tab=='pending' else ''}" href="/run/{token}?tab=pending">PENDING</a>
        <a class="tab {'active' if tab=='delivered' else ''}" href="/run/{token}?tab=delivered">DELIVERED</a>
        <a class="tab {'active' if tab=='undelivered' else ''}" href="/run/{token}?tab=undelivered">UNDELIVERED</a>
      </div>
    """

    summary_card = f"""
      <div class="card pending">
        <div class="row">
          <div class="order">Run {safe_str(run.get('run_number'))}</div>
          <div class="pill">PROGRESS {s['progress']}%</div>
        </div>

        <div class="meta" style="margin-top:10px;">
          Total: <b>{s['total_orders']}</b> &nbsp;&nbsp;|&nbsp;&nbsp;
          Remaining: <b>{s['remaining_orders']}</b> &nbsp;&nbsp;|&nbsp;&nbsp;
          PC Undelivered: <b>{s['pc_undelivered']}</b>
        </div>

        <div class="bar"><div style="width:{s['progress']}%"></div></div>

        {tabs_html}
      </div>
    """

    cards_html = ""
    if not list_to_show:
        msg = "No pending orders." if tab == "pending" else ("No delivered orders." if tab == "delivered" else "No undelivered orders.")
        cards_html = f'<div class="card pending"><b>{msg}</b></div>'
    else:
        for o in list_to_show:
            addr2 = (safe_str(o.get("Street")) + (" · " + safe_str(o.get("Building")) if safe_str(o.get("Building")) else "")).strip()
            status = o.get("state", "PENDING")
            order_no = safe_str(o.get("Order"))

            if status == "PENDING":
                pill = '<div class="pill">PENDING</div>'
                card_class = "card pending"
            elif status == "DELIVERED":
                pill = '<div class="pill good">DELIVERED</div>'
                card_class = "card"
            else:
                pill = '<div class="pill bad">UNDELIVERED</div>'
                card_class = "card"

            cards_html += f"""
            <a href="/run/{token}/order/{order_no}">
              <div class="{card_class}">
                <div class="row">
                  <div class="order">Order {order_no}</div>
                  {pill}
                </div>
                <div class="suburb">{safe_str(o.get("Suburb"))}</div>
                <div class="addr">{addr2}</div>
                <div class="acct">{safe_str(o.get("Account"))}</div>
                <div class="meta">PC:{o.get('PC',0)} &nbsp; CP:{o.get('CP',0)} &nbsp; PL:{o.get('PL',0)} &nbsp; LP:{o.get('LP',0)}</div>
              </div>
            </a>
            """

    cache_script = """
<script>
(function(){
  try {
    localStorage.setItem("poc_run_cache___TOKEN__", document.documentElement.outerHTML);
    localStorage.setItem("poc_run_cache_ts___TOKEN__", new Date().toISOString());
  } catch(e) {}
})();
</script>
""".replace("___TOKEN__", token)

    return HTMLResponse(f"""
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Driver Run</title>
      {css()}
    </head>
    <body>
      <div class="topbar">
        <div class="wrap">
          {summary_card}
        </div>
      </div>

      <div class="wrap">
        {cards_html}
        <div class="small">Offline-friendly: this page is cached on your phone when it loads.</div>
      </div>

      {cache_script}
    </body>
    </html>
    """)


@app.get("/run/{token}/order/{order_no}", response_class=HTMLResponse)
def order_detail(token: str, order_no: str):
    run = load_run_cached(token)
    if not run:
        return HTMLResponse("<h3>Bad or expired link</h3>", status_code=404)

    order = next((x for x in run["orders"] if safe_str(x.get("Order")) == str(order_no)), None)
    if not order:
        return HTMLResponse("<h3>Order not found</h3>", status_code=404)

    status = order.get("state", "PENDING")
    if status == "DELIVERED":
        pill = '<div class="pill good">DELIVERED</div>'
        back_tab = "delivered"
    elif status == "UNDELIVERED":
        pill = '<div class="pill bad">UNDELIVERED</div>'
        back_tab = "undelivered"
    else:
        pill = '<div class="pill">PENDING</div>'
        back_tab = "pending"

    addr2 = (safe_str(order.get("Street")) + (" · " + safe_str(order.get("Building")) if safe_str(order.get("Building")) else "")).strip()

    return HTMLResponse(f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1">{css()}</head>
    <body>
      <div class="wrap">
        <div class="row">
          <h2>Order {order_no}</h2>
          {pill}
        </div>

        <div class="card pending" style="margin-top:12px;">
          <div class="suburb" style="margin-top:0;">{safe_str(order.get('Suburb'))}</div>
          <div class="addr">{addr2}</div>
          <div class="acct">{safe_str(order.get('Account'))}</div>
          <div class="meta">PC:{order.get('PC',0)} &nbsp; CP:{order.get('CP',0)} &nbsp; PL:{order.get('PL',0)} &nbsp; LP:{order.get('LP',0)}</div>
        </div>

        <div class="btnrow">
          <a href="/run/{token}/order/{order_no}/nav"><button class="btn primary full">NAVIGATE</button></a>
          <a href="/run/{token}/order/{order_no}/deliver"><button class="btn good full">DELIVER</button></a>
          <a href="/run/{token}/order/{order_no}/undeliver"><button class="btn bad full">NO DELIVERY</button></a>
        </div>

        <div class="btnrow" style="margin-top:12px;">
          <a href="/run/{token}?tab={back_tab}"><button class="btn full">BACK</button></a>
        </div>
      </div>
    </body></html>
    """)


@app.get("/run/{token}/order/{order_no}/nav")
def nav_redirect(token: str, order_no: str):
    run = load_run_cached(token)
    if not run:
        return HTMLResponse("<h3>Bad or expired link</h3>", status_code=404)

    order = next((x for x in run["orders"] if safe_str(x.get("Order")) == str(order_no)), None)
    if not order:
        return HTMLResponse("<h3>Order not found</h3>", status_code=404)

    addr = " ".join([p for p in [safe_str(order.get("Street")), safe_str(order.get("Building")), safe_str(order.get("Suburb"))] if p]).strip()
    if not addr:
        return HTMLResponse("<h3>No address available</h3>", status_code=400)

    url = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(addr)
    return RedirectResponse(url, status_code=302)


@app.get("/run/{token}/order/{order_no}/deliver", response_class=HTMLResponse)
def deliver_page(token: str, order_no: str):
    return HTMLResponse(f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1">{css()}</head>
    <body>
      <div class="wrap">
        <h2>Deliver</h2>

        <div class="card pending" style="margin-top:12px;">
          <form id="deliverForm" action="/run/{token}/order/{order_no}/deliver" method="post" enctype="multipart/form-data">

            <div class="small">Photos (mandatory)</div>
            <input id="photoInput" type="file" name="photos" accept="image/*" capture="environment" multiple required>

            <div id="previewWrap" style="display:none;">
              <div class="small" style="margin-top:10px;">Preview</div>
              <div id="thumbs" class="thumbs"></div>
            </div>

            <div class="small" style="margin-top:14px;">Signature (optional)</div>
            <canvas id="sig" width="700" height="220"></canvas>

            <div class="btnrow">
              <button id="clearSig" class="btn full" type="button">CLEAR SIGNATURE</button>
            </div>

            <input type="hidden" name="signature_data" id="signature_data" value="">

            <div class="btnrow" style="margin-top:12px;">
              <button class="btn good full" type="submit">PUSH ORDER</button>
              <a href="/run/{token}/order/{order_no}"><button class="btn full" type="button">CANCEL</button></a>
            </div>

            <div class="small">Photo required. Signature optional.</div>
          </form>
        </div>
      </div>

      {DELIVER_SCRIPT}
    </body></html>
    """)


@app.post("/run/{token}/order/{order_no}/deliver")
async def deliver_submit(
    token: str,
    order_no: str,
    photos: List[UploadFile] = File(...),
    signature_data: str = Form("")
):
    run = load_run_cached(token)
    if not run:
        return HTMLResponse("<h3>Bad or expired link</h3>", status_code=404)

    if is_expired(run):
        return HTMLResponse("<h3>Run expired</h3>", status_code=410)

    if not POD_BUCKET:
        return HTMLResponse("<h3>Server error: POD_BUCKET is not set.</h3>", status_code=500)

    if not photos or len(photos) == 0:
        return HTMLResponse("<h3>At least 1 photo is required.</h3>", status_code=400)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    photo_urls = []
    idx = 0
    for ph in photos:
        data = await ph.read()
        if not data:
            continue
        idx += 1
        ctype = ph.content_type or "image/jpeg"
        obj = f"runs/{token}/orders/{order_no}/photos/{ts}_{idx}.jpg"
        url = upload_bytes_to_gcs(POD_BUCKET, obj, data, content_type=ctype)
        photo_urls.append(url)

    if len(photo_urls) == 0:
        return HTMLResponse("<h3>Photo upload failed (empty files).</h3>", status_code=400)

    signature_url = None
    if signature_data and signature_data.startswith("data:image/png;base64,"):
        try:
            b64 = signature_data.split(",", 1)[1]
            sig_bytes = base64.b64decode(b64)
            if sig_bytes and len(sig_bytes) > 10:
                obj = f"runs/{token}/orders/{order_no}/signature/{ts}.png"
                signature_url = upload_bytes_to_gcs(POD_BUCKET, obj, sig_bytes, content_type="image/png")
        except Exception:
            signature_url = None

    update_order_firestore(token, order_no, {
        "state": "DELIVERED",
        "delivered_ts": now_utc_iso(),
        "pod_photos": photo_urls,
        "signature_url": signature_url,
        "undelivered_reason": None,
        "undelivered_note": None,
    })

    RUNS_CACHE.pop(token, None)
    return RedirectResponse(f"/run/{token}?tab=pending", status_code=303)


@app.get("/run/{token}/order/{order_no}/undeliver", response_class=HTMLResponse)
def undeliver_page(token: str, order_no: str):
    return HTMLResponse(f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1">{css()}</head>
    <body>
      <div class="wrap">
        <h2>No Delivery</h2>

        <div class="card pending" style="margin-top:12px;">
          <form action="/run/{token}/order/{order_no}/undeliver" method="post">
            <div class="small">Reason</div>
            <select id="reason" name="reason_code" required>
              <option value="CUST_NOT_PRESENT">Customer not present</option>
              <option value="OTHER">Other</option>
              <option value="BORED">I could not be bothered ...</option>
            </select>

            <div id="otherWrap" style="margin-top:12px;">
              <div class="small">Other details</div>
              <textarea name="reason_text" placeholder="Type details if needed..."></textarea>
            </div>

            <div class="btnrow" style="margin-top:12px;">
              <button class="btn bad full" type="submit">PUSH</button>
              <a href="/run/{token}/order/{order_no}"><button class="btn full" type="button">CANCEL</button></a>
            </div>
          </form>
        </div>
      </div>

      {UNDELIVER_SCRIPT}
    </body></html>
    """)


@app.post("/run/{token}/order/{order_no}/undeliver")
async def undeliver_submit(
    token: str,
    order_no: str,
    reason_code: str = Form(...),
    reason_text: str = Form("")
):
    run = load_run_cached(token)
    if not run:
        return HTMLResponse("<h3>Bad or expired link</h3>", status_code=404)

    if is_expired(run):
        return HTMLResponse("<h3>Run expired</h3>", status_code=410)

    note = (reason_text or "").strip() if reason_code == "OTHER" else None
    if reason_code == "BORED":
        note = "I could not be bothered ..."

    update_order_firestore(token, order_no, {
        "state": "UNDELIVERED",
        "undelivered_ts": now_utc_iso(),
        "undelivered_reason": reason_code,
        "undelivered_note": note,
        "pod_photos": [],
        "signature_url": None,
    })

    RUNS_CACHE.pop(token, None)
    return RedirectResponse(f"/run/{token}?tab=pending", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    base = get_base_url(request)
    rows = []

    try:
        snaps = db.collection("runs").stream()
        for s in snaps:
            token = s.id
            r = s.to_dict() or {}
            orders = []
            for osnap in run_doc(token).collection("orders").stream():
                od = osnap.to_dict()
                if od:
                    orders.append(od)
            orders.sort(key=lambda x: x.get("_idx", 0))
            r["orders"] = orders
            r["_token"] = token
            r["_expired"] = is_expired(r)
            r["_summary"] = summarize_orders(orders)
            rows.append(r)
    except Exception as e:
        return HTMLResponse(f"""
        <html><head><meta name="viewport" content="width=device-width, initial-scale=1">{css()}</head>
        <body><div class="wrap"><div class="card pending"><b>Dashboard error:</b> {safe_str(e)}</div></div></body></html>
        """, status_code=500)

    def sort_key(r):
        created = parse_iso(r.get("created")) or datetime(1970, 1, 1, tzinfo=timezone.utc)
        return (1 if r["_expired"] else 0, -created.timestamp())

    rows.sort(key=sort_key)

    cards = ""
    for r in rows:
        token = r["_token"]
        summary = r["_summary"]
        url = driver_url(base, token)
        run_number = safe_str(r.get("run_number"))
        exp_pill = '<div class="pill warn">EXPIRED</div>' if r["_expired"] else '<div class="pill good">ACTIVE</div>'

        cards += f"""
        <div class="card pending">
          <div class="row">
            <div class="order">Run {run_number}</div>
            <div class="row" style="gap:8px;">
              {exp_pill}
              <div class="pill">PROGRESS {summary['progress']}%</div>
            </div>
          </div>

          <div class="grid2" style="margin-top:12px;">
            <div>
              <div class="small">Driver</div>
              <div>{safe_str(r.get('driver_email')) if safe_str(r.get('driver_email')) else '-'}</div>

              <div class="small">Created</div>
              <div>{safe_str(r.get('created'))}</div>

              <div class="small">Expires</div>
              <div>{safe_str(r.get('expires_at'))}</div>

              <div class="small">Run link</div>
              <div style="word-break:break-all;">{url}</div>

              <div class="meta">
                Total: {summary['total_orders']} &nbsp;|&nbsp;
                Delivered: {summary['delivered_orders']} &nbsp;|&nbsp;
                Remaining: {summary['remaining_orders']} &nbsp;|&nbsp;
                Undelivered: {summary['undelivered_orders']}
              </div>

              <div class="bar"><div style="width:{summary['progress']}%"></div></div>

              <div class="btnrow">
                <a href="/run/{token}?tab=pending"><button class="btn primary full">OPEN RUN</button></a>
                <a href="/dashboard/run/{token}"><button class="btn full">VIEW POD</button></a>
                <a href="/dashboard/print/{token}"><button class="btn full">PRINT QR</button></a>
                <a href="/dashboard/delete/{token}"><button class="btn bad full">DELETE RUN</button></a>
              </div>
            </div>

            <div style="text-align:center;">
              {qr_img(url, 220)}
            </div>
          </div>
        </div>
        """

    if not cards:
        cards = '<div class="card pending"><b>No runs found.</b></div>'

    return HTMLResponse(f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1">{css()}</head>
    <body>
      <div class="wrap">
        <div class="row">
          <h2>Manager Dashboard</h2>
          <div class="btnrow no-print" style="margin-top:0;">
            <a href="/admin"><button class="btn full">CREATE RUN</button></a>
          </div>
        </div>
        <div class="small">Active runs first, then expired. Newest first inside each group.</div>
        {cards}
      </div>
    </body></html>
    """)


@app.get("/dashboard/run/{token}", response_class=HTMLResponse)
def dashboard_run_detail(token: str):
    run = load_run_from_firestore(token)
    if not run:
        return HTMLResponse("<h3>Run not found</h3>", status_code=404)

    summary = summarize_orders(run["orders"])
    run_number = safe_str(run.get("run_number"))
    expired = is_expired(run)
    status_pill = '<div class="pill warn">EXPIRED</div>' if expired else '<div class="pill good">ACTIVE</div>'

    def render_orders(items: list[dict], title: str, mode: str) -> str:
        if not items:
            return f'<div class="card"><b>No {title.lower()} orders.</b></div>'

        html = f'<h2 style="margin-top:18px;font-size:18px;">{title}</h2>'
        for o in items:
            addr2 = (safe_str(o.get("Street")) + (" · " + safe_str(o.get("Building")) if safe_str(o.get("Building")) else "")).strip()

            extra = ""
            if mode == "delivered":
                photos = o.get("pod_photos") or []
                signature = o.get("signature_url")
                delivered_ts = safe_str(o.get("delivered_ts"))

                links = ""
                for i, p in enumerate(photos, start=1):
                    links += f'<div><a href="{p}" target="_blank">Photo {i}</a></div>'
                if signature:
                    links += f'<div><a href="{signature}" target="_blank">Signature</a></div>'

                extra = f"""
                <div class="small">Delivered at: {delivered_ts}</div>
                <div class="small" style="margin-top:6px;">POD links:</div>
                {links if links else '<div class="small">No POD files</div>'}
                """
            elif mode == "undelivered":
                reason = safe_str(o.get("undelivered_reason"))
                note = safe_str(o.get("undelivered_note"))
                extra = f"""
                <div class="small">Reason: {reason if reason else '-'}</div>
                <div class="small">Note: {note if note else '-'}</div>
                """

            html += f"""
            <div class="card">
              <div class="row">
                <div class="order">Order {safe_str(o.get('Order'))}</div>
                <div class="pill {'good' if mode=='delivered' else ('bad' if mode=='undelivered' else '')}">
                  {safe_str(o.get('state'))}
                </div>
              </div>
              <div class="suburb">{safe_str(o.get('Suburb'))}</div>
              <div class="addr">{addr2}</div>
              <div class="acct">{safe_str(o.get('Account'))}</div>
              <div class="meta">PC:{o.get('PC',0)} &nbsp; CP:{o.get('CP',0)} &nbsp; PL:{o.get('PL',0)} &nbsp; LP:{o.get('LP',0)}</div>
              {extra}
            </div>
            """
        return html

    return HTMLResponse(f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1">{css()}</head>
    <body>
      <div class="wrap">
        <div class="row">
          <h2>Run {run_number}</h2>
          {status_pill}
        </div>

        <div class="card pending">
          <div class="small">Driver: {safe_str(run.get('driver_email')) if safe_str(run.get('driver_email')) else '-'}</div>
          <div class="small">Created: {safe_str(run.get('created'))}</div>
          <div class="small">Expires: {safe_str(run.get('expires_at'))}</div>
          <div class="meta">
            Total: {summary['total_orders']} &nbsp;|&nbsp;
            Delivered: {summary['delivered_orders']} &nbsp;|&nbsp;
            Remaining: {summary['remaining_orders']} &nbsp;|&nbsp;
            Undelivered: {summary['undelivered_orders']}
          </div>
          <div class="bar"><div style="width:{summary['progress']}%"></div></div>
          <div class="btnrow">
            <a href="/dashboard"><button class="btn full">BACK</button></a>
            <a href="/dashboard/print/{token}"><button class="btn full">PRINT QR</button></a>
          </div>
        </div>

        {render_orders(summary['delivered'], "Delivered", "delivered")}
        {render_orders(summary['undelivered'], "Undelivered", "undelivered")}
        {render_orders(summary['pending'], "Pending", "pending")}
      </div>
    </body></html>
    """)


@app.get("/dashboard/delete/{token}", response_class=HTMLResponse)
def confirm_delete_run(token: str):
    run = load_run_from_firestore(token)
    if not run:
        return HTMLResponse("<h3>Run not found</h3>", status_code=404)

    run_number = safe_str(run.get("run_number"))

    return HTMLResponse(f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1">{css()}</head>
    <body>
      <div class="wrap">
        <div class="card pending">
          <h2>Delete Run {run_number}</h2>
          <div class="small">Are you sure you want to delete this run?</div>
          <div class="small">This removes the run, all order documents, and uploaded POD files.</div>

          <form action="/dashboard/delete/{token}" method="post">
            <div class="btnrow">
              <button class="btn bad full" type="submit">YES DELETE RUN</button>
              <a href="/dashboard"><button class="btn full" type="button">CANCEL</button></a>
            </div>
          </form>
        </div>
      </div>
    </body></html>
    """)


@app.post("/dashboard/delete/{token}")
def delete_run_confirmed(token: str):
    run = load_run_from_firestore(token)
    if not run:
        return RedirectResponse("/dashboard", status_code=303)

    delete_run_everywhere(token)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/dashboard/print/{token}", response_class=HTMLResponse)
def dashboard_print_run(request: Request, token: str):
    run = load_run_from_firestore(token)
    if not run:
        return HTMLResponse("<h3>Run not found</h3>", status_code=404)

    base = get_base_url(request)
    url = driver_url(base, token)
    summary = summarize_orders(run["orders"])
    expired = is_expired(run)
    exp_label = "EXPIRED" if expired else "ACTIVE"
    run_number = safe_str(run.get("run_number"))

    return HTMLResponse(f"""
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Print Run {run_number}</title>
      {css()}
    </head>
    <body>
      <div class="print-page">
        <div class="print-card">
          <div class="row no-print">
            <h2>Print Run {run_number}</h2>
            <div class="btnrow" style="margin-top:0;">
              <button class="btn primary" onclick="window.print()">PRINT</button>
              <a href="/dashboard"><button class="btn">BACK</button></a>
            </div>
          </div>

          <div class="print-title">Run {run_number}</div>

          <div class="print-qr">
            {qr_img(url, 340)}
          </div>

          <div class="print-meta">
            <div><b>Status:</b> {exp_label}</div>
            <div><b>Driver:</b> {safe_str(run.get('driver_email')) if safe_str(run.get('driver_email')) else '-'}</div>
            <div><b>Created:</b> {safe_str(run.get('created'))}</div>
            <div><b>Expires:</b> {safe_str(run.get('expires_at'))}</div>
            <div><b>Run link:</b> {url}</div>
            <br>
            <div><b>Total orders:</b> {summary['total_orders']}</div>
            <div><b>Delivered:</b> {summary['delivered_orders']}</div>
            <div><b>Remaining:</b> {summary['remaining_orders']}</div>
            <div><b>Undelivered:</b> {summary['undelivered_orders']}</div>
            <div><b>PC Undelivered:</b> {summary['pc_undelivered']}</div>
            <div><b>Progress:</b> {summary['progress']}%</div>
          </div>
        </div>
      </div>
    </body>
    </html>
    """)