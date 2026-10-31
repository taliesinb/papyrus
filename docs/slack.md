# Slack importer: research notes (2026-10-30)

Goal: find arXiv links and other paper/PDF links posted in a Slack workspace,
and record where each was shared, for the `release` variant. This note covers
the API, setup, auth and filters. Feeding the links into the pipeline comes
later, through the resolver described in
[architecture.md, "Adding a context source"](architecture.md#adding-a-context-source-slack-safari-reading-list-twitter).

## What we collect

One `contexts` row per message that has at least one matching link (the row
shape is already in [schema.md](schema.md#contexts-context_refs)), and one
unresolved `context_refs` row per link.

| field | where it comes from |
|---|---|
| `locator`, `url`: permalink | Built locally: `https://<domain>.slack.com/archives/<channel_id>/p<ts without the dot>`, plus `?thread_ts=<parent ts>&cid=<channel_id>` for thread replies. `chat.getPermalink` would cost one call per message. |
| `text`: the message as Markdown | Converted from the message's `blocks` (`rich_text`: links with their labels, user and channel mentions, code, lists), falling back to `text` in Slack's mrkdwn (`<url\|label>`, `<@U123>`, `*bold*`). Mentions are rendered with display names. |
| `actor`, `actor_id` | `user` (`U…`) of the message; display name from `users.list`, cached per run. Bots have `bot_id`/`username` instead. |
| `occurred_at` | `ts` (epoch seconds) as UTC ISO 8601. `edited.ts` goes in `extra`. |
| `source` | `<workspace>/#<channel>`; DMs as `<workspace>/@<other user>`, group DMs as `<workspace>/mpdm`. |
| `parent_id` | The thread's root message context, if it has a link too. |
| `extra` | `team_id`, `channel_id`, `channel_type` (`public`/`private`/`im`/`mpim`), `ts`, `thread_ts`, `reply_count`, `reactions`, `files`, and the unfurl of each link (`attachments[].title`, `from_url`, `service_name`): a free title for the ref, like a download context's link title. |

Files uploaded into Slack (`files[]`, PDFs especially) are links too:
`url_private` needs the token to download, so record them in `extra.files` now
and handle them in the resolver.

## How to read Slack

| option | reach | limits | verdict |
|---|---|---|---|
| **Internal app + user token** (`xoxp-`), `conversations.history`/`replies` | Every conversation the user is in, DMs included (per scope) | Internal apps keep Tier 3 for history/replies: 50+ requests/min, up to 1,000 messages per page ([Slack, 2025-05-29](https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps/), [clarification](https://docs.slack.dev/changelog/2025/06/03/rate-limits-clarity/)) | **Recommended.** Complete and fast enough. |
| Same token, `search.messages` (`search:read`) | Whatever the user's search sees, including old threads with new replies | Tier 2 (20+/min), 100 results per page, at most 100 pages; one result per cluster of nearby matches; marked "legacy" ([docs](https://docs.slack.dev/reference/methods/search.messages)) | Useful as a cheap fast path (`arxiv.org after:2026-10-01`), not as the only source: it can drop matches. |
| Real-time Search API, `assistant.search.context` | Same as search, with `channel_types` and `after`/`before` | ~10 requests/min per user, 20 results per page, meant for user-initiated AI queries; internal or Marketplace apps only ([guide](https://docs.slack.dev/apis/web-api/real-time-search-api)) | Wrong tool for a periodic scrape. |
| A distributed (non-Marketplace) "papyrus" app | – | `conversations.history`/`replies` drop to 1 request/min and 15 messages | Ruled out. Each user makes their own internal app instead. |
| Workspace export (admin) | Public channels only on Free/Pro | Manual ZIP | Fallback for a one-off backfill if we ever have admin rights. |
| Browser session token (`xoxc-` + `d` cookie), e.g. [slackdump](https://github.com/rusq/slackdump) | Everything the web client sees, no app needed | Against the spirit of the API terms; can break or trip security alerts | Only if the workspace forbids installing apps. Not for papyrus by default. |
| Official Slack MCP server | Search/read via an MCP client | Built for chat clients, not bulk export | Not for the CLI. |

## Setup and auth flow

1. **Create an internal app from a manifest.** `papyrus slack setup` prints the
   manifest and opens
   `https://api.slack.com/apps?new_app=1&manifest_json=<urlencoded JSON>`
   (Slack pre-fills the "create from manifest" dialog). The user picks the
   workspace. User-token scopes only, no bot:
   * `channels:history`, `channels:read` (public channels)
   * `groups:history`, `groups:read` (private channels)
   * `im:history`, `im:read`, `mpim:history`, `mpim:read` (DMs; optional, see
     filters)
   * `users:read` (display names)
   * `search:read` (optional fast path)
   * `files:read` (optional, to fetch PDFs uploaded to Slack later)
2. **Install to the workspace.** If the workspace requires app approval, this
   becomes a request to an admin. That is the one step we can't automate, and
   the main unknown per workspace.
3. **Hand the token to papyrus.** Two ways:
   * *Paste* (simplest, recommended first): after installing, the app's
     "OAuth & Permissions" page shows the `xoxp-` user token;
     `papyrus slack login` reads it (hidden prompt), checks it with
     `auth.test` (which returns the team, user and workspace URL), and stores
     it. Without token rotation, user tokens don't expire; they die when
     revoked or the app is uninstalled.
   * *OAuth with PKCE* (GA since [2026-03-30](https://docs.slack.dev/changelog/2026/03/30/pkce)):
     `papyrus slack login` opens the authorize URL and catches the code on a
     `localhost` redirect, with no client secret. But enabling PKCE is
     one-way, and refresh tokens for PKCE apps expire after 30 days, so an
     unattended scraper would need re-logins. Worth it only if pasting proves
     annoying.

## Credentials

* **macOS Keychain**, via `/usr/bin/security add-generic-password` /
  `find-generic-password -w` (no new dependency):
  service `papyrus.slack`, account `<variant>/<team_id>`. Several workspaces
  and variants can coexist.
* `PAPYRUS_SLACK_TOKEN` overrides it, as `S2_API_KEY` does for Semantic
  Scholar (tests, CI, one-offs).
* Never in `config.toml`, the database, logs or run args. `config.toml` keeps
  the non-secret settings under `[slack]`: workspace, channel lists, days,
  domain list.

## CLI and filters

```sh
papyrus slack setup                 # manifest + create-app URL
papyrus slack login [--workspace W] # paste or OAuth, store in Keychain
papyrus slack channels              # conversations the token sees, for picking filters
papyrus import-slack --days 30 [--dms | --no-dms] \
    [--channel NAME ...] [--exclude-channel NAME ...] \
    [--domains FILE] [--all-links] [--dry-run]
```

* `--days N` sets `oldest` on `conversations.history`. A reply posted today to
  an older thread is missed unless the thread's root is in range;
  `search.messages … after:<date>` catches those.
* `--dms/--no-dms` toggles `im,mpim` in `users.conversations types=`. Default
  off: DMs are the most private part of the workspace.
* Channels by name or id; `--channel` is an allow list, `--exclude-channel`
  a deny list.
* `--dry-run` prints what would be stored (as JSON lines), so we can review
  real output before writing to the release library.
* Cost: one `users.conversations` page, one `conversations.history` call per
  channel per 1,000 messages, and one `conversations.replies` per thread with
  replies, so threads dominate. At Tier 3 that's minutes, not hours, for a
  month of a busy workspace. Honour `Retry-After` on 429.
* Incremental runs: per channel, remember the newest `ts` scanned (in the
  run's stats, or a small state file next to the database) and pass it as
  `oldest` next time. Upserting on `(kind, locator)` keeps re-runs harmless
  either way.
* One recorded run per import (`db.recorded_run(conn, "import-slack", …)`);
  all Slack calls happen before the single write transaction.

## Which links count

In order of precision:

1. **Identifiers in the URL**: arXiv ids and DOIs, using `enrich/ids.py`
   (`find_arxiv_ids`, `find_dois`). This covers arxiv.org, alphaxiv,
   doi.org and every publisher URL that embeds a DOI (Wiley, ACM, Springer,
   T&F, PNAS, …).
2. **PDF-looking URLs**: path ends in `.pdf`, or known PDF routes
   (`openreview.net/pdf`, `…/download`). Uploaded PDFs (`files[].filetype
   == "pdf"`).
3. **A domain/regex allow list** for landing pages without a DOI in the URL
   (nature.com/articles, openreview.net/forum, proceedings.mlr.press,
   aclanthology.org, papers.nips.cc, semanticscholar.org, biorxiv, ssrn, …).

For 3, there's no clean, ready-made "scholarly hosts" list, but these sources
come close:

* **[Zotero translators](https://github.com/zotero/translators)**: 752
  translators, each with a `target` URL regex, updated weekly. Classifying
  them by the item types in their test cases gives about **261** that produce
  journal articles, preprints, conference papers, theses or reports
  (arXiv, Nature, J-Stage, Atypon's generic `/doi/10.` pattern, …). The other
  ~440 are newspapers, catalogues, video sites and so on. They are regexes,
  not hosts, and the files are AGPL-3, so fetch and derive the list at
  runtime (or `papyrus slack update-domains`) instead of vendoring it.
* **OpenAlex sources** (`/sources?filter=type:journal|repository|conference`):
  ~260k venues with `homepage_url` and publisher. Good for hosts, noisy for
  paths.
* **OpenDOAR** (Jisc): ~6,000 institutional and subject repositories with base
  URLs.
* **ROR** (`domains` field): university hosts, for PDFs on faculty pages. Too
  broad to accept on its own; use it together with `.pdf`.
* A small hand list in papyrus for ML hosts the others miss (openreview,
  PMLR, ACL Anthology, NeurIPS/ICML/ICLR proceedings, CVF open access,
  alphaxiv, huggingface.co/papers, paperswithcode, Semantic Scholar).

A fallback that avoids lists: `--all-links` stores every link, and the resolver
decides by fetching the page and looking for Highwire `citation_*` meta tags
(`citation_title`, `citation_pdf_url`, `citation_doi`). Google Scholar
requires these of indexed publishers, so they are the most reliable "is this a
paper" test, but each link costs a fetch. That belongs in the resolver, not
the scrape.

## Open questions

* **Workspace:** Symbolica. Whether it needs admin approval for app
  installs is unknown; installing the app below answers it (the button says
  "Request to Install" instead of "Install").
* **DMs:** off by default, so the first manifest leaves out the `im:*` and
  `mpim:*` scopes. Adding them later means reinstalling the app, which issues
  a new token.
* **Does `search.messages` match link hosts (`arxiv.org`)?** Unknown. The
  API uses the same engine as the search box, so a first check is in the
  Slack UI. The definitive check needs the token: scan one channel over 30
  days with `conversations.history`, run `arxiv.org in:#channel after:<date>`
  through `search.messages`, and compare the two sets of arXiv messages.

First manifest (user scopes only, read-only, no DMs):

```json
{
  "display_information": {
    "name": "papyrus",
    "description": "Reads messages for paper links (personal, read-only)."
  },
  "oauth_config": {
    "scopes": {
      "user": ["channels:history", "channels:read", "groups:history",
               "groups:read", "users:read", "search:read"]
    }
  },
  "settings": {
    "org_deploy_enabled": false,
    "socket_mode_enabled": false,
    "token_rotation_enabled": false
  }
}
```
