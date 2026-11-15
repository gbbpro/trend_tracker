# trend_tracker

Powered by `trendnamer.py`, a prototype

Finds brandable domain names in what people are talking about right now, checks if they're registered, and ranks them.

## Run it

Needs Python 3.9+ and nothing else.

```bash
python3 trendnamer.py                      # live sources + availability check → trendnamer_report.html/.csv
python3 trendnamer.py --no-check           # skip availability lookups (fast)
python3 trendnamer.py --snapshot demo_snapshot_2026-11-15.json   # replay saved data
python3 trendnamer.py --save-snapshot today.json                 # keep raw data for later
```

Run it daily (cron, Task Scheduler). Each run adds to `history.json`, so the tool can tell **rising** phrases from **fading** ones. Velocity gets more accurate after about a week of runs.

## Daily runs (GitHub Actions)

`.github/workflows/daily.yml` runs the scan every day at 13:23 UTC and commits:

- `reports/YYYY-MM-DD.html` / `.csv` / `.txt`: that day's ranked domains
- `reports/latest.html` / `latest.csv`: always the newest
- `snapshots/YYYY-MM-DD.json`: the raw signals, so the filters can be re-tuned against past days
- `history.json`: phrase scores over time, which drives rising/fading

Reports are published to **https://gbbpro.github.io/trend_tracker/** (needs Settings → Pages → Source: _GitHub Actions_).

To run it now: **Actions** tab → **Daily trend scan** → **Run workflow**.

Reddit needs two repository secrets: **Settings → Secrets and variables → Actions → New repository secret**, named `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET`. Without them the Reddit source is skipped and the rest still run.

## How it works

| Stage        | What it does                                                                                                                                                                                                                                                                                                               |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Sources      | Google Trends daily RSS, Wikipedia most-viewed (vs. same day last week), Hacker News front page, Google News top stories, Bluesky trending topics. Reddit is optional (see below).                                                                                                                                         |
| Phrases      | Short signals (slang, trend names) are kept whole, minus filler words like "challenge" or "trend". From headlines, it keeps only multi-word phrases that appear in 2+ signals, so lone words like "end" don't count.                                                                                                       |
| Velocity     | Today vs. the previous week, or vs. the average of earlier runs → breaking out / rising / steady / fading.                                                                                                                                                                                                                 |
| Risk         | Flags known brands and proper nouns (people, shows, products). High-risk names are dropped unless you pass `--include-risky`.                                                                                                                                                                                              |
| Candidates   | Exact phrase, plus get-, the-, -hq and -club versions, across .com .ai .co .io .app .xyz. Scored on trend strength, length, how easy it is to say, and TLD.                                                                                                                                                                |
| Availability | A name that resolves in DNS is `taken`. Otherwise it asks the extension's official RDAP server (from IANA's list). Not found → `available?`. No RDAP server, or any error → `unknown`, which never counts as available. Prices come from Porkbun's public price list; if that fails, it prints why and uses a rough table. |

## Turning on Reddit

Reddit blocks anonymous requests. To add it:

1. Go to https://www.reddit.com/prefs/apps, click **create another app**, and choose type **script**. Any redirect URL will do, e.g. `http://localhost`.
2. Put the two values in a file named `.env` next to `trendnamer.py`:

```
REDDIT_CLIENT_ID=xxxx
REDDIT_CLIENT_SECRET=yyyy
```

The tool reads `.env` automatically; values set in your shell take priority. Keep `.env` private (if this folder goes into git, add `.env` to `.gitignore`).
Without them, the Reddit source is skipped and the rest still run.

## Why news topics rarely make the list

Most of what's in the news is a person, a product or an event (e.g. "Starship Flight 15"). These are flagged as brand/person names and skipped, because registering them to resell is the classic cybersquatting case. The money is in generic phrases and new words. That's why Bluesky and slang-style sources matter most, while news sources mostly add overlap. Use `--include-risky` to see everything.

## Known limits

- `available?` means no registration record exists. Premium, reserved and registry-held names look available to RDAP but aren't. Confirm at a registrar before you get excited.
- The risk check is a heuristic, not legal clearance. Before buying a name, search it on [USPTO trademark search](https://tmsearch.uspto.gov/).
- TikTok has no free public trends feed. The demo snapshot's TikTok data was entered by hand from published trackers.

## Next steps

1. Add an LLM step to invent brandable versions of each phrase ("mogging" → "moggr", "mogly"), beyond the simple affix rules.
2. Add aftermarket comparable sales (NameBio) so you can estimate what a name might resell for.
3. Add registrar API keys (Porkbun or Namecheap) to detect premium pricing exactly.
4. Turn the report into a daily email or web dashboard for the "naming tool for founders" version.
