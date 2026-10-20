# Daily TikTok: Zambia and the world

An actual video pipeline using local AI and free GitHub Actions runners. Target: one original English-language 9:16 explainer each day, scheduled for **08:00 Africa/Lusaka**. This preserves the time of the existing ChatGPT task.

The code lives in `tiktok-daily/` inside the connected repository. The two workflow files belong at `.github/workflows/tiktok-daily.yml` and `.github/workflows/tiktok-test.yml` in the repository root.

## What runs

1. Research fresh Google search trends for Zambia, South Africa, and the US, and fresh Zambian/international RSS items. Each feed is independent; a failed feed is recorded. A country can have no trend results.
2. Fetch article excerpts, require either a primary publisher or two distinct publisher domains, reject stale inputs and unrelated extracted text, and avoid topics recently used. First-party publishers are recognized by their HTTPS host in trend results. When an accessible first-party account is available, it supplies the script evidence in preference to secondary headlines. NASA feed context also admits science titles without a narrow keyword match; an already retrieved first-party RSS summary can be used when its article is unavailable. Source outages do not consume the AI retry budget: all shortlisted candidates can be tried. Within the regional and trend preferences, first-party evidence takes priority over secondary-only stories.
3. Run **Qwen2.5-7B-Instruct Q4_K_M locally** on the runner to write an eight-scene story with 170–230 spoken words, caption, and hashtags. The output schema requires 22–28 narration words per scene. The story progresses through a hook, background, change, concrete detail, stakes, qualification, outcome, and closing question; an additional story check rejects disconnected facts or repeated filler. Validate structure, length, unsupported numbers, source references, and substantial copying. Numerical survey percentages and durations are omitted from unattended scripts and rejected by validation. This prevents a source timespan for a return or first start from being attached to a different event, such as scoring a goal. Check narration, scene headings, the video title, caption, and factual premises inside questions against cited evidence, and attempt a source-based revision if a check fails. Text cleanup preserves qualifications and negations after dashes. This is a heuristic check, not a guarantee of factual accuracy.
4. Run **Piper neural text-to-speech locally** with the LJ Speech voice. Generate original full-scene illustrations with Pillow and FFmpeg: campus buildings, documents, people, grant funding, satellites, stadiums, phones, music, and world views. The storyboard chooses the visual for each scene, uses varied layouts and colors, and animates the illustration, props, title entrances, and progress independently. Burn captions into the video and export an SRT. At least 60 seconds must come from actual narration; the pipeline rejects short speech instead of padding it with silence.
5. Verify a complete **1080×1920 H.264/AAC MP4**, decode the whole file, and enforce a 60–110 second duration and 50 MB cap.
6. Publish the MP4 as a public GitHub Release asset with a stable URL. Buffer requires public media hosting; an Actions artifact download URL would require authentication and is unsuitable.
7. Checkpoint the date and caption into `automation-state/ledger.json` before sending the **current Buffer GraphQL `createPost` mutation**. Request automatic publishing and set TikTok's `isAiGenerated` flag.
8. Check actual Buffer delivery status later. A scheduled post is reported as scheduled. Only `sent` is reported as published. Old release videos are removed only after confirmed delivery and seven days.

The standard workflow starts at **07:17 Lusaka (05:17 UTC)** to allow model setup and rendering before 08:00. A second run at **08:47 Lusaka (06:47 UTC)** checks delivery without creating another video. GitHub's scheduler can be delayed or skip a run; this is a daily target, not a delivery SLA.

## Activation

Publishing is gated by the repository variable `AUTOPUBLISH_ENABLED=true`. A checkout or merge alone does not publish anything. Use the Actions **Daily TikTok video** workflow with **preview** to generate and download a real video without Buffer credentials.

1. Use Buffer's **Free** plan (no trial, payment card, or paid upgrade). Connect the intended TikTok channel and choose **Automatic** publishing. If Buffer offers only notification publishing for your account, this system cannot make that channel fully automatic.
2. In Buffer's API settings, create the single free API key. Enter it directly as a GitHub Actions repository secret named `BUFFER_API_KEY`. Never paste a token into an issue, source file, or chat.
3. Run the workflow in **preflight** mode. Its output lists TikTok channel IDs and connection state. Put the intended ID in the GitHub Actions repository variable `BUFFER_TIKTOK_CHANNEL_ID`.
4. Run **preview** and review its video, source evidence, and caption. Run **preflight** again to verify the selected channel.
5. Pause the existing ChatGPT task **Daily TikTok Content Check**, when activating the replacement. It currently produces only a content package at 08:00 Africa/Lusaka. Do not create a second ChatGPT daily task.
6. Set `AUTOPUBLISH_ENABLED` to `true`. The next daily run will generate and schedule a post. A manual **live** run targets today and publishes later today if 08:00 has passed. Do not run live with a test video unless you intend that video to be public.
7. Verify the later **status** run reports `sent` and records the TikTok URL before considering end-to-end publishing tested.

Steps 2 and 3 need the account owner because no Buffer credential is available in this session. This project does not activate billing or create paid subscriptions.

## Local use

From the `tiktok-daily` folder, with Python 3.12, GCC/G++, FFmpeg, and DejaVu fonts installed:

```bash
python -m venv .venv
. .venv/bin/activate
CC=gcc CXX=g++ CMAKE_BUILD_PARALLEL_LEVEL=2 CMAKE_ARGS='-DGGML_NATIVE=OFF' pip install -r requirements.txt
python scripts/download_models.py
OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 python -m tiktok_daily preview
python -m unittest discover -s tests -v
```

The first model download is approximately 4.80 GB. The standard public runner has enough memory; allow at least 8 GB RAM for local use. This download runs on GitHub, not your phone. Model URLs are pinned to immutable revisions and verified with SHA-256. The model uses a 3,072-token context to limit memory on 8 GB machines and is closed before Piper/video rendering starts. Models and Python wheels are cached by Actions. There is no hosted AI inference, OpenAI API key, paid API fallback, or trial-credit dependency.

For a repeatable render-only preview, use `--candidate path/to/candidate.json --package path/to/script.json`. Recorded evidence and supplied scripts are explicitly blocked in live mode.

## $0 boundaries

| Component | Implementation | Limit or account requirement |
| --- | --- | --- |
| Scheduling and compute | Standard Ubuntu GitHub Actions runner in a public repository | Private repositories are rejected in live mode; no larger/GPU runners |
| Scripts | Local Qwen model | Local model; scripts can fail validation or contain errors despite checks |
| Voice | Local Piper model | English voice; Zambian names may be mispronounced |
| Video | Pillow and FFmpeg | Animated original illustrations; not photorealistic AI footage or licensed TikTok clips |
| Subtitles | Generated from narration | Scene timing is exact; word-group timing is estimated |
| Research | Public RSS/search trend signals | No official TikTok popularity measurements; feed outages and limited Zambia coverage |
| Media hosting | Public GitHub Release assets | Generated videos are public before TikTok publication; pending assets are retained |
| Buffer API | Buffer Free | Currently 1 key, 100 calls/15 minutes, 250/day, 3,000/30 days |
| Buffer queue | One post added daily | Free plan has 10 queued posts/channel at a time, not 10 per month |
| Audit artifacts | Actions artifacts retained for 2 days | Only the final video, subtitles, original script, source links/hashes, and small status files are uploaded; internal article excerpts are excluded |

Native TikTok trending sounds, stickers, polls, and certain effects require finishing the post inside TikTok. This pipeline uses its original narration and graphics to stay compatible with automatic publishing. No copyrighted songs or other creators' videos are downloaded.

There can be days without a publishable fresh story, a valid script, network access, or a functioning channel. The workflow stops with an error rather than manufacture a trend or substitute old news. The Actions result and repository ledger make failures reviewable.

## Duplicate prevention and recovery

The Actions concurrency group serializes all daily runs. A date-keyed repository ledger survives runner loss and cache eviction. Its compare-and-swap commit must succeed before the Buffer mutation. The workflow never blindly retries a post-creation POST.

If Buffer may have accepted a request before a timeout, the date remains `submission_uncertain`. A later run searches existing Buffer posts for the exact caption and recovers the ID if found. Status mode continues checking later posts even when an older submission cannot be resolved, saves each resolved status, and reports all failures together. If it cannot prove the outcome, it stops. Inspect Buffer before manually clearing that ledger entry. This favors avoiding duplicate posts over guaranteeing a post after an ambiguous failure.

If a post is scheduled but does not send, inspect Buffer's error, reconnect the channel if needed, and use status mode to verify. The script does not automatically recreate errored posts.

## Validation status

The regression suite covers the 60-second final-video gate, eight-beat story order, minimum script length, repeated filler, and the absence of long padding pauses. All **34 regression tests pass locally** for this story-format update. Those checks include the source fallback and first-party recognition errors that caused the failed Actions video job. Tests also cover date rollover, the Buffer payload, ambiguous-submission duplicate prevention, later-post status recovery, missing API objects, invalid JSON responses, captions and question premises, preservation of narration qualifications, unsupported astronaut or Mars-surface claims about simulation crews, and numerical duration claims even when the number appears in the source. The local AI evidence check remains heuristic.

A complete fresh-research Actions run passed on the source-fix commit (`5d0a910`), including local AI, neural voice, subtitles, video rendering, and full-file decoding. A separate fresh-research Actions job checks the implementation; consult the [latest branch runs](https://github.com/Fortune581/Gg/actions?query=branch%3Atiktok-daily-automation) for the current result. Buffer authorization and deployment approval are still required before activation; no real Buffer submission or TikTok publication has occurred. The existing ChatGPT task remains enabled until the replacement is activated. Never treat a passing unit-test job as proof that the video job or TikTok delivery passed.

## Sources and licenses

- [Buffer API quick start](https://developers.buffer.com/guides/getting-started.html)
- [Buffer video mutation](https://developers.buffer.com/examples/create-video-post.html)
- [Buffer API reference, including TikTok AI disclosure](https://developers.buffer.com/reference.html)
- [Buffer media hosting requirements](https://developers.buffer.com/guides/hosting-media.html)
- [Buffer Free limits](https://buffer.com/pricing)
- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [Qwen model, Apache-2.0](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF)
- [Piper, GPL-3.0-or-later](https://github.com/OHF-Voice/piper1-gpl)
- [LJ Speech voice model card](https://huggingface.co/rhasspy/piper-voices/blob/c10ece1aade47bb51c153c893d14e5bf8e5b7117/en/en_US/ljspeech/high/MODEL_CARD)

Dependency licenses remain with their respective projects. No model weights, proprietary footage, music, or personal credentials are committed to this repository.
