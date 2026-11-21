"""The watchlist and the new episodes look (bridge/watchlist.py), and their cards in Keep watching."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ["COUCH_DATA"] = tempfile.mkdtemp(prefix="couchtv-watchlist-")
sys.path.insert(0, str(Path(__file__).parents[1] / "bridge"))
import store  # noqa: E402
import server  # noqa: E402
import watchlist  # noqa: E402

NOW = datetime.now(timezone.utc)


def ago(days):
    return (NOW - timedelta(days=days)).isoformat(timespec="seconds")


def day(days_ago):
    return (NOW - timedelta(days=days_ago)).date().isoformat()


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import config
        self._people, self._ids = config.PEOPLE, config.PEOPLE_IDS
        config.PEOPLE = [{"id": "jim", "name": "Jim"}, {"id": "helen", "name": "Helen"}]
        config.PEOPLE_IDS = ["jim", "helen"]
        for f in (store.HISTORY, store.IMPORTED, store.HIDDEN, store.WHO_FILE, watchlist.WATCHLIST, watchlist.EPISODES):
            if os.path.exists(f):
                os.remove(f)
        store._cache.clear()
        self.tv = next(iter(server.TVS))
        server._current.set(self.tv)

    def tearDown(self):
        import config
        config.PEOPLE, config.PEOPLE_IDS = self._people, self._ids
        # run with the other tests, store may point at an earlier test's folder: leave nothing behind
        for f in (watchlist.WATCHLIST, watchlist.EPISODES):
            if os.path.exists(f):
                os.remove(f)

    def played(self, title, who, days_ago, **extra):
        store.add_history({"title": title, "type": "tv", "tv": self.tv, "who": who, "service": "netflix",
                           "when": ago(days_ago), **extra})

    def latest(self, season, episode, released, name="Ep", on=("Netflix",)):
        async def look(title):
            return {"show": title, "latest_available": {"season": season, "episode": episode, "name": name,
                                                        "released": released, "on": list(on)}}
        return look


class WatchlistTests(Base):
    def test_add_puts_it_on_top_once(self):
        watchlist.add("jim", "Severance", type_="tv")
        watchlist.add("jim", "Dune", type_="movie")
        first = watchlist.add("jim", "severance")   # again, any case: moves to the top, keeps its id
        self.assertEqual([i["title"] for i in watchlist.items("jim")], ["severance", "Dune"])
        self.assertTrue(first["id"].startswith("wl-"))
        self.assertEqual(watchlist.items("helen"), [])

    def test_remove_by_id_or_title(self):
        a = watchlist.add("jim", "Severance")
        watchlist.add("helen", "Severance")
        self.assertTrue(watchlist.remove("jim", a["id"]))
        self.assertEqual(watchlist.items("jim"), [])
        self.assertEqual(len(watchlist.items("helen")), 1)   # only Jim's went
        self.assertTrue(watchlist.remove(None, "SEVERANCE"))
        self.assertFalse(watchlist.remove(None, "Severance"))

    def test_capped(self):
        for n in range(watchlist.LIST_MAX + 5):
            watchlist.add("jim", f"Show {n}")
        self.assertEqual(len(watchlist.items("jim")), watchlist.LIST_MAX)


class NewEpisodeTests(Base):
    async def test_new_since_last_watched(self):
        self.played("Slow Horses", "jim", 10)
        await watchlist.refresh(["jim", "helen"], self.latest(4, 6, day(2)))
        new = watchlist.new_for("jim")
        self.assertEqual([(c["title"], c["season"], c["episode"]) for c in new], [("Slow Horses", 4, 6)])
        self.assertEqual(new[0]["service"], "netflix")       # where Jim watched it
        self.assertEqual(watchlist.new_for("helen"), [])     # Helen never watched it

    async def test_watched_after_it_came_out_is_not_new(self):
        self.played("Slow Horses", "jim", 1)
        await watchlist.refresh(["jim"], self.latest(4, 6, day(3)))
        self.assertEqual(watchlist.new_for("jim"), [])

    async def test_old_episode_is_not_new(self):
        self.played("Slow Horses", "jim", 60)
        await watchlist.refresh(["jim"], self.latest(4, 6, day(watchlist.NEW_DAYS + 5)))
        self.assertEqual(watchlist.new_for("jim"), [])

    async def test_watchlist_show_counts_without_history(self):
        watchlist.add("helen", "The Bear", type_="tv", service="disney-plus")
        await watchlist.refresh(["jim", "helen"], self.latest(5, 1, day(1), on=["Disney Plus"]))
        new = watchlist.new_for("helen")
        self.assertEqual([c["title"] for c in new], ["The Bear"])
        self.assertEqual(new[0]["service"], "disney-plus")

    async def test_films_on_the_list_are_not_looked_up(self):
        watchlist.add("jim", "Dune", type_="movie")
        look = AsyncMock(return_value=None)
        await watchlist.refresh(["jim"], look)
        look.assert_not_called()

    async def test_looked_up_once_a_day(self):
        self.played("Slow Horses", "jim", 5)
        look = AsyncMock(return_value=None)
        await watchlist.refresh(["jim"], look)
        await watchlist.refresh(["jim"], look)
        self.assertEqual(look.await_count, 1)
        await watchlist.refresh(["jim"], look, force=True)
        self.assertEqual(look.await_count, 2)

    async def test_a_failed_look_skips_only_that_show(self):
        self.played("Slow Horses", "jim", 5)
        self.played("Severance", "jim", 4)

        async def look(title):
            if title == "Severance":
                raise RuntimeError("JustWatch down")
            return {"show": title, "latest_available": {"season": 4, "episode": 6, "released": day(1), "on": []}}
        await watchlist.refresh(["jim"], look)
        self.assertEqual([c["title"] for c in watchlist.new_for("jim")], ["Slow Horses"])

    async def test_dismissed_stays_off_until_the_next_episode(self):
        self.played("Slow Horses", "jim", 10)
        await watchlist.refresh(["jim"], self.latest(4, 6, day(2)))
        watchlist.dismiss_new("jim", watchlist.new_for("jim")[0])
        self.assertEqual(watchlist.new_for("jim"), [])
        await watchlist.refresh(["jim"], self.latest(4, 7, day(1)), force=True)
        self.assertEqual([c["episode"] for c in watchlist.new_for("jim")], [7])

    async def test_imported_netflix_history_counts(self):
        store.add_imported([{"key": "k1", "source": "netflix", "who": "jim", "series": "Dark",
                             "title": "Dark: Season 1: Secrets", "when": ago(20)}])
        await watchlist.refresh(["jim"], self.latest(3, 8, day(1)))
        self.assertEqual([c["title"] for c in watchlist.new_for("jim")], ["Dark"])


class KeepWatchingCardTests(Base):
    async def test_cards_in_the_row(self):
        self.played("Slow Horses", "jim", 10)
        self.played("Dark", "jim", 1)   # watched since its newest episode: stays a plain card
        await watchlist.refresh(["jim"], self.latest(4, 6, day(2)))
        watchlist.add("jim", "Severance", type_="tv")
        cards = server.continue_items("jim")
        self.assertEqual([c["title"] for c in cards], ["Slow Horses", "Dark", "Severance"])
        self.assertEqual(cards[0]["kind"], "new_episode")
        self.assertEqual(cards[0]["label"], "New · S4 E6")
        self.assertEqual(cards[2]["kind"], "watchlist")
        self.assertEqual(cards[2]["label"], "On your list")

    def test_a_listed_show_already_in_the_row_is_not_twice(self):
        self.played("Dark", "jim", 3)
        watchlist.add("jim", "Dark")
        self.assertEqual([c["title"] for c in server.continue_items("jim")], ["Dark"])

    async def test_open_and_hide(self):
        item = watchlist.add("jim", "Severance", service="apple-tv")
        req = type("R", (), {})()
        with patch.object(server, "play", AsyncMock(return_value={"title": "Severance"})) as play, \
                patch.object(server, "body", AsyncMock(return_value={"id": item["id"], "who": "jim"})), \
                patch.object(server, "record_action"):
            await server.h_continue_open(req)
            play.assert_awaited_once_with("Severance", service="apple-tv", who="jim")
            await server.h_continue_hide(req)
        self.assertEqual(watchlist.items("jim"), [])


if __name__ == "__main__":
    unittest.main()
