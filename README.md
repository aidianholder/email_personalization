# ADG article vectorization

Pulls recently published Arkansas Democrat-Gazette stories from the Ellington
REST API, embeds headline + tease with OpenAI, and stores the vector alongside
the fields the Braze Connected Content endpoint will need.

This is the **article** half of the personalization project. The reader half
(Treasure Data history -> `users.interest`) lands separately.

## Layout

```
manage.py              Django entry point
config/                settings, urls, wsgi
feed/                  Braze Connected Content endpoint (read-only, thin)
pipeline/              models, admin, management commands
  management/commands/ one module per pipeline stage
adgvec/                the pipeline itself -- imports nothing from Django
sql/                   hand-written schema migrations
```

`adgvec/` stays Django-free on purpose. The scoring and evaluation code needs
to run in a notebook, over a subsample, with variants swapped in; dissolving
it into views and model methods is how that stops being possible. Management
commands are thin wrappers, and argument definitions live once in
`adgvec.cli.COMMANDS`, so a flag added there appears in both `manage.py <cmd>`
and `python -m adgvec <cmd>`.

**Two migration systems, cleanly split.** `sql/` owns every pipeline table;
Django owns only `pipeline_settings`. The models for existing tables are
`managed = False`, so Django records them for the admin without ever trying
to create or alter them.

## Setup

```bash
cp .env.example .env    # then fill in credentials
.venv/bin/python manage.py init_db     # apply sql/ migrations
.venv/bin/python manage.py migrate     # apply Django's (settings table only)
```

The venv is built against Python 3.14 (Homebrew's 3.11-3.13 have a broken
`pyexpat`; `brew reinstall python@3.13` would fix that if you'd rather pin
lower).

## Static files

```bash
.venv/bin/python manage.py collectstatic --noinput
```

Collects into `static/` (`STATIC_ROOT`) -- 130 files, ~1.7MB, all of it Django
admin CSS/JS/images. It is generated output, so it is gitignored; treat
collectstatic as a deploy step.

Project-owned assets, if any are ever added, belong in `STATICFILES_DIRS` and
not in `static/` -- Django refuses a `STATICFILES_DIRS` entry that is also
`STATIC_ROOT`.

**With `DEBUG=0`, Django does not serve these itself.** Verified: `/static/
admin/css/base.css` returns 200 under `DJANGO_DEBUG=1` and 404 without it, so
in production the admin renders unstyled unless something else serves the
directory. Two ways:

* **nginx** (preferred if it already fronts the app) --
  `location /static/ { alias /path/to/static/; }`
* **whitenoise** -- add the package, put
  `whitenoise.middleware.WhiteNoiseMiddleware` directly after
  `SecurityMiddleware`, and Django serves it itself. Simpler if there is no
  web server in front.

The feed endpoint is unaffected either way; it returns JSON and loads no
static assets.

## Commands

```bash
.venv/bin/python manage.py run              --hours 24   # fetch + embed stories
.venv/bin/python manage.py sync_readers     --timestamped
.venv/bin/python manage.py sync_popularity
.venv/bin/python manage.py profiles
.venv/bin/python manage.py match
.venv/bin/python manage.py status
.venv/bin/python manage.py backtest --temporal
```

A nightly cron chaining those five in order is the whole job. The advisory
lock stops two runs overlapping and `pipeline_runs` records every outcome, so
Celery is not needed for this; add `django-celery-beat` only if you want the
schedule itself editable in the admin.

## Tuning (admin)

`/admin/pipeline/pipelinesettings/` edits the ranking, layout, profile and
ingest knobs. Precedence is:

    explicit command-line flag  >  pipeline_settings row  >  .env default

Both entry points honour the table -- `python -m adgvec` boots Django to read
it -- so a weight changed in the GUI cannot be silently ignored by a cron job.
Secrets are never sourced from the database.

Every edition records the settings it ran under (`hybrid_weight`,
`editor_section_front`, and the three profile flags). Without that, a tuning
GUI quietly makes its own metrics uninterpretable: "click-through moved last
week" needs to resolve to a configuration.

The admin also exposes read-only views of editions, the run log (with status
colouring and durations) and the story corpus.

## Schema

Extends the existing `public.stories` table in `personalization`. Additive
only -- the 50 pre-existing rows and the `users` table were left untouched.
`"storyId"` was widened from `numeric(7,0)` to `bigint`; at ~3.7M it would
have started rejecting inserts once ids reached 8 digits.

New columns: `tease`, `kicker`, `share_url`, `thumbnail_url`, `thumbnail_raw`,
`subscription_required`, `updated_at`, `fetched_at`, `embed_input`,
`embed_input_hash`, `embedded_hash`, `embed_model`, `embedded_at`.

The pre-existing `story` and `summary` columns are unused by this pipeline and
are left as they are.

### Incremental re-embedding

`embed_input_hash` is the hash of the text as it reads *now*; `embedded_hash`
is the hash of the text the stored vector was actually built from. When an
editor rewrites a headline the two drift apart and that story alone
re-embeds on the next run. Re-running `run` costs nothing when nothing changed.

A story whose text changed keeps its previous vector until the new one is
written, so the endpoint never sees a NULL embedding mid-run.

## Reader profiles

```bash
.venv/bin/python -m adgvec profiles                     # build for every reader
.venv/bin/python -m adgvec profiles --user <id> --inspect --dry-run
```

A profile is **one unweighted centroid per reader** in `user_profiles`: the
mean of that reader's corpus-centered read vectors. Clustering, recency decay
and rarity weighting were all measured worse than this on a temporally honest
backtest and are off by default -- see the Backtest section. Re-enable any of
them with `--cluster`, `--recency`, `--rarity` to re-test.

Pipeline per reader: join `reads` (or `users.articles_read`) to
`stories."storyId"` -> subtract the corpus mean and renormalize -> mean.

A full rebuild prunes profiles for readers who have dropped out of the source,
so a stale profile built under different settings cannot survive a rebuild.

Centroids are stored in **corpus-centered space**. Candidate stories must have
the same `corpus_mean_id` subtracted before comparison, or the numbers are
meaningless. Each row records which mean it used.

`strength` is the number of reads behind the centroid. With one centroid per
reader it does not affect within-reader ranking, but it is a useful confidence
measure and the clustered path still uses it.

### Calibration

Inspect any reader's profile with `--inspect --user <email>`. If you turn
clustering back on, calibrate `--threshold` by reading the clustered headlines
rather than optimizing a metric -- "is this a coherent beat" is a human
judgment.

## Treasure Data imports (read-only)

```bash
.venv/bin/python -m adgvec sync-readers       # 30-day reading history
.venv/bin/python -m adgvec sync-popularity    # nightly pageview counts
# backfill per-story counts as of each story's own send time, for backtesting
.venv/bin/python -m adgvec sync-popularity --historical \
    --pub-since 2026-08-01 --pub-until 2026-09-23
```

Both are strictly read-only. Every statement is checked against a deny-list
before it is sent, and anything that is not a bare `SELECT`/`WITH` is refused,
so a query-construction mistake cannot mutate the warehouse.

**Readers** come from `reporting_environment.adg_articles_past_thirty`
(account_key, auth0_id, email_address, articles_read). `articles_read` holds
Ellington story ids, so reads join straight to `stories."storyId"` -- 93.9% of
reads match an embedded story, against 67% under the URL matching this
replaced. TD ships a few duplicate rows per address; they are merged on
`lower(trim(email_address))` and `source_rows` records how many contributed.
The source is a rolling 30-day window, so arrays are replaced rather than
merged. `--prune` drops readers missing from the latest sync.

**Popularity** comes from `raw_web.pageviews`. Two filters matter:

* `traffic_type = 'human'` -- excludes bots and the large null-traffic_type
  block, which carries almost no `article_id`.
* `td_host` -- **`pageviews` covers every WEHCO title**, and on a normal day
  nwaonline.com outweighs arkansasonline.com. Unfiltered, ~70% of counted
  volume is other papers' readers. `POPULARITY_HOSTS` defaults to
  `www.arkansasonline.com`; add `www.wholehogsports.com` to fold in ADG's
  sports vertical.

Counts are stored per snapshot in `story_popularity` along with `hours_live`
and `views_per_hour`. Rank on `views_per_hour`, not `views`: a story published
at 7am has had thirteen more hours to accumulate views than one published at
8pm, and raw counts would simply prefer whatever ran earliest.

## Read timestamps

```bash
.venv/bin/python -m adgvec sync-readers --timestamped
```

`adg_articles_past_thirty` is an untimed array, which blocked fitting the
recency decay and made an honest backtest impossible. The `reads` table fixes
that, reconstructed from `raw_web.pageviews`:
**76,672 reads, 5,193 readers, with `first_read_at`, `last_read_at` and
`view_count`** -- near-parity with the aggregate's 77,662.

The join is on `auth0_id`, not email. `pageviews` carries auth0 on far more
article views (22,550 vs 14,351 over a sample week), and matching known reads
recovers **100%** by auth0 against **63%** by email -- the aggregate table is
built on auth0 identity. The translation to email happens inside the Treasure
Data query, so auth0 is a source-side join key and never a pipeline
identifier.

What the timestamps show: median lag from publication to first read is
**13 hours**, p25 is 5.6 hours, and **72.9% of reads happen within 24 hours**.
A 5am send reaches most readers ahead of their own reading.

## Identity

The pipeline is keyed on **email address** throughout. `auth0_id` is retained
on `users` for cross-referencing other systems and is not an identifier
anywhere.

Putting a raw email in the feed URL exposes it to web server logs, referrer
headers and anything else on the request path. Set `FEED_HASH_SALT` and the
pipeline also stores `sha256(salt || lower(email))` per payload; the endpoint
accepts `?k=<hash>` in place of `?uid=<email>`, keeping the address out of
URLs entirely. Both forms work.

## Daily match

```bash
.venv/bin/python -m adgvec match --editor-picks 3813862,3813854,3813853,3813855,3813852
```

Builds an edition: selects each reader's stories, freezes the story metadata,
renders the payloads, then flips the edition to `ready` in one statement.
Until that flip the endpoint cannot see it, so a job that dies midway leaves
the previous edition serving rather than a partial one.

Personalized slots are ranked on a **blend of the personal match score and
pageview popularity**, min-max normalised within the candidate pool.
`--hybrid-weight` (default **0.6**) is the weight on the personal score;
`--no-hybrid` disables it. Needs a `sync-popularity` snapshot no older than
`--popularity-max-age` hours, and warns and falls back to the personal score
alone if there is none.

The weight was swept against the temporal backtest -- recall@2 peaks at 0.6
(34.2%) against 26.6% for popularity alone and 24.8% for the personal score
alone. It also trades against how personal the email *feels*, which the
backtest cannot see:

| weight | distinct stories used | share of readers getting the top story |
|---|---|---|
| 1.0 (personal only) | 37 of 37 | 14.7% |
| **0.6 (default)** | **34 of 37** | **31.1%** |
| 0.4 | 14 of 37 | 42.3% |

At 0.6 about a third of readers see the same story in a personalized slot; at
0.4 the spread collapses. If readers report the email feeling generic, raise
the weight rather than lowering it.

By default editor picks come from **section front 186** ("Morning News Update
- A"): its `lead_story` followed by `additional_stories`, in the order the
desk set them -- five picks, already prioritised. Override the section front
with `--editor-section-front`, supply ids directly with `--editor-picks`, or
build with none via `--no-editor-picks`. Other `pieces` keys on that front
(email_title, other_newsletters, bottom_navigation) are template furniture and
are skipped.

`--editor-picks` is an ordered list. The first ones fill the `E` slots in
`--layout` (default `EEEPP`); the rest are reserves. Slots are filled by a
waterfall -- editor picks, then personalized, then remaining editor reserves,
then a recency tail -- so **every reader gets a full email** even with no
profile, no editor picks, or both. Nothing is ever selected twice.

Measured on the current data (1,265 recipients, 69 eligible candidates):

| personalized slots filled | readers |
|---|---|
| 2 | 1,144 (90.4%) |
| 0 | 121 (9.6%, no profile -- these get 5 editor/tail) |

The 90.4% depends on the same-cluster fallback: 59.6% of readers currently
have only one interest cluster, and a strict "slot 2 must be a different
cluster" rule would leave all of them one story short. The second pass allows
a same-cluster pick subject to `--max-pair-sim`, which still rejects two
versions of one story.

## Serving (Django)

```bash
cd serve && ../.venv/bin/python manage.py runserver
```

Braze Connected Content points at:

```
https://personalized.arkansasonline.com/api/morning_email?uid={{${user_id}}}
```

Keyed on `users.auth0_user_id`, which is the external user id Braze sends.
Both the encoded (`auth0%7C...`) and raw pipe forms resolve, and a path form
(`/api/morning_email/<uid>`) exists for a CDN that strips query strings.

The view does one primary-key lookup of a pre-rendered `jsonb` blob and
returns it without re-serializing. It **always returns HTTP 200** -- unknown
reader, no edition, database unreachable all fall back to the editor-only
`__default__` payload, then to an empty one. A personalization outage costs
personalization, not the newsletter.

Set `FEED_TOKEN` in production and have Braze send it as `X-Feed-Token`;
auth0 ids are not secret and the response reveals inferred interests.

`GET /healthz` reports the current edition and its payload count -- worth
checking before each send.

Measured: 0.5 ms/request, ~1,900 req/s on the single-threaded dev server.

### Braze template

```liquid
{% connected_content
   https://personalized.arkansasonline.com/api/morning_email?uid={{${user_id}}}
   :headers {"X-Feed-Token": "{{feed_token}}"}
   :save feed %}

{% for story in feed.pieces.stories %}
  <a href="{{story.share_url}}">
    <img src="{{story.thumbnails.medium}}" alt="">
    <h2>{{story.headline}}</h2>
  </a>
  <p>{{story.tease}}</p>
{% endfor %}
```

The payload mirrors Ellington's `/api/sectionfronts/<id>/` shape, so the same
template renders either an editor-curated section front or this personalized
feed -- which makes an A/B test a matter of swapping the URL. The full `story`
body a section front carries is deliberately omitted; five complete articles
would be a large response and the template never uses it.

## Backtest

```bash
.venv/bin/python -m adgvec backtest
```

Leave-one-out: withhold one of a reader's reads, rebuild the profile from the
rest, and measure where the withheld story ranks among the stories published
that same day. Pool size (~67) matches the production candidate pool, so
recall@2 means what it means in the email -- did the story the reader chose
land in one of the two personalized slots? All variants score identical
(reader, story, pool) instances.

A temporal split was not viable: reader histories span a median of 5
publication days, leaving nothing to hold out.

### Results (19,012 evaluations, 2,684 readers, real 30-day history)

Popularity baselines come from `story_popularity` rows with `kind='at_send'`:
views each story had actually accumulated by the first 5am after publication.
Across Aug-Sep 2026 that is only **33.9%** of a story's eventual views, so an
all-time count credits popularity with readership it could not have known.

| variant | recall@2 | recall@5 | MRR |
|---|---|---|---|
| **hybrid: pageviews/hour + model** | **33.0%** | 56.8% | 0.362 |
| model, single mean (no panel weights) | 28.3% | 43.3% | 0.319 |
| baseline: pageviews/hour @send | 28.1% | 44.1% | 0.318 |
| model, single mean | 26.1% | 41.4% | 0.301 |
| baseline: pageviews @send | 25.0% | 40.2% | 0.286 |
| model, clustered | 22.0% | 34.4% | 0.259 |
| baseline: newest first | 8.3% | 18.7% | 0.133 |
| baseline: random | 2.7% | 6.9% | 0.066 |
| ~~baseline: panel reads~~ | ~~35.8%~~ | | leaky, see below |

**The "most-read" baseline that earlier looked unbeatable was an artifact.**
It counted how many other readers in the panel had opened each story, drawn
from the same 30-day histories being evaluated and with no time bound -- so it
knew about reads that happened days after the send. Against real pageviews as
of send time, the model is competitive on its own (28.3% vs 28.1%) and the
hybrid beats both by ~5 points. The earlier conclusion that personalization
must not ship standalone does not survive an honest baseline.

Two secondary findings:

* **Rank on views-per-hour, not raw views** (28.1% vs 25.0%). Raw counts
  simply prefer whatever was published earliest in the window.
* **Dropping the rarity/recency weights improves the model** (28.3% vs 26.1%).
  Those weights are derived from panel read counts, so removing them also
  removes the model's own dependence on that leaky signal.

One caveat remains, and it cuts against the model. Leave-one-out builds each
profile from the reader's other reads across the whole window, including reads
that happened *after* the held-out story, while the pageview baseline is
strictly as-of send time. A genuinely fair comparison needs per-read
timestamps from Treasure Data, which `adg_articles_past_thirty` does not
carry.

### Temporal evaluation

```bash
.venv/bin/python -m adgvec backtest --temporal
```

`--temporal` builds each profile only from reads that happened *before* the
held-out read. Plain leave-one-out draws part of every profile from the
reader's future, which is hindsight the pageview baseline never gets. This
needs the `reads` table.

Temporal results (15,312 evaluations, 2,658 readers):

| variant | recall@2 | recall@5 | MRR |
|---|---|---|---|
| **hybrid: pageviews/hour + model** | **32.4%** | 53.1% | 0.347 |
| baseline: pageviews/hour @send | 26.4% | 41.8% | 0.299 |
| model, plain mean | 23.9% | 39.1% | 0.281 |
| model, recency + rarity weights | 22.7% | 37.3% | 0.271 |
| baseline: pageviews @send | 22.0% | 36.8% | 0.259 |
| model, clustered | 20.4% | 32.5% | 0.244 |
| baseline: random | 2.9% | 6.9% | 0.067 |

Honest evaluation costs the model roughly 2.5 points against plain
leave-one-out, so its earlier apparent parity with popularity was partly
hindsight. Standalone it now sits ~2.5 points behind views-per-hour. The
hybrid still wins clearly, by **+6.0 points** over popularity alone.

### Fitted weights

With real read times the recency half-life can be measured instead of
guessed. It should not be used at all:

| half-life | recall@2 |
|---|---|
| 0.25d | 16.6% |
| 1d | 18.7% |
| 5d | 22.0% |
| 14d (the guess) | 22.7% |
| 30d | 22.9% |
| **none** | **23.9%** |

Monotonic: every amount of decay loses information. Reader interests are
stable across a 30-day window, and with a median of 13 matched reads there is
nothing to spare. Separating the two weights shows rarity is the harmful one:

| weighting | recall@2 |
|---|---|
| **plain mean** | **23.9%** |
| recency only (14d) | 23.9% |
| recency + rarity | 22.7% |
| rarity only | 22.5% |

So the profile should be an unweighted mean of centered read vectors. Both
weighting schemes I proposed were wrong, and rarity actively costs ~1.4
points.

### Clustering does not pay for itself

Recall@2 tracks how many of a reader's reads survive clustering:

| profile | reads used | recall@2 (week sample) |
|---|---|---|
| single weighted mean | 100% | **19.6%** |
| clustered, uncapped | 100% | 17.1% |
| clustered, cap 8 | 64% | 16.3% |
| clustered, cap 3 | 28% | 13.3% |
| clustered, cap 1 | 10% | 10.6% |

On the real 30-day data the gap is wider still, 26.1% against 22.0%, so more
history does not rescue clustering.

Two separate losses: capping discards reads, and even at full coverage
max-similarity is a worse estimator than the average. The gap widens with
more history, so more data will not rescue it. Diversity between the two
slots -- the original reason for clustering -- is better handled by the
`--max-pair-sim` ceiling in `match`, which needs no clusters.

Corpus centering measured neutral (16.2% vs 16.3%). It is retained for
spread across readers, which this metric does not capture, but that is now
an assumption rather than a measured win.

### Where personalization earns its place

recall@2 by size of the reader's history:

| reads | model | pageviews @send | hybrid |
|---|---|---|---|
| 2-3 | 26.2% | 28.2% | 33.7% |
| 4-5 | 26.3% | 29.9% | 35.0% |
| 6-9 | 26.1% | 26.5% | 31.6% |
| 10-19 | 26.2% | 25.5% | 30.2% |
| 20+ | 26.0% | **18.2%** | 22.6% |

The model is flat at ~26% regardless of history depth -- profiles saturate
early, so 30 days buys little over roughly 10 reads. Pageview popularity
collapses as readers get heavier, from 28.2% to 18.2%: they read into the
tail, where "what everyone read" stops working. Past about 6 reads the model
overtakes popularity outright, and at 20+ reads it leads by 7.8 points. Those
heavy readers are the majority of evaluations (7,304 of 19,012), and they are
the case for personalizing.

## Notes

- Window boundaries are interpreted in `ELLINGTON_TZ` (default
  `America/Chicago`) and sent to the API as offset-aware ISO 8601.
- `tease` falls back to `description`, then `subhead`, when an editor left it
  blank. `probe` reports how often that happens.
- `thumbnails` is typed only as "string" in the API schema, so the raw value
  is stored in `thumbnail_raw` and `thumbnail_url` is derived defensively.
  Run `probe` to see the real shape before trusting it.
