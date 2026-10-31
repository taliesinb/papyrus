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
| `locator`, `url`: permalink | The search match's `permalink`: `https://<domain>.slack.com/archives/<channel_id>/p<ts without the dot>`, plus `?thread_ts=<root ts>` for thread replies (checked against local construction for 928 messages). A full scan builds the same URL locally; `chat.getPermalink` would cost one call per message. |
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
| Same token, `search.messages` (`search:read`) | Whatever the user's search sees, including old threads with new replies | Tier 2 (20+/min), 100 results per page, at most 100 pages; one result per cluster of nearby matches; marked "legacy" ([docs](https://docs.slack.dev/reference/methods/search.messages)) | **Primary path** after the 2026-10-30 check below: it found every arXiv message a full scan found, in one call instead of ~1,000. |
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
   * `search:read` (the main path, see the check below)
   * `files:read` (optional, to fetch PDFs uploaded to Slack later)
2. **Install to the workspace.** If the workspace requires app approval, this
   becomes a request to an admin. That is the one step we can't automate, and
   the main unknown per workspace (Symbolica needed no approval).
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

* **macOS Keychain**, via `/usr/bin/security` (no new dependency): service
  `papyrus.slack`, account `<variant>/<workspace>`, where `<workspace>` is a
  short name you choose at login (`release/symbolica`). Several workspaces
  and variants can coexist. `papyrus slack login` writes it through
  `security -i` on stdin, so the token never appears in a process list.
* `PAPYRUS_SLACK_TOKEN` overrides it, as `S2_API_KEY` does for Semantic
  Scholar (tests, one-offs).
* Never in `config.toml`, the database, the API log (redacted) or run args.

## CLI and filters (built 2026-10-30, `contexts/slack.py`, `contexts/slack_cli.py`)

```sh
papyrus slack setup [--open]          # manifest + create-app URL
papyrus slack login -w symbolica      # paste the xoxp- token (hidden), check it, store it
papyrus slack whoami                  # user, team, scopes of the stored token
papyrus slack links --days 30 [--since YYYY-MM-DD] [--dms | --no-dms] \
    [--channel NAME ...] [--exclude-channel NAME ...] [--domain HOST[/PATH] ...] \
    [--all-links] [--json] [-o FILE] [-v]
```

`links` reads only: it prints one entry per message (with `-v` the message as
Markdown; with `--json` the full record, i.e. the future `contexts` row with
its links) and adds nothing to the library. It is a recorded run
(`slack-links`), so its API log events carry the run id.

* **How:** `has:link after:<date>` and `type:pdf after:<date>`, all pages,
  merged by `(channel, ts)`; then `users.list` and `users.conversations`
  for names. 7 calls for 30 days on Symbolica.
* `--days` (default `[slack] days`, else 30) or `--since`; `after:` is
  exclusive, so the query goes a day further back and results are cut at
  the exact time.
* `--dms/--no-dms` (default `[slack] dms`, else off): drops matches whose
  channel `is_im`/`is_mpim`. Search ignores token scopes, so this filter is
  what keeps DMs out.
* `--channel` (allow list) and `--exclude-channel`: name, `#name` or id;
  defaults `[slack] channels` / `exclude_channels`.
* `--domain` adds allow-list entries to the built-in `links.DEFAULT_DOMAINS`
  (as does `[slack] domains`); `--all-links` keeps every web link.
* Slack caps a search at 100 pages of 100. If a period has more, the summary
  says so; use fewer days.
* Not covered by search (measured above): a reply in a thread is found, but
  1 web-link message in 929 was missed. A full scan (`--full-scan`, not built
  yet) would be the audit for that.
* Next: write the records as `contexts` (`kind='slack'`, upsert on the
  permalink) and `context_refs`, in one transaction after the API calls.

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

* **Workspace:** Symbolica (`symbolicaai.slack.com`, `T06G6CMDF1V`). Installing
  the internal app needed no admin approval. The user token is in the
  Keychain as service `papyrus.slack`, account `release/symbolica`.
* **DMs:** off by default, so the first manifest leaves out the `im:*` and
  `mpim:*` scopes. Adding them later means reinstalling the app, which issues
  a new token.
* **Does `search.messages` match link hosts (`arxiv.org`)?** Yes, checked
  2026-10-30 on the 26 channels the user is in. A full scan
  (`conversations.history` + `conversations.replies`) and one
  `search.messages` query, `arxiv.org after:<date>`, found the same messages
  with arXiv links: 13 of 13 over 30 days and 63 of 63 over 180 days, with
  nothing found by only one side. The 63 include 20 thread replies, 3 messages
  whose link was only in the unfurl (`attachments`), and links behind labels.
  Search also returned 12 DM matches, which were left out of the comparison.
* **Does `has:link after:<date>` list every message with a link?** Yes, for
  web links (checked 2026-10-30, 180 days, 26 channels; raw responses in the
  API log, `~/.papyrus/release/api-log/2026-10-30.jsonl`). The full scan read
  9,294 messages (1,005 calls, 12 min); the search returned 1,415 messages
  in 15 calls (4 s), 929 of them in those channels and 486 in DMs and group
  DMs (`channel.is_im` / `is_mpim`: filter locally, since search ignores
  scopes the token has).
  * Agreed on 928. All 63 arXiv messages were among them, including the ones
    whose link was only in the unfurl.
  * Scan only, but not web links: 847 messages whose only "link" is an
    uploaded file, 34 `mailto:`/`tel:` links, 5 shares of other Slack
    messages (`is_msg_unfurl`), 11 bot posts.
  * Scan only, a real miss: 1 GitHub link in a thread reply.
  * Search only: 1 reply posted inside the window to a thread whose root is
    older, which `conversations.history` with `oldest` can't see.
* **Uploaded PDFs:** `type:pdf after:<date>` found exactly the 20 messages
  with a PDF upload that the scan found (23 PDFs), in 1 call. `has:pdf` is
  something else (0 of 20).
* **What a search match carries:** `text` (mrkdwn), `blocks` (1,411 of 1,416;
  rich text with typed `link`, `user`, `channel`, `emoji` elements, so
  faithful Markdown is possible), `attachments` (unfurls, identical to the
  scan's), `files`, `user`, `username`, `ts`, `channel` (id, name, type
  flags), and `permalink`. Search's blocks drop `display_url` and
  `display_team_id` on some elements (36 of 928 messages); nothing else
  differs.
* **Permalinks:** search's `permalink` equals the locally built
  `/archives/<channel>/p<ts>` for all 928, and adds `?thread_ts=<root ts>`
  for thread replies. Use it as `contexts.locator` as given.

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
