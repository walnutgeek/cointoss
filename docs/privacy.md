# Privacy

_Last updated 2026-10-27. The history of this page is the
[git log of `docs/privacy.md`](https://github.com/walnutgeek/cointoss/commits/main/docs/privacy.md)._

## Scope

This page covers the cointoss instances this project runs: `int` today, and `prod` once it
runs. cointoss is open source, and anyone can run their own instance. A self-hosted instance
is the responsibility of whoever runs it, and this page does not describe it.

cointoss is served by [woodglue](https://github.com/walnutgeek/woodglue), which handles
sign-in, sessions and access control. What follows describes what woodglue and cointoss do,
as checked against their code and the deployment.

## Who can reach an instance

An instance is reachable only inside the operator's tailnet, the private network
[Tailscale](https://tailscale.com/) provides. It is not on the public internet. Guests are
invited to the tailnet and granted access to the instance and nothing else on it, so Tailscale
also holds their tailnet identity (see [Sharing](#sharing)).

## What is collected at sign-in, and why

You sign in with an account you already have. The instance asks the provider only for what it
needs to know who you are and whether to let you in.

- **Google** (scopes `openid email profile`): your Google account id, your name, and your email
  address with whether Google has verified it.
- **GitHub** (scopes `read:user user:email`): your GitHub account id, your name (or your login
  if you have not set a name), and your primary email if it is verified. If an instance's rules
  ever refer to GitHub organizations or teams, it also asks for `read:org` and reads your
  organization and team memberships.

Your identity is checked against the instance's admission rules, and the matching roles are
assigned. That is the only use of this information: deciding whether you may sign in and what
you may do once you have.

The instances admit anyone who reaches them and signs in, with no roles. Your first sign-in
creates your account record (name, verified email, and the linked provider account) and a
session, but gives no access to any data until the operator approves the account by giving it
a role, or disables it. The record stays while it is pending, and after it is disabled. A
sign-in that an instance's admission rules refuse is turned away before any record is created;
only the log line described under [Logs](#logs) is kept.

## What is stored

woodglue keeps accounts in a database on the instance's host, `auth.db`:

- Your name, your email if the provider verified it, and the provider and account id it is
  linked to. An unverified email is not stored.
- Your role assignments.
- Your sessions and API tokens, each with when it was created, when it was last used and when
  it expires. A session ends after 12 hours without use or 7 days after sign-in, whichever
  comes first.

The provider's access token is used only during sign-in, to read the details above, and is
not stored. Organization and team memberships are not stored either; only the roles they lead
to are.

## Cookies

- `wgl_session`: your session. It is HttpOnly, so no page script can read it, and is sent only
  over HTTPS when the instance is served over HTTPS.
- `wgl_login`: set when you start signing in and cleared when the provider sends you back. It
  ties the sign-in to your browser.
- `_streamlit_xsrf`: set by the Dataset explorer (Streamlit) to protect its forms against
  cross-site requests.

There are no analytics or advertising cookies, and no trackers. Streamlit's usage statistics
are turned off (`--browser.gatherUsageStats=false`).

One third-party request remains: woodglue's console page (`/console/`) loads its fonts from
Google Fonts, so opening it sends a request, with your IP address, to Google.

## Logs

The service's log goes to the systemd journal on the host. It is used to operate the service
and keep it secure. It records:

- One line per request: method, path and query string, response status, response time, and the
  address the connection came from. The instance sits behind a Tailscale proxy on the same
  host, so that address is the proxy's (`127.0.0.1`), not yours.
- Each sign-in, with your account id and email, and each sign-in refused by the admission
  rules, with the provider's account id and the email it gave.

The journal is kept as long as journald keeps it. The host sets no retention of its own, so
journald's defaults apply: the journal is bounded by size (at most 10% of its file system, and
at most 4 GiB), not by age, and the oldest entries are deleted as space is needed.

## Backups

`cointoss backup` never copies `auth.db`. The deployment's daily snapshot does copy it, as a
separate file readable only by the service account (mode 600), so that accounts and approval
decisions survive rebuilding or moving the instance. The copy holds everything `auth.db` holds,
including session and token secrets, which woodglue stores as they are rather than hashed.
Snapshots stay on the instance's host. They are kept for the last 7 days plus one for each of
the last 4 weeks; snapshots labelled for a specific event, such as an upgrade, are kept until
deleted by hand.

## Sharing

Your data is not sold, and it is not shared with anyone.

- Sign-in goes through the provider you choose, under its own privacy policy:
  [Google](https://policies.google.com/privacy) or
  [GitHub](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement).
- Traffic reaches the instance through the operator's tailnet. As a guest you have a Tailscale
  account in it, under [Tailscale's privacy policy](https://tailscale.com/privacy-policy).

## Market data

The prices and universes cointoss records come from CoinGecko. They are public market data and
contain no personal data.

## Your choices

- **See what is stored about you:** ask (see [Contact](#contact)), and you will be told what
  `auth.db` holds for your account.
- **Have your account removed:** ask, and the account is disabled, which deletes all of its
  sessions and tokens at once, and then its record is deleted from `auth.db`. Copies in
  snapshots taken before then age out on the schedule above, except in labelled snapshots,
  which last until those are deleted.
- **Sign out:** signing out deletes your session on the instance.
- **Revoke the app's access** at the provider: in Google, under
  [Third-party apps and services](https://myaccount.google.com/connections); in GitHub, under
  [Settings, Applications, Authorized OAuth Apps](https://github.com/settings/applications).
  Revoking it at the provider does not end a session already open on the instance; ask to have
  your account removed for that.

## Contact

Open an issue at <https://github.com/walnutgeek/cointoss/issues>. Issues are public, so do not
put anything in one that you would not want published. To ask about your own account, open an
issue saying so, without personal details, and the maintainer will reply there to arrange it.

## Changes

When what the instance collects or keeps changes, this page changes with it, and its date is
updated. Every change is in the git history linked at the top.

## This site

This documentation site is hosted on GitHub Pages, under
[GitHub's privacy statement](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement).
It sets no cookies of its own and loads no fonts, analytics or trackers from other sites. The
repository link in its header asks GitHub's API for the repository's latest release, stars and
forks.
