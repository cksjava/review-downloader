import json
import unittest
import urllib.parse

from playreviews.client import reviews_form
from playreviews.parse import (
    html_to_text,
    parse_listing,
    parse_permissions,
    parse_review,
    parse_review_response,
)


def listing_node():
    node = [None] * 151
    node[0] = ["Demo Maps"]
    node[9] = ["Everyone"]
    node[13] = ["1,000,000+", 1000000, 2500000, "1M+"]
    node[19] = ["$0.99 - $9.99 per item", [0]]
    node[48] = ["Contains ads"]
    node[51] = [
        ["4.5", 4.5],
        [None, ["10", 10], ["20", 20], ["30", 30], ["40", 40], ["50", 50]],
        ["150", 150],
        ["80", 80],
    ]
    node[57] = [[[[None, [[0, "USD", ""]]]]]]
    node[68] = ["Demo Studio", [None, None, None, None, [None, None, "/store/apps/dev?id=99"]], "99"]
    node[69] = [
        [None, None, None, None, None, [None, None, "https://example.com"]],
        ["dev@example.com"],
        ["1 Market Street", "Springfield"],
    ]
    node[72] = [[None, "Find places.<br>Then go there."]]
    node[73] = [[None, "Maps &amp; routes"]]
    node[78] = [[None, 2, [1080, 1920], [None, None, "https://example.com/shot.png"]]]
    node[79] = [[["Travel", [None, None, None, None, [None, None, "/store/apps/category/TRAVEL"]], "TRAVEL"]]]
    node[95] = [[None, 2, [512, 512], [None, None, "https://example.com/icon.png"]]]
    node[96] = [[None, 2, [1024, 500], [None, None, "https://example.com/header.png"]]]
    node[99] = [[None, None, None, None, None, [None, None, "https://example.com/privacy"]]]
    node[136] = [
        [None, [1, 0]],
        [
            [
                [None, 2, None, [None, None, "https://example.com/safety.png"]],
                "No data shared with third parties",
                [None, "Learn more about <b>sharing</b>"],
            ]
        ],
    ]
    node[140] = [[[["11.2.0"]]]]
    node[144] = [None, [None, "Bug fixes<br>and polish"]]
    node[145] = [["Nov 1, 2026", [1790000000, 0]]]
    node[150] = [[2, 3], [[2, [None, 4.4]], [3, [None, 4.1]]]]
    return node


class ParseTests(unittest.TestCase):
    def test_listing_fields(self):
        app = parse_listing(
            listing_node(),
            "com.example.demo",
            "https://play.google.com/store/apps/details?id=com.example.demo",
            "en",
            "us",
            "2026-11-18T00:00:00Z",
        )
        self.assertEqual(app["title"], "Demo Maps")
        self.assertEqual(app["description"], "Find places.\nThen go there.")
        self.assertEqual(app["summary"], "Maps & routes")
        self.assertEqual(app["recentChanges"], "Bug fixes\nand polish")
        self.assertEqual(app["histogram"], {"1": 10, "2": 20, "3": 30, "4": 40, "5": 50})
        self.assertEqual(app["ratings"], 150)
        self.assertEqual(app["reviews"], 80)
        self.assertEqual(app["realInstalls"], 2500000)
        self.assertTrue(app["free"])
        self.assertEqual(app["currency"], "USD")
        self.assertEqual(app["inAppPurchases"], "$0.99 - $9.99 per item")
        self.assertEqual(app["developerEmail"], "dev@example.com")
        self.assertEqual(app["developerWebsite"], "https://example.com")
        self.assertEqual(app["developerAddress"], "1 Market Street, Springfield")
        self.assertEqual(app["privacyPolicy"], "https://example.com/privacy")
        self.assertEqual(app["screenshots"][0]["width"], 1080)
        wrapped = listing_node()
        wrapped[78] = [wrapped[78]]
        wrapped_app = parse_listing(wrapped, "com.example.demo", "https://example.com", "en", "us", "2026-11-18T00:00:00Z")
        self.assertEqual(wrapped_app["screenshots"], app["screenshots"])
        self.assertEqual(app["version"], "11.2.0")
        self.assertEqual(app["dataSafety"][0]["title"], "No data shared with third parties")
        self.assertEqual(app["dataSafety"][0]["detail"], "Learn more about sharing")
        self.assertEqual(app["deviceScores"][0], {"device": "mobile", "deviceId": 2, "score": 4.4})
        self.assertTrue(app["containsAds"])

    def test_permissions_groups_and_bare_entries(self):
        payload = [
            [
                ["Camera", ["icon"], [[None, "take pictures and videos"]], ["perm_camera"]],
            ],
            [
                [None, "receive data from Internet"],
                [None, "download files without notification"],
            ],
        ]
        parsed = parse_permissions(payload)
        self.assertEqual(parsed[0]["category"], "Camera")
        self.assertEqual(parsed[0]["permissions"], ["take pictures and videos"])
        self.assertEqual(
            parsed[1],
            {
                "category": "Uncategorized",
                "permissions": [
                    "receive data from Internet",
                    "download files without notification",
                ],
            },
        )

    def test_review_keeps_reply_version_and_criteria(self):
        item = [
            "review-1",
            ["Ada", [None, 2, None, [None, None, "https://example.com/ada.png"]]],
            5,
            None,
            "Works on the train.",
            [1794880110, 165000000],
            3,
            ["Demo Studio", "Thanks for the note.", [1794870000, 0]],
            None,
            ["profile"],
            "11.2.0",
            None,
            [[["vaf_navigation", [2]], ["vaf_battery", [1]]]],
            None,
            [True, False],
            None,
            1,
        ]
        review = parse_review(item)
        self.assertEqual(review["userName"], "Ada")
        self.assertEqual(review["userImage"], "https://example.com/ada.png")
        self.assertEqual(review["at"], "2026-11-17T01:48:30Z")
        self.assertEqual(review["reply"]["content"], "Thanks for the note.")
        self.assertEqual(review["reply"]["at"], "2026-11-16T23:00:00Z")
        self.assertEqual(review["appVersion"], "11.2.0")
        self.assertEqual(review["criteria"][0], {"id": "vaf_navigation", "rating": 2})
        self.assertNotIn("profile", json.dumps(review["criteria"]))
        self.assertIn("14", review["extras"])

    def test_review_response_reads_token_after_xssi_prefix(self):
        review = [
            "review-1",
            ["Ada"],
            4,
            None,
            "Fine",
            [1794880110, 0],
            0,
            None,
            None,
            None,
            "1.0.0",
        ]
        inner = [[review], [None, "next-token"], [None]]
        line = json.dumps([["wrb.fr", "oCPfdb", json.dumps(inner), None, None, None, "generic"]])
        raw = ")]}'\n\n128\n" + line + "\n"
        reviews, token = parse_review_response(raw)
        self.assertEqual(token, "next-token")
        self.assertEqual(reviews[0]["content"], "Fine")
        self.assertEqual(reviews[0]["score"], 4)

    def test_form_body_roundtrips_the_continuation_token(self):
        token = 'abc"quoted\\token'
        body = reviews_form("com.example.demo", 2, 40, token, 5, 2)
        outer = json.loads(urllib.parse.parse_qs(body.decode())["f.req"][0])
        inner = json.loads(outer[0][0][1])
        self.assertEqual(inner[1][2], [40, None, token])
        self.assertEqual(inner[1][4][1], 5)
        self.assertEqual(inner[1][4][8], 2)
        self.assertEqual(inner[2][0], "com.example.demo")

        first = reviews_form("com.example.demo", 2, 40, None, None, None)
        first_inner = json.loads(json.loads(urllib.parse.parse_qs(first.decode())["f.req"][0])[0][0][1])
        self.assertEqual(first_inner[1][2], [40])

    def test_html_to_text_strips_markup(self):
        self.assertEqual(html_to_text("A<br>B &amp; C"), "A\nB & C")
        self.assertIsNone(html_to_text(None))


if __name__ == "__main__":
    unittest.main()
