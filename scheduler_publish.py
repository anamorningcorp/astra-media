#!/usr/bin/env python3
"""
Astra scheduled publisher -- runs inside GitHub Actions, every 10 minutes.

Unlike a fixed-UTC cron entry, this computes "now" in Europe/Zurich and
checks it against each post's own slot time, so it keeps publishing at the
right wall-clock hour straight through the CEST -> CET switch (2026-10-25)
without anyone having to touch the workflow file.

For every post whose slot time has passed in the last PUBLISH_WINDOW_MIN
minutes and whose status is still "aprovado", it publishes via the
Instagram Graph API (graph.instagram.com -- Instagram API with Instagram
Login), then writes status="publicado" + permalink back into posts.json and
commits that change, which is also what prevents a double-publish on the
next run.

Media is read from this same repo via raw.githubusercontent.com -- no
external hosting dependency.
"""
import csv
import datetime
import json
import os
import subprocess
import sys
import time
import zoneinfo

import requests

ZURICH = zoneinfo.ZoneInfo("Europe/Zurich")
PUBLISH_WINDOW_MIN = 12  # cron runs every 10 min; a little slack for GH Actions scheduling jitter
GRAPH_API = "https://graph.instagram.com/v21.0"
POSTS_JSON = "posts.json"
LOG_CSV = "publish_log.csv"
LOG_FIELDS = [
    "timestamp", "dia", "slot", "data_publicacao", "formato",
    "status", "container_id", "media_id", "permalink", "erro",
]
SLOT_TIMES = {
    "morning": (7, 0),
    "lunch": (12, 30),
    "evening": (20, 0),
}


def load_posts():
    with open(POSTS_JSON, encoding="utf-8") as f:
        return json.load(f)


def save_posts(posts):
    with open(POSTS_JSON, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)
        f.write("\n")


def media_paths_for(post, base_url):
    stem = f"day{post['dia']:03d}_{post['slot']}"
    formato = post["formato"]
    if formato.startswith("Carousel"):
        return [f"{base_url}/{stem}_slide{i}.png" for i in range(3)]
    if formato.startswith("Animated Reel"):
        return [f"{base_url}/{stem}.mp4"]
    return [f"{base_url}/{stem}.png"]


def is_video(url):
    return url.endswith(".mp4")


def log_row(row):
    is_new = not os.path.exists(LOG_CSV)
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def graph_post(path, data):
    r = requests.post(f"{GRAPH_API}/{path}", data=data, timeout=30)
    body = r.json()
    if r.status_code >= 400:
        raise RuntimeError(f"Graph API error {r.status_code} on POST {path}: {body}")
    return body


def graph_get(path, params):
    r = requests.get(f"{GRAPH_API}/{path}", params=params, timeout=30)
    body = r.json()
    if r.status_code >= 400:
        raise RuntimeError(f"Graph API error {r.status_code} on GET {path}: {body}")
    return body


def wait_until_finished(container_id, token, timeout=240, interval=5):
    start = time.time()
    while time.time() - start < timeout:
        data = graph_get(container_id, {"fields": "status_code", "access_token": token})
        status = data.get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError(f"Container {container_id} failed processing: {data}")
        time.sleep(interval)
    raise TimeoutError(f"Container {container_id} did not reach FINISHED within {timeout}s")


def new_row(post):
    return {
        "timestamp": datetime.datetime.now(ZURICH).isoformat(timespec="seconds"),
        "dia": post["dia"],
        "slot": post["slot"],
        "data_publicacao": post["data_publicacao"],
        "formato": post["formato"],
        "status": "", "container_id": "", "media_id": "", "permalink": "", "erro": "",
    }


def due_now(post, now):
    if post["data_publicacao"] != now.strftime("%d-%m-%Y"):
        return False
    hour, minute = SLOT_TIMES[post["slot"]]
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta_min = (now - target).total_seconds() / 60
    return 0 <= delta_min <= PUBLISH_WINDOW_MIN


def publish_post(post, ig_user_id, token, base_url):
    row = new_row(post)
    tag = f"dia {post['dia']} {post['slot']}"
    paths = media_paths_for(post, base_url)

    try:
        caption = (post.get("legenda_post_com_cta", "") + "\n\n" + post.get("hashtags", "")).strip()

        if len(paths) == 1 and not is_video(paths[0]):
            container = graph_post(f"{ig_user_id}/media", {
                "image_url": paths[0], "caption": caption, "access_token": token,
            })
        elif len(paths) == 1 and is_video(paths[0]):
            container = graph_post(f"{ig_user_id}/media", {
                "media_type": "REELS", "video_url": paths[0], "caption": caption,
                "access_token": token,
            })
            wait_until_finished(container["id"], token)
        else:
            child_ids = []
            for p in paths:
                child = graph_post(f"{ig_user_id}/media", {
                    "image_url": p, "is_carousel_item": "true", "access_token": token,
                })
                child_ids.append(child["id"])
            container = graph_post(f"{ig_user_id}/media", {
                "media_type": "CAROUSEL", "children": ",".join(child_ids),
                "caption": caption, "access_token": token,
            })

        row["container_id"] = container["id"]
        publish = graph_post(f"{ig_user_id}/media_publish", {
            "creation_id": container["id"], "access_token": token,
        })
        media_id = publish["id"]
        row["media_id"] = media_id

        perma = graph_get(media_id, {"fields": "permalink", "access_token": token})
        permalink = perma.get("permalink", "")
        row["permalink"] = permalink
        row["status"] = "publicado"
        log_row(row)

        post["status"] = "publicado"
        post["permalink"] = permalink
        print(f"[OK] {tag} publicado: {permalink}")
        return True

    except Exception as e:  # noqa: BLE001 -- one post failing shouldn't stop the run
        row["status"] = "erro"
        row["erro"] = str(e)
        log_row(row)
        print(f"[ERRO] {tag}: {e}", file=sys.stderr)
        return False


def git_commit_and_push(changed):
    if not changed:
        return
    subprocess.run(["git", "config", "user.name", "astra-scheduler-bot"], check=True)
    subprocess.run(["git", "config", "user.email", "actions@github.com"], check=True)
    subprocess.run(["git", "add", POSTS_JSON, LOG_CSV], check=True)
    subprocess.run(["git", "commit", "-m", "Scheduled publish run"], check=True)
    subprocess.run(["git", "push"], check=True)


def main():
    token = os.environ["META_ACCESS_TOKEN"]
    ig_user_id = os.environ["IG_BUSINESS_ACCOUNT_ID"]
    base_url = os.environ["MEDIA_PUBLIC_BASE_URL"]

    now = datetime.datetime.now(ZURICH)
    posts = load_posts()
    changed = False

    for post in posts:
        if post.get("status") != "aprovado":
            continue
        if not due_now(post, now):
            continue
        if publish_post(post, ig_user_id, token, base_url):
            changed = True
        else:
            changed = True  # still persist the error log row

    if not changed:
        print(f"Nada para publicar agora ({now.isoformat(timespec='minutes')}).")

    save_posts(posts)
    git_commit_and_push(changed)


if __name__ == "__main__":
    main()
