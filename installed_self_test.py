"""Offline installation checks. Never authenticate, upload, or publish."""
import argparse
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import traceback


def main(arguments=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(arguments)
    root = Path(__file__).resolve().parent
    checks = []

    def check(name, test):
        test()
        checks.append({'name': name, 'passed': True})

    try:
        import conf
        import cv2
        import numpy as np
        import tkinter as tk
        import patchright
        from patchright.async_api import async_playwright
        from batch_table import read_tables, records_from_rows, match_records
        from daily_publish_limit import BANGKOK, DailyPublishBudget, DailyPublishLimitReached, HourlyPublishLimitReached
        from video_channels_batch import BatchPublisher, DEFAULT_FOLDER
        from video_channels_gui import Publisher
        import channels_analytics
        import channels_report

        def runtime_paths():
            assert Path(sys.prefix).resolve() == (root/'runtime').resolve(), sys.prefix
            for module in (conf, cv2, np, patchright, channels_analytics, channels_report):
                assert Path(module.__file__).resolve().is_relative_to(root), module.__file__
            assert conf.BASE_DIR == root
            assert Path(conf.LOCAL_CHROME_PATH).is_file()
            assert (root/'utils/stealth.min.js').is_file()
            assert DEFAULT_FOLDER == Path.home()/'Videos'
        check('bundled_runtime_and_relative_paths', runtime_paths)

        with tempfile.TemporaryDirectory(prefix='sau-selftest-', dir=root/'.local/temp') as folder:
            work = Path(folder)
            video = work/'z001.mp4'
            writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'mp4v'), 10, (64,64))
            assert writer.isOpened()
            for _ in range(3): writer.write(np.zeros((64,64,3), dtype=np.uint8))
            writer.release()
            def read_media_and_table():
                capture = cv2.VideoCapture(str(video))
                ok, frame = capture.read()
                capture.release()
                assert ok and frame is not None
                table = work/'中文文案.csv'
                table.write_text('集,短标题,介绍+关键词\n1,测试短标题,测试简介 #标签\n', encoding='utf-8-sig')
                rows = next(iter(read_tables(table).values()))
                matched, _ = match_records(records_from_rows(rows), work, series_prefix='z')
                assert len(matched) == 1 and matched[0]['short_title'] == '测试短标题'
                assert matched[0]['publish_desc'] == '测试简介 #标签'
            check('video_decode_and_chinese_table', read_media_and_table)

            def quota():
                now = datetime(2026, 5, 12, 9, tzinfo=BANGKOK)
                budget = DailyPublishBudget(work, lambda: now)
                for hour in range(6):
                    if hour:
                        now += timedelta(hours=1)
                    for _ in range(6):
                        budget.reserve('selftest')
                    if hour == 0:
                        try:
                            budget.reserve('selftest')
                        except HourlyPublishLimitReached:
                            pass
                        else:
                            raise AssertionError('7th hourly reservation was accepted')
                assert DailyPublishBudget(work, lambda: now).usage('selftest')['remaining'] == 0
                now += timedelta(hours=1)
                try:
                    budget.reserve('selftest')
                except DailyPublishLimitReached:
                    pass
                else:
                    raise AssertionError('37th daily reservation was accepted')
            check('persistent_hourly_six_daily_thirty_six', quota)

            def analytics_report():
                fixture = {'complete':True,'requested_count':1,'fetched_at':'2026-11-06T23:00:00+07:00',
                           'window_start':'2026-10-31T00:00:00+07:00','window_end':'2026-11-06T23:00:00+07:00',
                           'videos':[{'rank':1,'title':'安装测试<报告>','published_at':'2026-11-06T10:00:00+07:00',
                                      'views':100,'follows':2,'completion_rate':.2,'average_watch_seconds':10,
                                      'trend_window_start':'2026-10-31T00:00:00+07:00','trend_window_end_inclusive_date':'2026-11-06'}]}
                report = channels_report.write_report(fixture,work/'离线分析报告.html')
                html = report.read_text(encoding='utf-8')
                assert '安装测试&lt;报告&gt;' in html and '20.00%' in html and '未提供' in html
            check('analytics_module_and_local_html_report', analytics_report)

        def desktop_and_cli():
            window = tk.Tk()
            window.withdraw()
            try:
                app = BatchPublisher(window)
                window.update_idletasks()
                assert window.title().startswith('视频号批量发布助手 v') and not app.busy
                app.platform.set("dual")
                app.switch_platform()
                assert app.dual_page.winfo_manager() == "pack"
                assert all(not flag.get() for flag in app.dual_page.enabled.values())
                assert app.dual_page.publish_button.instate(("disabled",))
                app.platform.set("wechat")
                app.switch_platform()
                code, log = app.execute_cli(['--help'], '安装自检')
                assert code == 0, log.read_text(encoding='utf-8')
                assert 'upload-video' in log.read_text(encoding='utf-8')
                if app.poll_id: window.after_cancel(app.poll_id)
            finally:
                window.destroy()
            single = tk.Tk()
            single.withdraw()
            try:
                app = Publisher(single)
                single.update_idletasks()
                assert not app.busy
                with tempfile.TemporaryDirectory(prefix='sau-stats-ui-',dir=root/'.local/temp') as folder:
                    work=Path(folder)
                    ranking=work/'ranking.json'
                    detail=work/'details.json'
                    report=work/'report.html'
                    ranking.write_text(json.dumps({'window_start':'2026-10-31T00:00:00+07:00',
                        'window_end':'2026-11-06T23:00:00+07:00','scanned_count':1,'eligible_video_count':1,
                        'details_path':str(detail),'top':[{'id':'qa','rank':1,'title':'安装测试',
                        'published_at':'2026-11-06T10:00:00+07:00','views':100}]}),encoding='utf-8')
                    detail.write_text(json.dumps({'report_path':str(report),'videos':[{'id':'qa','views':100,
                        'completion_rate':.2,'average_watch_seconds':10,'follows':2}]}),encoding='utf-8')
                    opened=[]
                    app.open_statistics_report=lambda path:opened.append(str(path))
                    app.stats_path=ranking
                    app.task_finished(0,'读取播放Top10')
                    single.update_idletasks()
                    def widgets(widget):
                        yield widget
                        for child in widget.winfo_children():yield from widgets(child)
                    trees=[w for w in widgets(single) if w.winfo_class()=='Treeview']
                    assert any(len(t.get_children())==1 and '20.00%' in t.item(t.get_children()[0],'values') for t in trees)
                    assert opened==[str(report)]
                    calls=[]
                    app.start=lambda arguments,action:calls.append((arguments,action))
                    app.show_statistics_details(ranking)
                    assert calls[0][0][0]=='stats-details' and str(ranking) in calls[0][0]
                if app.poll_id: single.after_cancel(app.poll_id)
            finally:
                single.destroy()
        check('both_desktop_forms_and_child_cli', desktop_and_cli)

        async def browser():
            async with async_playwright() as p:
                instance = await p.chromium.launch(executable_path=conf.LOCAL_CHROME_PATH, headless=True)
                try:
                    context = await instance.new_context()
                    await context.route('**/*', lambda route: route.abort())
                    await context.add_init_script(path=str(root/'utils/stealth.min.js'))
                    page = await context.new_page()
                    await page.set_content('<title>离线安装自检</title><input id="title">')
                    await page.locator('#title').fill('短标题')
                    assert await page.title() == '离线安装自检'
                    assert await page.locator('#title').input_value() == '短标题'
                    await context.close()
                finally:
                    await instance.close()
        check('bundled_browser_offline', lambda: asyncio.run(browser()))
        result = {'passed': True, 'checks': checks}
    except Exception:
        result = {'passed': False, 'checks': checks, 'error': traceback.format_exc()}
    result.update(at=datetime.now().astimezone().isoformat(), install_root=str(root), python=sys.executable)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
