"""Run a TikTok queue in order and reconcile interrupted submissions."""
from datetime import datetime, timedelta
from pathlib import Path
import time

from publisher_options import BANGKOK
from tiktok_queue import DAILY_LIMIT, HOURLY_LIMIT, _check_capacity, file_signature


def caption_key(text):
    return " ".join(text.split())


def next_slot(items, earliest, *, hourly_limit=HOURLY_LIMIT, daily_limit=DAILY_LIMIT):
    candidates = {earliest}
    for row in items.values():
        if row.get("reserved_at"):
            stamp = datetime.fromisoformat(row["reserved_at"]).astimezone(BANGKOK)
            candidates.add(stamp + timedelta(hours=1))
            candidates.add((stamp + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
    for stamp in sorted(candidates):
        if stamp < earliest:
            continue
        try:
            _check_capacity(items, stamp, hourly_limit=hourly_limit, daily_limit=daily_limit)
        except ValueError:
            continue
        return stamp
    raise ValueError("无法计算 TikTok 的下一可用时段。")


def verified_post(posts, row):
    attempt = row["studio_attempt"]
    known = set(attempt["prior_urls"])
    matches = [post for post in posts if post.url not in known
               and caption_key(post.caption) == caption_key(row["caption"])]
    if len(matches) > 1:
        raise ValueError("后台有多条相同文案的作品，请先人工核对。")
    return matches[0] if matches and matches[0].public else None


def wait_for_post(studio, row, stop, progress, timeout=180):
    deadline = time.monotonic() + timeout
    while not stop.is_set():
        post = verified_post(studio.posts(), row)
        if post:
            return post
        if time.monotonic() >= deadline:
            break
        progress("已提交，等待 TikTok 审查及公开状态…")
        stop.wait(min(6, max(0, deadline - time.monotonic())))
    return None


def record_post(store, digest, row, post):
    with store.edit() as state:
        saved = state["items"][digest]
        if saved["status"] != "checking" or saved.get("studio_attempt") != row["studio_attempt"]:
            raise ValueError("队列记录已变化，请核对 Studio。")
        saved.update(status="published", platform_url=post.url,
                     confirmed_at=datetime.now(BANGKOK).isoformat())


def run_batch(store, selected, studio, stop, progress, *, now=lambda: datetime.now(BANGKOK),
              min_interval=timedelta(), hourly_limit=HOURLY_LIMIT, daily_limit=DAILY_LIMIT):
    if min_interval < timedelta() or min(hourly_limit, daily_limit) < 1:
        raise ValueError("发布间隔或额度不正确。")
    published = skipped = 0
    for digest in selected:
        if stop.is_set():
            return f"已暂停。后台核验 {published} 条，跳过 {skipped} 条。"
        row = store.read()["items"][digest]
        name = Path(row["file"]).name
        if row["status"] in ("published", "scheduled") or row.get("error"):
            skipped += 1
            continue
        if row["status"] == "checking":
            if not row.get("studio_attempt"):
                return f"{name} 曾手动处理。请先在 Studio 核对，再登记结果。"
            progress(f"正在核对上次提交的 {name}…")
            post = verified_post(studio.posts(), row)
            if not post:
                return f"{name} 的上次提交仍待核对。请在 Studio 查看该作品，确认结果后再继续。"
            record_post(store, digest, row, post)
            published += 1
            progress(f"{name} 已核验公开。", refresh=True)
            continue
        if row["status"] != "pending":
            return f"{name} 的队列状态无法识别，请先核对。"

        planned = datetime.fromisoformat(row["scheduled_at"]) if row["scheduled_at"] else None
        if planned and planned <= now():
            return f"{name} 的表格排期已过，请更新表格后重新读取。"
        while not stop.is_set():
            current = now()
            items = store.read()["items"]
            earliest = max(current, planned) if planned else current
            if min_interval:
                previous = [datetime.fromisoformat(saved["reserved_at"])
                            for saved in items.values() if saved.get("reserved_at")]
                if previous:
                    earliest = max(earliest, max(previous) + min_interval)
            slot = next_slot(items, earliest, hourly_limit=hourly_limit, daily_limit=daily_limit)
            if slot <= current:
                break
            progress(f"{name} 等待到 {slot:%m-%d %H:%M}（曼谷）自动继续。")
            stop.wait(min(30, (slot - current).total_seconds()))
        if stop.is_set():
            return f"已暂停。后台核验 {published} 条，跳过 {skipped} 条。"

        posts = studio.posts()
        if any(caption_key(post.caption) == caption_key(row["caption"]) for post in posts):
            return f"Studio 已有与 {name} 相同文案的作品。请核对，避免重复发布。"
        if stop.is_set():
            return f"已暂停。{name} 尚未提交，后台核验 {published} 条。"
        if file_signature(row["file"]) != row["signature"]:
            return f"{name} 已变化，请重新读取表格。"
        progress(f"正在上传 {name} 并填写文案…")
        studio.prepare(row["file"], row["caption"])
        if stop.is_set():
            return f"已暂停。{name} 尚未提交，后台核验 {published} 条。"
        posts = studio.posts()
        if any(caption_key(post.caption) == caption_key(row["caption"]) for post in posts):
            return f"Studio 已有与 {name} 相同文案的作品。请核对，避免重复发布。"

        if stop.is_set():
            return f"已暂停。{name} 尚未提交，后台核验 {published} 条。"
        # Persist the attempt before the single click. A crash after this
        # write requires readback or a human decision before another click.
        with store.edit() as state:
            saved = state["items"][digest]
            if saved != row or file_signature(row["file"]) != row["signature"]:
                return f"{name} 的文件或文案已变化，请重新加载队列。"
            started = now()
            _check_capacity(state["items"], started, hourly_limit=hourly_limit, daily_limit=daily_limit)
            attempt = {"prior_urls": sorted({post.url for post in posts})}
            saved.update(status="checking", studio_attempt=attempt,
                         reserved_at=started.isoformat(), started_at=started.isoformat())
            row = dict(saved)
        progress(f"正在提交 {name}，并核对作品管理…", refresh=True)
        studio.submit()
        post = wait_for_post(studio, row, stop, progress)
        if not post:
            return f"{name} 已尝试提交，公开状态待核对。已保留提交记录，再次启动时先核对后台。"
        record_post(store, digest, row, post)
        published += 1
        progress(f"{name} 已核验公开。", refresh=True)
    return f"本批结束。后台核验 {published} 条，跳过 {skipped} 条。"
