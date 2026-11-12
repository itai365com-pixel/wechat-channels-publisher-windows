"""tencent 的命令参数与执行逻辑。"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
import asyncio
import json
import os
from pathlib import Path
from conf import BASE_DIR
from daily_publish_limit import DailyPublishBudget, DailyPublishLimitReached
from uploader.tencent_uploader.main import (
    TENCENT_PUBLISH_STRATEGY_IMMEDIATE,
    TENCENT_PUBLISH_STRATEGY_SCHEDULED,
    TencentVideo,
    cookie_auth as tencent_cookie_auth,
    tencent_setup,
)
from sau_commands.arguments import (
    SCHEDULE_FORMAT,
    add_runtime_flags,
    existing_file_path,
    parse_tags,
    resolve_account_file,
    schedule_value,
)


def register(platform_parsers):
    """声明本平台命令、参数及合法选项。"""
    schedule_help = SCHEDULE_FORMAT.replace("%", "%%")
    tencent_parser = platform_parsers.add_parser("tencent", help="Tencent/WeChat Channels operations")
    tencent_actions = tencent_parser.add_subparsers(dest="action", required=True)

    for action_name in ("login", "check"):
        action_parser = tencent_actions.add_parser(action_name, help=f"Tencent/WeChat Channels {action_name}")
        action_parser.add_argument("--account", required=True, help="Tencent user-defined account_name")
        if action_name == "login":
            add_runtime_flags(action_parser)

    stats_parser = tencent_actions.add_parser('stats', help='Read recent published-video view rankings (read only)')
    stats_parser.add_argument('--account', required=True)
    stats_parser.add_argument('--days', type=int, default=7)
    stats_parser.add_argument('--top', type=int, default=10)
    stats_parser.add_argument('--output', type=Path)
    stats_parser.add_argument('--details', action='store_true', help='Read only these top videos and generate a local HTML analysis report')
    add_runtime_flags(stats_parser)

    details_parser = tencent_actions.add_parser('stats-details', help='Read details and generate a report for a fixed saved Top10 (read only)')
    details_parser.add_argument('--account', required=True)
    details_parser.add_argument('--snapshot', type=Path, required=True)
    details_parser.add_argument('--output', type=Path, required=True)
    add_runtime_flags(details_parser)

    tencent_upload_video_parser = tencent_actions.add_parser("upload-video", help="Upload one video to WeChat Channels")
    tencent_upload_video_parser.add_argument("--account", required=True, help="Tencent user-defined account_name")
    tencent_upload_video_parser.add_argument("--file", required=True, type=existing_file_path, help="Video file path")
    tencent_upload_video_parser.add_argument("--title", required=True, help="Video title")
    tencent_upload_video_parser.add_argument("--desc", default="", help="Optional video description")
    tencent_upload_video_parser.add_argument("--tags", default="", help="Comma-separated tags, such as tag1,tag2")
    tencent_upload_video_parser.add_argument("--schedule", type=schedule_value, help=f"Schedule time in {schedule_help}")
    tencent_upload_video_parser.add_argument("--thumbnail", type=existing_file_path, help="Optional 3:4 portrait thumbnail path")
    tencent_upload_video_parser.add_argument("--thumbnail-landscape", type=existing_file_path, help="Optional 4:3 landscape thumbnail path")
    tencent_upload_video_parser.add_argument("--thumbnail-portrait", type=existing_file_path, help="Optional 3:4 portrait thumbnail path")
    tencent_upload_video_parser.add_argument("--short-title", help="Optional WeChat Channels short title")
    tencent_upload_video_parser.add_argument("--category", help="Optional original content category")
    tencent_upload_video_parser.add_argument("--draft", action="store_true", help="Save as draft instead of publishing")
    tencent_upload_video_parser.add_argument("--collection", default=None, help="Optional collection name to add the work into (must already exist)")
    tencent_upload_video_parser.add_argument("--require-collection", action="store_true", help="Stop before publishing if the requested collection cannot be selected")
    tencent_upload_video_parser.add_argument("--declare-original", action="store_true", help="Check the independent original-content declaration")
    tencent_upload_video_parser.add_argument("--content-label", default=None, help="Exact video-label option text; an empty value leaves it unset")
    tencent_upload_video_parser.add_argument("--description-only", action="store_true", help="Use desc as the video description, without prepending the short title")
    add_runtime_flags(tencent_upload_video_parser)


@dataclass(slots=True)
class TencentVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    publish_date: datetime | int
    thumbnail_file: Path | None = None
    thumbnail_landscape_file: Path | None = None
    thumbnail_portrait_file: Path | None = None
    short_title: str | None = None
    category: str | None = None
    is_draft: bool = False
    publish_strategy: str = TENCENT_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True
    collection_name: str | None = None
    require_collection: bool = False
    declare_original: bool = False
    content_label: str | None = None
    description_only: bool = False


async def login_tencent_account(account_name: str, headless: bool = True) -> dict:
    account_file = resolve_account_file("tencent", account_name)
    return await tencent_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_tencent_account(account_name: str) -> bool:
    account_file = resolve_account_file("tencent", account_name)
    if not account_file.exists():
        return False
    return await tencent_cookie_auth(str(account_file), remember_name=True)


async def upload_tencent_video(request: TencentVideoUploadRequest) -> Path:
    if not request.is_draft:
        usage = DailyPublishBudget(BASE_DIR).usage(request.account_name)
        if not usage['remaining']:
            raise DailyPublishLimitReached(f'今日已达到单账号 {usage["limit"]} 条上限（曼谷时间），未打开上传页面。')
    account_file = resolve_account_file("tencent", request.account_name)
    is_ready = await tencent_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Tencent/WeChat Channels cookie is missing or expired: {account_file}. "
            f"Run `sau tencent login --account {request.account_name}` first."
        )

    app = TencentVideo(
        title=request.title,
        file_path=str(request.video_file),
        tags=request.tags,
        publish_date=request.publish_date,
        account_file=str(account_file),
        category=request.category,
        is_draft=request.is_draft,
        desc=request.description,
        thumbnail_path=str(request.thumbnail_file) if request.thumbnail_file else None,
        thumbnail_landscape_path=(
            str(request.thumbnail_landscape_file) if request.thumbnail_landscape_file else None
        ),
        thumbnail_portrait_path=(
            str(request.thumbnail_portrait_file) if request.thumbnail_portrait_file else None
        ),
        short_title=request.short_title,
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
        collection_name=request.collection_name,
        require_collection=request.require_collection,
        declare_original=request.declare_original,
        content_label=request.content_label,
        description_only=request.description_only,
    )
    app.quota_account = request.account_name
    await app.tencent_upload_video()
    return account_file


async def run(args):
    """执行本平台命令；发布结果由平台实现核验。"""
    if args.action == "login":
        result = await login_tencent_account(args.account, headless=args.headless)
        if not result["success"]:
            raise RuntimeError(result["message"])
        print(f"Tencent/WeChat Channels login flow completed: {result['account_file']}")
        return 0

    if args.action == "check":
        is_valid = await check_tencent_account(args.account)
        print("valid" if is_valid else "invalid")
        return 0 if is_valid else 1

    if args.action == 'stats-details':
        from channels_analytics import read_details, save_json
        from channels_report import write_report
        held = os.environ.get('SAU_ACCOUNT_LOCK_HELD', '').casefold() == args.account.casefold()
        result = await read_details(args.account, args.snapshot, output=args.output,
                                    headless=args.headless, acquire_lock=not held)
        report = args.output.with_suffix('.html')
        write_report(result, report)
        result['report_path'] = str(report.resolve())
        save_json(args.output, result)
        print(f'已读取固定 {len(result["videos"])} 条详情；分析报告：{report}')
        return 0

    if args.action == 'stats':
        from channels_statistics import read_statistics
        output = args.output or Path(BASE_DIR)/'.local/gui/reports'/f'video-top-{datetime.now():%Y%m%d-%H%M%S-%f}.json'
        held = os.environ.get('SAU_ACCOUNT_LOCK_HELD', '').casefold() == args.account.casefold()
        result = await read_statistics(args.account, days=args.days, top=args.top, output=output,
                                       headless=args.headless, acquire_lock=not held)
        print(f'近{args.days}天正常发表视频 {result["eligible_video_count"]} 条；按当前累计播放量排名：')
        for row in result['top']:
            print(f'{row["rank"]}. {row["views"]:,} 播放 · {" ".join(row["title"].split())}')
        print(f'统计结果：{output}')
        if args.details and result['top']:
            from channels_analytics import read_details, save_json
            from channels_report import write_report
            detail_output = Path(output).with_name(Path(output).stem+'-details.json')
            details = await read_details(args.account, output, output=detail_output,
                                         headless=args.headless, acquire_lock=not held)
            report = detail_output.with_suffix('.html')
            write_report(details, report)
            details['report_path'] = str(report.resolve())
            save_json(detail_output, details)
            result['details_path'], result['report_path'] = str(detail_output.resolve()), str(report.resolve())
            save_json(output, result)
            print(f'详情与分析报告：{report}')
        return 0

    publish_strategy = TENCENT_PUBLISH_STRATEGY_SCHEDULED if args.schedule else TENCENT_PUBLISH_STRATEGY_IMMEDIATE

    if args.action == "upload-video":
        pause_file = Path(BASE_DIR) / ".local/gui/publish-pause.json"
        while pause_file.is_file():
            pause = json.loads(pause_file.read_text(encoding="utf-8"))
            if pause.get("account") != args.account:
                break
            print("发布已暂停，尚未上传或点击发表：" + pause.get("reason", "等待核对设置"), flush=True)
            await asyncio.sleep(20)
        override_file = Path(BASE_DIR) / ".local/gui/approved-copy-overrides.json"
        if override_file.is_file():
            for override in json.loads(override_file.read_text(encoding="utf-8")).get("items", []):
                if override.get("account") != args.account or Path(override["file"]).resolve() != Path(args.file).resolve():
                    continue
                if override.get("source_short_title") != args.short_title or override.get("source_desc") != args.desc:
                    continue
                manifest_path = Path(override["batch_manifest"]).resolve()
                if not manifest_path.is_relative_to((Path(BASE_DIR) / ".local/gui/batches").resolve()):
                    raise ValueError("文案调整的批次路径无效")
                batch = json.loads(manifest_path.read_text(encoding="utf-8"))
                running = any(Path(row["file"]).resolve() == Path(args.file).resolve() and row.get("status") == "running" for row in batch.get("items", []))
                if batch.get("account") == args.account and running:
                    args.short_title = args.title = override["approved_short_title"]
                    print(f"已按本批已确认文案替换短标题：{args.short_title}", flush=True)
                    break
        request = TencentVideoUploadRequest(
            account_name=args.account,
            video_file=args.file,
            title=args.title,
            description=args.desc,
            tags=parse_tags(args.tags),
            publish_date=args.schedule or 0,
            thumbnail_file=args.thumbnail,
            thumbnail_landscape_file=args.thumbnail_landscape,
            thumbnail_portrait_file=args.thumbnail_portrait,
            short_title=args.short_title,
            category=args.category,
            is_draft=args.draft,
            publish_strategy=publish_strategy,
            debug=args.debug,
            headless=args.headless,
            collection_name=args.collection,
            require_collection=getattr(args, "require_collection", False),
            declare_original=getattr(args, "declare_original", False),
            content_label=getattr(args, "content_label", None),
            description_only=getattr(args, "description_only", False),
        )
        await upload_tencent_video(request)
        print(f"Tencent/WeChat Channels video upload submitted: {request.video_file}")
        return 0

    raise RuntimeError(f"Unsupported Tencent/WeChat Channels action: {args.action}")
