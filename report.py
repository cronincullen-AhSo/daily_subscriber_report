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
DB_NAME   = os.environ.get('DB_NAME', 'castability')
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

cur.execute("""
    SELECT a.id AS actor_id, u.first_name, u.last_name, u.email,
           a.has_external_subscription, u.subscriber_id, a.created_at
    FROM actors a
    JOIN users u ON u.actor_id = a.id
    WHERE a.created_at >= %s
    ORDER BY a.id ASC
""", (since,))
new_actors = cur.fetchall()

cur.execute("""
    SELECT a.id AS actor_id, u.first_name, u.last_name, u.email,
           u.subscriber_id, a.updated_at
    FROM actors a
    JOIN users u ON u.actor_id = a.id
    WHERE a.has_external_subscription = 1
      AND a.updated_at >= %s
      AND a.created_at < %s
    ORDER BY a.id ASC
""", (since, since))
conversions = cur.fetchall()

cur.execute("SELECT MAX(id) FROM actors")
max_actor_id = list(cur.fetchone().values())[0] or 0

cur.execute("SELECT MAX(id) FROM actors WHERE created_at < %s", (since,))
prev_max_actor_id = list(cur.fetchone().values())[0] or 0

cur.close()
conn.close()

# RevenueCat enrichment
def get_rc(sid):
    if not sid:
        return None
    try:
        req = urllib.request.Request(
            f"https://api.revenuecat.com/v1/subscribers/{sid}",
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
    rc = get_rc(a['subscriber_id'])
    t = rc_type(rc) or ('Subscriber (DB)' if a['has_external_subscription'] else 'Trial / Free')
    rows.append([a['actor_id'], a['first_name'], a['last_name'], a['email'], t,
                 rc_product(rc), rc_expires(rc),
                 a['created_at'].strftime('%Y-%m-%d %H:%M UTC') if a['created_at'] else ''])

for a in conversions:
    rc = get_rc(a['subscriber_id'])
    t = rc_type(rc) or 'Converted to Subscriber (DB)'
    rows.append([a['actor_id'], a['first_name'], a['last_name'], a['email'], t,
                 rc_product(rc), rc_expires(rc),
                 a['updated_at'].strftime('%Y-%m-%d %H:%M UTC') if a['updated_at'] else ''])

n    = len(new_actors)
s    = len(conversions)
gap  = max_actor_id - prev_max_actor_id
disc = gap - n
idr  = f"{new_actors[0]['actor_id']}-{new_actors[-1]['actor_id']}" if new_actors else 'none today'

if disc == 0:
    conf = 'HIGH'
elif abs(disc) <= 2:
    conf = 'HIGH (minor rounding)'
elif abs(disc) <= 5:
    conf = 'MEDIUM'
else:
    conf = 'LOW - please check'

diff = 'None - exact match' if disc == 0 else f'{abs(disc)} {"missing from report" if disc > 0 else "extra in report"}'

# CSV
buf = io.StringIO()
w = csv.writer(buf)
w.writerow(['Actor ID', 'First Name', 'Last Name', 'Email', 'Type', 'RC Product', 'Subscription Expires', 'Timestamp (UTC)'])
for r in rows:
    w.writerow(r)
csv_data = buf.getvalue()

# Check block
if disc > 2:
    check = f"""<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:4px;padding:10px 14px;font-size:13px;margin-top:8px;">
      <strong>Taylor - please double-check this.</strong><br><br>
      ID gap says <strong>{gap} new signups</strong> today but report captured <strong>{n}</strong>. Difference: <strong>{disc}</strong>.<br><br>
      <strong>How to check (30 seconds):</strong>
      <ol style="margin:8px 0 0 0;padding-left:18px;line-height:1.8;">
        <li>Go to <a href="https://castability.gojilabs.app/admin/actors">castability.gojilabs.app/admin/actors</a></li>
        <li>Sort by Date Created - newest first</li>
        <li>Look for Actor IDs between <strong>{prev_max_actor_id}</strong> and <strong>{max_actor_id}</strong></li>
        <li>Any IDs in that range not in the CSV are the missing ones</li>
      </ol><br>
      Most likely: incomplete profile, test account, or deleted signup. Flag to Cronin if something looks off.
    </div>"""
elif disc == 0:
    check = '<p style="margin:8px 0 0 0;font-size:13px;color:#2e7d32;"><strong>Numbers match exactly.</strong> CSV has everything.</p>'
else:
    check = f'<p style="margin:8px 0 0 0;font-size:13px;color:#e65100;"><strong>Small difference of {abs(disc)}.</strong> Likely a test account. Probably fine.</p>'

# HTML email
html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;padding:24px;color:#222;">
  <h2 style="margin-bottom:4px;">Castability - Daily Signups Report</h2>
  <p style="color:#666;margin-top:0;">{date_str}</p>
  <table style="background:#f5f5f5;border-radius:8px;padding:16px;width:100%;margin-bottom:24px;border-collapse:collapse;">
    <tr><td style="padding:6px 16px 6px 0;color:#666;font-size:14px;">New profiles today</td><td style="font-size:14px;"><strong>{n}</strong></td></tr>
    <tr><td style="padding:6px 16px 6px 0;color:#666;font-size:14px;">New subscribers / conversions today</td><td style="font-size:14px;"><strong>{s}</strong></td></tr>
    <tr><td style="padding:6px 16px 6px 0;color:#666;font-size:14px;">New Actor ID range today</td><td style="font-size:14px;"><strong>{idr}</strong></td></tr>
    <tr><td style="padding:6px 16px 6px 0;color:#666;font-size:14px;">Highest Actor ID on platform right now</td><td style="font-size:14px;"><strong>{max_actor_id}</strong></td></tr>
    <tr><td style="padding:6px 16px 6px 0;color:#666;font-size:14px;">Yesterday's highest Actor ID</td><td style="font-size:14px;"><strong>{prev_max_actor_id}</strong></td></tr>
  </table>
  <div style="background:#fff8e1;border-left:4px solid #f5a623;padding:16px 20px;border-radius:4px;margin-bottom:24px;">
    <p style="margin:0 0 8px 0;font-weight:bold;font-size:15px;">What this tells us - read this first</p>
    <p style="margin:0 0 10px 0;font-size:14px;line-height:1.6;">Actor IDs are assigned in order like ticket numbers. The CSV includes RevenueCat data where available - RC Product shows the subscription plan, Subscription Expires shows the renewal date. Type shows the source: (RC) = confirmed by RevenueCat, (DB) = from our database only.</p>
    <table style="border-collapse:collapse;width:100%;font-size:14px;margin-bottom:10px;">
      <tr><td style="padding:4px 12px 4px 0;color:#555;">Highest ID yesterday</td><td><strong>{prev_max_actor_id}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#555;">Highest ID today</td><td><strong>{max_actor_id}</strong></td></tr>
      <tr style="border-top:1px solid #ddd;"><td style="padding:6px 12px 6px 0;color:#555;">ID gap (estimated new signups)</td><td><strong>{gap}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#555;">Profiles captured in this report</td><td><strong>{n}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#555;">Difference</td><td><strong>{diff}</strong></td></tr>
      <tr style="border-top:1px solid #ddd;"><td style="padding:6px 12px 6px 0;color:#555;font-weight:bold;">Confidence rating</td><td style="font-weight:bold;">{conf}</td></tr>
    </table>
    {check}
  </div>
  <p style="font-size:14px;margin-bottom:4px;"><strong>Attached CSV</strong> - Actor ID, name, email, subscription type, RC product, expiry, signup time. Sort by Actor ID to see in order.</p>
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
  <p style="font-size:11px;color:#aaa;margin:0;">Sent automatically at 7am PT - Platform max Actor ID: {max_actor_id} - Questions? Contact Cronin.</p>
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
print(f"Sent {len(rows)} records to {RECIPIENT} | Status: {response.status_code} | IDs: {idr} | Max: {max_actor_id}")
