# ShotGrid setup

Only steps 1 and 2 are required. `reviewmeh check` tests everything on this page and tells you what your site needs. In Docker, run it with `docker compose exec reviewmeh reviewmeh check`.

## 1. Add a Script

We recommend not using an API Admin Script for ReviewMeh. API Admin can do far more than ReviewMeh needs, and a flaw in ReviewMeh could expose anything the Script's key can reach. Give the Script its own permission group instead.

### Permissions the Script needs

Under **Permissions - Scripts**, create a group that can:

- read Versions, Shots, Tasks, Playlists, Notes, Replies, Files, People and Statuses, plus Episodes, Sequences and Scenes if you use them
- create Notes, Replies and Files, and edit Version statuses
- use Advanced → **Perform actions as though logged in as another user**, for [notes under the right name](#notes-under-the-right-name)
- create and edit Action Menu Items, only if you use `reviewmeh setup-amis --apply`

### Create the Script

In ShotGrid's admin menu, open **Scripts** and add one:

- **Script Name:** `reviewmeh`. This goes in `.env` as `REVIEWMEH_SG_SCRIPT_NAME`.
- **Application Key:** this goes in `.env` as `REVIEWMEH_SG_SCRIPT_KEY`.
- **Permission Group:** the group you just created, not API Admin.
- **Generate Events:** optional. Turn it on to show the Script's changes in the Activity stream.

ReviewMeh only needs this one Script. Once it's set up, run `reviewmeh check` to test its permissions.

## 2. Turn on the App Session Launcher

In **Site Preferences**, enable the **App Session Launcher**. "Sign in with ShotGrid" depends on it: each person approves ReviewMeh in a ShotGrid tab.

## Notes under the right name

Notes appear under the person who wrote them, not the Script. This depends on two settings:

- **The Script's permission group:** Advanced → **Perform actions as though logged in as another user** must be on. You'll find it under **Permissions - Scripts**, and it's part of the [permissions the Script needs](#permissions-the-script-needs).
- **Each person:** **Admins Can Perform Actions As** must be on. This field is on the Person, and you only need to change it if your site has turned it off.

If ShotGrid refuses, the Script still creates the Note, with the author's name in the text. Acting as a person also uses that person's ShotGrid permissions, so the same fallback applies to anyone who can't create Notes.

## Permission groups

ReviewMeh uses your ShotGrid permission groups by name, so custom groups work. It re-reads rights every 5 minutes.

1. **Choose who runs ReviewMeh.** Members of the admin group have every right and can edit the rights of the other groups. If your site has no `Admin` group, or you want several groups to manage ReviewMeh, set this in `.env`:
   ```yaml
   REVIEWMEH_ADMIN_GROUP=Studio Admin,Pipeline
   ```
2. **Set the other groups' rights.** On the main page, open the **Access** tab, which lists every group on your site. `Manager` and `Artist` start with sensible rights. Any other group has no access until you tick some rights for it.

| Right | What it allows |
|---|---|
| Watch | Opening a share link |
| All reviews | The dashboard and studio links. Without it, people can only open the review a share link points to |
| Comment | Notes, drawings and attachments |
| Download | Movies, frames and exports |
| Other versions | Mini-cut, the cut view and Compare |
| Status | Changing Version statuses in ShotGrid |
| Publish | Sending notes to ShotGrid |
| Add versions | Adding versions to a review |
| Present | Leading the room |
| Share | Making share links |

## Statuses

ReviewMeh reads Version statuses from your site, per project, with their names and colors. When you add a new status in ShotGrid, it appears within 5 minutes, or on the next page load after that.

Two settings use status codes. Change them in your `.env` if your site's codes are different:

| Setting | Default | What it does |
|---|---|---|
| `REVIEWMEH_SCHEMA_OMIT_STATUS` | `omt` | Leaves Shots with these statuses out of cuts. Separate several with commas: `omt,cut,hld` |
| `REVIEWMEH_SCHEMA_NOTE_STATUS` | `opn` | Sets the status of new Notes. If your Notes don't have this status, they get the site's default |

By default, ReviewMeh uses each status's background color from your site. To override a color in ReviewMeh only, set `REVIEWMEH_SCHEMA_STATUS_COLORS='{"rev": "#c061fd"}'`.

## Cuts

Mini-cut and the cut view need to know what groups your shots. ReviewMeh works this out for each project, so a series and a film can share a site. It picks the first of these that the project's shots link to:

1. **Episode**, directly or through the shot's Sequence
2. **Sequence**

It finds the link fields in your site's schema and uses your site's name for the entity. If a project has neither link, its versions play on their own.

ReviewMeh never assumes scenes, because a Shot can link to a Scene that isn't part of your hierarchy. If you do cut by scene, set `REVIEWMEH_CUT_BY`:

| Your site | Setting |
|---|---|
| Scenes rather than sequences | `REVIEWMEH_CUT_BY=scene` |
| Episodes made of scenes | `REVIEWMEH_CUT_BY=episode,scene` |
| Some projects use sequences, others scenes | `REVIEWMEH_CUT_BY=episode,sequence` |
| Episodes only, or sequences only | `REVIEWMEH_CUT_BY=episode` or `sequence` |
| Mini-cut and cut view turned off | `REVIEWMEH_CUT_BY=off` |

If your field codes or names are different, change these:

| Setting | Default | What it sets |
|---|---|---|
| `REVIEWMEH_SCHEMA_CUT_ORDER` | `sg_cut_order` | The Shot field for its position in the cut |
| `REVIEWMEH_SCHEMA_CUT_DURATION` | `sg_cut_duration` | The Shot field for its length, used until its movie is ready |
| `REVIEWMEH_SCHEMA_TASK_ORDER` | `sg_sort_order` | The Task field that orders the rows above the timeline |
| `REVIEWMEH_SCHEMA_CUT_LABEL` | The entity's name | The name shown for the cut |

To group by something else, such as a custom entity, name it yourself. This replaces `REVIEWMEH_CUT_BY` for the whole site:

```yaml
REVIEWMEH_SCHEMA_CUT_PARENT_TYPE=CustomEntity01
REVIEWMEH_SCHEMA_CUT_PARENT_FIELD=sg_reel
REVIEWMEH_SCHEMA_CUT_LABEL=Reel
```

## Menu buttons

Run `reviewmeh setup-amis --apply` to add **Send to Review**, **Publish Review Notes** and **Export Review Notes** to Playlists and Versions. To see what it would do first, leave out `--apply`.

If `REVIEWMEH_PUBLIC_URL` changes, run the command again. Before you do, run `docker compose down && docker compose up -d` to reload the environment.

## Live updates

This step is optional. A webhook sends status changes to open reviews, processes replaced movies again and refreshes cuts. ShotGrid has to be able to reach your address, so a cloud site needs a public one.

Run `reviewmeh webhook`. It prints the URL, Secret Token and events to enter under **Webhooks** in ShotGrid.

## Reverse proxy

You need a reverse proxy for HTTPS, because `docker-compose.yml` only runs ReviewMeh. Configure your proxy to forward to port 8086, with websocket upgrades on `/ws/`, and allow uploads as large as `REVIEWMEH_ACCESS_MAX_UPLOAD_MB`.

Then set these in `.env`:

- **`REVIEWMEH_PUBLIC_URL`:** your `https://` address.
- **`REVIEWMEH_SERVER_FORWARDED_ALLOW_IPS`:** your proxy's address as the container sees it. Without this, the password-guessing limit sees only your proxy, not the real client addresses. If the proxy is on the same machine, this is the Docker network's gateway:
  ```bash
  docker inspect -f '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}' $(docker compose ps -q reviewmeh)
  ```

If the proxy is on the same machine, also change the port line in `docker-compose.yml` to `"127.0.0.1:8086:8086"`, so people can't bypass it.

If you've already added the [menu buttons](#menu-buttons), run `reviewmeh setup-amis --apply` again, since your address has changed.
