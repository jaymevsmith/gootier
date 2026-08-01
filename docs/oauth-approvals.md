# OAuth approvals — what each platform actually requires

The OAuth flows in Gootier work end-to-end in code, but every platform gates the **write** scopes behind a review process before any user outside your developer-app team can grant them. This doc captures what's needed for each platform, what the review actually asks for, and the rough timeline to plan around.

Run order if you're shipping to real customers: **Meta first** (FB + IG, longest review), **LinkedIn** in parallel, **TikTok** last (audit + privacy gate are the most opinionated).

---

## 1. Meta (Facebook Pages + Instagram)

**One developer app powers both.** The same `META_APP_ID` / `META_APP_SECRET` you set in `/admin/env` handles both Facebook Page connections and the Instagram Business Account connections that Gootier auto-creates from each connected Page.

### Permissions to request

- `pages_manage_posts` — write posts to FB Pages
- `pages_read_engagement` — read post engagement (powers the analytics panel)
- `pages_show_list` — list a user's Pages during OAuth
- `instagram_basic` — read IG account info
- `instagram_content_publish` — publish to IG (images, REELS)

### What Meta's App Review actually wants

1. **Business verification.** Submit your business's legal name, address, tax ID. Meta cross-checks with public records. Usually 1-3 days.
2. **Privacy policy URL.** Must be reachable, must list every Meta permission you use, what data you store, and how users can request deletion. Put it at `/legal/privacy` on your Gootier domain.
3. **Data Deletion Instructions URL.** A separate page or section explaining how a user revokes access + asks for their data to be deleted. `/legal/data-deletion`.
4. **Per-permission screencast** showing the exact flow a user takes that exercises that permission. For `pages_manage_posts` you'd record: log in → Connect Facebook → grant scopes → compose a post → publish → show it on the FB Page. Each clip needs to actually call the API live, not just walk through the UI.
5. **Verbal justification** per permission: "Why does your app need this?" Keep it under 50 words each. The pattern that gets approved: describe the user-facing feature, the API call, and the data you read/store.
6. **App icon (1024×1024 PNG, transparent), tagline, category.** Mostly cosmetic but required.

### Common rejection reasons

- Reviewers can't reproduce the flow because your dev app is in **Development mode** and they aren't added as a Tester. Add them: `App Settings → Roles → Add Test Users` and use the email Meta sends in the review communication.
- Privacy policy missing a specific permission by name. Reviewers grep for the literal permission strings (`pages_manage_posts`, etc.).
- Screencast doesn't show the actual scope being granted — the OAuth dialog with the permission name highlighted is non-negotiable.

### Timeline

- Business verification: 1-3 days
- App Review per submission round: 5-10 business days
- Average from first submission to approval: **2-4 weeks** with one revision round

### Until approved

App is in **Development mode** — only people listed as Admin / Developer / Tester on your Meta app can complete the OAuth flow. Use this period to test the full publishing path internally and on a couple of pilot customer accounts (add them as Testers explicitly).

---

## 2. LinkedIn (Marketing Developer Platform)

The default LinkedIn dev app gets you `openid profile email` — enough to sign users in but not enough to **post on their behalf**. To get `w_member_social` you need to apply to LinkedIn's **Marketing Developer Platform**.

### Permissions to request

- `openid`, `profile`, `email` — sign-in basics (no review needed)
- `w_member_social` — publish UGC posts as the authenticated user (review required)
- `w_organization_social` — only if you want to post as a Company Page (separate review)

### Application form

LinkedIn's intake form is at **developer.linkedin.com → Products → Share on LinkedIn → Request Access**. It asks for:

1. **Company name + LinkedIn Company Page URL.** Your business needs an actual Company Page that's been around for a while.
2. **Product description** (200 words). What does Gootier do, who's the user, how does posting to LinkedIn fit?
3. **Expected monthly API call volume.** Estimate honestly — they care more about the integration's purpose than the exact number.
4. **Use-case category** — pick "Social Media Management" or "Content Publishing".
5. **Demo video or live URL.** A 60-90 second screencast showing a user connecting their LinkedIn, composing a post in Gootier, hitting publish, and seeing it appear on their LinkedIn feed.
6. **Privacy policy and terms of service URLs.**

### Common rejection reasons

- Company Page is brand-new (LinkedIn prefers >6 months old + some posting history).
- Demo video shows mock-ups instead of the real flow.
- Privacy policy doesn't mention LinkedIn or `w_member_social` by name.
- You ask for both `w_member_social` AND `w_organization_social` in the same application — submit them separately, member first.

### Timeline

- Response: **1-3 weeks** for the first review, often radio silence in between
- Approval is per-app — if you stand up a staging environment, you have to re-apply for that app

### Until approved

The flow works for the developer app owner (you) only. You can hand-add **Verified App Authors** to your app, but it's a friction-heavy path — not suitable for paying customers.

---

## 3. TikTok (Content Posting API)

TikTok's flow has the most opinionated UX requirements of any platform here. Read carefully — the publish behavior is fundamentally different from the others.

### Two tiers of access

- **Unaudited apps**: `PULL_FROM_URL` content posting works, but every post lands in the user's **TikTok app inbox**. The user has to open the TikTok app and tap to publish. Privacy must be `SELF_ONLY` (no public posts). This is what Gootier uses today.
- **Audited apps**: Direct publish (no inbox step), `PUBLIC` privacy allowed, plus other Content Posting endpoints.

### Setup steps (sandbox / development)

1. Sign up at **developers.tiktok.com** with a personal TikTok account.
2. Register a new app — pick the **Login Kit** and **Content Posting API** products.
3. Set the **Redirect URI** to `https://gootier-prod.up.railway.app/oauth/tiktok/callback`.
4. **Add a Verified Domain.** TikTok requires the domain hosting any video URL you `PULL_FROM_URL` to be verified via a DNS TXT record or `.well-known/tiktok-developers-site-verification.txt` file. Add `fal.media` and your own app domain.
5. Copy Client Key + Client Secret → `/admin/env` → `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`.

### Audit application

To get past the inbox model and let users publish directly:

1. Have your app in **Production status** (toggled in the dev portal — they cap you at 100 unique users until audited).
2. Submit an **Audit application** from the app dashboard.
3. Provide:
   - Demo video (3-5 min) showing the full user flow including the post that lands on TikTok
   - Privacy policy URL covering the Content Posting permissions you request
   - Terms of service URL
   - A written compliance statement: how do you prevent users from publishing copyrighted content? How do you handle takedown requests? How do you enforce that the user owns the content they're uploading?
4. TikTok responds in **2-6 weeks**. Resubmissions are common — most first-round responses ask for specific copy changes on the privacy policy.

### Permissions to request

- `user.info.basic` — display name, avatar
- `video.publish` — submit videos for posting
- `video.upload` — upload videos (PULL_FROM_URL doesn't strictly need this but the audit form expects it)

### Common rejection reasons

- No DNS verification on the domain hosting your videos (fal.media gets rejected if you didn't verify it under your app).
- Privacy policy doesn't address copyright / DMCA / content moderation.
- Audit video missing the "Privacy Policy" / "Terms of Service" footer links inside the Gootier UI itself.
- App's described use-case sounds like spam (e.g. "automated cross-posting" without showing the user's editorial control).

### Timeline

- Sandbox: same-day. Real publishing to your own TikTok works immediately.
- Production status (cap removed): instant toggle once audit is approved.
- Audit: **2-6 weeks** including the resubmission rounds.

### Until audited

Production status is capped to 100 unique users. The inbox-publishing flow is **functional** but every TikTok post requires the user to confirm it inside the TikTok app — flag this loudly in your customer onboarding so they're not surprised.

---

## Where to register each redirect URI

For Gootier on Railway, the production redirect URIs are:

| Platform | Redirect URI |
|---|---|
| Facebook + Instagram | `https://gootier-prod.up.railway.app/oauth/facebook/callback` |
| LinkedIn | `https://gootier-prod.up.railway.app/oauth/linkedin/callback` |
| TikTok | `https://gootier-prod.up.railway.app/oauth/tiktok/callback` |

If you move to a custom domain (e.g. `app.gootier.com`), update both the platform-side registration **and** the corresponding `*_OAUTH_REDIRECT` value in `/admin/env`. They must match byte-for-byte — protocol, host, path, no trailing slash.

---

## Realistic timeline summary

If you start today with no business verification and no dev apps registered:

| Day | What's possible |
|---|---|
| Day 0-3 | All three dev apps registered, Gootier OAuth flows work for you + handpicked testers, fully testable end-to-end |
| Week 1-2 | LinkedIn application submitted, Meta business verification kicked off, TikTok sandbox connected for any TikTok user you list as a tester |
| Week 3-6 | LinkedIn approved (typical), Meta first-round response (usually a revision ask) |
| Week 6-10 | Meta approved after revision, TikTok audit response |
| Week 10+ | All three platforms fully open to any customer who signs up |

Plan customer-facing launch for **6-10 weeks out** if you want all three platforms unblocked at GA. Or launch with Facebook + LinkedIn only and add IG/TikTok as their approvals land — Gootier's per-platform connect cards already gray out when a platform isn't configured, so this gating is a per-env-var concern, not a code release.

---

## What lives where in Gootier when you're filling out these forms

- **Privacy policy URL**: not yet at `/legal/privacy` — write one before submitting Meta or LinkedIn. (See deferred follow-up.)
- **Terms of service URL**: same — `/legal/terms`.
- **OAuth callback URLs**: documented above.
- **Demo video script**: walk through `/login → /connections → click Connect → grant scopes → /compose → write post → publish → show on platform`. Total under 90 seconds per platform.
- **API call volume estimate**: scheduler fires once per minute, publishing 0-N due posts; analytics tick every 10 min updates ~20 posts. At 100 active users posting 10/week each, that's ~1,500 writes + ~12k reads per week per platform.
