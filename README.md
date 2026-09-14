# Mr Gold Trading Portal — V3 Free Edition

A $0/month hobby/personal architecture for recording MT5 performance without MetaApi.

## Architecture

`MT5 + MR Gold V1.58` → `MR_Gold_MT5_Sync_Free_V3.mq5` → `Render Free FastAPI` → `Supabase Free Postgres`

The public dashboard separates results by:

- MT5 account
- MR Gold V1.58 Unique ID
- effective Magic Number
- strategy/source (Previous Day / Asia Session when present in V1.58 comments)
- symbol

## V1.58 compatibility

The supplied V1.58 source uses:

`effective magic = InpMagicNumber * 1000 + InpInstanceID`

With its default base magic 1517:

- 1517001 = Unique ID 1
- 1517002 = Unique ID 2
- 1517015 = Unique ID 15

V1.58 also writes its Instance ID as the fourth field of its entry comment, e.g. `PD|4450.00|B|1|SLP=1000.0`. The sync EA understands both methods.

## Why Supabase instead of a local SQLite file?

Render Free has an ephemeral local filesystem. A local SQLite database can disappear after restart/redeploy. This edition keeps durable trading data in a separate Supabase project instead.

## Free-tier efficiency changes

- Snapshot interval defaults to 5 minutes instead of 30 seconds.
- First EA run sends the configured initial history (730 days by default).
- Later sync cycles resend only the most recent 14 days.
- The API deduplicates closed deals using `(account_id, deal_ticket)`.
- Dashboard requests page through Supabase API results, so statistics are not limited to a single 1000-row response.

## Security design

- Supabase `sb_secret_...` is stored only as a Render secret environment variable.
- The secret key is never sent to browser JavaScript and never stored in MT5.
- Each tracked account receives a random `mg_...` upload key. Only the SHA-256 hash of that upload key is stored in the database.
- The public report exposes a masked MT5 login only.
- Row Level Security is enabled on all three Supabase tables; the backend secret key performs server-side access.

## Main files

- `main.py` — FastAPI backend and public/admin APIs
- `static/public.html` — public Strategy-X-Ray-style performance dashboard
- `static/admin.html` — account setup and upload-key creation
- `MR_Gold_MT5_Sync_Free_V3.mq5` — MT5 sync EA
- `supabase_schema.sql` — one-time database schema
- `render.yaml` — Render Free Blueprint
- `QUICK_START.txt` — exact deployment steps

## Important free-tier limitations

This is intended for personal/hobby tracking rather than a commercial SLA. Free hosting may cold-start, can be restarted, and free database plans have capacity/backup limitations. Upgrade hosting/storage later if the tracker becomes client-facing or business-critical.
