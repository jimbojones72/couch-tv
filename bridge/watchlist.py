"""A watchlist for each person, and a daily look for new episodes of the shows they watch.

Both are kept as files in bridge/data/, next to the history (store.py):

- watchlist.json: person id -> their list, newest first. Each item has its own id ("wl-<hex>"),
  the title, year, type ("tv" or "movie"), the app it was found on, pictures and when it was added.
  A title is on a person's list once (the same title added again moves it to the top).
- new-episodes.json: what the daily look found, per show ("shows": normalised title -> the newest
  episode out in this home's country, with its date and the apps it is on), and the new episode
  cards removed by hand ("dismissed": "who|title|season|episode" -> when).

Which shows are looked at: every show (type "tv") a person played on our TVs or imported in the last
LOOK_DAYS days, and every show on their watchlist. A show has a new episode for that person when the
newest episode out came out within NEW_DAYS days and after they last watched the show (never watched:
a watchlist show counts as new while it is within NEW_DAYS days). Films never have new episodes.

The look itself (agentic.latest_episode, JustWatch) is passed in, so this module stays free of the
server and is easy to test.
"""
import asyncio
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

import store

log = logging.getLogger("watchlist")

WATCHLIST = store.os.path.join(store.DATA, "watchlist.json")
EPISODES = store.os.path.join(store.DATA, "new-episodes.json")
LIST_MAX = 50          # items on one person's list
LOOK_DAYS = 120        # shows played within this many days are looked at
NEW_DAYS = 14          # an episode is "new" for this many days after it came out
CHECK_EVERY = 20 * 3600
SHOWS_MAX = 60         # shows looked at in one run, the most recently watched first
_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc)


def norm(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


# ---------- the watchlist ----------
def _all():
    data = store._read(WATCHLIST, {})
    return data if isinstance(data, dict) else {}


def items(who):
    """This person's list, newest first."""
    got = _all().get(who) or []
    return [i for i in got if isinstance(i, dict) and i.get("title")]


def add(who, title, year=None, type_=None, service=None, poster=None, backdrop=None):
    """Put a title on this person's list (at the top). Returns the item."""
    title = (title or "").strip()
    if not who or not title:
        raise ValueError("who and title are required")
    with _lock:
        data = _all()
        mine = [i for i in data.get(who) or [] if norm(i.get("title")) != norm(title)]
        old = next((i for i in data.get(who) or [] if norm(i.get("title")) == norm(title)), None)
        item = {"id": (old or {}).get("id") or "wl-" + uuid.uuid4().hex[:12], "title": title,
                "year": year, "type": type_ if type_ in ("tv", "movie") else None, "service": service,
                "poster": poster, "backdrop": backdrop, "added": _now().isoformat(timespec="seconds")}
        data[who] = [item] + mine[:LIST_MAX - 1]
        store._write(WATCHLIST, data)
    return item


def remove(who, key):
    """Take an item off a list by its id or its title. who None: whoever has it. True if one went."""
    k = norm(key)
    gone = False
    with _lock:
        data = _all()
        for w in ([who] if who else list(data)):
            before = data.get(w) or []
            after = [i for i in before if i.get("id") != key and norm(i.get("title")) != k]
            if len(after) != len(before):
                data[w] = after
                gone = True
        if gone:
            store._write(WATCHLIST, data)
    return gone


def find_item(item_id):
    """(who, item) for a watchlist id, or (None, None)."""
    for who, mine in _all().items():
        for i in mine or []:
            if i.get("id") == item_id:
                return who, i
    return None, None


# ---------- new episodes ----------
def _episodes():
    data = store._read(EPISODES, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("shows", {})
    data.setdefault("dismissed", {})
    return data


def _when(iso):
    return store._when({"when": iso})


def last_watched(who):
    """Normalised show title -> (when, title, service, poster, backdrop) of this person's newest play
    of each show, from our history and the imported one. who None or "both": everyone's."""
    out = {}
    lines = [(h.get("title"), h) for h in store.history() if h.get("type") in ("tv", None)]
    # an imported Netflix line names the show in "series" (a film has none); YouTube has no shows
    lines += [(h.get("series"), h) for h in store.imported() if h.get("source") == "netflix" and h.get("series")]
    for title, h in lines:
        if who not in (None, "both") and h.get("who") not in (who, "both"):
            continue
        t = _when(h.get("when"))
        k = norm(title)
        if not k or t is None:
            continue
        if k not in out or t > out[k][0]:
            out[k] = (t, title, h.get("service") or (h.get("source") if h.get("source") == "netflix" else None),
                      h.get("poster"), h.get("backdrop"))
    return out


def shows_to_look_at(people, now=None):
    """Every show worth a look, most recently watched first: played within LOOK_DAYS days by anyone
    in `people`, or on anyone's watchlist as a show (or of unknown type)."""
    now = now or _now()
    cutoff = now - timedelta(days=LOOK_DAYS)
    seen = {}
    for who in people:
        for k, (t, title, *_rest) in last_watched(who).items():
            if t >= cutoff and (k not in seen or t > seen[k][0]):
                seen[k] = (t, title)
        for i in items(who):
            if i.get("type") in ("tv", None):
                k = norm(i["title"])
                t = _when(i.get("added")) or cutoff
                if k not in seen or t > seen[k][0]:
                    seen[k] = (t, i["title"])
    return [title for _, (t, title) in sorted(seen.items(), key=lambda kv: kv[1][0], reverse=True)][:SHOWS_MAX]


async def refresh(people, look, now=None, force=False):
    """Look up the newest episode of each show (`look` is agentic.latest_episode). A show looked at
    within CHECK_EVERY is skipped unless force. Returns how many were looked up."""
    now = now or _now()
    data = _episodes()
    done = 0
    for title in shows_to_look_at(people, now):
        k = norm(title)
        old = data["shows"].get(k) or {}
        checked = _when(old.get("checked"))
        if not force and checked and (now - checked).total_seconds() < CHECK_EVERY:
            continue
        try:
            r = await look(title)
        except Exception as e:   # one show failing never stops the rest
            log.info("new episodes: %s not looked up (%s: %s)", title, type(e).__name__, e)
            continue
        entry = {"title": title, "checked": now.isoformat(timespec="seconds")}
        newest = (r or {}).get("latest_available") or {}
        if newest.get("season") and newest.get("episode"):
            entry |= {"show": r.get("show") or title, "season": newest["season"], "episode": newest["episode"],
                      "name": newest.get("name"), "released": newest.get("released"), "on": newest.get("on") or []}
        data["shows"][k] = entry
        done += 1
        await asyncio.sleep(0.5)   # gentle on JustWatch
    if done:
        with _lock:
            fresh = _episodes()
            fresh["shows"].update(data["shows"])
            store._write(EPISODES, fresh)
    return done


def _dismiss_key(who, title, season, episode):
    return f"{who}|{norm(title)}|{season}|{episode}"


def new_for(who, now=None):
    """The shows with a new episode for this person, newest episode first. Each: id ("ne-..."),
    title, season, episode, name, released, on (apps), plus the service and pictures of their own
    last play when there was one."""
    now = now or _now()
    data = _episodes()
    watched = last_watched(who)
    listed = {norm(i["title"]): i for i in items(who)} if who not in (None, "both") else {}
    out = []
    for k, e in data["shows"].items():
        if not e.get("season") or not e.get("released"):
            continue
        released = _when(e["released"][:10] + "T00:00:00+00:00")
        if released is None or (now - released).days > NEW_DAYS or released > now:
            continue
        mine = watched.get(k)
        if mine:
            if mine[0] >= released:
                continue   # watched since it came out
        elif k not in listed:
            continue       # nobody here watches it
        if _dismiss_key(who, e.get("show") or e["title"], e["season"], e["episode"]) in data["dismissed"]:
            continue
        src = listed.get(k) or {}
        out.append({"id": f"ne-{k.replace(' ', '_')}-{e['season']}-{e['episode']}",
                    "title": e.get("show") or e["title"], "season": e["season"], "episode": e["episode"],
                    "name": e.get("name"), "released": e["released"], "on": e.get("on") or [],
                    "service": mine[2] if mine else src.get("service"),
                    "poster": (mine[3] if mine else None) or src.get("poster"),
                    "backdrop": (mine[4] if mine else None) or src.get("backdrop")})
    out.sort(key=lambda c: c["released"], reverse=True)
    return out


def find_new(card_id, who):
    """The new episode card with this id for this person, or None."""
    return next((c for c in new_for(who) if c["id"] == card_id), None)


def dismiss_new(who, card):
    with _lock:
        data = _episodes()
        data["dismissed"][_dismiss_key(who, card["title"], card["season"], card["episode"])] = \
            _now().isoformat(timespec="seconds")
        store._write(EPISODES, data)
