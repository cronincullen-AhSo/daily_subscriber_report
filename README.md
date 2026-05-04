# daily_subscriber_report

Automated daily email report of new Castability signups and subscribers sent to marketing every morning at 7am PT.

## Files — where each one goes in gojilabs/castability

| File | Destination |
|---|---|
| `.github/workflows/daily_subscriber_report.yml` | `.github/workflows/daily_subscriber_report.yml` |
| `api/lib/tasks/subscriber_report.rake` | `api/lib/tasks/subscriber_report.rake` |
| `api/app/mailers/report_mailer.rb` | `api/app/mailers/report_mailer.rb` |
| `api/app/views/report_mailer/daily_subscribers.html.erb` | `api/app/views/report_mailer/daily_subscribers.html.erb` |

## Secrets required in gojilabs/castability → Settings → Secrets → Actions

- `DATABASE_URL` — production Postgres connection string
- `SENDGRID_API_KEY` — already exists

## Manual trigger

After merging: GitHub Actions → Daily Subscriber Report → Run workflow
