# Security

## What to know first

- **The Script key can read across your site and act as its people.** ReviewMeh limits what each person sees, but a flaw in the app could expose anything the key can reach. We recommend not using an API Admin Script: [give the Script its own permission group](shotgrid-setup.md#permissions-the-script-needs) instead. Keep `.env` and `reviewmeh.toml` private.
- **The data folder holds secrets.** Unless you set them yourself, the sign-in secret, menu key and webhook token are created in `secrets/` inside `storage.data_dir` (`/data` in Docker). Anyone who can read them can sign in as anyone, so protect that folder and its backups as you would the Script key.
- **Use HTTPS.** Sign-in cookies are only marked secure when `server.public_url` starts with `https://`.
- **Run ReviewMeh as an unprivileged user.** The Dockerfile and the example systemd unit already do this.

## Identity

ShotGrid's Action Menu Items post a `user_id`, but nothing in the request proves it is genuine. Anyone who can see Action Menu Items can read a menu item's address, so `hooks.ami_key` only keeps out strangers. The key allows opening a review page and adding the selected Versions to a review. It never signs anyone in.

People are identified by **Sign in with ShotGrid**, which ShotGrid itself approves.

### `hooks.trust_ami_user`

Turning this on makes the menu items sign people in as the posted `user_id`, with no approval step. **Anyone who has the key can then sign in as any user, including an admin.** The setting exists for sites on a private network where the App Session Launcher is off. Leave it off otherwise.

## Links

- **Studio links** need a signed-in user whose group has the All reviews right.
- **Share links** are 256-bit random values. They always expire and can be revoked, and a page that is already open loses access too.
- **Guests** only get in if the link allows them, and they only have the rights ticked on that link. They never see other reviews, linked fields, ShotGrid's own notes or people's logins.
- **Guest passwords** are stored as scrypt hashes and limited to 5 wrong tries per address in 5 minutes, and 50 per link in an hour. Set `server.forwarded_allow_ips`, otherwise the per-address limit sees only your proxy.
- **Download** can be turned off, which hides the button and refuses downloads. Video that plays in a browser can always be captured, however.

## Uploads

- Attachments are off for guests unless a link allows them, and `access.max_guest_upload_mb` caps their size.
- Scripts and web pages are refused by extension. This is not antivirus: files people attach still reach ShotGrid and other people's computers.
- Only pictures display in the page. Everything else downloads, with `nosniff` and a sandbox policy so it can't run in this site.

## Webhook

Requests to `/hooks/shotgrid` must carry a valid `X-SG-SIGNATURE`. There is no replay protection, so a captured request could be sent again. At worst, that repeats a status the request already carried.

## Reporting

Please report security problems privately to the maintainers rather than in a public issue.
