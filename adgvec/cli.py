"""Command line entry point for the ADG article vectorization pipeline."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from . import db
from .config import ConfigError, load_config
from .ellington import EllingtonClient
from .embed import BATCH_SIZE, Embedder, build_embed_input, input_hash
from .match import (DEFAULT_ALPHA, DEFAULT_HYBRID_WEIGHT, DEFAULT_LAYOUT,
                    DEFAULT_MAX_PAIR_SIM)
from .profile import (
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_SIM_THRESHOLD,
    DEFAULT_MAX_CLUSTERS,
    DEFAULT_SINGLETON_DROP_MIN_READS,
    MIN_READS_TO_CLUSTER,
    Read,
    as_array,
    build_profile,
)

log = logging.getLogger("adgvec")

# Commands that mutate data: these take the advisory lock and are recorded
# in pipeline_runs. Read-only commands skip both.
LOCKED_COMMANDS = {"fetch", "embed", "run", "corpus-mean", "profiles",
                   "match", "sync-readers", "sync-popularity"}


def _window(args, tz) -> tuple[datetime, datetime | None]:
    if getattr(args, "since", None):
        since = datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=tz)
    else:
        since = datetime.now(tz) - timedelta(hours=getattr(args, "hours", 24))
    if getattr(args, "until", None):
        until = datetime.fromisoformat(args.until)
        if until.tzinfo is None:
            until = until.replace(tzinfo=tz)
    else:
        # Always bound the top of the window. Ellington carries evergreen
        # stories with far-future pub_dates (2029, 2035, a 2038 sentinel);
        # with only a lower bound those sort to the top of "last 24 hours"
        # and would crowd out the actual news.
        until = datetime.now(tz)
    return since, until


def _client(cfg) -> EllingtonClient:
    cfg.require_ellington()
    return EllingtonClient(
        cfg.ellington_base_url,
        cfg.ellington_token,
        auth_scheme=cfg.ellington_auth_scheme,
        site_id=cfg.ellington_site_id,
    )


def _to_row(story) -> dict:
    text = build_embed_input(story.headline, story.tease, story.kicker)
    return {
        "story_id": story.story_id,
        "headline": story.headline,
        "tease": story.tease,
        "kicker": story.kicker,
        "share_url": story.share_url,
        "thumbnail_url": story.thumbnail_url,
        "thumbnail_raw": story.thumbnail_raw,
        "published": story.published,
        "updated_at": story.updated_at,
        "subscription_required": story.subscription_required,
        "embed_input": text,
        "embed_input_hash": input_hash(text) if text else None,
    }


# --------------------------------------------------------------------------- #
# stages -- each returns a counts dict merged into the run log
# --------------------------------------------------------------------------- #

def stage_fetch(args, cfg, conn) -> dict:
    client = _client(cfg)
    since, until = _window(args, cfg.ellington_tz)
    log.info("fetching stories published since %s", since.isoformat())

    rows, skipped = [], 0
    for story in client.iter_stories(since, until, limit=getattr(args, "limit", None),
                                    status=cfg.ellington_status):
        row = _to_row(story)
        if not row["embed_input"]:
            log.warning("story %s has no usable text; skipping", story.story_id)
            skipped += 1
            continue
        rows.append(row)

    print(f"fetched {len(rows)} stories ({skipped} skipped)")
    if args.dry_run:
        for row in rows[:20]:
            print(f"  {row['story_id']}  {row['headline'][:64]}")
        if len(rows) > 20:
            print(f"  ... and {len(rows) - 20} more")
        print("dry run: nothing written")
        return {"fetched": len(rows), "skipped": skipped}

    inserted = updated = 0
    if rows:
        inserted, updated = db.upsert_stories(conn, rows)
        print(f"upserted: {inserted} new, {updated} updated")
    return {"fetched": len(rows), "skipped": skipped,
            "inserted": inserted, "updated": updated}


def stage_embed(args, cfg, conn) -> dict:
    cfg.require_openai()
    pending = db.stories_needing_embedding(
        conn, cfg.embed_model, force=getattr(args, "force", False),
        limit=getattr(args, "limit", None),
    )
    if not pending:
        print("nothing to embed")
        return {"embedded": 0}

    print(f"{len(pending)} stories need embedding "
          f"({cfg.embed_model}, {cfg.embed_dimensions}d)")
    if args.dry_run:
        for row in pending[:20]:
            print(f"  {row['story_id']}  {row['headline'][:64]}")
        print("dry run: no API calls made")
        return {"embedded": 0}

    embedder = Embedder(cfg.openai_token, cfg.embed_model, cfg.embed_dimensions)
    texts = [row["embed_input"] for row in pending]
    done = 0
    for offset, vectors in embedder.embed_all(
        texts, batch_size=getattr(args, "batch_size", BATCH_SIZE)
    ):
        batch = [
            (pending[offset + i]["story_id"],
             pending[offset + i]["embed_input_hash"], vec)
            for i, vec in enumerate(vectors)
        ]
        # Commit per batch: a failure halfway through costs one batch, not the
        # whole run, and the next run resumes where this one stopped.
        done += db.save_embeddings(conn, cfg.embed_model, batch)
        print(f"  embedded {done}/{len(pending)}")
    return {"embedded": done}


def stage_corpus_mean(args, cfg, conn, run_id=None) -> dict:
    result = db.recompute_corpus_mean(
        conn, cfg.embed_model, cfg.embed_dimensions,
        cfg.corpus_mean_window_days, run_id=run_id,
    )
    if result:
        print(f"corpus mean #{result['id']} over {result['story_count']} stories "
              f"({cfg.corpus_mean_window_days}-day window)")
    return {}


def stage_sync_readers(args, cfg, conn, run_id=None) -> dict:
    """Pull the 30-day reading history from Treasure Data. Read-only there."""
    from datetime import datetime as _dt, timezone as _tz

    from .treasure import TreasureDataClient

    cfg.require_td()
    client = TreasureDataClient(cfg.td_api_key, cfg.td_endpoint)
    started = _dt.now(_tz.utc)

    rows, merged = [], 0
    for row in client.reader_history(limit=args.limit):
        ids = [int(a) for a in (row.get("articles_read") or [])
               if str(a).strip().isdigit()]
        merged += max(0, int(row.get("source_rows") or 1) - 1)
        rows.append({
            "email_address": row["email_address"],
            "account_key": row.get("account_key"),
            "auth0_id": row.get("auth0_id"),
            "articles_read": ids,
            "source_rows": int(row.get("source_rows") or 1),
        })

    total_reads = sum(len(r["articles_read"]) for r in rows)
    print(f"fetched {len(rows)} readers, {total_reads} reads "
          f"({merged} duplicate source rows merged)")
    if args.dry_run:
        for r in rows[:10]:
            print(f"  {r['email_address']:<40} {len(r['articles_read']):>4} articles")
        print("dry run: nothing written")
        return {"fetched": len(rows)}

    inserted, updated = db.upsert_readers(conn, rows)
    print(f"upserted: {inserted} new, {updated} updated")
    if args.prune:
        removed = db.prune_readers(conn, started)
        print(f"pruned {removed} readers absent from this sync")

    counts = {"fetched": len(rows), "inserted": inserted, "updated": updated}
    if args.timestamped:
        counts["updated"] = counts.get("updated", 0) + _sync_reads(args, cfg, conn, client)
    return counts


def _sync_reads(args, cfg, conn, client) -> int:
    """Per-read detail with timestamps, from pageviews via auth0_id."""
    from datetime import datetime as _dt, timezone as _tz

    known = set(db.all_external_ids(conn))
    rows, skipped = [], 0
    for r in client.reader_history_timestamped(
        days=args.history_days, hosts=cfg.popularity_hosts,
    ):
        email = (r.get("email_address") or "").strip().lower()
        if email not in known:
            skipped += 1
            continue
        rows.append({
            "email_address": email,
            "story_id": int(r["story_id"]),
            "first_read_at": _dt.fromtimestamp(float(r["first_ts"]), _tz.utc),
            "last_read_at": _dt.fromtimestamp(float(r["last_ts"]), _tz.utc),
            "view_count": int(r["view_count"] or 1),
        })

    print(f"timestamped reads: {len(rows)} rows"
          + (f" ({skipped} for readers not in the panel)" if skipped else ""))
    if args.dry_run:
        print("dry run: reads not written")
        return 0
    saved = db.replace_reads(conn, rows)
    summary = db.reads_summary(conn)
    print(f"reads table: {summary['reads']} reads | {summary['readers']} readers | "
          f"{summary['stories']} stories | avg {summary['avg_views']} views/read")
    print(f"  span {summary['oldest']:%Y-%m-%d} .. {summary['newest']:%Y-%m-%d}")
    return saved


def stage_sync_popularity(args, cfg, conn, run_id=None) -> dict:
    """Pull view counts for recently published stories. Read-only in TD."""
    from datetime import datetime as _dt, timezone as _tz

    from .treasure import TreasureDataClient

    cfg.require_td()
    client = TreasureDataClient(cfg.td_api_key, cfg.td_endpoint)
    as_of = _dt.now(_tz.utc)

    print(f"hosts: {', '.join(cfg.popularity_hosts)}")

    if args.historical:
        if not (args.pub_since and args.pub_until):
            raise RuntimeError("--historical requires --pub-since and --pub-until")
        rows = []
        for r in client.historical_popularity(
            args.pub_since, args.pub_until, hosts=cfg.popularity_hosts,
            send_hour_local=args.send_hour, human_only=not args.include_bots,
        ):
            cutoff = _dt.fromtimestamp(float(r["cutoff_ts"]), _tz.utc)
            pub = _dt.fromtimestamp(float(r["pub_ts"]), _tz.utc)
            rows.append({
                "story_id": r["story_id"], "as_of": cutoff,
                "views": r["views"], "uniques": r["uniques"],
                "identified": r["identified"], "views_total": r["views_total"],
                "hours_live": max(0.0, (cutoff - pub).total_seconds() / 3600.0),
            })
        if not rows:
            print("no pageview rows returned")
            return {"fetched": 0}
        at_send = sum(int(x["views"]) for x in rows)
        total = sum(int(x["views_total"]) for x in rows)
        vs = sorted(int(x["views"]) for x in rows)
        print(f"{len(rows)} stories published {args.pub_since}..{args.pub_until}")
        print(f"  views by send time: {at_send} of {total} eventual "
              f"({100.0 * at_send / max(1, total):.1f}%)")
        print(f"  per story: median {vs[len(vs)//2]} | "
              f"p10 {vs[max(0, len(vs)//10)]} | max {vs[-1]}")
        if args.dry_run:
            print("dry run: nothing written")
            return {"fetched": len(rows)}
        saved = db.save_popularity(conn, as_of, args.window_hours, rows,
                                   kind="at_send")
        print(f"saved {saved} at_send rows")
        return {"fetched": len(rows), "inserted": saved}

    rows = list(client.story_popularity(
        window_hours=args.window_hours,
        published_within_hours=args.published_within_hours,
        human_only=not args.include_bots,
        hosts=cfg.popularity_hosts,
    ))
    if not rows:
        print("no pageview rows returned")
        return {"fetched": 0}

    views = sorted(int(r["views"]) for r in rows)
    mid = views[len(views) // 2]
    print(f"{len(rows)} stories | {sum(views)} views | median {mid} | "
          f"p10 {views[max(0, len(views) // 10)]} | max {views[-1]}")
    if args.dry_run:
        for r in sorted(rows, key=lambda x: -int(x["views"]))[:10]:
            print(f"  {r['story_id']:<10} {r['views']:>6} views  "
                  f"{float(r['hours_live'] or 0):>5.1f}h live")
        print("dry run: nothing written")
        return {"fetched": len(rows)}

    saved = db.save_popularity(conn, as_of, args.window_hours, rows)
    matched = db.popularity_match_rate(conn, as_of)
    print(f"saved {saved} rows at {as_of:%Y-%m-%d %H:%M} UTC "
          f"({matched} of them match a story we have embedded)")
    return {"fetched": len(rows), "inserted": saved}


def stage_profiles(args, cfg, conn, run_id=None) -> dict:
    from datetime import datetime as _dt, timezone as _tz

    import numpy as np

    started = _dt.now(_tz.utc)

    mean = db.load_corpus_mean_vector(
        conn, cfg.embed_model, getattr(args, "corpus_mean_id", None)
    )
    if mean is None:
        raise RuntimeError(
            "no corpus mean available -- run `adgvec corpus-mean` first. "
            "Profiles are built in centered space and cannot be made without it."
        )
    mean_id, mean_vector = mean
    corpus_mean = as_array(mean_vector)

    popularity = db.reader_story_counts(conn, cfg.embed_model)
    reads_by_user = db.load_reads(
        conn, cfg.embed_model, user_id=args.user, limit=args.limit
    )
    if not reads_by_user:
        print("no readers with matched, embedded reads")
        return {"fetched": 0}

    # Precedence: explicit flag > pipeline_settings row > .env default.
    pick = lambda flag, fallback: fallback if flag is None else flag
    use_cluster = pick(args.cluster, cfg.profile_cluster)
    use_recency = pick(args.recency, cfg.profile_recency)
    use_rarity = pick(args.rarity, cfg.profile_rarity)
    shape = "clustered" if use_cluster else "plain mean"
    weights = ", ".join(
        w for w, on in (("recency", use_recency), ("rarity", use_rarity)) if on
    ) or "unweighted"
    print(f"building profiles for {len(reads_by_user)} readers "
          f"({shape}, {weights}, corpus mean #{mean_id})")

    built = clusters_total = 0
    histogram: dict[int, int] = {}
    for user_id, rows in reads_by_user.items():
        reads = [
            Read(story_id=r["story_id"],
                 vector=as_array(r["embedding"]),
                 published=r["published"], headline=r["headline"] or "",
                 read_at=r.get("read_at"))
            for r in rows
        ]
        clusters = build_profile(
            reads, corpus_mean, popularity,
            sim_threshold=args.threshold,
            half_life_days=args.half_life,
            min_reads_to_cluster=args.min_reads_to_cluster,
            singleton_drop_min_reads=args.singleton_drop_min,
            max_clusters=args.max_clusters,
            use_recency=use_recency,
            use_rarity=use_rarity,
            cluster=use_cluster,
        )
        if not clusters:
            continue
        histogram[len(clusters)] = histogram.get(len(clusters), 0) + 1
        clusters_total += len(clusters)
        built += 1

        if args.inspect:
            print(f"\n{'=' * 74}\nreader {user_id} -- {len(reads)} reads "
                  f"-> {len(clusters)} cluster(s)")
            for i, c in enumerate(clusters):
                print(f"  cluster {i}: {c.story_count} stories, "
                      f"strength {c.strength:.2f}")
                for sid in c.story_ids[:6]:
                    hl = next((r.headline for r in reads if r.story_id == sid), "")
                    print(f"      {hl[:66]}")
                if c.story_count > 6:
                    print(f"      ... and {c.story_count - 6} more")

        if not args.dry_run:
            db.save_profiles(conn, user_id, clusters, cfg.embed_model, mean_id, run_id)

    if not args.dry_run:
        conn.commit()
        # A full rebuild owns the whole table; a targeted one does not.
        if not args.user and not args.limit:
            stale = db.prune_profiles(conn, started)
            if stale:
                print(f"pruned {stale} stale profile rows for readers no longer "
                      f"in the source")

    print(f"\n{built} readers profiled, {clusters_total} clusters "
          f"({clusters_total / built:.2f} per reader)" if built else "nothing built")
    print("cluster-count distribution:")
    for n in sorted(histogram):
        share = 100.0 * histogram[n] / built
        print(f"  {n} cluster(s): {histogram[n]:>5} readers ({share:4.1f}%)")
    if args.dry_run:
        print("dry run: nothing written")
    return {"fetched": built, "updated": clusters_total}


def stage_match(args, cfg, conn, run_id=None) -> dict:
    import numpy as np
    from datetime import date as _date

    from .match import (
        Candidate, DEFAULT_ALPHA, MatchStats, choose_personalized,
        eligible_candidates, fill_slots, render_payload,
    )

    # Precedence: explicit command-line flag > pipeline_settings row >
    # .env default. Flags default to None so "not given" is distinguishable
    # from "given the same value as the default".
    if args.editor_section_front is None:
        args.editor_section_front = cfg.editor_section_front
    if args.layout is None:
        args.layout = cfg.layout
    if args.alpha is None:
        args.alpha = cfg.alpha
    if args.max_pair_sim is None:
        args.max_pair_sim = cfg.max_pair_sim
    if args.hybrid_weight is None:
        args.hybrid_weight = cfg.hybrid_weight
    if args.allow_no_thumbnail is None:
        args.allow_no_thumbnail = not cfg.require_thumbnail
    if not cfg.hybrid_enabled:
        args.no_hybrid = True

    layout = args.layout.upper()
    edition_date = (_date.fromisoformat(args.edition_date)
                    if args.edition_date else datetime.now(cfg.ellington_tz).date())

    mean = db.load_corpus_mean_vector(conn, cfg.embed_model, args.corpus_mean_id)
    if mean is None:
        raise RuntimeError("no corpus mean -- run `adgvec corpus-mean` first")
    mean_id, mean_vector = mean
    corpus_mean = as_array(mean_vector)

    since, until = _window(args, cfg.ellington_tz)
    rows = db.load_candidates(conn, cfg.embed_model, since, until)
    if not rows:
        raise RuntimeError(f"no candidate stories published in {since} .. {until}")

    cands = []
    for r in rows:
        v = as_array(r["embedding"]) - corpus_mean
        n = np.linalg.norm(v)
        thumbs = {}
        if r["thumbnail_raw"]:
            try:
                thumbs = json.loads(r["thumbnail_raw"])
            except (json.JSONDecodeError, TypeError):
                thumbs = {}
        cands.append(Candidate(
            story_id=r["story_id"], headline=r["headline"] or "",
            tease=r["tease"] or "", share_url=r["share_url"] or "",
            thumbnail_url=r["thumbnail_url"], thumbnails=thumbs,
            pub_date=r["published"], vector=v / n if n > 1e-9 else v,
        ))

    pool = eligible_candidates(cands, require_thumbnail=not args.allow_no_thumbnail)
    print(f"candidate pool: {len(pool)} eligible of {len(cands)} published "
          f"({since:%Y-%m-%d %H:%M} .. {until:%Y-%m-%d %H:%M})")
    if not pool:
        raise RuntimeError("no eligible candidates after filtering")

    if args.editor_picks:
        editor_ids = [int(x) for x in args.editor_picks.split(",") if x.strip()]
        print(f"editor picks: {len(editor_ids)} supplied on the command line")
    elif args.editor_section_front:
        # Default: whatever the desk curated on the newsletter section front.
        editor_ids = _client(cfg).section_front_story_ids(args.editor_section_front)
        print(f"editor picks: {len(editor_ids)} from section front "
              f"{args.editor_section_front} -> {editor_ids}")
    else:
        editor_ids = []
        print("editor picks: none")
    known = {c.story_id for c in cands}
    missing = [s for s in editor_ids if s not in known]
    if missing:
        # Editors can legitimately pick something outside the window; pull it
        # in rather than silently dropping it from the email.
        extra = db.load_candidates(conn, cfg.embed_model,
                                   since - timedelta(days=7), until)
        by_id = {r["story_id"]: r for r in extra}
        for sid in missing:
            r = by_id.get(sid)
            if r is None:
                print(f"  WARNING: editor pick {sid} not found; skipping")
                editor_ids.remove(sid)
                continue
            thumbs = json.loads(r["thumbnail_raw"]) if r["thumbnail_raw"] else {}
            cands.append(Candidate(
                r["story_id"], r["headline"] or "", r["tease"] or "",
                r["share_url"] or "", r["thumbnail_url"], thumbs, r["published"], None))

    stories_by_id = {c.story_id: c for c in cands}
    # Recency tail guarantees a full email even if the desk supplies nothing.
    tail_ids = [c.story_id for c in pool]

    popularity: dict[int, float] = {}
    if not args.no_hybrid:
        snapshot = db.load_popularity(conn, max_age_hours=args.popularity_max_age)
        popularity = {sid: float(row["views_per_hour"] or 0.0)
                      for sid, row in snapshot.items()}
        if popularity:
            covered = sum(1 for c in pool if c.story_id in popularity)
            print(f"popularity blend: weight {args.hybrid_weight} on the personal "
                  f"score, {covered}/{len(pool)} candidates have view data")
        else:
            print("WARNING: no recent popularity snapshot -- ranking on the "
                  "personal score alone. Run `adgvec sync-popularity`.")

    profiles = db.load_all_profiles(conn)
    already_read = db.load_read_story_ids(conn)
    recipients = db.all_external_ids(conn)
    print(f"{len(recipients)} recipients | {len(profiles)} with profiles")

    if args.dry_run:
        print("dry run: nothing written")
        edition_id = 0
    else:
        edition_id = db.create_edition(
            conn, edition_date, layout, len(layout), mean_id, cfg.embed_model,
            settings={
                "hybrid_weight": None if args.no_hybrid else args.hybrid_weight,
                "editor_section_front": args.editor_section_front or None,
                "profile_cluster": cfg.profile_cluster,
                "profile_recency": cfg.profile_recency,
                "profile_rarity": cfg.profile_rarity,
            })
        db.save_edition_stories(conn, edition_id, rows)

    stats = MatchStats()
    selections: list[tuple] = []
    payloads: list[tuple] = []

    # Fallback served to unknown readers and anyone we cannot personalize.
    default_picks = fill_slots(layout, editor_ids, [], tail_ids, set())
    payloads.append(("__default__",
                     render_payload(default_picks, stories_by_id, edition_id,
                                    edition_date)))

    for uid in recipients:
        clusters = profiles.get(uid, [])
        exclude = set(already_read.get(uid, ()))
        personalized: list = []
        if clusters:
            centroids = np.vstack([as_array(c["centroid"]) for c in clusters])
            strengths = np.array([c["strength"] for c in clusters], dtype=np.float64)
            n_wanted = layout.count("P")
            personalized = choose_personalized(
                pool, centroids, strengths, n_wanted, exclude,
                alpha=args.alpha, max_pair_sim=args.max_pair_sim,
                popularity=popularity, hybrid_weight=args.hybrid_weight,
            )
        picks = fill_slots(layout, editor_ids, personalized, tail_ids, exclude)
        stats.record(picks)
        for slot, p in enumerate(picks, start=1):
            selections.append((uid, slot, p.story_id, p.source, p.score, p.cluster_index))
        payloads.append((uid, render_payload(picks, stories_by_id, edition_id,
                                             edition_date)))

    if not args.dry_run:
        db.save_selections(conn, edition_id, selections)
        db.save_payloads(conn, edition_id, payloads, cfg.feed_hash_salt)
        conn.commit()
        db.publish_edition(conn, edition_id)
        print(f"edition #{edition_id} published for {edition_date}")

    print(f"\n{stats.readers} readers | {len(selections)} selections")
    print("personalized slots filled per reader:")
    for n in sorted(stats.personalized_filled):
        c = stats.personalized_filled[n]
        print(f"  {n}: {c:>5} readers ({100.0 * c / stats.readers:4.1f}%)")
    print("slots by source:")
    total_slots = sum(stats.source_counts.values()) or 1
    for src, c in sorted(stats.source_counts.items(), key=lambda x: -x[1]):
        print(f"  {src:<14} {c:>6} ({100.0 * c / total_slots:4.1f}%)")
    return {"fetched": stats.readers, "updated": len(selections)}


def stage_backtest(args, cfg, conn) -> int:
    import numpy as np

    from .backtest import (
        DEFAULT_ALPHA, Instance, evaluate, hybrid_scorer, pageview_scorer,
        popularity_scorer, profile_scorer, random_scorer, recency_scorer,
        MAX_EVALS_PER_READER,
    )

    mean = db.load_corpus_mean_vector(conn, cfg.embed_model, args.corpus_mean_id)
    if mean is None:
        raise RuntimeError("no corpus mean -- run `adgvec corpus-mean` first")
    _, mean_vector = mean
    corpus_mean = as_array(mean_vector)

    reads_by_user = db.load_reads(conn, cfg.embed_model)
    excluded = db.users_in_domains(
        conn, [d for d in (args.exclude_domains or "").split(",") if d.strip()])
    if excluded:
        before = len(reads_by_user)
        reads_by_user = {u: r for u, r in reads_by_user.items() if u not in excluded}
        print(f"excluded {before - len(reads_by_user)} readers in "
              f"{args.exclude_domains} (also removed from the popularity signal)")
    popularity = db.reader_story_counts_excluding(conn, cfg.embed_model, excluded)
    reader_reads = {u: {r["story_id"] for r in rows} for u, rows in reads_by_user.items()}

    # Candidate pools are drawn from the period the reading history covers.
    dates = [r["published"] for rows in reads_by_user.values()
             for r in rows if r["published"]]
    lo, hi = min(dates), max(dates)
    rows = db.load_candidates(conn, cfg.embed_model,
                              lo - timedelta(days=1), hi + timedelta(days=1))
    if not args.allow_no_thumbnail:
        rows = [r for r in rows if r["thumbnail_url"]]
    by_day: dict[object, list] = {}
    for r in rows:
        by_day.setdefault(r["published"].date(), []).append(r)
    print(f"pool source: {len(rows)} eligible stories over {len(by_day)} days "
          f"({lo:%Y-%m-%d} .. {hi:%Y-%m-%d})")

    rng = np.random.default_rng(args.seed)
    instances, skipped_ineligible, skipped_small = [], 0, 0
    skipped_untimed = skipped_no_history = 0

    for uid, rws in reads_by_user.items():
        uniq = {r["story_id"]: r for r in rws}
        if len(uniq) < args.min_reads:
            skipped_small += 1
            continue
        items = list(uniq.values())
        order = rng.permutation(len(items))[:MAX_EVALS_PER_READER]
        for i in order:
            held = items[i]
            day = held["published"].date() if held["published"] else None
            pool_rows = by_day.get(day, [])
            # Only evaluate stories that would actually have been candidates;
            # an ineligible story could never have been selected in production.
            if not any(p["story_id"] == held["story_id"] for p in pool_rows):
                skipped_ineligible += 1
                continue
            others = {r["story_id"] for j, r in enumerate(items) if j != i}
            pool = [p for p in pool_rows if p["story_id"] not in others]
            if len(pool) < args.min_pool:
                continue
            # --temporal restricts the profile to reads that happened
            # strictly before the held-out read. Without it, leave-one-out
            # builds part of each profile from the reader's *future*, which
            # is hindsight the pageview baseline does not get.
            held_at = held.get("read_at")
            train_rows = [r for j, r in enumerate(items) if j != i]
            if args.temporal:
                if held_at is None:
                    skipped_untimed += 1
                    continue
                train_rows = [r for r in train_rows
                              if r.get("read_at") is not None
                              and r["read_at"] < held_at]
                if len(train_rows) < args.min_train:
                    skipped_no_history += 1
                    continue
            train = [Read(r["story_id"], as_array(r["embedding"]),
                          r["published"], r["headline"] or "", r.get("read_at"))
                     for r in train_rows]
            instances.append(Instance(
                user_id=uid, train=train, held_out_id=held["story_id"],
                pool_ids=np.array([p["story_id"] for p in pool]),
                pool_vecs=np.vstack([as_array(p["embedding"]) for p in pool]),
                pool_published=np.array([p["published"].timestamp() for p in pool]),
            ))

    if not instances:
        raise RuntimeError("no evaluable instances")
    readers = len({i.user_id for i in instances})
    extra = ""
    if args.temporal:
        extra = (f", {skipped_untimed} untimed, "
                 f"{skipped_no_history} with no prior reads")
    print(f"{len(instances)} evaluations across {readers} readers "
          f"({skipped_small} readers below --min-reads, "
          f"{skipped_ineligible} held-out stories ineligible{extra})")
    print(f"mode: {'TEMPORAL (profile uses only earlier reads)' if args.temporal else 'leave-one-out (profile may include later reads)'}\n")

    hl = args.half_life if args.half_life is not None else DEFAULT_HALF_LIFE_DAYS
    pf = lambda **kw: profile_scorer(corpus_mean, popularity, alpha=args.alpha,
                                     sim_threshold=args.threshold,
                                     half_life_days=hl, **kw)
    pop = popularity_scorer(popularity, reader_reads)
    pv = db.load_at_send_popularity(conn)
    have_pv = sum(1 for i in ()) or len(pv)
    if pv:
        covered = sum(1 for inst in instances
                      if int(inst.held_out_id) in pv)
        print(f"pageview popularity: {len(pv)} stories, covering "
              f"{100.0 * covered / len(instances):.1f}% of held-out stories")
    pv_send = pageview_scorer(pv, "views")
    pv_all = pageview_scorer(pv, "views_total")
    pv_rate = pageview_scorer(pv, "views_per_hour", subtract_self=False)

    scorers = {
        "model (centered+clustered)": pf(),
        "no clustering (single mean)": pf(cluster=False),
        "no centering": pf(center=False),
        "no recency/rarity weights": pf(weights=False),
        "baseline: panel reads": pop,
        "baseline: pageviews @send": pv_send,
        "baseline: pageviews/hour @send": pv_rate,
        "baseline: pageviews all-time*": pv_all,
        "baseline: newest first": recency_scorer(),
        "baseline: random": random_scorer(args.seed),
        "hybrid: pageviews + model 0.2": hybrid_scorer(pv_send, pf(cluster=False), 0.2),
        "hybrid: pageviews + model 0.35": hybrid_scorer(pv_send, pf(cluster=False), 0.35),
        "hybrid: pageviews + model 0.5": hybrid_scorer(pv_send, pf(cluster=False), 0.5),
        "hybrid: panel + model 0.35": hybrid_scorer(pop, pf(cluster=False), 0.35),
    }

    results = evaluate(instances, scorers)
    hdr = f"{'variant':<30}{'recall@1':>10}{'recall@2':>10}{'recall@5':>10}{'MRR':>8}{'pctile':>9}"
    print(hdr); print("-" * len(hdr))
    for m in results:
        r = m.row()
        print(f"{r['variant']:<30}{r['recall@1']:>9.1f}%{r['recall@2']:>9.1f}%"
              f"{r['recall@5']:>9.1f}%{r['MRR']:>8.3f}{r['pctile']:>8.1f}%")
    print(f"\nmean pool size {results[0].row()['pool']:.0f}; "
          f"recall@2 by chance is about {200.0 / results[0].row()['pool']:.1f}%")

    # Does more history help? This is the direct test of whether thin
    # sample history -- rather than the approach -- is the binding constraint.
    print("\nrecall@2 by size of the reader's training history:")
    buckets = [(2, 3), (4, 5), (6, 9), (10, 19), (20, 10**6)]
    model_only = pf(cluster=False)
    pop_for_buckets = pv_send if pv else pop
    print(f"  {'reads':<10}{'n':>7}{'model':>9}{'pageviews':>11}{'hybrid .35':>12}")
    hyb = hybrid_scorer(pop_for_buckets, model_only, 0.35)
    for lo_b, hi_b in buckets:
        subset = [i for i in instances if lo_b <= len(i.train) <= hi_b]
        if len(subset) < 30:
            continue
        res = evaluate(subset, {"m": model_only, "p": pop_for_buckets, "h": hyb})
        by = {m.name: m.row() for m in res}
        label = f"{lo_b}-{hi_b}" if hi_b < 10**6 else f"{lo_b}+"
        print(f"  {label:<10}{len(subset):>7}{by['m']['recall@2']:>8.1f}%"
              f"{by['p']['recall@2']:>10.1f}%{by['h']['recall@2']:>11.1f}%")
    return 0


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_init_db(args, cfg, conn=None, run_id=None) -> int:
    db.init_db(cfg.database_url)
    print(f"schema applied to {cfg.database_url}")
    return 0


def cmd_probe(args, cfg, conn=None, run_id=None) -> int:
    """One live call, printed raw. Use this to confirm field shapes."""
    client = _client(cfg)
    since, until = _window(args, cfg.ellington_tz)
    print(f"window: {since.isoformat()} .. {until.isoformat() if until else 'now'}\n")
    stories = list(client.iter_stories(since, until, limit=args.limit,
                                      status=cfg.ellington_status))
    if not stories:
        print("no stories returned -- widen --hours, or check ELLINGTON_SITE_ID")
        return 1
    for story in stories:
        print("=" * 72)
        print(f"  id             {story.story_id}")
        print(f"  headline       {story.headline[:70]}")
        print(f"  kicker         {story.kicker[:70]}")
        print(f"  tease          {story.tease[:140]}")
        print(f"  share_url      {story.share_url}")
        print(f"  published      {story.published}")
        print(f"  sub_required   {story.subscription_required}")
        print(f"  thumbnail_url  {story.thumbnail_url}")
        print(f"  thumbnails RAW {str(story.thumbnail_raw)[:300]}")
        if args.raw:
            print(json.dumps(story.raw, indent=2, default=str)[:4000])
        print("  --> embed_input: "
              f"{build_embed_input(story.headline, story.tease, story.kicker)[:200]!r}")
    print("=" * 72)
    print(f"{len(stories)} stories | "
          f"{sum(1 for s in stories if not s.thumbnail_url)} without a thumbnail | "
          f"{sum(1 for s in stories if not s.tease)} without a tease")
    return 0


def cmd_fetch(args, cfg, conn, run_id) -> int:
    counts = stage_fetch(args, cfg, conn)
    _record(conn, run_id, counts)
    return 0


def cmd_embed(args, cfg, conn, run_id) -> int:
    counts = stage_embed(args, cfg, conn)
    _record(conn, run_id, counts)
    if counts.get("embedded") and not args.no_corpus_mean:
        stage_corpus_mean(args, cfg, conn, run_id)
    return 0


def cmd_run(args, cfg, conn, run_id) -> int:
    counts = stage_fetch(args, cfg, conn)
    if not args.dry_run:
        counts |= stage_embed(args, cfg, conn)
    _record(conn, run_id, counts)
    # Recompute whenever new vectors landed -- the mean must reflect the
    # corpus the matching step is about to search.
    if counts.get("embedded") and not args.no_corpus_mean:
        stage_corpus_mean(args, cfg, conn, run_id)
    return 0


def cmd_corpus_mean(args, cfg, conn, run_id) -> int:
    stage_corpus_mean(args, cfg, conn, run_id)
    return 0


def cmd_profiles(args, cfg, conn, run_id) -> int:
    counts = stage_profiles(args, cfg, conn, run_id)
    _record(conn, run_id, counts)
    return 0


def cmd_match(args, cfg, conn, run_id) -> int:
    counts = stage_match(args, cfg, conn, run_id)
    _record(conn, run_id, counts)
    return 0


def cmd_backtest(args, cfg, conn=None, run_id=None) -> int:
    with db.connect(cfg.database_url) as conn:
        return stage_backtest(args, cfg, conn)


def cmd_sync_readers(args, cfg, conn, run_id) -> int:
    _record(conn, run_id, stage_sync_readers(args, cfg, conn, run_id))
    return 0


def cmd_sync_popularity(args, cfg, conn, run_id) -> int:
    _record(conn, run_id, stage_sync_popularity(args, cfg, conn, run_id))
    return 0


def cmd_status(args, cfg, conn=None, run_id=None) -> int:
    with db.connect(cfg.database_url) as conn:
        print("stories")
        for key, value in db.status(conn).items():
            print(f"  {key:<16} {value}")
        mean = db.current_corpus_mean(conn, cfg.embed_model)
        print("\ncorpus mean")
        if mean:
            print(f"  id {mean['id']} | {mean['story_count']} stories | "
                  f"{mean['dimensions']}d | {mean['window_days']}d window | "
                  f"computed {mean['computed_at']:%Y-%m-%d %H:%M}")
        else:
            print(f"  none for {cfg.embed_model}")
        pop = db.popularity_summary(conn)
        print("\npopularity (from raw_web.pageviews)")
        if pop and pop.get("stories"):
            print(f"  {pop['stories']} stories | {pop['views']} views | "
                  f"median {pop['median_views']} | snapshot "
                  f"{pop['as_of']:%Y-%m-%d %H:%M} UTC")
        else:
            print("  none imported")
        prof = db.profile_summary(conn)
        print("\nprofiles")
        if prof.get("readers"):
            print(f"  {prof['readers']} readers | {prof['clusters']} clusters | "
                  f"avg {prof['avg_stories_per_cluster']} stories/cluster | "
                  f"max {prof['max_stories']} | built {prof['built_at']:%Y-%m-%d %H:%M}")
        else:
            print("  none built")
        eds = db.edition_summary(conn, limit=3)
        print("\neditions")
        if eds:
            for e in eds:
                print(f"  #{e['id']} {e['edition_date']} {e['status']:<11} "
                      f"{e['layout']} | {e['payloads']} payloads | "
                      f"{e['stories']} stories")
        else:
            print("  none built")
        runs = db.recent_runs(conn, limit=args.runs)
        print(f"\nlast {len(runs)} runs")
        for r in runs:
            detail = (f"f={r['fetched']} i={r['inserted']} u={r['updated']} "
                      f"e={r['embedded']} s={r['skipped']}")
            print(f"  #{r['id']:<4} {r['started_at']:%m-%d %H:%M} "
                  f"{r['command']:<12} {r['status']:<8} {detail}"
                  + (f"  ERR: {r['error'][:60]}" if r["error"] else ""))
    return 0


def _record(conn, run_id, counts) -> None:
    """Fold stage counts into the in-flight run row as they happen."""
    if run_id is not None:
        db.update_run(conn, run_id, counts)


# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# command registry
#
# Argument definitions live here once and are consumed by two entry points:
# the `python -m adgvec` CLI and the Django management commands in
# pipeline/management/commands/. Keeping one definition means a flag added
# here appears in both without being restated.
# --------------------------------------------------------------------------- #

def _add_window(p) -> None:
    p.add_argument("--hours", type=int, default=24,
                   help="look back this many hours (default: 24)")
    p.add_argument("--since", help="ISO datetime overriding --hours")
    p.add_argument("--until", help="ISO datetime upper bound")
    p.add_argument("--limit", type=int, help="stop after N stories")


def args_init_db(p) -> None:
    pass


def args_probe(p) -> None:
    _add_window(p)
    p.add_argument("--raw", action="store_true", help="dump the full JSON record")


def args_fetch(p) -> None:
    _add_window(p)
    p.add_argument("--dry-run", action="store_true")


def args_embed(p) -> None:
    p.add_argument("--force", action="store_true", help="re-embed everything")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    p.add_argument("--limit", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-corpus-mean", action="store_true")


def args_run(p) -> None:
    _add_window(p)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    p.add_argument("--no-corpus-mean", action="store_true")


def args_corpus_mean(p) -> None:
    pass


def args_sync_readers(p) -> None:
    p.add_argument("--limit", type=int, help="only the first N readers")
    p.add_argument("--prune", action="store_true",
                   help="delete readers absent from this sync")
    p.add_argument("--timestamped", action="store_true",
                   help="also rebuild the reads table with per-read "
                        "timestamps from pageviews")
    p.add_argument("--history-days", type=int, default=30,
                   help="lookback for timestamped reads (default: 30)")
    p.add_argument("--dry-run", action="store_true")


def args_sync_popularity(p) -> None:
    p.add_argument("--window-hours", type=int, default=24,
                   help="how far back to count views (default: 24)")
    p.add_argument("--published-within-hours", type=int, default=48,
                   help="only stories published this recently (default: 48)")
    p.add_argument("--include-bots", action="store_true",
                   help="do not filter to traffic_type='human'")
    p.add_argument("--historical", action="store_true",
                   help="backfill per-story counts as of each story's own "
                        "send time, for backtesting")
    p.add_argument("--pub-since", help="historical mode: first publication day")
    p.add_argument("--pub-until",
                   help="historical mode: last publication day (exclusive)")
    p.add_argument("--send-hour", type=int, default=5,
                   help="local hour the email sends (default: 5)")
    p.add_argument("--dry-run", action="store_true")


def args_profiles(p) -> None:
    p.add_argument("--threshold", type=float, default=DEFAULT_SIM_THRESHOLD,
                   help="cosine similarity above which two stories are one "
                        f"interest (default: {DEFAULT_SIM_THRESHOLD})")
    p.add_argument("--half-life", type=float, default=DEFAULT_HALF_LIFE_DAYS,
                   help=f"recency half-life in days (default: {DEFAULT_HALF_LIFE_DAYS})")
    p.add_argument("--min-reads-to-cluster", type=int, default=MIN_READS_TO_CLUSTER,
                   help="below this many reads, use a single weighted mean")
    p.add_argument("--singleton-drop-min", type=int,
                   default=DEFAULT_SINGLETON_DROP_MIN_READS,
                   help="at/above this many reads, discard one-story clusters")
    p.add_argument("--max-clusters", type=int, default=DEFAULT_MAX_CLUSTERS,
                   help="keep at most N clusters per reader, strongest first "
                        f"(default: {DEFAULT_MAX_CLUSTERS}; 0 = unlimited)")
    p.add_argument("--cluster", action="store_true", default=None,
                   help="build multi-centroid profiles (off by default: "
                        "measured worse than a plain mean)")
    p.add_argument("--recency", action="store_true", default=None,
                   help="apply recency decay (off by default: measured "
                        "monotonically harmful)")
    p.add_argument("--rarity", action="store_true", default=None,
                   help="apply rarity weighting (off by default: costs "
                        "~1.4pts recall@2)")
    p.add_argument("--corpus-mean-id", type=int, help="use a specific corpus mean")
    p.add_argument("--user", help="build for one email address")
    p.add_argument("--limit", type=int, help="only the first N readers")
    p.add_argument("--inspect", action="store_true",
                   help="print each cluster's headlines for calibration")
    p.add_argument("--dry-run", action="store_true")


def args_match(p) -> None:
    _add_window(p)
    p.add_argument("--edition-date", help="ISO date for the edition (default: today)")
    p.add_argument("--editor-picks", default="",
                   help="comma-separated story ids, in priority order; "
                        "overrides --editor-section-front")
    p.add_argument("--editor-section-front", type=int, default=None,
                   help="pull editor picks from this section front's "
                        "lead_story + additional_stories")
    p.add_argument("--no-editor-picks", dest="editor_section_front",
                   action="store_const", const=0,
                   help="build with no editor picks at all")
    p.add_argument("--layout", default=None,
                   help="slot plan, E=editor P=personalized")
    p.add_argument("--alpha", type=float, default=None,
                   help="strength exponent in strength^alpha * cosine")
    p.add_argument("--max-pair-sim", type=float, default=None,
                   help="similarity ceiling between two same-cluster picks")
    p.add_argument("--corpus-mean-id", type=int)
    p.add_argument("--hybrid-weight", type=float, default=None,
                   help="weight on the personal score when blended with "
                        f"pageview popularity (default: {DEFAULT_HYBRID_WEIGHT})")
    p.add_argument("--no-hybrid", action="store_true",
                   help="rank on the personal score alone")
    p.add_argument("--popularity-max-age", type=int, default=36,
                   help="ignore popularity snapshots older than N hours")
    p.add_argument("--allow-no-thumbnail", action="store_true",
                   help="keep stories with no image in the candidate pool")
    p.add_argument("--dry-run", action="store_true")


def args_backtest(p) -> None:
    p.add_argument("--min-reads", type=int, default=3,
                   help="readers need this many distinct reads (default: 3)")
    p.add_argument("--min-pool", type=int, default=10,
                   help="skip days with a smaller candidate pool")
    p.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    p.add_argument("--threshold", type=float, default=DEFAULT_SIM_THRESHOLD)
    p.add_argument("--corpus-mean-id", type=int)
    p.add_argument("--allow-no-thumbnail", action="store_true")
    p.add_argument("--temporal", action="store_true",
                   help="build each profile only from reads that happened "
                        "before the held-out read (needs the reads table)")
    p.add_argument("--min-train", type=int, default=2,
                   help="temporal mode: minimum prior reads required")
    p.add_argument("--half-life", type=float, default=None,
                   help="override the recency half-life in days")
    p.add_argument("--exclude-domains", default="",
                   help="comma-separated email domains to hold out of the "
                        "evaluation and the popularity signal")
    p.add_argument("--seed", type=int, default=0)


def args_status(p) -> None:
    p.add_argument("--runs", type=int, default=5, help="how many runs to list")


@dataclass(frozen=True)
class CommandSpec:
    name: str
    help: str
    add_arguments: Callable[[Any], None]
    handler: Callable[..., int]
    locked: bool = False
    defaults: dict[str, Any] = field(default_factory=dict)

    @property
    def module_name(self) -> str:
        """Django management command name, e.g. sync-readers -> sync_readers."""
        return self.name.replace("-", "_")


SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("init-db", "apply the schema migrations (idempotent)",
                args_init_db, cmd_init_db),
    CommandSpec("probe", "print raw API fields for a few stories",
                args_probe, cmd_probe, defaults={"limit": 3}),
    CommandSpec("fetch", "pull stories into Postgres (no embedding)",
                args_fetch, cmd_fetch, locked=True),
    CommandSpec("embed", "embed stories that need it",
                args_embed, cmd_embed, locked=True),
    CommandSpec("run", "fetch then embed (the daily story job)",
                args_run, cmd_run, locked=True),
    CommandSpec("corpus-mean", "recompute the corpus mean vector now",
                args_corpus_mean, cmd_corpus_mean, locked=True),
    CommandSpec("sync-readers", "import reading history from Treasure Data",
                args_sync_readers, cmd_sync_readers, locked=True),
    CommandSpec("sync-popularity", "import pageview counts from Treasure Data",
                args_sync_popularity, cmd_sync_popularity, locked=True),
    CommandSpec("profiles", "build reader interest profiles",
                args_profiles, cmd_profiles, locked=True),
    CommandSpec("match", "build the daily edition and payloads",
                args_match, cmd_match, locked=True),
    CommandSpec("backtest", "leave-one-out evaluation of the ranker",
                args_backtest, cmd_backtest),
    CommandSpec("status", "show what is in the database",
                args_status, cmd_status),
)

COMMANDS: dict[str, CommandSpec] = {spec.name: spec for spec in SPECS}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adgvec",
        description="ADG personalized email pipeline.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--env-file", help="path to an alternate .env")
    sub = parser.add_subparsers(dest="command", required=True)
    for spec in SPECS:
        p = sub.add_parser(spec.name, help=spec.help)
        spec.add_arguments(p)
        p.set_defaults(func=spec.handler, **spec.defaults)
    return parser


def execute(name: str, args) -> int:
    """Run one pipeline command. Shared by the CLI and Django commands.

    Owns config loading, the advisory lock and the run log, so neither entry
    point has to reimplement any of it.
    """
    spec = COMMANDS[name]
    try:
        cfg = load_config(getattr(args, "env_file", None))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    # Database-backed settings override the .env defaults when the Django
    # pipeline app is installed. Secrets stay in the environment.
    try:
        from pipeline.settings_store import apply_overrides

        cfg = apply_overrides(cfg)
    except Exception:  # noqa: BLE001 - CLI-only installs have no Django app
        pass

    if not spec.locked:
        try:
            return spec.handler(args, cfg)
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 130
        except Exception as exc:  # noqa: BLE001
            log.error("%s", exc, exc_info=getattr(args, "verbose", False))
            return 1

    try:
        conn = db.connect(cfg.database_url)
    except Exception as exc:  # noqa: BLE001
        log.error("could not connect to the database: %s", exc)
        return 1

    run_id = None
    try:
        with db.advisory_lock(conn):
            since, until = _window(args, cfg.ellington_tz)
            run_id = db.start_run(conn, name, since, until)
            rc = spec.handler(args, cfg, conn, run_id)
            db.finish_run(conn, run_id, "ok" if rc == 0 else "error")
            return rc
    except ConfigError as exc:
        if run_id:
            db.finish_run(conn, run_id, "error", str(exc))
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        if run_id:
            db.finish_run(conn, run_id, "error", "interrupted")
        return 130
    except Exception as exc:  # noqa: BLE001
        if run_id:
            db.finish_run(conn, run_id, "error", traceback.format_exc())
        log.error("%s", exc, exc_info=getattr(args, "verbose", False))
        return 1
    finally:
        conn.close()


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)
    return execute(args.command, args)


if __name__ == "__main__":
    raise SystemExit(main())
