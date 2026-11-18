import json
import random
import tempfile
import unittest
from pathlib import Path

from playreviews.client import StoreError
from playreviews.download import Config, DownloadError, run


def review(review_id, content):
    return {"reviewId": review_id, "content": content}


class FakeStore:
    def __init__(self, pages, fail_permissions=False):
        self.pages = pages
        self.calls = []
        self.fail_permissions = fail_permissions

    def fetch_app(self, app_id, fetched_at):
        return {
            "appId": app_id,
            "title": "Demo",
            "score": 4.5,
            "ratings": 10,
            "reviews": 4,
            "fetchedAt": fetched_at,
        }

    def fetch_permissions(self, app_id):
        if self.fail_permissions:
            raise StoreError("permissions unavailable")
        return [{"category": "Camera", "permissions": ["take pictures and videos"]}]

    def fetch_reviews(self, app_id, sort, count, token, score, device):
        self.calls.append(token)
        reviews, nxt = self.pages[token]
        return [dict(item) for item in reviews], nxt


PAGES = {
    None: ([review("a", "one"), review("b", "two")], "t1"),
    "t1": ([review("c", "three"), review("b", "dup")], "t2"),
    "t2": ([review("d", "four")], None),
}


def config(root, **overrides):
    values = dict(
        app_id="com.example.demo",
        lang="en",
        country="us",
        sort="newest",
        page_size=40,
        delay=0,
        burst_every=0,
        out=root,
    )
    values.update(overrides)
    return Config(**values)


def saved_ids(directory):
    ids = []
    with (directory / "reviews.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                ids.append(json.loads(line)["reviewId"])
    return ids


class DownloadTests(unittest.TestCase):
    def test_walks_every_page_until_the_token_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = FakeStore(PAGES)
            result = run(
                config(root),
                store,
                log=lambda message: None,
                sleep=self.fail_if_sleeping,
                rng=random.Random(0),
            )
            self.assertEqual(store.calls, [None, "t1", "t2"])
            self.assertEqual(saved_ids(result.output_dir), ["a", "b", "c", "d"])
            self.assertTrue(result.finished)
            checkpoint = json.loads((result.output_dir / "checkpoint.json").read_text())
            self.assertTrue(checkpoint["finished"])
            self.assertIsNone(checkpoint["nextToken"])
            app = json.loads((result.output_dir / "app.json").read_text())
            self.assertEqual(app["permissions"][0]["category"], "Camera")

            again = FakeStore(PAGES)
            second = run(
                config(root),
                again,
                log=lambda message: None,
                sleep=self.fail_if_sleeping,
                rng=random.Random(0),
            )
            self.assertEqual(again.calls, [])
            self.assertEqual(second.saved, 4)

    def test_max_stops_on_a_page_boundary_and_resume_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = FakeStore(PAGES)
            result = run(
                config(root, max_reviews=3),
                store,
                log=lambda message: None,
                sleep=self.fail_if_sleeping,
                rng=random.Random(0),
            )
            self.assertEqual(saved_ids(result.output_dir), ["a", "b", "c"])
            self.assertFalse(result.finished)
            checkpoint = json.loads((result.output_dir / "checkpoint.json").read_text())
            self.assertEqual(checkpoint["nextToken"], "t2")

            more = FakeStore(PAGES)
            resumed = run(
                config(root),
                more,
                log=lambda message: None,
                sleep=self.fail_if_sleeping,
                rng=random.Random(0),
            )
            self.assertEqual(more.calls, ["t2"])
            self.assertEqual(saved_ids(resumed.output_dir), ["a", "b", "c", "d"])
            self.assertTrue(resumed.finished)

    def test_max_mid_page_does_not_drop_the_rest_of_that_page(self):
        pages = {
            None: ([review("a", "one"), review("b", "two"), review("c", "three")], "t1"),
            "t1": ([review("d", "four")], None),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run(
                config(root, max_reviews=2),
                FakeStore(pages),
                log=lambda message: None,
                sleep=self.fail_if_sleeping,
                rng=random.Random(0),
            )
            checkpoint = json.loads((root / "com.example.demo" / "en-us-newest" / "checkpoint.json").read_text())
            self.assertIsNone(checkpoint["nextToken"])
            self.assertFalse(checkpoint["finished"])

            run(
                config(root, max_reviews=3),
                FakeStore(pages),
                log=lambda message: None,
                sleep=self.fail_if_sleeping,
                rng=random.Random(0),
            )
            self.assertEqual(
                saved_ids(root / "com.example.demo" / "en-us-newest"),
                ["a", "b", "c"],
            )

    def test_checkpoint_for_a_different_sort_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run(
                config(root, max_reviews=1),
                FakeStore(PAGES),
                log=lambda message: None,
                sleep=self.fail_if_sleeping,
                rng=random.Random(0),
            )
            checkpoint_path = result.output_dir / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text())
            checkpoint["sort"] = "rating"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            with self.assertRaises(DownloadError):
                run(
                    config(root),
                    FakeStore(PAGES),
                    log=lambda message: None,
                    sleep=self.fail_if_sleeping,
                    rng=random.Random(0),
                )

    def test_permissions_failure_still_saves_reviews(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run(
                config(root, max_reviews=1),
                FakeStore(PAGES, fail_permissions=True),
                log=lambda message: None,
                sleep=self.fail_if_sleeping,
                rng=random.Random(0),
            )
            self.assertEqual(saved_ids(result.output_dir), ["a"])
            app = json.loads((result.output_dir / "app.json").read_text())
            self.assertEqual(app["permissions"], [])

    def test_store_error_keeps_the_pages_already_written(self):
        class Flaky(FakeStore):
            def fetch_reviews(self, app_id, sort, count, token, score, device):
                if token == "t1":
                    raise StoreError("throttled")
                return super().fetch_reviews(app_id, sort, count, token, score, device)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(StoreError):
                run(
                    config(root),
                    Flaky(PAGES),
                    log=lambda message: None,
                    sleep=self.fail_if_sleeping,
                    rng=random.Random(0),
                )
            directory = root / "com.example.demo" / "en-us-newest"
            self.assertEqual(saved_ids(directory), ["a", "b"])
            checkpoint = json.loads((directory / "checkpoint.json").read_text())
            self.assertEqual(checkpoint["nextToken"], "t1")
            self.assertFalse(checkpoint["finished"])

    @staticmethod
    def fail_if_sleeping(seconds):
        raise AssertionError(f"unexpected sleep of {seconds}")


if __name__ == "__main__":
    unittest.main()
