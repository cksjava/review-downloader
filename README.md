# Play Store review downloader

Downloads the public listing, permissions, and reviews for a Google Play app. Reviews are requested the way the Play Store website loads them: a phone-browser session opens the app page, then asks for one short page at a time and sends back the continuation token from the previous response. The walk has no review cap unless you set one, and each page is saved before the next request.

Requires Python 3.10 or newer. The tool uses only the standard library.

```bash
python3 -m playreviews download com.whatsapp
python3 -m playreviews status com.whatsapp
```

A Play Store details URL works in place of the package name. `--lang` and `--country` override `hl` and `gl` on that URL.

```bash
python3 -m playreviews download "https://play.google.com/store/apps/details?id=com.whatsapp&hl=en&gl=us"
```

## What gets saved

Files go in `downloads/<package>/<lang>-<country>-<sort>/`.

| File | Contents |
| --- | --- |
| `app.json` | Title, description, ratings, histogram, install counts, developer, media, data-safety notes, and permissions |
| `permissions.json` | Permission groups returned by the store |
| `reviews.jsonl` | One review per line |
| `checkpoint.json` | Continuation token and progress, written after every page |

Each review line includes the author, rating, text, timestamp, app version, thumbs-up count, developer reply, and any criteria ratings the store attached. Reviews already in the file are not written again.

Stop a run with Ctrl-C. Start it again with the same command and it continues from the saved token.

```bash
python3 -m playreviews download com.whatsapp --max 200
python3 -m playreviews download com.whatsapp --fresh
```

`--max` stops once that many reviews are stored. The page that crosses the limit is requested again on the next run, so the reviews after the cutoff are not dropped. `--fresh` deletes the review file and checkpoint for that query.

## Pacing

The default page is 40 reviews, which is about what the site loads as someone moves through reviews. The default pause is 2.5 seconds plus jitter, and every 10 pages there is a longer pause. If the store returns HTTP 429 or a temporary error, the tool waits and retries, then leaves the checkpoint where the last full page was saved.

```bash
python3 -m playreviews download com.whatsapp --delay 4 --burst-every 8 --page-size 40
```

`--sort` is `newest` (default), `relevant`, or `rating`. `--score 1` keeps one star rating. `--device mobile` limits reviews to that device class; leave it off to collect every device. A different sort, score, language, country, or device is a different walk and gets its own folder.

The store lists a review count on the app page. That number is kept in `app.json`. The downloader still follows the continuation token until the store stops sending one, including when a language returns a different set than the headline count.
