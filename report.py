import os
import csv
import io
import json
import pymysql
import urllib.request
from datetime import datetime, timedelta, timezone
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import (
    Mail, Attachment, FileContent, FileName, FileType, Disposition
)
import base64

# Config
DB_HOST   = os.environ['DB_HOST']
DB_USER   = os.environ['DB_USER']
DB_PASS   = os.environ['DB_PASSWORD']
DB_NAME   = os.environ.get('DB_NAME', 'castabilityprod')
SENDGRID  = os.environ['SENDGRID_API_KEY']
RC_KEY    = os.environ['REVENUE_CAT_API_KEY']
RECIPIENT = os.environ.get('REPORT_RECIPIENT', 'taylor.shurte@castability.actor')
SENDER    = os.environ.get('REPORT_SENDER',    'cronin.cullen@castability.actor')

now      = datetime.now(timezone.utc)
since    = now - timedelta(hours=24)
date_str = now.strftime('%B %-d, %Y')

# DB Query
conn = pymysql.connect(
    host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME,
    charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor
)
cur = conn.cursor()

# New profiles in last 24hrs
cur.execute("""
    SELECT
        pa.id         AS actor_id,
        pa.firstName  AS first_name,
        pa.lastName   AS last_name,
        u.email,
        pa.userId,
        pa.createdAt
    FROM ProfileActors pa
    JOIN Users u ON u.id = pa.userId
    WHERE pa.createdAt >= %s
    ORDER BY pa.createdAt ASC
""", (since,))
new_actors = cur.fetchall()

# New orders/subscriptions in last 24hrs from existing users
cur.execute("""
    SELECT
        oh.userId,
        pa.id        AS actor_id,
        pa.firstName AS first_name,
        pa.lastName  AS last_name,
        u.email,
        oh.productId,
        oh.status,
        oh.createdAt
    FROM OrderHistories oh
    JOIN Users u ON u.id = oh.userId
    JOIN ProfileActors pa ON pa.userId = oh.userId
    WHERE oh.createdAt >= %s
      AND oh.status = 'success'
      AND pa.createdAt < %s
    ORDER BY oh.createdAt ASC
""", (since, since))
new_orders = cur.fetchall()

# ID range for deduction
cur.execute("SELECT MAX(id) FROM ProfileActors")
max_id_row = cur.fetchone()
max_actor_id = list(max_id_row.values())[0] or 'N/A'

cur.execute("SELECT COUNT(*) as cnt FROM ProfileActors WHERE createdAt < %s", (since,))
prev_count = list(cur.fetchone().values())[0] or 0

cur.execute("SELECT COUNT(*) as cnt FROM ProfileActors", )
total_count = list(cur.fetchone().values())[0] or 0

cur.close()
conn.close()

# RevenueCat enrichment
def get_rc(user_id):
    if not user_id:
        return None
    try:
        req = urllib.request.Request(
            f"https://api.revenuecat.com/v1/subscribers/{user_id}",
            headers={'Authorization': f'Bearer {RC_KEY}', 'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None

def rc_active(rc):
    if not rc:
        return []
    subs = rc.get('subscriber', {}).get('subscriptions', {})
    return [v for v in subs.values() if v.get('expires_date') and
            datetime.fromisoformat(v['expires_date'].replace('Z', '+00:00')) > now]

def rc_type(rc):
    active = rc_active(rc)
    if not active:
        return None
    return 'Trial (RC)' if any(s.get('period_type') == 'trial' for s in active) else 'Subscriber (RC)'

def rc_product(rc):
    active = rc_active(rc)
    return active[0].get('product_identifier', '') if active else ''

def rc_expires(rc):
    active = rc_active(rc)
    if not active:
        return ''
    try:
        return datetime.fromisoformat(active[0]['expires_date'].replace('Z', '+00:00')).strftime('%Y-%m-%d')
    except Exception:
        return ''

# Build rows
rows = []
for a in new_actors:
    rc = get_rc(a['userId'])
    t = rc_type(rc) or 'New Profile'
    rows.append([
        a['actor_id'], a['first_name'], a['last_name'], a['email'],
        t, rc_product(rc), rc_expires(rc),
        a['createdAt'].strftime('%Y-%m-%d %H:%M UTC') if a['createdAt'] else ''
    ])

seen_user_ids = {a['userId'] for a in new_actors}
for o in new_orders:
    if o['userId'] in seen_user_ids:
        continue
    rc = get_rc(o['userId'])
    t = rc_type(rc) or f"New Order ({o['productId']})"
    rows.append([
        o['actor_id'], o['first_name'], o['last_name'], o['email'],
        t, rc_product(rc), rc_expires(rc),
        o['createdAt'].strftime('%Y-%m-%d %H:%M UTC') if o['createdAt'] else ''
    ])

n          = len(new_actors)
s          = len(new_orders)
gap        = total_count - prev_count
disc       = gap - n
confidence = 'HIGH' if abs(disc) <= 2 else ('MEDIUM' if abs(disc) <= 5 else 'LOW - please check')
diff       = 'None - exact match' if disc == 0 else f'{abs(disc)} {"missing" if disc > 0 else "extra"}'

# CSV
buf = io.StringIO()
w = csv.writer(buf)
w.writerow(['Actor ID', 'First Name', 'Last Name', 'Email',
            'Type', 'RC Product', 'Subscription Expires', 'Timestamp (UTC)'])
for r in rows:
    w.writerow(r)
csv_data = buf.getvalue()

# Check block
if disc > 2:
    check = f"""<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:4px;padding:10px 14px;font-size:13px;margin-top:8px;">
      <strong>Taylor - please double-check this.</strong><br><br>
      Profile count increased by <strong>{gap}</strong> today but report captured <strong>{n}</strong> new profiles. Difference: <strong>{disc}</strong>.<br><br>
      <strong>How to check (30 seconds):</strong>
      <ol style="margin:8px 0 0 0;padding-left:18px;line-height:1.8;">
        <li>Go to <a href="https://castability.gojilabs.app/admin/actors">castability.gojilabs.app/admin/actors</a></li>
        <li>Sort by "Date Created" - newest first</li>
        <li>Check anyone created today not in the CSV</li>
      </ol><br>
      Most likely: incomplete profile or test account. Flag to Cronin if something looks off.
    </div>"""
elif disc == 0:
    check = '<p style="margin:8px 0 0 0;font-size:13px;color:#2e7d32;"><strong>Numbers match exactly.</strong> CSV has everything.</p>'
else:
    check = f'<p style="margin:8px 0 0 0;font-size:13px;color:#e65100;"><strong>Small difference of {abs(disc)}.</strong> Likely a test account. Probably fine.</p>'

# HTML
html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;padding:24px;color:#222;">
  <h2 style="margin-bottom:4px;">Castability - Daily Signups Report</h2>
  <p style="color:#666;margin-top:0;">{date_str}</p>
  <table style="background:#f5f5f5;border-radius:8px;padding:16px;width:100%;margin-bottom:24px;border-collapse:collapse;">
    <tr><td style="padding:6px 16px 6px 0;color:#666;font-size:14px;">New profiles today</td><td style="font-size:14px;"><strong>{n}</strong></td></tr>
    <tr><td style="padding:6px 16px 6px 0;color:#666;font-size:14px;">New orders / conversions today</td><td style="font-size:14px;"><strong>{s}</strong></td></tr>
    <tr><td style="padding:6px 16px 6px 0;color:#666;font-size:14px;">Total platform profiles</td><td style="font-size:14px;"><strong>{total_count}</strong></td></tr>
    <tr><td style="padding:6px 16px 6px 0;color:#666;font-size:14px;">Profile count growth today</td><td style="font-size:14px;"><strong>{gap}</strong></td></tr>
  </table>
  <div style="background:#fff8e1;border-left:4px solid #f5a623;padding:16px 20px;border-radius:4px;margin-bottom:24px;">
    <p style="margin:0 0 8px 0;font-weight:bold;font-size:15px;">What this tells us - read this first</p>
    <p style="margin:0 0 10px 0;font-size:14px;line-height:1.6;">
      Profile count grew by {gap} today. This report captured {n} new profiles directly.
      The CSV includes RevenueCat data where available - RC Product shows the subscription plan,
      Subscription Expires shows the renewal date.
    </p>
    <table style="border-collapse:collapse;width:100%;font-size:14px;margin-bottom:10px;">
      <tr><td style="padding:4px 12px 4px 0;color:#555;">Profile count yesterday</td><td><strong>{prev_count}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#555;">Profile count today</td><td><strong>{total_count}</strong></td></tr>
      <tr style="border-top:1px solid #ddd;"><td style="padding:6px 12px 6px 0;color:#555;">Growth</td><td><strong>{gap}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#555;">Captured in report</td><td><strong>{n}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#555;">Difference</td><td><strong>{diff}</strong></td></tr>
      <tr style="border-top:1px solid #ddd;"><td style="padding:6px 12px 6px 0;color:#555;font-weight:bold;">Confidence</td><td style="font-weight:bold;">{confidence}</td></tr>
    </table>
    {check}
  </div>
  <p style="font-size:14px;margin-bottom:4px;"><strong>Attached CSV</strong> - name, email, type, RC product, expiry, signup time. All new profiles and orders from the last 24 hours.</p>
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
  <p style="font-size:11px;color:#aaa;margin:0;">Sent automatically at 7am PT - Total profiles: {total_count} - Questions? Contact Cronin.</p>
</body>
</html>"""

# Send
filename = f"castability_signups_{now.strftime('%Y-%m-%d')}.csv"
message = Mail(
    from_email=SENDER, to_emails=RECIPIENT,
    subject=f"Castability - New Signups {date_str} ({len(rows)})",
    html_content=html
)
message.attachment = Attachment(
    FileContent(base64.b64encode(csv_data.encode()).decode()),
    FileName(filename), FileType('text/csv'), Disposition('attachment')
)
response = SendGridAPIClient(SENDGRID).send(message)
print(f"Sent {len(rows)} records to {RECIPIENT} | Status: {response.status_code} | New profiles: {n} | Total: {total_count}")
