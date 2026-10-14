# ig-comment-bot

Open-source **"comment a keyword → get the link in your DMs"** for Instagram.
One Python file, standard library only, MIT licence. It uses **only the official Instagram API** (no password logins and no scraping), so it won't get your account flagged.

```
someone comments "BOARD" on your Reel
        │  Meta webhook (signed, X-Hub-Signature-256)
        ▼
  igbot.py  ──►  private reply DM with your link   (POST /{ig-user-id}/messages, recipient.comment_id)
            ──►  public reply "check your DMs"     (POST /{comment-id}/replies, optional)
            ──►  row in SQLite (+ optional Google Sheet log)
```

## What it handles
- Whole-word, case-insensitive keyword (`BOARD`, `board 🙏` match; `cardboard` doesn't).
- One DM per person per post. Meta's duplicate deliveries are ignored, and your own comments are skipped.
- Optional filter by specific post or Reel IDs.
- Personalised DM (`{name}`), with rotating public replies so they don't look spammy.
- Refreshes the long-lived token automatically (every 7 days, so the 60-day token never expires).
- Checks the signature on every webhook and answers Meta immediately, then does the work in the background.
- `GET /stats?key=ADMIN_KEY` lists who got a DM and any errors. `GET /privacy` serves a privacy page (Meta asks for one).

## Run
```bash
cp .env.example .env      # fill in the values below
python igbot.py --selftest  # 18 checks against a mock Instagram API, no Meta account needed
python igbot.py           # listens on 127.0.0.1:8091 (/webhook or /ig/webhook)
```
Expose it over HTTPS. Here it's `https://<your-machine>.ts.net/ig/webhook` through Tailscale Funnel; ngrok, Cloudflare Tunnel or any VPS also work.

## Meta setup (one time)
1. **Instagram account**: switch to *Professional* (Creator or Business). In Settings → Messages and story replies → Message controls → Connected tools, turn on **Allow access to messages**.
2. **developers.facebook.com** → *Create app* → use case **"Manage messaging & content on Instagram"** (Instagram API with Instagram Login).
3. In **API setup with Instagram login**:
   - **Add account**: log in with your Instagram account, then *Generate token*. Copy the token → `IG_ACCESS_TOKEN`, and the account ID → `IG_USER_ID`.
   - **Configure webhooks**: Callback URL `https://…/ig/webhook`, Verify token = your `IG_VERIFY_TOKEN`. Click **Verify and save**, then **subscribe to `comments`**.
   - App secret (*App settings → Basic*, or the Instagram app secret shown there) → `IG_APP_SECRET`.
4. Permissions needed: `instagram_business_basic`, `instagram_business_manage_comments`, `instagram_business_manage_messages`.
5. **Testing before review**: the app is in *Development* mode, so only people with a role on the app get webhooks. Add a second Instagram account under *App roles → Instagram testers*, accept the invite in that account (Settings → Apps and websites → Tester invites), and comment BOARD from it.
6. **Go live for everyone**: submit **App Review** for the three permissions (a screencast of the BOARD → DM flow plus the privacy URL `https://…/ig/privacy`), complete Business verification if asked, then switch the app to **Live**.

## Instagram rules to know
- A private reply must be sent **within 7 days** of the comment, and only **one** per comment.
- People who don't follow you get the DM under **Message requests**, so say so in the public reply.
- Keep the DM useful and one-off. Mass unsolicited messaging can get the app restricted.
