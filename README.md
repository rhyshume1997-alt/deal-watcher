# Deal watcher

A personal deal, savings and opportunity watcher. It runs every 30 minutes on GitHub Actions, reads your Gmail and a handful of deal sources, and only emails you when something passes a real value test.

Email is the whole interface:

| You want to… | Do this |
|---|---|
| Watch for something | Email `you+watch@gmail.com` one line: `65in OLED TV under £1100`. Add a product link to track that exact product. |
| Stop watching | Email `+watch`: `stop sofa` or `bought sofa` |
| Rate an alert | Click 👍 Useful or 👎 Not useful in the email |
| Import an Amex statement | Email the CSV/PDF to `+watch` as an attachment |

Replying to any alert goes to `+watch` too, so replying with a line adds it.

## What it sends

- **Instant email**: F1 tickets and presales, possible price errors, restocks, a planned purchase hitting your target, strong stacked offers, anything relevant ending within 48h, a price drop on something you bought while it can still be returned, free cancellation about to end.
- **Daily digest (7:30am)**: everything else that passes the value test, top 12 by score, plus reminders (returns, warranties, renewals).
- **Monthly summary (1st, 8am)**: estimated money saved, offers acted on, alerts ignored, offers suppressed, what it learnt.

Each alert shows BUY NOW / WAIT / IGNORE, the offer, why it matters, the cash saving with discount, Amex credit and cashback listed separately, Avios on their own line (never converted to cash), price history context, urgency and a link.

## How it decides

`watcher/scoring.py` is the value test:

1. **Junk filter**: clickbait "up to X% off", spend-to-save offers and trivial savings are dropped.
2. **Relevance**: how much you spend at that retailer and in that category (from your statements, recent spend weighted higher), boosted for planned purchases, F1 and upcoming trip destinations, then scaled by what you've found useful before.
3. **Price history**: discounts are measured against the 90-day median, not the "was" price. Fake discounts are dropped; "lowest in 6 months" is flagged.
4. **Per-category gates** (`config/categories.yaml`): minimum £ saving and % off, e.g. groceries £10 and 30%, electronics £25 and 15%.
5. **Cooldowns**: after you buy something, similar alerts are held back (coffee 7 days, clothes 30, large electronics 180) unless the deal is exceptional.
6. **Verdict**: WAIT if a known sale (Black Friday, Boxing Day, January sales, Prime Day) is close and this isn't a genuine low.

## Setup

The database (Supabase project `deal-watcher`, London), its tables and the tracking function are already set up.
The link-signing secret is generated automatically and stored in the database.

What's left is adding these **GitHub secrets** (repo → Settings → Secrets and variables → Actions → New repository secret):

| Secret | Where to get it |
|---|---|
| `DATABASE_URL` | Supabase → project `deal-watcher` → Connect → Session pooler → URI (put your database password into it) |
| `GMAIL_ADDRESS` | your Gmail address |
| `GMAIL_APP_PASSWORD` | Google Account → Security → 2-Step Verification (must be on) → App passwords → create "deal watcher" |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API keys |
| `KEEPA_API_KEY` | optional: keepa.com → API access |
| `ALERT_TO` | optional: only if alerts should go to another address |

Then go to Actions → **watch** → Run workflow, and run `doctor`, then `test-email`. After that it runs by itself every 30 minutes.

## Running locally

```
pip install -r requirements-dev.txt
pytest -q
DRY_RUN=1 python -m watcher import statements/*.csv
python -m watcher profile
```

Without `DATABASE_URL` it uses a local SQLite file.

## Adding a source

Add an entry to `config/sources.yaml`, either an RSS feed, a Google News search or a page to watch for phrases like "on sale now". New retailers go in `config/retailers.yaml`. A new kind of source is a function in `watcher/sources/` that returns `Offer` objects; everything after that (scoring, emails, learning) is shared.

## Costs

- GitHub Actions: about 1,500 of the 2,000 free private-repo minutes a month.
- Supabase: free tier.
- Claude API: capped by `LLM_MONTHLY_CALL_CAP` (default 3,000 calls a month). The model is set by the `LLM_MODEL` repository variable (default `claude-opus-5`); set it to `claude-haiku-4-5` for a cheaper option.
- Keepa: optional, paid.

## Limits

- **Amex Offers** have no API. Offers are picked up from Amex emails, anything you forward to `+watch`, and write-ups on Head for Points and HotUKDeals.
- **Cashback sites** (TopCashback, Quidco) have no consumer API; cashback shows only when a deal post or email mentions it.
- **Opens aren't tracked**, because Gmail pre-loads images and makes open tracking meaningless. Clicks and feedback are the signal.
- The watcher respects `robots.txt` and spaces out requests to each site. Retailers that block automated requests can't be price-tracked directly; for those it relies on deal posts.
