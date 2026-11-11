"""Deterministic local content report; no uploads, AI service, or external assets."""
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from channels_analytics import number
from channels_statistics import StatisticsReadError


def fmt(value, *, percent=False, decimals=2):
    if number(value) is None:
        return '未提供'
    return f'{value:.{decimals}%}' if percent else f'{value:,.{decimals}f}' if decimals else f'{value:,}'


def source_counts(video):
    counts = []
    for source in video.get('source_data') or []:
        values = (source.get('metrics') or {}).get('browse') or []
        if values and all(number(n) is not None for n in values):
            counts.append((source['source'], sum(values)))
    return counts


def recommend_share(video):
    sources = video.get('source_data') or []
    counts = source_counts(video)
    # A missing/malformed source is not silently dropped from the denominator.
    if len(counts) != len(sources) or not counts:
        return None
    total = sum(n for _, n in counts)
    return sum(n for s, n in counts if s == '推荐') / total if total else None


def ratio(video, key):
    value, views = number(video.get(key)), number(video.get('views'))
    return value/views if value is not None and views else None


def video_advice(video):
    notes = []
    early = [p for p in (video.get('retention') or {}).get('points', []) if 0 < p['sec'] <= 5]
    if early and early[0]['rate'] < .4:
        point = early[0]
        notes.append(f'首个{point["sec"]:g}秒留存采样为{point["rate"]:.1%}。可测试先给可用完整句子、正确/错误发音对比，再解释；这里只是测试假设，未观看视频画面。')
    duration, completion = number(video.get('duration_seconds')), number(video.get('completion_rate'))
    if duration and duration >= 60 and completion is not None and completion < .1:
        notes.append('长视频完播较低，可测试30—45秒单场景或单知识点版；同时比较平均观看、转发和关注，避免与20秒短片只比完播。')
    share = recommend_share(video)
    if share is not None and share >= .8 and (ratio(video, 'follows') or 0) >= .01 and (video.get('follows') or 0) >= 3:
        notes.append('推荐来源占比较高，关注/播放比达到1%以上，可优先复测相近题材2—3条。关注事件仍少，暂不作因果结论。')
    counts = source_counts(video)
    total = sum(n for _, n in counts)
    other = sum(n for s, n in counts if s == '其他')
    if total and other/total >= .5:
        notes.append(f'“其他”来源占已取得来源播放的{other/total:.1%}。无法确定其具体渠道，不应将总播放直接解释为自然推荐偏好。')
    if (ratio(video, 'forwards') or 0) >= .02:
        notes.append('转发/播放比达到2%以上，可保留其传播点，测试缩短或拆分后能否保持转发。该比例是事件次数之比。')
    if not notes:
        notes.append('以相同选题再做2—3条，只改变开头或表达结构，并按发表后相同的24/72小时窗口比较推荐、留存和关注。')
    return notes


def report_insights(videos):
    measured = [v for v in videos if ratio(v, 'follows') is not None]
    leaders = sorted(measured, key=lambda v:ratio(v, 'follows'), reverse=True)[:3]
    notes = []
    if leaders:
        notes.append('关注/播放比靠前：'+'；'.join(f'《{v["title"]}》{ratio(v,"follows"):.2%}（{v["follows"]}次关注/{v["views"]}次播放）' for v in leaders)+'。这是事件次数之比，不是去重转化率；小样本应先复测。')
    promising = [v for v in leaders if (recommend_share(v) or 0) >= .8 and (v.get('follows') or 0) >= 3]
    if promising:
        notes.append('优先验证这些兼具推荐来源和关注表现的选题：'+'、'.join('《'+v['title']+'》' for v in promising)+'。每类再做2—3条，每次只改一个因素。')
    low = []
    for v in videos:
        points = [p for p in (v.get('retention') or {}).get('points', []) if 0 < p['sec'] <= 5]
        if points and points[0]['rate'] < .4:
            low.append(v)
    if low:
        notes.append(f'{len(low)}条视频在首个5秒内留存采样点低于40%。测试开头立即给答案、可用句子或声音对比；40%仅用于本报告筛选，并非平台或行业合格线。')
    notes.append('固定发表后24小时、72小时分别记录推荐来源播放、完播、平均观看、关注和转发；同时控制时长和流量来源，再决定是否扩大某类选题。')
    return notes


def table(head, rows):
    return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+escape(str(c))+'</th>' for c in head)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+escape(str(c))+'</td>' for c in row)+'</tr>' for row in rows)+'</tbody></table></div>'


def list_html(items):
    return '<ul>'+''.join('<li>'+escape(item)+'</li>' for item in items)+'</ul>'


def write_report(result, output):
    videos = result.get('videos')
    if not result.get('complete') or not isinstance(videos, list) or not videos or len(videos) != result.get('requested_count'):
        raise StatisticsReadError('详情未完整读取，未生成分析报告。')
    fetched = datetime.fromisoformat(result['fetched_at']).strftime('%Y-%m-%d %H:%M')
    start = datetime.fromisoformat(result['window_start']).strftime('%Y-%m-%d %H:%M')
    end = datetime.fromisoformat(result['window_end']).strftime('%Y-%m-%d %H:%M')
    rows, cards = [], []
    for v in videos:
        rows.append([v['rank'], v['title'], fmt(v.get('views'),decimals=0), fmt(v.get('duration_seconds')),
                     fmt(v.get('completion_rate'),percent=True), fmt(v.get('average_watch_seconds')),
                     fmt(v.get('follows'),decimals=0), fmt(recommend_share(v),percent=True)])
        interactions = table(['点赞','评论','转发','收藏','关注/播放比','转发/播放比'], [[
            *[fmt(v.get(k),decimals=0) for k in ('likes','comments','forwards','favorites')],
            fmt(ratio(v,'follows'),percent=True),fmt(ratio(v,'forwards'),percent=True)]])
        retention = (v.get('retention') or {}).get('points') or []
        retention_html = table(['采样位置（秒）','留存'], [(fmt(p['sec'],decimals=0),fmt(p['rate'],percent=True)) for p in retention]) if retention else '<p>未提供有效留存采样。</p>'
        counts = source_counts(v)
        total = sum(n for _,n in counts)
        source_html = table(['播放来源','播放次数','占已取得来源播放比例'], [(s,fmt(n,decimals=0),fmt(n/total if total else None,percent=True)) for s,n in sorted(counts,key=lambda s:s[1],reverse=True)]) if counts else '<p>未提供有效播放来源。</p>'
        if len(counts) != len(v.get('source_data') or []):
            source_html += '<p>部分来源未提供有效数据；不计算推荐来源占比。</p>'
        totals = v.get('daily_totals') or {}
        day_start = datetime.fromisoformat(v['trend_window_start']).date()
        day_end = datetime.fromisoformat(v['trend_window_end_inclusive_date']).date()
        day_rows = []
        for i in range((day_end-day_start).days+1):
            day_rows.append([(day_start+timedelta(days=i)).isoformat()]+[
                fmt(totals.get(k,[])[i] if i < len(totals.get(k,[])) else None,decimals=0)
                for k in ('browse','like','comment','forward','fav','follow')])
        daily = table(['日期','播放','点赞','评论','转发','收藏','关注'],day_rows)
        cards.append(f'<details><summary>{escape(str(v["rank"]))}. {escape(v["title"])}</summary><p>发表：{escape(v["published_at"])}</p>{interactions}{list_html(video_advice(v))}<div class="columns"><section><h3>留存采样</h3>{retention_html}</section><section><h3>播放来源</h3>{source_html}</section></div><h3>每日趋势</h3>{daily}<p class="muted">当前累计播放：{fmt(v.get("views"),decimals=0)}；来源合计：{fmt(total if counts else None,decimals=0)}。不同接口刷新时间可能有少量差异，保留原值。</p></details>')
    body = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'"><title>视频号Top10内容分析</title><style>body{font:16px/1.7 "Microsoft YaHei",sans-serif;color:#213147;background:#f5f7fa;margin:0}main{max-width:1200px;margin:auto;padding:28px}h1{font-size:28px}h2{font-size:21px;margin-top:28px}.muted{color:#607086;font-size:14px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px;background:white}th,td{padding:9px 12px;border:1px solid #dce3eb;text-align:left;white-space:nowrap}th{background:#eaf0f7}details{background:white;border:1px solid #dce3eb;border-radius:8px;margin:14px 0;padding:14px}summary{font-weight:bold;cursor:pointer}.columns{display:grid;grid-template-columns:1fr 1fr;gap:22px}li{margin-bottom:9px}@media(max-width:720px){main{padding:14px}.columns{display:block}}@media print{details{break-inside:avoid}table{font-size:10px}main{padding:0}}</style></head><body><main>'
    body += f'<h1>视频号 Top{len(videos)} 内容分析</h1><p class="muted">详情读取：{fetched}（曼谷时间）；选定作品发表范围：{start} 至 {end}。保留原Top10名单和排名，详情中的累计指标以本次读取为准。</p><p>排名口径：范围内发表作品的读取时累计播放，非平台提供的近7天增量排行。报告在本机生成。</p><h2>核心数据</h2>'
    body += table(['原排名','短标题','播放','时长/秒','完播率','平均观看/秒','关注','推荐来源占比'],rows)
    body += '<h2>优先测试方向</h2>'+list_html(report_insights(videos))+'<h2>逐条详情与建议</h2><p>点击标题展开。</p>'+''.join(cards)
    body += '<h2>数据边界</h2>'+list_html([
        '留存为平台返回的离散采样，不作逐秒插值；末端采样0不能代替独立完播率。',
        '未提供的数值显示“未提供”，不以0补齐。曝光、点击率、观众画像、粉丝/非粉丝分层及手机端专属指标未取得。',
        '关注/播放和转发/播放是事件次数之比，不是去重用户转化率，关注也不代表净增粉。',
        '“其他”来源不代表已确认的付费流量或具体渠道。',
        'Top10是有选择偏差的小样本，发表时间、时长和来源不同；建议是可验证的假设，不是因果判断。',
        '本功能只读取和生成报告，不上传、不修改、不发表作品，不占发布额度。'])+'</main></body></html>'
    path = Path(output)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(body,encoding='utf-8')
    temporary.replace(path)
    return path
