# Morning Email — Editor's Guide

A guide to the admin screens for the personalized morning newsletter.
No programming needed. If something here doesn't match what you see,
stop and ask the development team rather than guessing.

---

## 1. What the system does

Every night, after the day's stories are published, the system builds one
email lineup **per subscriber**:

| Slot | Where it comes from |
|------|--------------------|
| 1, 2, 3 | **You.** The three stories you put on the newsletter section front. Everyone gets these. |
| 4, 5 | **Chosen automatically** for that reader, based on what they've read before and what's getting traffic today. |

It then hands each reader's five stories to Braze, which drops them into the
email template.

**Every reader always gets five stories.** If the system can't personalize
someone — a brand-new subscriber, say — your picks quietly fill all five
slots instead. Nobody receives a short email. About 1 reader in 40 falls into
that category on a normal day (2.3% on the most recent build).

---

## 2. The thing you'll change most often is *not* in this admin

The three editor slots come from the **"Morning News Update - A" section
front in the CMS** — not from these screens.

- The **lead story** becomes slot 1.
- The **additional stories** become slots 2, 3, and then the reserves.
- They're used in the order you put them in.

**Put five stories on it, not three.** The first three always run. Numbers
four and five are reserves, used when a reader has already read one of your
top three — the system never shows anyone the same story twice. On a typical
night that affects a few dozen readers out of sixteen thousand, but those
readers are your most engaged ones.

If you put fewer than five, the system fills the gap on its own, newest
story first. Nothing breaks. You just have less control over what lands
there.

You do not need to log into the Django admin to change the day's stories.
Edit the section front, and the next run picks it up.

---

## 3. Logging in

Go to **`/admin/`** on the personalization site and sign in.

Accounts are created by the development team — there's no self-signup. Ask
them for a login, and ask for the "editor" level unless you specifically need
more.

### The screens

| Screen | What it's for | Can you change it? |
|--------|---------------|--------------------|
| **Pipeline settings** | The tuning knobs | Yes |
| **Email editions** | One row per night's email. Did it build? | No, view only |
| **Pipeline runs** | The log of every job that ran | No, view only |
| **Stories** | Browse and search the story archive | No, view only |
| **Run requests** | Who started a run by hand, and when | No, view only |

---

## 4. Your daily check: did tonight's email build?

Open **Email editions**. The top row is the most recent. Look at **status**:

| Status | Meaning |
|--------|---------|
| **ready** | Built and live. This is what Braze will send. |
| **building** | Still working, or it stopped partway. If it's been sitting on "building" for more than ten minutes, something is wrong — see §7. |
| **superseded** | An older build for the same day, replaced by a newer one. Normal and harmless. |

Check two things on the **ready** row:

1. **The date is today's.** If the newest ready edition is yesterday's, tonight's
   run didn't finish, and Braze will send yesterday's lineup.
2. **editor_section_front says 186.** If it's blank, the run couldn't reach the
   section front and your picks aren't in the email.

There's also a one-line health check at **`/healthz`** on the same site. It
should say `"ok": true` and show today's date. That's the quickest way to
confirm things are fine without logging in.

---

## 5. Settings: the one that actually matters

Open **Pipeline settings**. There's a single row — click into it to edit.

> **Changes do not apply immediately.** They take effect the next time the
> nightly job runs. Changing a setting at 10am does not alter the email that
> already went out this morning.

### Personal score weight (`hybrid_weight`)

This is the only setting most people will ever want to touch. It decides how
much slots 4 and 5 lean on *that reader's own history* versus *what's getting
traffic today*.

`1.0` is entirely personal history. `0.0` is entirely today's traffic.
**The default is 0.6.**

Here's the trade-off, measured on real data:

| Setting | Different stories used across all readers | Readers who see the same story in slot 4 |
|---------|------------------------------------------|------------------------------------------|
| 1.0 — all personal | 37 of 37 | 15% |
| **0.6 — the default** | **34 of 37** | **31%** |
| 0.4 — leaning popular | 14 of 37 | 42% |

Read that second column carefully. At the default, about a third of readers
see the same story in slot 4. Turn the dial *down* toward popularity and that
climbs fast — at 0.4, more than four readers in ten get an identical "picked
for you" story, and the system only ever uses 14 of the day's stories.

**So if the email starts feeling generic, move this number UP, toward 1.0.**
That's the opposite of most people's instinct. Down means more popular, and
more popular means more identical.

The default was chosen because it's the most *accurate* setting — it most
often surfaces a story the reader genuinely goes on to read. Higher is more
varied; lower is more accurate-feeling in the short term but more repetitive
across your list.

### Also on the Ranking panel

- **Personal score enabled** (`hybrid_enabled`) — unticking this ranks slots 4
  and 5 on reader history alone, ignoring traffic. Only useful as a test.
- **`alpha`** and **`max_pair_sim`** — leave these alone. Ask the dev team.

### Layout panel

- **`layout`** — `EEEPP` means five slots: three Editor, two Personalized.
  Changing this changes the shape of every email, and the Braze template
  expects five. Talk to the dev team first.
- **`editor_section_front`** — the CMS section front your picks come from.
  `186` is "Morning News Update - A". Only change this if you're moving to a
  different section front.
- **`require_thumbnail`** — keep this ticked. About one story in five has no
  image, and the email template needs one. Unticking it lets those stories
  into slots 4 and 5, where they'll render as a broken gap.

### Profiles panel

`profile_cluster`, `profile_recency`, `profile_rarity` — **leave all three
off.** They're alternative approaches that were tested and measured as worse.
They exist so the dev team can re-test them.

### Ingest panel

`corpus_mean_window_days`, `popularity_hosts`, `popularity_window_hours` —
leave these alone. `popularity_hosts` in particular: it must stay limited to
`www.arkansasonline.com`, or traffic from the other WEHCO papers floods the
numbers and slots 4 and 5 start reflecting Northwest Arkansas readers.

### Before you save

Fill in the **change note**. Write why you changed it, in a sentence. Every
night's email records the settings it ran under, so months later someone can
connect a shift in click-through to a decision — but only if the decision was
written down.

---

## 6. Reading the run log

Open **Pipeline runs**. Each row is one job. Newest first.

| Column | What it means |
|--------|---------------|
| **command** | Which job. `match` is the one that builds the email lineups. |
| **status** | Green **ok**, red **error**, orange **running**. |
| **took** | How long it ran. |
| **fetched / inserted / updated / embedded** | Counts. Useful to the dev team; ignore. |
| **error** | The first line of what went wrong. |

A normal night shows five `ok` rows in this order:

1. `run` — collected the day's new stories and worked out what each is about
2. `sync-readers` — pulled the latest reading history
3. `sync-popularity` — pulled today's traffic numbers
4. `profiles` — updated what each reader is interested in
5. `match` — built the lineups

They must run in that order. `match` at the end is the one that produces the
email, so it's the row to look at first.

**One red row does not necessarily mean a broken email.** If `match` finished
`ok` after it, you're fine. Check the Email editions screen — that's the
source of truth. If the latest `match` is red and no `ready` edition exists
for today, the email is in trouble.

Orange **running** rows that never finish are the signal to call someone.

---

## 7. Starting a run yourself

Open **Pipeline runs**. Above the log there's a **Start a run** panel with four
buttons:

| Button | What it does | Roughly how long |
|--------|--------------|------------------|
| **Rebuild today's email** | Rebuilds every reader's five-story lineup using the current section front and settings. **This is the one you want.** | 2&ndash;3 minutes |
| **Refresh stories from the CMS** | Collects stories published since the last run. Use it if a story you just published isn't available to the system yet. | about a minute |
| **Refresh traffic numbers** | Re-reads today's view counts. | under a minute |
| **Full nightly run** | All five stages in order &mdash; the same thing the overnight job does. Use it if the overnight job failed entirely. | 5&ndash;10 minutes |

**After you press a button:** the page comes back with a green message saying
it started. The job runs on the server, not in your browser, so you can close
the tab and it keeps going. Refresh the page after a minute or two &mdash; the
top row of the log turns green (**ok**) when it finishes.

**You can't start two at once.** If a run is already going, the button gives
you an orange message saying what's running and since when. Wait for it to
finish, then try again. Pressing a button twice by accident is harmless.

**The usual sequence** is: change the section front (or a setting) &rarr; press
**Rebuild today's email** &rarr; wait &rarr; check **Email editions** shows a
fresh **ready** row for today.

### If you don't see the buttons

You need the **&ldquo;Can trigger a pipeline run&rdquo;** permission. Ask the
dev team to add it to your account &mdash; it's separate from permission to
change settings, so you can be given one without the other.

Every press is recorded under **Run requests**, which shows who started what
and when.

## 8. Troubleshooting

| What you're seeing | Likely cause | What to do |
|---|---|---|
| Newest ready edition is yesterday | The nightly run didn't finish | Check Pipeline runs for a red `match`; tell the dev team |
| My section front stories aren't in the email | You edited it after the run, or `editor_section_front` is blank on the edition | Press **Rebuild today's email** (&sect;7) |
| Everyone's "picked for you" stories look the same | `hybrid_weight` is too low | Raise it toward 1.0 (see §5) |
| A slot 4 or 5 story has no image | `require_thumbnail` got unticked | Re-tick it |
| An edition stuck on "building" | A job died partway | Wait for the run to clear, then press **Rebuild today's email**. The previous ready edition keeps serving meanwhile. |
| A setting change had no effect | It applies on the next run | Wait for tonight, or press **Rebuild today's email** (&sect;7) |
| Slots 4 and 5 show other papers' stories | `popularity_hosts` was widened | Set it back to `www.arkansasonline.com` |

**The reassuring part:** if the personalization fails entirely, readers get
your section front picks in all five slots. A failure makes the email less
personal, not broken or empty.

---

## 9. What to leave alone

- Anything on the **Profiles** or **Ingest** panels
- `layout`, `alpha`, `max_pair_sim`
- The **Users** and **Groups** screens, unless you manage accounts
- The **Stories** screen is read-only; you can't break anything there

---

## 10. Things worth knowing

- The system reads a **headline and the one-line summary** of each story to
  decide what it's about. A vague or clickbait headline gives it less to work
  with. Good summaries make the personalization better.
- It knows about **two kinds of reader**: web readers, where we know when they
  read something, and app readers, where we only know what they read. App
  readers are about two thirds of the list, and they're handled fine.
- It **never recommends a story someone has already read.**
- Reader interests are rebuilt from **the last 30 days** of reading. A reader's
  habits from two months ago don't count.
