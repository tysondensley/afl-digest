# AFL Digest

Sends a scheduled AFL news digest email three times a day via GitHub Actions.

Each email contains:
- **News & Transfers** — ~7 bullet-point summary of the latest AFL news from four RSS feeds (trades, injuries, signings, suspensions), filtered and summarised by Claude.
- **Fan Buzz** — 3–5 bullets on what's generating discussion on Reddit r/AFL and BigFooty.
- **Breaking / Viral** — Claude searches the web for breaking stories not yet picked up by mainstream AFL media. Omitted if nothing novel is found.

---

## Schedule

| Label | AEST | AEDT | UTC cron |
|---|---|---|---|
| 6am | 06:15 | 07:15\* | `15 20 * * *` |
| Midday | 12:00 | 13:00\* | `0 2 * * *` |
| End of Day | 16:30 | 17:30\* | `30 6 * * *` |

\* During daylight saving (AEDT, roughly Oct–Apr) emails arrive 1 hour later than the label implies. GitHub Actions does not support timezone-aware cron, so AEST (UTC+10) is hardcoded.

---

## Prerequisites

- A GitHub account with this repository
- An Anthropic API key (claude.ai/account or console.anthropic.com)
- A Gmail account with 2-Step Verification enabled

---

## Setup

### 1. Get the code onto GitHub

GitHub is the free service that stores the code and runs the digest on schedule. The easiest way to get set up is with **GitHub Desktop** — a free app that does everything with clicks, no terminal needed.

**a) Create a free GitHub account** (skip if you already have one)

Go to https://github.com and click **Sign up**. A free account is all you need.

**b) Install GitHub Desktop**

Download and install it from https://desktop.github.com. When it opens, sign in with your GitHub account.

**c) Add this project folder to GitHub Desktop**

1. In GitHub Desktop, click **File → Add Local Repository**
2. Click **Choose…** and navigate to the `afl-digest` folder on your computer (wherever you saved these project files), then click **Open**
3. GitHub Desktop will say it's not a Git repository yet — click **create a repository** in the message that appears
4. Fill in the name as `afl-digest`, leave everything else as-is, and click **Create Repository**

**d) Publish to GitHub**

1. Click the **Publish repository** button at the top of GitHub Desktop
2. Make sure **Keep this code private** is checked if you don't want it public (recommended, since it will reference your secrets)
3. Click **Publish Repository**

That's it — all the files in the folder (including hidden ones like the `.github` folder that contains the schedule) are now on GitHub. You can confirm by visiting https://github.com and clicking on the `afl-digest` repository that now appears under your account.

### 2. Generate a Gmail App Password

An App Password is a special one-time code that lets GitHub send emails from your Gmail without using your real password.

1. Go to https://myaccount.google.com/apppasswords (sign in if prompted)
   - If you get an error saying the page doesn't exist, you need to turn on 2-Step Verification first: go to https://myaccount.google.com/signinoptions/two-step-verification and follow the prompts, then come back here
2. You'll see a text field that says **"App name"** — type `AFL Digest` and click **Create**
3. Google will show you a 16-character password in a box (e.g. `abcd efgh ijkl mnop`) — copy it now
4. Store it somewhere safe (e.g. Notes app); you won't be able to see it again

### 3. Get your Anthropic API key

This is what authorises Claude to summarise the news. You may already have one — if so, skip to step 4.

1. Go to https://console.anthropic.com and sign in (or create a free account)
2. Click **API Keys** in the left sidebar
3. Click **Create Key**, give it a name like `AFL Digest`, and click **Create Key**
4. Copy the key shown (it starts with `sk-ant-`) — store it alongside your Gmail App Password

### 4. Add the secrets to GitHub

These three values need to be stored securely in your GitHub repository so the scheduled workflow can use them.

1. Go to your `afl-digest` repository on GitHub
2. Click **Settings** (top navigation bar, far right)
3. In the left sidebar, click **Secrets and variables → Actions**
4. Click **New repository secret** and add each of the following — one at a time:

| Secret name | Value to paste |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (starts with `sk-ant-`) |
| `GMAIL_USER` | Your Gmail address, e.g. `you@gmail.com` |
| `GMAIL_APP_PASSWORD` | The 16-character App Password from step 2 |

### 5. Enable Actions (if needed)

If this is a new repo, GitHub may ask you to enable Actions. Go to the **Actions** tab and click **I understand my workflows, go ahead and enable them**.

---

## Manual test run

You can trigger the digest immediately without waiting for the scheduled time:

1. Go to the **Actions** tab in your repository
2. Click **AFL Digest** in the left sidebar
3. Click **Run workflow**
4. Choose a time slot label (e.g. *Midday*) and click **Run workflow**
5. Watch the run complete — a green tick means the email was sent

---

## Local development

```bash
# Install dependencies
pip install -r requirements.txt

# Set required env vars
export ANTHROPIC_API_KEY="sk-ant-..."
export GMAIL_USER="you@gmail.com"
export GMAIL_APP_PASSWORD="abcdefghijklmnop"
export TIME_SLOT="Midday"   # optional override

python digest.py
```

---

## RSS feed URLs

If a feed stops working, update the `RSS_FEEDS` list at the top of `digest.py`:

**AFL.com.au** (own section)

| Source | URL |
|---|---|
| AFL.com.au | `https://www.afl.com.au/rss` |

**Other Media** (pooled section — exclusives/breaking/opinion prioritised)

| Source | URL |
|---|---|
| The Age | `https://www.theage.com.au/rss/sport/afl.xml` |
| The Guardian | `https://www.theguardian.com/sport/afl/rss` |
| Herald Sun (via Google News) | `https://news.google.com/rss/search?q=AFL+site:heraldsun.com.au&hl=en-AU&gl=AU&ceid=AU:en` |
| Fox Footy (via Google News) | `https://news.google.com/rss/search?q=AFL+site:foxsports.com.au&hl=en-AU&gl=AU&ceid=AU:en` |
| ABC News (via Google News) | `https://news.google.com/rss/search?q=AFL+site:abc.net.au&hl=en-AU&gl=AU&ceid=AU:en` |
| SEN (via Google News) | `https://news.google.com/rss/search?q=AFL+site:sen.com.au&hl=en-AU&gl=AU&ceid=AU:en` |
| The Australian (via Google News) | `https://news.google.com/rss/search?q=AFL+site:theaustralian.com.au&hl=en-AU&gl=AU&ceid=AU:en` |
| AFR (via Google News) | `https://news.google.com/rss/search?q=AFL+site:afr.com&hl=en-AU&gl=AU&ceid=AU:en` |

---

## Troubleshooting

**Email not arriving**
- Check the Actions run log for errors under the *Run AFL digest* step
- Verify all three secrets are set correctly (no trailing spaces)
- Make sure 2-Step Verification is enabled on the Gmail account before generating the App Password

**`smtplib.SMTPAuthenticationError`**
- Regenerate the App Password — they expire if 2-Step Verification is turned off and back on

**RSS feed returning no articles**
- The feed URL may have changed; check the source website for their current RSS link
- Some feeds (Herald Sun, Fox Footy) may require a subscription or block scrapers intermittently

**BigFooty section missing**
- BigFooty blocking is expected and handled silently — the digest still sends without it

**Claude web search section missing**
- This is normal when Claude finds no novel stories; it is omitted by design
