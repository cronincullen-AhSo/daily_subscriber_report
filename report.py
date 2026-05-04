import os
import csv
import io
import json
import psycopg2
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import (
    Mail, Attachment, FileContent, FileName, FileType, Disposition
)
import base64

# ── Config ────────────────────────────────────────────────────────────────────
DATABASE_URL   = os.environ['DATABASE_URL']
SENDGRID_KEY   = os.environ['SENDGRID_API_KEY']
RC_API_KEY     = os.environ['REVENUE_CAT_API_KEY']
RECIPIENT      = os.environ.get('REPORT_RECIPIENT', 'taylor.shurte@castability.actor')
SENDER         = os.environ.get('REPORT_SENDER',    'cronin.cullen@castability.actor')

now      = datetime.now(timezone.utc)
since    = now - timedelta(hours=24)
date_str = now.strftime('%B %-d, %Y')

# ── DB Query ──────────────────────────────────────────────────────────────────
conn = psycopg2.connect(DATABASE_URL)
cur  = conn.cursor()

cur.execute("""
    SELECT
        a.id            AS actor_id,
        u.first_name,
        u.last_name,
        u.email,
        a.has_external_subscription,
        u.subscriber_id,
        a.created_at
    FROM actors a
    JOIN users u ON u.actor_id = a.id
    WHERE a.created_at >= %s
    ORDER BY a.id ASC
""", (since,))
new_actors = cur.fetchall()

cur.execute("""
    SELECT
        a.id            AS actor_id,
        u.first_name,
        u.last_name,
        u.email,
        u.subscriber_id,
        a.updated_at
    FROM actors a
    JOIN users u ON u.actor_id = a.id
    WHERE a.has_external_subscription = true
      AND a.updated_at >= %s
      AND a.created_at < %s
    ORDER BY a.id ASC
""", (since, since))
conversions = cur.fetchall()

cur.execute("SELECT MAX(id) FROM actors")
max_actor_id = cur.fetchone()[0] or 0

cur.execute("SELECT MAX(id) FROM actors WHERE created_at < %s", (since,))
prev_max_actor_id = cur.fetchone()[0] or 0

cur.close()
conn.close()

# ── RevenueCat enrichment ─────────────────────────────────────────────────────
def get_rc_subscriber(subscriber_id):
    if not subscriber_id:
        return None
    try:
        url = f"https://api.revenuecat.com/v1/subscribers/{subscriber_id}"
        req = urllib.request.Request(url, headers={
            'Authorization': f'Bearer {RC_API_KEY}',
            'Content-Type': 'application/json'
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None

def rc_subscription_type(rc_data):
    if not rc_data:
        return None
    subs = rc_data.get('subscriber', {}).get('subscriptions', {})
    ents = rc_data.get('subscriber', {}).get('entitlements', {})
    active_subs = [v for v in subs.values() if v.get('expires_date') and
                   datetime.fromisoformat(v['expires_date'].replace('Z','+00:00')) > now]
    if not active_subs and not ents:
        return None
    is_trial = any(s.get('period_type') == 'trial' for s in active_subs)
    return 'Trial (RC)' if is_trial else 'Subscriber (RC)'

def rc_product(rc_data):
    if not rc_data:
        return ''
    subs = rc_data.get('subscriber', {}).get('subscriptions', {})
    active = [v for v in subs.values() if v.get('expires_date') and
              datetime.fromisoformat(v['expires_date'].replace('Z','+00:00')) > now]
    if active:
        return active[0].get('product_identifier', '')
    return ''

def rc_expires(rc_data):
    if not rc_data:
        return ''
    subs = rc_data.get('subscriber', {}).get('subscriptions', {})
    active = [v for v in subs.values() if v.get('expires_date') and
              datetime.fromisoformat(v['expires_date'].replace('Z','+00:00')) > now]
    if active:
        exp = active[0].get('expires_date', '')
        try:
            return datetime.fromisoformat(exp.replace('Z','+00:00')).strftime('%Y-%m-%d')
        except Exception:
            return exp
    return ''

# ── Build rows ────────────────────────────────────────────────────────────────
rows = []

for r in new_actors:
    actor_id, first, last, email, has_sub, subscriber_id, created_at = r
    rc = get_rc_subscriber(subscriber_id)
    rc_type = rc_subscription_type(rc)
    # Determine final type: RC is authoritative if available, else fall back to DB flag
    if rc_type:
        sub_type = rc_type
    elif has_sub:
        sub_type = 'Subscriber (DB)'
    else:
        sub_type = 'Trial / Free'
    rows.append([
        actor_id, first, last, email, sub_type,
        rc_product(rc), rc_expires(rc),
        created_at.strftime('%Y-%m-%d %H:%M UTC') if created_at else ''
    ])

for r in conversions:
    actor_id, first, last, email, subscriber_id, updated_at = r
    rc = get_rc_subscriber(subscriber_id)
    rc_type = rc_subscription_type(rc) or 'Converted → Subscriber (DB)'
    rows.append([
        actor_id, first, last, email, rc_type,
        rc_product(rc), rc_expires(rc),
        updated_at.strftime('%Y-%m-%d %H:%M UTC') if updated_at else ''
    ])

new_signups_count = len(new_actors)
new_subs_count    = len(conversions)
gap               = max_actor_id - prev_max_actor_id
discrepancy       = gap - new_signups_count
id_range          = f"{new_actors[0][0]}-{new_actors[-1][0]}" if new_actors else "none today"

if discrepancy == 0:
    confidence_label = "HIGH ✅"
elif abs(discrepancy) <= 2:
    confidence_label = "HIGH ✅ (minor rounding)"
elif abs(discrepancy) <= 5:
    confidence_label = "MEDIUM ⚠️"
else:
    confidence_label = "LOW ❌ — please check"

# ── CSV ───────────────────────────────────────────────────────────────────────
buf = io.StringIO()
writer = csv.writer(buf)
writer.writerow(['Actor ID', 'First Name', 'Last Name', 'Email',
                 'Type', 'RC Product', 'Subscription Expires', 'Timestamp (UTC)'])
for r in rows:
    writer.writerow(r)
csv_data = buf.getvalue()

# ── Check block ───────────────────────────────────────────────────────────────
diff_label = 'None — exact match' if discrepancy == 0 else \
    f'{abs(discrepancy)} {"missing from report" if discrepancy > 0 else "extra in report"}'

if discrepancy > 2:
    check_block = f"""
    <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:4px;padding:10px 14px;font-size:13px;margin-top:8px;">
      <strong>⚠️ Taylor — please double-check this.</strong><br><br>
      The ID gap says <strong>{gap} new signups</strong> today but this report captured
      <strong>{new_signups_count}</strong>. Difference of <strong>{discrepancy}</strong>.<br><br>
      <strong>How to check (30 seconds):</strong>
      <ol style="margin:8px 0 0 0;padding-left:18px;line-height:1.8;">
        <li>Go to <a href="https://castability.gojilabs.app/admin/actors">castability.gojilabs.app/admin/actors</a></li>
        <li>Sort by "Date Created" — newest first</li>
        <li>Look for Actor IDs between <strong>{prev_max_actor_id}</strong> and <strong>{max_actor_id}</strong></li>
        <li>Any IDs in that range not in the CSV are the missing ones</li>
      </ol><br>
      Most likely: incomplete profile, test account, or deleted signup. Flag to Cronin if something looks off.
    </div>"""
elif discrepancy == 0:
    check_block = '<p style="margin:8px 0 0 0;font-size:13px;color:#2e7d32;">✅ <strong>Numbers match exactly.</strong> CSV has everything.</p>'
else:
    check_block = f'<p style="margin:8px 0 0 0;font-size:13px;color:#e65100;">⚠️ <strong>Small difference of {abs(discrepancy)}.</strong> Likely a test account or timing edge. Probably fine.</p>'

# ── HTML ──────────────────────────────────────────────────────────────────────
html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;padding:24px;color:#222;">
  <h2 style="margin-bottom:4px;">Castability — Daily Signups Report</h2>
  <p style="color:#666;margin-top:0;">{date_str}</p>
  <table style="background:#f5f5f5;border-radius:8px;padding:16px;width:100%;margin-bottom:24px;border-collapse:collapse;">
    <tr><td style="padding:6px 16px 6px 0;color:#666;font-size:14px;">New profiles today</td><td style="font-size:14px;"><strong>{new_signups_count}</strong></td></tr>
    <tr><td style="padding:6px 16px 6px 0;color:#666;font-size:14px;">New subscribers / conversions today</td><td style="font-size:14px;"><strong>{new_subs_count}</strong></td></tr>
    <tr><td style="padding:6px 16px 6px 0;color:#666;font-size:14px;">New Actor ID range today</td><td style="font-size:14px;"><strong>{id_range}</strong></td></tr>
    <tr><td style="padding:6px 16px 6px 0;color:#666;font-size:14px;">Highest Actor ID on platform right now</td><td style="font-size:14px;"><strong>{max_actor_id}</strong></td></tr>
    <tr><td style="padding:6px 16px 6px 0;color:#666;font-size:14px;">Yesterday's highest Actor ID</td><td style="font-size:14px;"><strong>{prev_max_actor_id}</strong></td></tr>
  </table>
  <div style="background:#fff8e1;border-left:4px solid #f5a623;padding:16px 20px;border-radius:4px;margin-bottom:24px;">
    <p style="margin:0 0 8px 0;font-weight:bold;font-size:15px;">📊 What this tells us — read this first</p>
    <p style="margin:0 0 10px 0;font-size:14px;line-height:1.6;">
      Actor IDs are assigned in order like ticket numbers. The CSV now includes RevenueCat data where available —
      "RC Product" shows the subscription plan and "Subscription Expires" shows when it renews or ends.
      "Type" shows the source: <strong>(RC)</strong> = confirmed by RevenueCat, <strong>(DB)</strong> = from our database only.
    </p>
    <table style="border-collapse:collapse;width:100%;font-size:14px;margin-bottom:10px;">
      <tr><td style="padding:4px 12px 4px 0;color:#555;">Highest ID yesterday</td><td><strong>{prev_max_actor_id}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#555;">Highest ID today</td><td><strong>{max_actor_id}</strong></td></tr>
      <tr style="border-top:1px solid #ddd;"><td style="padding:6px 12px 6px 0;color:#555;">ID gap (estimated new signups)</td><td><strong>{gap}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#555;">Profiles captured in this report</td><td><strong>{new_signups_count}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#555;">Difference</td><td><strong>{diff_label}</strong></td></tr>
      <tr style="border-top:1px solid #ddd;"><td style="padding:6px 12px 6px 0;color:#555;font-weight:bold;">Confidence rating</td><td style="font-weight:bold;">{confidence_label}</td></tr>
    </table>
    {check_block}
  </div>
  <p style="font-size:14px;margin-bottom:4px;"><strong>Attached CSV</strong> — Actor ID, name, email, subscription type, RC product, expiry date, signup time. Sort by Actor ID to see in order.</p>
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
  <p style="font-size:11px;color:#aaa;margin:0;">Sent automatically at 7am PT · Platform max Actor ID: {max_actor_id} · Questions? Contact Cronin.</p>
</body>
</html>"""

# ── Send ──────────────────────────────────────────────────────────────────────
filename = f"castability_signups_{now.strftime('%Y-%m-%d')}.csv"
encoded  = base64.b64encode(csv_data.encode()).decode()

message = Mail(
    from_email=SENDER,
    to_emails=RECIPIENT,
    subject=f"Castability — New Signups {date_str} ({len(rows)})",
    html_content=html
)
message.attachment = Attachment(
    FileContent(encoded),
    FileName(filename),
    FileType('text/csv'),
    Disposition('attachment')
)

sg       = SendGridAPIClient(SENDGRID_KEY)
response = sg.send(message)
print(f"Sent {len(rows)} records to {RECIPIENT} | Status: {response.status_code} | IDs: {id_range} | Max: {max_actor_id}")
