# Setting up Sign in with Google

Step-by-step Google Cloud Console setup for the "Continue with Google"
button on `/login` and `/signup`.  ~15 minutes the first time.

If you already set up YouTube OAuth, you can reuse the same Google Cloud
project + OAuth client; just add the new redirect URI to the existing
client.  Jump to **Step 4** in that case.

---

## Step 1 — Create or pick a Google Cloud project

1. Go to <https://console.cloud.google.com/>.
2. Click the project picker at the top of the page → **New Project**
   (or pick an existing one — e.g. the project you used for YouTube).
3. Give it a name like `Gootier` and click **Create**.

## Step 2 — Enable the People API

Google's `userinfo` endpoint (which is how we read your email + name on
sign-in) is gated behind the **People API**.

1. In the left sidebar, go to **APIs & Services → Library**.
2. Search for **People API**.
3. Click it → **Enable**.

You do **not** need to enable the OAuth2 API itself — it's always on.

## Step 3 — Configure the OAuth consent screen

This is what users see when they click "Continue with Google".  Skip
to step 4 if you already configured this for YouTube.

1. **APIs & Services → OAuth consent screen**.
2. **User Type**: pick **External** (lets anyone with a Google account
   sign in) unless you're inside a Google Workspace org and want to
   restrict to that org.  Click **Create**.
3. **App information**:
   - App name: **Gootier**
   - User support email: your address
   - App logo: upload a Gootier square logo (192px PNG works — there's
     one at `static/icons/icon-192.png`).
4. **App domain**:
   - Application home page: `https://gootier.jhomeautomation.com`
   - Application privacy policy: `https://gootier.jhomeautomation.com/privacy`
   - Application terms of service: `https://gootier.jhomeautomation.com/terms`
5. **Authorized domains**: add `jhomeautomation.com`.
6. **Developer contact information**: your email.
7. Click **Save and Continue**.

### Scopes screen

8. Click **Add or Remove Scopes** and pick these three (search the box):
   - `openid`
   - `.../auth/userinfo.email`
   - `.../auth/userinfo.profile`
9. Click **Update → Save and Continue**.

> **Note**: `openid`, `email`, and `profile` are **non-sensitive** scopes —
> they don't require Google verification.  Your app can stay in
> **Testing** mode forever with these scopes.  Sensitive scopes
> (`youtube.upload`, Gmail read, Drive, etc.) DO require verification,
> but only if you also use them.

### Test users (if staying in Testing mode)

10. While the app is in **Testing**, only emails you add here can sign in.
    Add yourself + anyone helping you test.
11. **Save and Continue** through the summary.

### When you're ready to launch

When you want real users to sign in:

12. Back on the **OAuth consent screen** page, click **Publish App** →
    confirm.  As long as you only use non-sensitive scopes, this is
    instant — no Google review required.

## Step 4 — Create the OAuth 2.0 Client ID

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. **Application type**: **Web application**.
3. **Name**: `Gootier Web` (any name — only you see this).
4. **Authorized JavaScript origins**: leave blank.
5. **Authorized redirect URIs** — this is the critical bit.  Add ALL of these:

   ```
   https://gootier.jhomeautomation.com/oauth/google/callback
   http://localhost:8002/oauth/google/callback
   ```

   The localhost entry lets you test against the local Docker container
   (port 8002, the default the Compose file uses).  If you serve locally
   on a different port, swap accordingly.

   If you ALSO want this same OAuth client to handle YouTube uploads,
   add `https://gootier.jhomeautomation.com/oauth/youtube/callback`
   here too.  Otherwise create a separate client for YouTube.

6. Click **Create**.
7. A modal pops up with the **Client ID** and **Client Secret**.  Copy
   both — you won't see the secret again after closing this modal.
   (You can always regenerate it from the credential's detail page.)

## Step 5 — Paste credentials into Gootier

1. In Gootier, go to <https://gootier.jhomeautomation.com/admin/env>
   (you must be signed in as an admin).
2. Find these three rows under the **auth** group:

   | Key                          | Value                                                         |
   |------------------------------|---------------------------------------------------------------|
   | `GOOGLE_AUTH_CLIENT_ID`      | Paste the Client ID                                           |
   | `GOOGLE_AUTH_CLIENT_SECRET`  | Paste the Client Secret                                       |
   | `GOOGLE_AUTH_REDIRECT`       | `https://gootier.jhomeautomation.com/oauth/google/callback`   |

3. Click **Save** on each row.  No app restart needed — values are
   read from the DB on each request.

> **Shortcut**: if `GOOGLE_AUTH_CLIENT_ID` / `GOOGLE_AUTH_CLIENT_SECRET`
> are blank, Gootier falls back to `YOUTUBE_CLIENT_ID` /
> `YOUTUBE_CLIENT_SECRET`.  So if you set up YouTube OAuth with the same
> Google Cloud project, you only need to fill in `GOOGLE_AUTH_REDIRECT`
> here (and add the new redirect URI to the existing OAuth client in
> step 4).

## Step 6 — Test it

1. Open `/login` in an Incognito window (so you're not already signed in).
2. Click **Continue with Google**.
3. Google shows the account picker → pick an account.
4. First time: Google shows a consent screen ("Gootier wants access to:
   your email + profile") → **Continue**.
5. You should land on `/dashboard`, signed in.

### Local container testing

The same flow works against `http://localhost:8002` as long as you added
the `localhost` redirect URI in step 4 and your local
`GOOGLE_AUTH_REDIRECT` is set to `http://localhost:8002/oauth/google/callback`.

Open the local DB env editor:

```bash
docker exec -it gootier-local bash -c \
  'cd /app && python -c "from services.env_config import set_env; \
    set_env(\"GOOGLE_AUTH_CLIENT_ID\", \"YOUR_CLIENT_ID_HERE\"); \
    set_env(\"GOOGLE_AUTH_CLIENT_SECRET\", \"YOUR_SECRET_HERE\"); \
    set_env(\"GOOGLE_AUTH_REDIRECT\", \"http://localhost:8002/oauth/google/callback\")"'
```

Or just visit `http://localhost:8002/admin/env` and paste them in.

---

## Troubleshooting

**`Error 400: redirect_uri_mismatch`**
The redirect URI in step 4 doesn't exactly match `GOOGLE_AUTH_REDIRECT`.
Trailing slash, http vs https, port mismatch — they all break the match.
Fix: copy the URL from Google's error message and add it verbatim to the
client's authorized URIs.

**`Access blocked: This app's request is invalid`**
Usually the consent screen isn't set up.  Go back to step 3 and complete
every required field, save, then retry.

**`This app isn't verified` warning page**
Normal while the app is in **Testing** mode — only added test users can
proceed.  Click **Advanced → Continue to Gootier (unsafe)** to dismiss.
To make the warning go away for real users: complete step 3, then
**Publish App** in the OAuth consent screen page.  Non-sensitive scopes
(what we use) skip Google's review queue.

**`Google declined the sign-in: access_denied`**
The user clicked Cancel on Google's consent screen.  Not a bug.

**Sign-in works but lands on /login with "Invalid or forged sign-in state"**
Something is rewriting the OAuth `state` parameter in the callback URL.
Check that any reverse proxy in front of Gootier isn't stripping query
strings.

**Sign-in works but ends up logged out**
The `SECRET_KEY` env var in Gootier doesn't match between processes (or
changed mid-flow).  All app instances must share the same secret.
