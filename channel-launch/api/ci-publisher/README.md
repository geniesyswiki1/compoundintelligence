# ci-publisher

Programmatic publishing for the Compound Intelligence channels. YouTube Data
API v3 today; other platforms slot in behind the same CLI.

## Setup

```bash
cd channel-launch/api/ci-publisher
python3 -m pip install -r requirements.txt
```

### 1. Create the OAuth client

In Google Cloud Console, in the project that owns the channel:

1. **APIs & Services → Library** → enable **YouTube Data API v3**.
2. **OAuth consent screen** → External. While the app is in Testing, add the
   channel's Google account under **Test users**. Skipping this is the usual
   cause of `access_denied` at the consent screen.
3. **Credentials → Create credentials → OAuth client ID → Desktop app.**

The client type matters and the tool enforces it. Google's TV/limited-input
device flow refuses the `youtube.force-ssl` scope with `invalid_scope`, and
that scope is what reading comments requires. A Desktop app client uses the
loopback redirect and is granted it. A Web client cannot use the loopback
redirect at all.

### 2. Put the client secrets on disk

Download the JSON and save it as `client_secret.json` in this directory:

```bash
mv ~/Downloads/client_secret_*.apps.googleusercontent.com.json ./client_secret.json
```

The file is gitignored, along with `.env` and `token*.json`. It is also found
automatically under the name the Console gives it, so renaming is optional.
Set `CI_PUBLISHER_CLIENT_SECRETS` to use a path elsewhere.

### 3. Authorise

```bash
python3 -m ci_publisher auth youtube
```

A browser opens for consent, lands on a localhost page, and the token is
written to `~/.ci-publisher/tokens/youtube.json` (mode 0600). The command
then prints which channel the token actually reaches. Check that line: one
token is one channel, and the credential itself says nothing about which
channel it controls.

On a headless or remote machine, use `--no-browser`. It prints the consent
URL, you approve in any browser, the browser fails to load a localhost page,
and you paste that page's full URL back. Nothing needs to be listening there.

When the approval happens somewhere other than the shell running the command,
split it in two:

```bash
python3 -m ci_publisher auth youtube --print-url        # prints the URL, exits
python3 -m ci_publisher auth youtube --code '<the URL the browser landed on>'
```

`--print-url` parks the PKCE `code_verifier` in
`~/.ci-publisher/pending_auth.json`, because that value otherwise lives only
in memory and the exchange would fail with "code verifier is invalid". The
authorisation code itself expires within minutes, so run `--code` promptly.

## Commands

```bash
python3 -m ci_publisher doctor            # check every layer, stop at the first failure
python3 -m ci_publisher whoami            # which channel does this token control
python3 -m ci_publisher quota             # units spent and uploads left today
python3 -m ci_publisher logout            # delete the stored token

python3 -m ci_publisher upload video.mp4 \
    --title "Episode 1" \
    --description "..." \
    --tags "one,two" \
    --publish-at 2026-10-20T14:30:00Z \
    --thumbnail thumb.png
```

`doctor` checks credentials, then the stored token, then scopes, then
refreshability, then channel reachability, then quota, and stops at the first
failure. Every layer below a broken one reports a misleading error, so there
is nothing to learn from continuing past it.

## Quota is the binding constraint

The daily allowance is 10,000 units. An upload costs 1,600, so roughly **six
uploads a day**. Thumbnails cost 50, reads cost 1.

`ci_publisher/quota.py` keeps a local ledger in `~/.ci-publisher/quota.json`
and debits **before** each call, refusing outright once the day's budget
cannot cover the next operation. Google charges for a request whether or not
it succeeds, so firing one that is certain to fail costs the same as one that
works. The quota day resets at **midnight Pacific**, not UTC and not local
time, and the ledger follows that.

If Google returns `quotaExceeded` while the ledger still shows units left,
the ledger has drifted because requests were made outside this tool. Reconcile
against **APIs & Services → YouTube Data API v3 → Quotas**.

## Publishing safely

- Uploads default to `private`. `--privacy public` is refused unless you also
  pass `--confirm-public`, which is a decision for that specific video.
- Scheduling uploads as private with `status.publishAt` set, and YouTube flips
  it public at that instant with nothing running locally. Use `--publish-at`.
- A `--publish-at` slot in the past is refused rather than published
  immediately, because the point of scheduling is that a backlog does not land
  at once. `--shift-past-slot` moves it to the same time on the next day.

## Traps worth knowing

- **Custom thumbnails need a phone-verified channel.** The API returns a 403
  that reads like a scope problem. It is an account problem: verify at
  <https://www.youtube.com/verify>. A thumbnail failure is reported separately
  from the upload, which has already succeeded.
- **A token is only refreshable against the client that minted it.** The
  issuing `client_id` is recorded beside each token and checked on load, so
  swapping the client secrets file produces a clear error instead of a
  confusing refresh failure.
- **Refresh tokens expire after 7 days while the OAuth app is in Testing.**
  Publish the consent screen to stop re-authorising every week.
- **There is no "under one minute" exemption for copyrighted music.**
  YouTube's licensed catalogue only applies to tracks added through the in-app
  Shorts picker, which API uploads cannot use. Muxing a commercial track into
  an API upload earns a Content ID claim, and since October 2024 a Short over
  60s with an active claim is blocked globally. Use original or genuinely
  royalty-free audio.

## Tests

No network and no credentials required:

```bash
python3 -m unittest discover -s tests -t .
```

## Layout

| Path | Purpose |
| --- | --- |
| `ci_publisher/cli.py` | argparse front end and the publishing guards |
| `ci_publisher/auth.py` | loopback OAuth, scopes, token refresh |
| `ci_publisher/config.py` | finding and validating client secrets |
| `ci_publisher/store.py` | token storage, records the issuing client |
| `ci_publisher/quota.py` | the daily unit ledger |
| `ci_publisher/youtube.py` | uploads, thumbnails, scheduling, error translation |
| `ci_publisher/errors.py` | exception types, messages written for the operator |
